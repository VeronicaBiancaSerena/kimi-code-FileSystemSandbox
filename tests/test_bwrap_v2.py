"""Tests for v2 bwrap command features and non-merged-/usr layout (design 29)."""

from __future__ import annotations

from pathlib import Path

from kimi_sandbox import bwrap as bwrap_mod
from kimi_sandbox.bwrap import build_bwrap_command
from kimi_sandbox.config import (
    MODE_READ_ONLY,
    ExtraMount,
    SandboxConfig,
)


def make_config(tmp_path: Path, **overrides) -> SandboxConfig:
    project = tmp_path / "proj"
    project.mkdir(exist_ok=True)
    kimi_home = tmp_path / "home"
    kimi_home.mkdir(exist_ok=True)
    kimi = tmp_path / "kimi"
    kimi.write_text("x")
    base = dict(
        project_dir=project.resolve(),
        kimi_code_home=kimi_home.resolve(),
        kimi_path=kimi.resolve(),
        inner_command=["/sandbox/bin/kimi"],
        env={"HOME": "/home/sandbox"},
    )
    base.update(overrides)
    return SandboxConfig(**base)


def _pairs(cmd: list[str], flag: str) -> list[tuple[str, str]]:
    out = []
    for i, tok in enumerate(cmd):
        if tok == flag:
            out.append((cmd[i + 1], cmd[i + 2]))
    return out


# --- read-only mode ------------------------------------------------------

def test_read_only_mode_ro_binds_workspace(tmp_path):
    cfg = make_config(tmp_path, mode=MODE_READ_ONLY)
    cmd = build_bwrap_command(cfg)
    # /workspace must be ro-bind, NOT a writable --bind.
    assert (str(cfg.project_dir), "/workspace") in _pairs(cmd, "--ro-bind")
    assert (str(cfg.project_dir), "/workspace") not in _pairs(cmd, "--bind")


def test_workspace_write_mode_binds_rw(tmp_path):
    cfg = make_config(tmp_path)  # default workspace-write
    cmd = build_bwrap_command(cfg)
    assert (str(cfg.project_dir), "/workspace") in _pairs(cmd, "--bind")


# --- network -------------------------------------------------------------

def test_no_network_adds_unshare_net(tmp_path):
    cmd = build_bwrap_command(make_config(tmp_path, no_network=True))
    assert "--unshare-net" in cmd


def test_default_keeps_network(tmp_path):
    cmd = build_bwrap_command(make_config(tmp_path))
    assert "--unshare-net" not in cmd


# --- seccomp fd ----------------------------------------------------------

def test_seccomp_fd_emitted(tmp_path):
    cmd = build_bwrap_command(make_config(tmp_path), seccomp_fd=7)
    assert "--seccomp" in cmd
    idx = cmd.index("--seccomp")
    assert cmd[idx + 1] == "7"
    # It must come before the -- separator.
    assert idx < cmd.index("--")


def test_no_seccomp_fd_no_flag(tmp_path):
    cmd = build_bwrap_command(make_config(tmp_path))
    assert "--seccomp" not in cmd


# --- persistent cache ----------------------------------------------------

def test_cache_dir_bound(tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    cmd = build_bwrap_command(make_config(tmp_path, cache_dir=cache.resolve()))
    assert (str(cache.resolve()), "/cache") in _pairs(cmd, "--bind")


def test_no_cache_dir_no_bind(tmp_path):
    cmd = build_bwrap_command(make_config(tmp_path))
    assert "/cache" not in [d for _, d in _pairs(cmd, "--bind")]


# --- extra mounts --------------------------------------------------------

def test_extra_ro_mount(tmp_path):
    src = tmp_path / "ref"
    src.mkdir()
    cfg = make_config(
        tmp_path,
        extra_mounts=(ExtraMount(source=src.resolve(), target="/opt/ref", writable=False),),
    )
    cmd = build_bwrap_command(cfg)
    assert (str(src.resolve()), "/opt/ref") in _pairs(cmd, "--ro-bind")


def test_extra_rw_mount(tmp_path):
    src = tmp_path / "rw"
    src.mkdir()
    cfg = make_config(
        tmp_path,
        extra_mounts=(ExtraMount(source=src.resolve(), target="/srv/data", writable=True),),
    )
    cmd = build_bwrap_command(cfg)
    assert (str(src.resolve()), "/srv/data") in _pairs(cmd, "--bind")


# --- non-merged-/usr layout (#13 / design 12.2) --------------------------

def test_non_merged_usr_layout(tmp_path, monkeypatch):
    """On a distro where /bin, /sbin, /lib are REAL dirs (not symlinks into
    /usr), they must be ro-bound directly, not recreated as --symlink."""

    real_dirs = {"/usr", "/bin", "/sbin", "/lib", "/lib64"}

    class FakePath:
        def __init__(self, p):
            self._p = str(p)

        def is_symlink(self):
            return False  # nothing is a symlink on this fake distro

        def is_dir(self):
            return self._p in real_dirs

    monkeypatch.setattr(bwrap_mod, "Path", FakePath)
    args = bwrap_mod._system_mount_args()
    # No --symlink entries on a non-merged-/usr system.
    assert "--symlink" not in args
    # /bin and /sbin ro-bound as real dirs.
    pairs = []
    for i, tok in enumerate(args):
        if tok == "--ro-bind":
            pairs.append((args[i + 1], args[i + 2]))
    assert ("/bin", "/bin") in pairs
    assert ("/sbin", "/sbin") in pairs
    assert ("/usr", "/usr") in pairs


def test_merged_usr_layout(tmp_path, monkeypatch):
    """On a merged-/usr distro, /bin etc. are symlinks and must be recreated
    via --symlink, with only the real /usr ro-bound."""

    symlinks = {"/bin": "usr/bin", "/sbin": "usr/sbin", "/lib": "usr/lib",
                "/lib64": "usr/lib64"}
    real_dirs = {"/usr"}

    class FakePath:
        def __init__(self, p):
            self._p = str(p)

        def is_symlink(self):
            return self._p in symlinks

        def is_dir(self):
            return self._p in real_dirs

    monkeypatch.setattr(bwrap_mod, "Path", FakePath)
    monkeypatch.setattr(bwrap_mod.os, "readlink", lambda p: symlinks[str(p)])
    args = bwrap_mod._system_mount_args()
    # /bin recreated as a symlink to usr/bin.
    assert "--symlink" in args
    sym_pairs = []
    for i, tok in enumerate(args):
        if tok == "--symlink":
            sym_pairs.append((args[i + 1], args[i + 2]))
    assert ("usr/bin", "/bin") in sym_pairs
    # /usr ro-bound.
    ro_pairs = []
    for i, tok in enumerate(args):
        if tok == "--ro-bind":
            ro_pairs.append((args[i + 1], args[i + 2]))
    assert ("/usr", "/usr") in ro_pairs
