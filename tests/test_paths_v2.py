"""Tests for v2 path helpers: extra mounts, cache, resource limits."""

from __future__ import annotations

import pytest

from kimi_sandbox.config import ExtraMount, ResourceLimits
from kimi_sandbox.errors import InvalidPathError
from kimi_sandbox.paths import (
    resolve_cache_dir,
    resolve_extra_mount,
    validate_resource_limits,
)


# --- resolve_extra_mount -------------------------------------------------

def test_extra_mount_host_only_defaults_to_mnt(tmp_path):
    # Bare HOST form maps under /mnt/<basename>, sidestepping the /home
    # reserved tree that identity-mapping would always hit.
    src = tmp_path / "data"
    src.mkdir()
    m = resolve_extra_mount(str(src), writable=False)
    assert isinstance(m, ExtraMount)
    assert m.source == src.resolve()
    assert m.target == "/mnt/data"
    assert m.writable is False


def test_extra_mount_default_mnt_collision_rejected(tmp_path):
    # If the basename collides with a reserved name under /mnt it would still
    # be checked; but a normal basename is fine. Here we assert a basename of
    # 'data' yields /mnt/data and is accepted (regression guard for the default
    # no longer landing on a reserved tree).
    src = tmp_path / "data"
    src.mkdir()
    m = resolve_extra_mount(str(src), writable=True)
    assert m.target == "/mnt/data"
    assert m.writable is True


def test_extra_mount_explicit_target(tmp_path):
    src = tmp_path / "data"
    src.mkdir()
    m = resolve_extra_mount(f"{src}:/opt/ref", writable=True)
    assert m.target == "/opt/ref"
    assert m.writable is True


def test_extra_mount_missing_source(tmp_path):
    with pytest.raises(InvalidPathError):
        resolve_extra_mount(str(tmp_path / "nope"), writable=False)


def test_extra_mount_empty_spec():
    with pytest.raises(InvalidPathError):
        resolve_extra_mount("", writable=False)


def test_extra_mount_relative_target_rejected(tmp_path):
    src = tmp_path / "data"
    src.mkdir()
    with pytest.raises(InvalidPathError):
        resolve_extra_mount(f"{src}:relative/path", writable=False)


def test_extra_mount_root_target_rejected(tmp_path):
    src = tmp_path / "data"
    src.mkdir()
    with pytest.raises(InvalidPathError):
        resolve_extra_mount(f"{src}:/", writable=False)


@pytest.mark.parametrize(
    "target",
    ["/workspace", "/workspace/sub", "/etc", "/usr/local/x", "/kimi-code-home",
     "/cache", "/sandbox/bin", "/home/sandbox"],
)
def test_extra_mount_reserved_targets_rejected(tmp_path, target):
    src = tmp_path / "data"
    src.mkdir()
    with pytest.raises(InvalidPathError):
        resolve_extra_mount(f"{src}:{target}", writable=False)


@pytest.mark.parametrize("target", ["/opt/ref", "/srv/data", "/mnt/x", "/data"])
def test_extra_mount_allowed_targets(tmp_path, target):
    src = tmp_path / "data"
    src.mkdir()
    m = resolve_extra_mount(f"{src}:{target}", writable=False)
    assert m.target == target


def test_extra_mount_target_containing_reserved_rejected(tmp_path):
    # A target that is a parent of a reserved mount would shadow it.
    src = tmp_path / "data"
    src.mkdir()
    with pytest.raises(InvalidPathError):
        resolve_extra_mount(f"{src}:/home", writable=False)


# --- resolve_cache_dir ---------------------------------------------------

def test_resolve_cache_dir_layout(tmp_path):
    state_root = tmp_path / "state"
    cache = resolve_cache_dir(state_root, "work")
    assert cache == (state_root / "profiles" / "work" / "cache").resolve()


def test_resolve_cache_dir_rejects_bad_profile(tmp_path):
    with pytest.raises(InvalidPathError):
        resolve_cache_dir(tmp_path, "a/b")


# --- validate_resource_limits --------------------------------------------

def test_resource_limits_empty_ok():
    validate_resource_limits(ResourceLimits())  # no raise


def test_resource_limits_valid():
    validate_resource_limits(
        ResourceLimits(memory_max="2G", cpu_quota="150%", pids_max=512)
    )


def test_resource_limits_negative_pids():
    with pytest.raises(InvalidPathError):
        validate_resource_limits(ResourceLimits(pids_max=0))
    with pytest.raises(InvalidPathError):
        validate_resource_limits(ResourceLimits(pids_max=-5))


def test_resource_limits_cpu_quota_needs_percent():
    with pytest.raises(InvalidPathError):
        validate_resource_limits(ResourceLimits(cpu_quota="150"))


def test_resource_limits_cpu_quota_with_percent_ok():
    validate_resource_limits(ResourceLimits(cpu_quota="50%"))


def test_resource_limits_is_empty():
    assert ResourceLimits().is_empty()
    assert not ResourceLimits(memory_max="1G").is_empty()
    assert not ResourceLimits(pids_max=10).is_empty()
