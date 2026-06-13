"""Tests for v2 path helpers: extra mounts, cache, resource limits."""

from __future__ import annotations

from pathlib import Path

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


# --- conda path resolution & validation (mod_v2 §11) ---------------------

import os  # noqa: E402
import shutil  # noqa: E402
import tempfile  # noqa: E402

import pytest  # noqa: E402

from kimi_sandbox.config import CondaConfig  # noqa: E402
from kimi_sandbox.paths import (  # noqa: E402
    resolve_conda_existing_env,
    resolve_conda_root,
    validate_conda_config,
)


@pytest.fixture
def home_tmp():
    base = Path(tempfile.mkdtemp(dir=os.path.expanduser("~"), prefix="kimitest_"))
    try:
        yield base
    finally:
        shutil.rmtree(base, ignore_errors=True)


def _fake_root(base):
    root = base / "anaconda3"
    (root / "bin").mkdir(parents=True)
    exe = root / "bin" / "conda"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    return root


def _conda(root, **over):
    base = dict(
        root=root.resolve(),
        sandbox_original_root=str(root.resolve()),
        writable_root="/cache/conda",
        shell_integration=True,
        existing_envs=(),
    )
    base.update(over)
    return CondaConfig(**base)


def test_resolve_conda_root_ok(home_tmp):
    root = _fake_root(home_tmp)
    assert resolve_conda_root(str(root)) == root.resolve()


def test_resolve_conda_root_missing():
    with pytest.raises(InvalidPathError):
        resolve_conda_root("/no/such/conda/root/xyz")


def test_resolve_conda_existing_env_ok(home_tmp):
    env = home_tmp / "envs" / "foo"
    env.mkdir(parents=True)
    result = resolve_conda_existing_env(f"{env}:foo")
    assert result.name == "foo"
    assert result.source == env.resolve()


def test_resolve_conda_existing_env_bad_name(home_tmp):
    env = home_tmp / "envs" / "foo"
    env.mkdir(parents=True)
    with pytest.raises(InvalidPathError):
        resolve_conda_existing_env(f"{env}:bad/name")


def test_validate_conda_missing_bin_conda(home_tmp):
    root = home_tmp / "broken"
    root.mkdir()
    conda = _conda(root)
    with pytest.raises(InvalidPathError):
        validate_conda_config(
            conda, persistent_cache=True, project_dir=home_tmp / "p",
            state_root=home_tmp / "s", kimi_code_home=home_tmp / "k",
            cache_dir=home_tmp / "c",
        )


def test_validate_conda_cache_requires_persistent(home_tmp):
    root = _fake_root(home_tmp)
    conda = _conda(root, writable_root="/cache/conda")
    with pytest.raises(InvalidPathError, match="persistent_cache"):
        validate_conda_config(
            conda, persistent_cache=False, project_dir=home_tmp / "p",
            state_root=home_tmp / "s", kimi_code_home=home_tmp / "k",
            cache_dir=None,
        )


def test_validate_conda_root_in_project_rejected(home_tmp):
    proj = home_tmp / "proj"
    proj.mkdir()
    root = proj / "anaconda3"
    (root / "bin").mkdir(parents=True)
    (root / "bin" / "conda").write_text("#!/bin/sh\n")
    (root / "bin" / "conda").chmod(0o755)
    conda = _conda(root)
    with pytest.raises(InvalidPathError):
        validate_conda_config(
            conda, persistent_cache=True, project_dir=proj,
            state_root=home_tmp / "s", kimi_code_home=home_tmp / "k",
            cache_dir=home_tmp / "c",
        )


def test_validate_conda_compat_target_reserved_rejected(home_tmp):
    # An original path under /tmp collides with the launcher /tmp mount.
    root = home_tmp / "anaconda3"
    (root / "bin").mkdir(parents=True)
    (root / "bin" / "conda").write_text("#!/bin/sh\n")
    (root / "bin" / "conda").chmod(0o755)
    conda = _conda(root, sandbox_original_root="/tmp/anaconda3")
    with pytest.raises(InvalidPathError, match="launcher-managed"):
        validate_conda_config(
            conda, persistent_cache=True, project_dir=home_tmp / "p",
            state_root=home_tmp / "s", kimi_code_home=home_tmp / "k",
            cache_dir=home_tmp / "c",
        )


def test_validate_conda_duplicate_existing_env_names(home_tmp):
    from kimi_sandbox.config import CondaExistingEnv

    root = _fake_root(home_tmp)
    e1 = home_tmp / "a" / "foo"
    e2 = home_tmp / "b" / "foo"
    e1.mkdir(parents=True)
    e2.mkdir(parents=True)
    conda = _conda(
        root,
        existing_envs=(
            CondaExistingEnv(source=e1.resolve(), name="foo"),
            CondaExistingEnv(source=e2.resolve(), name="foo"),
        ),
    )
    with pytest.raises(InvalidPathError, match="duplicate"):
        validate_conda_config(
            conda, persistent_cache=True, project_dir=home_tmp / "p",
            state_root=home_tmp / "s", kimi_code_home=home_tmp / "k",
            cache_dir=home_tmp / "c",
        )
