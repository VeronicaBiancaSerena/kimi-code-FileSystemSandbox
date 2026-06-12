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
    # Sandbox-internal mount target for the kimi binary.
    sandbox_kimi_target: str = "/sandbox/bin/kimi"


# Sandbox-internal canonical paths (see design section 11).
SANDBOX_WORKSPACE = "/workspace"
SANDBOX_KIMI_CODE_HOME = "/kimi-code-home"
SANDBOX_HOME = "/home/sandbox"
SANDBOX_KIMI_BIN_DIR = "/sandbox/bin"
SANDBOX_KIMI_BIN = "/sandbox/bin/kimi"
SANDBOX_CACHE = "/cache"

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
    "ResourceLimits",
    "MODE_WORKSPACE_WRITE",
    "MODE_READ_ONLY",
    "SANDBOX_WORKSPACE",
    "SANDBOX_KIMI_CODE_HOME",
    "SANDBOX_HOME",
    "SANDBOX_KIMI_BIN_DIR",
    "SANDBOX_KIMI_BIN",
    "SANDBOX_CACHE",
    "ETC_MIN_FILES",
    "ETC_MIN_DIRS",
]
