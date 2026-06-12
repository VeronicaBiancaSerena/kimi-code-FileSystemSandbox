"""Host-side path resolution and safety validation.

Everything here is pure (no sandbox launch). Functions resolve user-supplied
paths to absolute, symlink-free forms and enforce the safety rules from design
sections 10, 13 and 14. Failures raise the typed errors from ``errors.py`` with
actionable hints.
"""

from __future__ import annotations

import os
import shutil
import stat
from pathlib import Path

from .config import ExtraMount, ResourceLimits
from .errors import (
    InvalidPathError,
    InvalidProjectError,
    KimiNotFoundError,
    MissingDependencyError,
)

# Broad / system roots that must never be used as a project directory or as a
# sandbox KIMI_CODE_HOME. Matching is exact (after resolution); descendants are
# allowed (e.g. /var/lib/myproj is fine, /var is not).
_BROAD_DIRS: tuple[str, ...] = (
    "/",
    "/home",
    "/etc",
    "/usr",
    "/var",
    "/tmp",
    "/boot",
    "/bin",
    "/sbin",
    "/lib",
    "/lib64",
    "/opt",
    "/root",
    "/run",
    "/proc",
    "/sys",
    "/dev",
)

DEFAULT_STATE_ROOT = "~/.local/state/kimi-sandbox"


def _real_home() -> Path:
    """Return the host user's real home directory, resolved."""
    return Path(os.path.expanduser("~")).resolve()


def _real_kimi_code_home() -> Path:
    """Return the host's real ~/.kimi-code, resolved (may not exist)."""
    return (_real_home() / ".kimi-code").resolve()


def _expand_no_resolve(path: str | os.PathLike[str]) -> str:
    """Expand ``~``/vars only, leaving symlinks intact (no ``resolve()``).

    Used where we must report the *discovered* path before symlink resolution
    (e.g. the design 13 "kimi is a symlink" note), so callers do not have to run
    ``shutil.which`` a second time just to learn the pre-resolution path.
    """
    return os.path.expanduser(os.path.expandvars(os.fspath(path)))


def _expand(path: str | os.PathLike[str]) -> Path:
    """Expand ``~``/vars and resolve to an absolute, symlink-free path."""
    return Path(_expand_no_resolve(path)).resolve()


def _is_within(child: Path, parent: Path) -> bool:
    """True if ``child`` is ``parent`` or lives inside it (resolved paths)."""
    if child == parent:
        return True
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# Project directory
# ---------------------------------------------------------------------------

def resolve_project_dir(raw: str | None) -> Path:
    """Resolve and validate the host project directory mounted at /workspace.

    Defaults to the current working directory. Rejects broad/system roots and
    the real HOME so the sandbox keeps its value (design 14).
    """
    project = _expand(raw if raw not in (None, "") else ".")

    if not project.exists():
        raise InvalidProjectError(
            f"project directory does not exist: {project}",
            "Pass an existing directory, for example:\n"
            "  kimi-sandbox ~/work/my-project",
        )
    if not project.is_dir():
        raise InvalidProjectError(
            f"project path is not a directory: {project}",
            "The project argument must be a directory, not a file.",
        )

    if str(project) in _BROAD_DIRS or project == _real_home():
        raise InvalidProjectError(
            f"refusing to mount broad project root: {project}",
            "Choose a specific project directory instead, for example:\n"
            "  kimi-sandbox ~/work/my-project",
        )
    return project


# ---------------------------------------------------------------------------
# State root / profile / kimi-code-home
# ---------------------------------------------------------------------------

def resolve_state_root(raw: str | None) -> Path:
    """Resolve the host state root (design 10). Defaults to the XDG-ish path."""
    return _expand(raw if raw not in (None, "") else DEFAULT_STATE_ROOT)


def default_kimi_code_home(state_root: Path, profile: str) -> Path:
    """Compute the default profile kimi-code-home under the state root."""
    if not profile or "/" in profile or profile in (".", ".."):
        raise InvalidPathError(
            f"invalid profile name: {profile!r}",
            "Profile names must be a single path segment, e.g. 'work'.",
        )
    return (state_root / "profiles" / profile / "kimi-code-home").resolve()


def resolve_unsafe_kimi_code_home(raw: str) -> Path:
    """Resolve and guard a ``--unsafe-kimi-code-home`` path (design 10).

    Still rejects broad/system roots, the real HOME (and parents), and the real
    ``~/.kimi-code``. v1 provides no override for those.
    """
    path = _expand(raw)
    home = _real_home()

    if str(path) in _BROAD_DIRS:
        raise InvalidPathError(
            f"refusing to use broad/system path as kimi-code-home: {path}",
            "Pick a dedicated directory, not a system or broad root.",
        )
    if path == home or _is_within(home, path):
        # path == real HOME, or path is an ancestor of real HOME.
        raise InvalidPathError(
            f"refusing to use real HOME (or its parent) as kimi-code-home: {path}",
            "This would expose your home directory to all sandboxed processes.",
        )
    if path == _real_kimi_code_home():
        raise InvalidPathError(
            f"refusing to use the real ~/.kimi-code as kimi-code-home: {path}",
            "Sandbox profiles must stay separate from your real Kimi home.\n"
            "v1 intentionally provides no override for this.",
        )
    return path


# ---------------------------------------------------------------------------
# Cross-path relationship validation (design 10)
# ---------------------------------------------------------------------------

def validate_path_relationships(
    *,
    project_dir: Path,
    state_root: Path,
    kimi_code_home: Path,
    unsafe_kimi_code_home: bool,
) -> None:
    """Enforce the hard validation rules between the three key directories.

    Rules (design 10):
      - project_dir must not equal, contain, or live inside kimi_code_home.
      - state_root must not live inside project_dir.
      - project_dir must not live inside state_root.

    When ``unsafe_kimi_code_home`` is true the state_root is not mounted or used
    for this invocation, so the two state_root-vs-project checks are skipped to
    avoid confusing rejections about an unrelated default directory.
    """
    if _is_within(project_dir, kimi_code_home):
        raise InvalidPathError(
            f"project dir is inside kimi-code-home: {project_dir} ⊆ {kimi_code_home}",
            "The project and the Kimi profile home must be separate trees.",
        )
    if _is_within(kimi_code_home, project_dir):
        raise InvalidPathError(
            f"kimi-code-home is inside the project dir: {kimi_code_home} ⊆ {project_dir}",
            "Use --state-root/--profile outside the project, or a different "
            "--unsafe-kimi-code-home.",
        )
    if unsafe_kimi_code_home:
        # state_root is unused in this mode; its relationship to the project is
        # irrelevant. (kimi_code_home was validated against the project above.)
        return
    if _is_within(state_root, project_dir):
        raise InvalidPathError(
            f"state root is inside the project dir: {state_root} ⊆ {project_dir}",
            "Pick a --state-root outside the project directory.",
        )
    if _is_within(project_dir, state_root):
        raise InvalidPathError(
            f"project dir is inside the state root: {project_dir} ⊆ {state_root}",
            "Pick a project directory outside the --state-root.",
        )


# ---------------------------------------------------------------------------
# Extra mounts, persistent cache, resource limits (v2 §33)
# ---------------------------------------------------------------------------

# In-sandbox targets the launcher owns; an extra mount must not collide with
# (equal, contain, or be nested under) any of these or it would shadow a core
# mount or be shadowed by one. ``/`` is handled separately (every absolute path
# is nested under it, so it cannot participate in the containment check).
_RESERVED_TARGETS: tuple[str, ...] = (
    "/workspace",
    "/kimi-code-home",
    "/cache",
    "/home",
    "/tmp",
    "/run",
    "/proc",
    "/dev",
    "/etc",
    "/usr",
    "/lib",
    "/lib32",
    "/lib64",
    "/libx32",
    "/bin",
    "/sbin",
    "/sandbox",
)


def resolve_extra_mount(spec: str, *, writable: bool) -> ExtraMount:
    """Parse and validate a ``--ro-mount``/``--rw-mount`` spec (v2 §33.3).

    Accepted forms:
      ``HOST``            -> mounted at ``/mnt/<basename>`` inside the sandbox
      ``HOST:TARGET``     -> explicit absolute in-sandbox target

    The host source must exist. The target must be an absolute path that does
    not collide with a reserved sandbox mount point. The bare ``HOST`` form
    deliberately maps under ``/mnt`` rather than identity-mapping the host path,
    because most real host paths (e.g. ``~/data``) live under ``/home`` — a
    reserved tree — and would otherwise always be rejected.
    """
    if not spec:
        raise InvalidPathError(
            "empty mount spec",
            "Use --ro-mount HOST[:TARGET] (TARGET must be an absolute path).",
        )
    # Split on the last ':' so odd host paths still work; the leading-only form
    # (no ':') maps the host basename under /mnt.
    if ":" in spec:
        host_raw, target = spec.rsplit(":", 1)
        if not host_raw:
            raise InvalidPathError(
                f"mount spec missing host path: {spec!r}",
                "Use --ro-mount HOST[:TARGET].",
            )
    else:
        host_raw, target = spec, ""

    source = _expand(host_raw)
    if not source.exists():
        raise InvalidPathError(
            f"extra mount source does not exist: {source}",
            "Pass an existing host file or directory to mount.",
        )

    if not target:
        target = f"/mnt/{source.name}"
    if not target.startswith("/"):
        raise InvalidPathError(
            f"extra mount target must be absolute: {target!r}",
            "Use an absolute in-sandbox path, e.g. /opt/data.",
        )
    # Normalise without touching the host filesystem (target is in-sandbox).
    target = os.path.normpath(target)

    if target == "/":
        raise InvalidPathError(
            "extra mount target may not be the sandbox root '/'",
            "Choose a subdirectory, e.g. /opt/<name>, /srv/<name>, /mnt/<name>.",
        )

    target_path = Path(target)
    for reserved in _RESERVED_TARGETS:
        rp = Path(reserved)
        if target_path == rp or _is_within(target_path, rp) or _is_within(rp, target_path):
            raise InvalidPathError(
                f"extra mount target collides with reserved path: {target} vs {reserved}",
                "Choose a target outside the sandbox's own mounts "
                "(e.g. /opt/<name>, /srv/<name>, /mnt/<name>).",
            )
    return ExtraMount(source=source, target=target, writable=writable)


def resolve_cache_dir(state_root: Path, profile: str) -> Path:
    """Compute the per-profile persistent cache dir under the state root (31.4).

    Stored alongside the profile's kimi-code-home so a profile's cache and
    state live together and are easy to wipe as a unit.
    """
    if not profile or "/" in profile or profile in (".", ".."):
        raise InvalidPathError(
            f"invalid profile name: {profile!r}",
            "Profile names must be a single path segment, e.g. 'work'.",
        )
    return (state_root / "profiles" / profile / "cache").resolve()


def validate_resource_limits(limits: ResourceLimits) -> None:
    """Sanity-check resource-limit values before handing them to systemd-run.

    Catches obvious typos early (systemd would reject them too, but with a
    cryptic message after the sandbox has already been set up). ``memory_max``
    and ``cpu_quota`` are systemd-formatted strings; ``pids_max`` is a positive
    integer.
    """
    if limits.pids_max is not None and limits.pids_max <= 0:
        raise InvalidPathError(
            f"--pids-max must be positive: {limits.pids_max}",
            "Pass a positive integer, e.g. --pids-max 512.",
        )
    if limits.cpu_quota is not None and not limits.cpu_quota.endswith("%"):
        raise InvalidPathError(
            f"--cpu-quota must end with '%': {limits.cpu_quota!r}",
            "Use a percentage, e.g. --cpu-quota 150% (1.5 cores).",
        )
    if limits.memory_max is not None and not limits.memory_max:
        raise InvalidPathError(
            "--memory-max must not be empty",
            "Use a systemd size, e.g. --memory-max 2G or 512M.",
        )


def resolve_systemd_run(raw: str | None = None) -> Path | None:
    """Locate ``systemd-run`` for resource limiting; None if unavailable."""
    if raw:
        candidate = _expand(raw)
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
        return None
    found = shutil.which("systemd-run")
    return Path(found).resolve() if found else None


# ---------------------------------------------------------------------------
# Kimi binary discovery (design 13)
# ---------------------------------------------------------------------------

def resolve_kimi(raw: str | None) -> Path:
    """Locate the host ``kimi`` executable, resolving symlinks (design 13)."""
    return resolve_kimi_with_source(raw)[0]


def resolve_kimi_with_source(raw: str | None) -> tuple[Path, str]:
    """Locate ``kimi`` and also return its *unresolved* discovery path.

    Returns ``(resolved_path, source)`` where ``source`` is the explicit
    ``--kimi`` value (expanded) or the ``shutil.which("kimi")`` hit, before
    symlink resolution. Exposing ``source`` lets the CLI emit the design 13
    symlink note without calling ``shutil.which`` a second time.
    """
    if raw:
        source = _expand_no_resolve(raw)
        candidate = Path(source).resolve()
        if not candidate.exists():
            raise KimiNotFoundError(
                f"kimi executable not found at: {candidate}",
                "Check the --kimi path.",
            )
    else:
        found = shutil.which("kimi")
        if not found:
            raise KimiNotFoundError(
                "kimi executable not found",
                "Install Kimi Code first or pass an explicit path:\n"
                "  kimi-sandbox . --kimi /path/to/kimi",
            )
        source = found
        candidate = Path(found).resolve()

    if not candidate.is_file():
        raise KimiNotFoundError(
            f"kimi path is not a regular file: {candidate}",
            "Pass the path to the kimi executable itself with --kimi.",
        )
    if not os.access(candidate, os.X_OK):
        raise KimiNotFoundError(
            f"kimi path is not executable: {candidate}",
            "Make it executable (chmod +x) or pass a different --kimi path.",
        )
    return candidate, source


def kimi_is_script(kimi_path: Path) -> bool:
    """Best-effort detection of a shell-wrapper kimi (design 13 warning)."""
    try:
        with open(kimi_path, "rb") as fh:
            head = fh.read(2)
        return head == b"#!"
    except OSError:
        return False


# ---------------------------------------------------------------------------
# bubblewrap discovery (design 7)
# ---------------------------------------------------------------------------

def resolve_bwrap(raw: str | None = None) -> Path:
    """Locate the ``bwrap`` executable, failing with install guidance."""
    if raw:
        candidate = _expand(raw)
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
        raise MissingDependencyError(
            f"bubblewrap not found at: {candidate}",
            "Check the path passed for bwrap.",
        )
    found = shutil.which("bwrap")
    if not found:
        raise MissingDependencyError(
            "bubblewrap not found",
            "Install bubblewrap with your system package manager.\n"
            "On Ubuntu/Debian:\n"
            "  sudo apt install bubblewrap",
        )
    return Path(found).resolve()


# ---------------------------------------------------------------------------
# Directory creation helpers (design 31.1 / 31.2)
# ---------------------------------------------------------------------------

def ensure_dir(path: Path, *, mode: int = 0o700) -> None:
    """Create ``path`` (and parents) if missing, with owner-only perms."""
    path.mkdir(parents=True, exist_ok=True, mode=mode)


def world_or_group_readable(path: Path) -> bool:
    """True if ``path`` grants any group/other permission bits."""
    try:
        st = path.stat()
    except OSError:
        return False
    return bool(st.st_mode & (stat.S_IRWXG | stat.S_IRWXO))


__all__ = [
    "resolve_project_dir",
    "resolve_state_root",
    "default_kimi_code_home",
    "resolve_unsafe_kimi_code_home",
    "validate_path_relationships",
    "resolve_extra_mount",
    "resolve_cache_dir",
    "validate_resource_limits",
    "resolve_systemd_run",
    "resolve_kimi",
    "resolve_kimi_with_source",
    "kimi_is_script",
    "resolve_bwrap",
    "ensure_dir",
    "world_or_group_readable",
    "DEFAULT_STATE_ROOT",
]
