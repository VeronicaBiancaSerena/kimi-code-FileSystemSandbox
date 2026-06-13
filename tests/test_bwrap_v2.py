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


# --- mount pinning via --bind-fd / --ro-bind-fd (opt #3) -----------------

def test_path_fds_pin_workspace_and_kimi(tmp_path):
    cfg = make_config(tmp_path)
    path_fds = {str(cfg.project_dir): 11, str(cfg.kimi_path): 12,
                str(cfg.kimi_code_home): 13}
    cmd = build_bwrap_command(cfg, path_fds=path_fds)
    # rw project -> --bind-fd <fd> /workspace ; ro kimi -> --ro-bind-fd ...
    assert (("11", "/workspace") in _pairs(cmd, "--bind-fd"))
    assert (("13", "/kimi-code-home") in _pairs(cmd, "--bind-fd"))
    assert (("12", "/sandbox/bin/kimi") in _pairs(cmd, "--ro-bind-fd"))
    # No path-based bind remains for the pinned sources.
    assert (str(cfg.project_dir), "/workspace") not in _pairs(cmd, "--bind")
    assert (str(cfg.kimi_path), "/sandbox/bin/kimi") not in _pairs(cmd, "--ro-bind")


def test_path_fds_read_only_workspace_uses_ro_bind_fd(tmp_path):
    cfg = make_config(tmp_path, mode=MODE_READ_ONLY)
    cmd = build_bwrap_command(cfg, path_fds={str(cfg.project_dir): 9})
    assert ("9", "/workspace") in _pairs(cmd, "--ro-bind-fd")
    assert ("9", "/workspace") not in _pairs(cmd, "--bind-fd")


def test_path_fds_only_pins_known_sources(tmp_path):
    # A source without an fd falls back to a path bind (e.g. cache here).
    cache = tmp_path / "cache"
    cache.mkdir()
    cfg = make_config(tmp_path, cache_dir=cache.resolve())
    cmd = build_bwrap_command(cfg, path_fds={str(cfg.project_dir): 5})
    assert ("5", "/workspace") in _pairs(cmd, "--bind-fd")
    # cache had no fd -> still a path bind.
    assert (str(cache.resolve()), "/cache") in _pairs(cmd, "--bind")


def test_no_path_fds_uses_path_binds(tmp_path):
    cfg = make_config(tmp_path)
    cmd = build_bwrap_command(cfg)
    assert "--bind-fd" not in cmd
    assert "--ro-bind-fd" not in cmd
    assert (str(cfg.project_dir), "/workspace") in _pairs(cmd, "--bind")


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


# --- controlled conda mounts (mod_v2 §12) --------------------------------

from kimi_sandbox.config import (  # noqa: E402
    CondaConfig,
    CondaExistingEnv,
    GeneratedFileMount,
)


def _make_conda(tmp_path, *, writable="/cache/conda", existing=()):
    root = tmp_path / "anaconda3"
    (root / "bin").mkdir(parents=True, exist_ok=True)
    return CondaConfig(
        root=root.resolve(),
        sandbox_original_root=str(root.resolve()),
        writable_root=writable,
        shell_integration=True,
        existing_envs=existing,
    )


def _gen_mounts(tmp_path):
    shim = tmp_path / "g" / "conda"
    rc = tmp_path / "g" / "condarc"
    be = tmp_path / "g" / "conda-bash-env"
    (tmp_path / "g").mkdir(exist_ok=True)
    for f in (shim, rc, be):
        f.write_text("x")
    return (
        GeneratedFileMount(source=shim, target="/sandbox/bin/conda", executable=True),
        GeneratedFileMount(source=rc, target="/sandbox/etc/condarc"),
        GeneratedFileMount(source=be, target="/sandbox/etc/conda-bash-env"),
    )


def test_conda_root_ro_bound_canonical_and_original(tmp_path):
    conda = _make_conda(tmp_path)
    cfg = make_config(tmp_path, conda=conda, generated_file_mounts=_gen_mounts(tmp_path),
                      cache_dir=(tmp_path / "cache").resolve())
    (tmp_path / "cache").mkdir(exist_ok=True)
    cmd = build_bwrap_command(cfg)
    ro = _pairs(cmd, "--ro-bind")
    assert (str(conda.root), "/opt/kimi-conda/root") in ro
    assert (str(conda.root), conda.sandbox_original_root) in ro


def test_conda_existing_env_ro_bound(tmp_path):
    env_src = tmp_path / "extra" / "envs" / "foo"
    env_src.mkdir(parents=True)
    conda = _make_conda(
        tmp_path,
        existing=(CondaExistingEnv(source=env_src.resolve(), name="foo"),),
    )
    cfg = make_config(tmp_path, conda=conda, generated_file_mounts=_gen_mounts(tmp_path))
    cmd = build_bwrap_command(cfg)
    ro = _pairs(cmd, "--ro-bind")
    assert (str(env_src.resolve()), "/opt/kimi-conda/existing-envs/foo") in ro
    assert (str(env_src.resolve()), str(env_src.resolve())) in ro


def test_conda_generated_files_ro_bound(tmp_path):
    conda = _make_conda(tmp_path)
    gen = _gen_mounts(tmp_path)
    cfg = make_config(tmp_path, conda=conda, generated_file_mounts=gen)
    cmd = build_bwrap_command(cfg)
    ro = _pairs(cmd, "--ro-bind")
    assert (str(gen[0].source), "/sandbox/bin/conda") in ro
    assert (str(gen[1].source), "/sandbox/etc/condarc") in ro
    assert "--dir" in cmd and "/sandbox/etc" in cmd


def test_conda_tmp_writable_creates_tmp_dir(tmp_path):
    conda = _make_conda(tmp_path, writable="/tmp/kimi-conda")
    cfg = make_config(tmp_path, conda=conda, generated_file_mounts=_gen_mounts(tmp_path))
    cmd = build_bwrap_command(cfg)
    dirs = [cmd[i + 1] for i, t in enumerate(cmd) if t == "--dir"]
    assert "/tmp/kimi-conda" in dirs
    assert "/tmp/kimi-conda/envs" in dirs


def test_conda_root_fd_pinned(tmp_path):
    conda = _make_conda(tmp_path)
    cfg = make_config(tmp_path, conda=conda, generated_file_mounts=_gen_mounts(tmp_path))
    cmd = build_bwrap_command(cfg, path_fds={str(conda.root): 21})
    # fd-pinned ro bind used for the canonical conda root.
    assert (("21", "/opt/kimi-conda/root") in _pairs(cmd, "--ro-bind-fd"))


def test_no_conda_no_opt_mounts(tmp_path):
    cmd = build_bwrap_command(make_config(tmp_path))
    assert "/opt/kimi-conda/root" not in cmd
    assert "/sandbox/etc" not in cmd


# --- audit #6: original-prefix alias bind uses a distinct pinned fd ----------

def test_conda_original_alias_uses_distinct_fd(tmp_path):
    from kimi_sandbox.config import CONDA_ALIAS_FD_SUFFIX

    conda = _make_conda(tmp_path)
    cfg = make_config(tmp_path, conda=conda, generated_file_mounts=_gen_mounts(tmp_path))
    canonical_fd = 30
    alias_fd = 31
    path_fds = {
        str(conda.root): canonical_fd,
        str(conda.root) + CONDA_ALIAS_FD_SUFFIX: alias_fd,
    }
    cmd = build_bwrap_command(cfg, path_fds=path_fds)
    ro_fd = _pairs(cmd, "--ro-bind-fd")
    # canonical bind uses fd 30 at the canonical path, alias uses fd 31 at the
    # original path -> the same fd is never emitted twice (bwrap closes it).
    assert (str(canonical_fd), "/opt/kimi-conda/root") in ro_fd
    assert (str(alias_fd), conda.sandbox_original_root) in ro_fd
    fds_used = [fd for fd, _ in ro_fd]
    assert fds_used.count(str(canonical_fd)) == 1


def test_conda_alias_falls_back_to_path_bind_without_fd(tmp_path):
    conda = _make_conda(tmp_path)
    cfg = make_config(tmp_path, conda=conda, generated_file_mounts=_gen_mounts(tmp_path))
    # No path_fds (e.g. --dry-run): alias uses a plain path ro-bind.
    cmd = build_bwrap_command(cfg)
    ro = _pairs(cmd, "--ro-bind")
    assert (str(conda.root), conda.sandbox_original_root) in ro
