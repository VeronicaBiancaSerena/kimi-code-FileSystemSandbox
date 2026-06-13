"""Configuration data structures for the sandbox.

These are plain, immutable value objects. ``SandboxConfig`` is the fully
resolved description of one sandbox invocation; ``build_bwrap_command`` (in
``bwrap.py``) turns it into an argv list without performing any I/O or
launching anything. Keeping construction and execution separate makes the
command builder trivially unit-testable.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# Sandbox permission modes (design 15 / v2 §33.2).
MODE_WORKSPACE_WRITE = "workspace-write"
MODE_READ_ONLY = "read-only"


@dataclass(frozen=True)
class ExtraMount:
    """An extra host->sandbox bind requested via --ro-mount / --rw-mount.

    ``source`` is a resolved host path; ``target`` is an absolute in-sandbox
    path; ``writable`` selects ``--bind`` vs ``--ro-bind``.
    """

    source: Path
    target: str
    writable: bool


@dataclass(frozen=True)
class ProfileMount:
    """Read-only host mount below ``/kimi-code-home`` (mod_v1 §6).

    ``source`` is a resolved host path (v1: must be an existing directory).
    ``relative_target`` is a normalized, ``..``-free relative path such as
    ``"skills"`` or ``"integrations/skills"``. The final in-sandbox target is
    always ``/kimi-code-home/<relative_target>``.

    A dedicated type (rather than reusing :class:`ExtraMount`) is what keeps
    these mounts strictly confined to the profile tree: ``ExtraMount.target`` is
    an arbitrary absolute sandbox path, whereas a profile sub-mount must never
    escape ``/kimi-code-home``. That confinement is a security boundary.
    """

    source: Path
    relative_target: str


@dataclass(frozen=True)
class ResourceLimits:
    """Optional cgroup resource limits applied via ``systemd-run --user``.

    Values mirror systemd resource-control properties; ``None`` means unset.
    ``memory_max``/``cpu_quota``/``pids_max`` are passed as
    ``MemoryMax=``/``CPUQuota=``/``TasksMax=`` respectively.
    """

    memory_max: str | None = None
    cpu_quota: str | None = None
    pids_max: int | None = None

    def is_empty(self) -> bool:
        return (
            self.memory_max is None
            and self.cpu_quota is None
            and self.pids_max is None
        )


@dataclass(frozen=True)
class CondaExistingEnv:
    """An extra, already-existing host conda env, mounted read-only (mod_v2 §10).

    ``source`` is a resolved host env directory; ``name`` is the in-sandbox env
    name it is exposed under at ``/opt/kimi-conda/existing-envs/<name>``. Unlike
    a generic mount these are always read-only — the v2 contract is that
    pre-existing host conda content can never be modified inside the sandbox.
    """

    source: Path
    name: str


@dataclass(frozen=True)
class CondaConfig:
    """Fully resolved controlled-conda integration (mod_v2 §10).

    ``root`` is the resolved host conda root (must contain ``bin/conda``). It is
    bound read-only at two in-sandbox paths: the canonical ``sandbox_root``
    (``/opt/kimi-conda/root``) and ``sandbox_original_root`` — the conda root's
    original absolute host path, recreated inside the sandbox so hard-coded
    console-script shebangs (``#!/home/user/anaconda3/bin/python``) still
    resolve (mod_v2 §7.6).

    ``writable_root`` is the in-sandbox writable area for *new* envs/packages
    (``/cache/conda`` or ``/tmp/kimi-conda``); host conda content is never
    written. ``shell_integration`` toggles the ``conda activate`` bash hook
    (``BASH_ENV``). ``existing_envs`` are extra read-only host envs.
    """

    root: Path
    sandbox_original_root: str
    writable_root: str
    shell_integration: bool
    existing_envs: tuple[CondaExistingEnv, ...] = ()
    sandbox_root: str = "/opt/kimi-conda/root"


@dataclass(frozen=True)
class GeneratedFileMount:
    """A launcher-generated file bound read-only into the sandbox (mod_v2 §10).

    ``source`` is a host path the launcher wrote (e.g. the conda shim); ``target``
    is the absolute in-sandbox path it is bound at. ``executable`` records that
    the source needs +x (the conda shim) so the launcher can chmod it. Generic
    so future launcher helpers can reuse it, not just conda.
    """

    source: Path
    target: str
    executable: bool = False


@dataclass(frozen=True)
class SandboxConfig:
    """Fully resolved description of a single sandbox launch.

    All paths are host-side absolute paths that have already been resolved and
    validated. ``inner_command`` is the argv to run *inside* the sandbox (e.g.
    ``["/sandbox/bin/kimi", "--version"]`` or ``["/usr/bin/bash", "-lc", ...]``).
    """

    project_dir: Path
    kimi_code_home: Path
    kimi_path: Path
    inner_command: list[str]
    env: dict[str, str]
    state_root: Path | None = None
    profile: str = "default"
    unsafe_kimi_code_home: bool = False
    debug: bool = False
    # Permission mode: workspace-write (default) or read-only (design 15).
    mode: str = MODE_WORKSPACE_WRITE
    # Network isolation: when True, add --unshare-net (v2 §33.4).
    no_network: bool = False
    # Optional persistent cache dir bound at /cache (design 11 / 31.4).
    cache_dir: Path | None = None
    # Extra host mounts (v2 §33.3).
    extra_mounts: tuple[ExtraMount, ...] = ()
    # Read-only profile sub-mounts under /kimi-code-home (mod_v1 §6/§10).
    profile_ro_mounts: tuple[ProfileMount, ...] = ()
    # Lay down /home/sandbox/.kimi-code -> /kimi-code-home symlink (mod_v1 §10.3).
    compat_kimi_home: bool = True
    # Sandbox-internal mount target for the kimi binary.
    sandbox_kimi_target: str = "/sandbox/bin/kimi"
    # Controlled conda integration (mod_v2); None when conda is disabled.
    conda: CondaConfig | None = None
    # Launcher-generated files bound read-only (conda shim/condarc/bash hook).
    generated_file_mounts: tuple[GeneratedFileMount, ...] = ()


# Sandbox-internal canonical paths (see design section 11).
SANDBOX_WORKSPACE = "/workspace"
SANDBOX_KIMI_CODE_HOME = "/kimi-code-home"
SANDBOX_HOME = "/home/sandbox"
SANDBOX_KIMI_BIN_DIR = "/sandbox/bin"
SANDBOX_KIMI_BIN = "/sandbox/bin/kimi"
SANDBOX_CACHE = "/cache"

# Controlled-conda sandbox-internal paths (mod_v2 §4.1).
SANDBOX_ETC_DIR = "/sandbox/etc"
SANDBOX_CONDA_ROOT = "/opt/kimi-conda/root"
SANDBOX_CONDA_EXISTING_ENVS = "/opt/kimi-conda/existing-envs"
SANDBOX_CONDA_SHIM = "/sandbox/bin/conda"
SANDBOX_CONDARC = "/sandbox/etc/condarc"
SANDBOX_CONDA_BASH_ENV = "/sandbox/etc/conda-bash-env"
SANDBOX_CONDA_CACHE_WRITABLE = "/cache/conda"
SANDBOX_CONDA_TMP_WRITABLE = "/tmp/kimi-conda"

# A host conda root / existing env is bound read-only twice: once at its
# canonical /opt/kimi-conda path and once at its original absolute path (for
# shebang compatibility). bubblewrap closes each bind fd after use, so the
# second (alias) bind needs its *own* pinned fd. ``path_fds`` therefore carries
# the alias fd under ``<source><CONDA_ALIAS_FD_SUFFIX>`` — a key that cannot
# collide with any real host path (NUL is not allowed in pathnames).
CONDA_ALIAS_FD_SUFFIX = "\x00alias"

# Minimal /etc allowlist for DNS, hosts, NSS and TLS trust (design 12.3 / 28.11).
#
# Deliberately excludes /etc/passwd and /etc/group: binding the host copies
# would leak host usernames, which contradicts the isolation intent. The only
# visible cost is a cosmetic "id: cannot find name for user ID" warning; tools
# that need a username still work because HOME is set explicitly. /etc/localtime
# is included for correct local time; on most distros it is a symlink into /usr
# (already mounted read-only), so a ro-bind-try resolves transparently.
ETC_MIN_FILES = (
    "/etc/resolv.conf",
    "/etc/hosts",
    "/etc/nsswitch.conf",
    "/etc/host.conf",
    "/etc/gai.conf",
    "/etc/localtime",
)
# Directories under /etc that may be needed for TLS trust; bound when present.
ETC_MIN_DIRS = (
    "/etc/ssl",
    "/etc/ca-certificates",
    "/etc/pki",
)

__all__ = [
    "SandboxConfig",
    "ExtraMount",
    "ProfileMount",
    "ResourceLimits",
    "CondaConfig",
    "CondaExistingEnv",
    "GeneratedFileMount",
    "MODE_WORKSPACE_WRITE",
    "MODE_READ_ONLY",
    "SANDBOX_WORKSPACE",
    "SANDBOX_KIMI_CODE_HOME",
    "SANDBOX_HOME",
    "SANDBOX_KIMI_BIN_DIR",
    "SANDBOX_KIMI_BIN",
    "SANDBOX_CACHE",
    "SANDBOX_ETC_DIR",
    "SANDBOX_CONDA_ROOT",
    "SANDBOX_CONDA_EXISTING_ENVS",
    "SANDBOX_CONDA_SHIM",
    "SANDBOX_CONDARC",
    "SANDBOX_CONDA_BASH_ENV",
    "SANDBOX_CONDA_CACHE_WRITABLE",
    "SANDBOX_CONDA_TMP_WRITABLE",
    "CONDA_ALIAS_FD_SUFFIX",
    "ETC_MIN_FILES",
    "ETC_MIN_DIRS",
]
