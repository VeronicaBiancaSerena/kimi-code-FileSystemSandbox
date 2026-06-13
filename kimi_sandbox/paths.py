"""Host-side path resolution and safety validation.

Everything here is pure (no sandbox launch). Functions resolve user-supplied
paths to absolute, symlink-free forms and enforce the safety rules from design
sections 10, 13 and 14. Failures raise the typed errors from ``errors.py`` with
actionable hints.
"""

from __future__ import annotations

import os
import posixpath
import re
import shutil
import stat
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .config import (
    SANDBOX_CONDA_CACHE_WRITABLE,
    CondaConfig,
    CondaExistingEnv,
    ExtraMount,
    ProfileMount,
    ResourceLimits,
)
from .errors import (
    InvalidPathError,
    InvalidProjectError,
    KimiNotFoundError,
    MissingDependencyError,
    SandboxError,
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


# ---------------------------------------------------------------------------
# Controlled conda integration (mod_v2 §11)
# ---------------------------------------------------------------------------

# Launcher-managed in-sandbox paths that an original-prefix compatibility bind
# (the host conda root recreated at its real absolute path, mod_v2 §7.6) must
# never cover or be nested under: doing so would shadow a core mount or require
# binding over a read-only system tree. ``/home`` is deliberately *absent* —
# conda roots usually live under ``/home/<user>/anaconda3`` and the sandbox
# ``/home`` is an empty tmpfs, so recreating the root there is exactly intended.
_CONDA_RESERVED_COMPAT_PREFIXES: tuple[str, ...] = (
    "/workspace",
    "/kimi-code-home",
    "/cache",
    "/tmp",
    "/sandbox",
    "/proc",
    "/dev",
    "/etc",
    "/usr",
    "/bin",
    "/sbin",
    "/lib",
    "/lib32",
    "/lib64",
    "/libx32",
    "/opt",
)

# Valid conda existing-env name (mod_v2 §11): a single path segment.
_CONDA_ENV_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


def _conda_compat_target_conflict(original: str) -> str | None:
    """Return the launcher path an original-prefix compat target would collide
    with, or ``None`` if it is safe.

    A conflict is any reserved prefix that *equals*, *contains*, or *is nested
    under* ``original`` (the host conda root's absolute path, recreated inside
    the sandbox). Mirrors the containment logic of :data:`_RESERVED_TARGETS`.
    """
    op = Path(original)
    for reserved in _CONDA_RESERVED_COMPAT_PREFIXES:
        rp = Path(reserved)
        if op == rp or _is_within(op, rp) or _is_within(rp, op):
            return reserved
    return None


def resolve_conda_root(spec: str) -> Path:
    """Resolve and validate the host conda root (mod_v2 §11).

    Expands ``~``/vars and resolves symlinks. The result must be an existing
    directory; the ``bin/conda`` executable check and the cross-path / compat
    checks live in :func:`validate_conda_config` so callers get one consolidated
    error surface.

    Note (audit #4): symlinks are deliberately resolved to the real path. This
    matches conda's own behaviour — conda records the *realpath* of a prefix in
    activation state and in console-script shebangs — so the original-prefix
    compatibility bind (which uses this resolved path) lines up with what those
    shebangs reference even when the user points ``conda_root`` at a symlink
    such as ``~/anaconda3 -> /data/anaconda3``.
    """
    if not spec:
        raise InvalidPathError(
            "conda_enabled is true but conda_root is empty",
            "Set conda_root to your conda install, e.g. conda_root = \"~/anaconda3\".",
        )
    root = _expand(spec)
    if not root.exists():
        raise InvalidPathError(
            f"conda_root does not exist: {root}",
            "Point conda_root at an existing conda install (it must contain "
            "bin/conda).",
        )
    if not root.is_dir():
        raise InvalidPathError(
            f"conda_root is not a directory: {root}",
            "conda_root must be the conda install directory, not a file.",
        )
    return root


def resolve_conda_existing_env(spec: str) -> CondaExistingEnv:
    """Parse and validate a ``HOST_ENV_DIR:NAME`` extra existing-env (mod_v2 §11).

    The host env directory must exist; ``NAME`` is the read-only in-sandbox name
    it is exposed under at ``/opt/kimi-conda/existing-envs/NAME`` and must match
    ``^[A-Za-z0-9_.-]+$`` (a single safe path segment).
    """
    if not spec:
        raise InvalidPathError(
            "empty conda_existing_envs spec",
            "Use \"HOST_ENV_DIR:NAME\", e.g. \"~/somewhere/envs/foo:foo\".",
        )
    if ":" not in spec:
        raise InvalidPathError(
            f"conda_existing_envs spec missing ':': {spec!r}",
            "Use \"HOST_ENV_DIR:NAME\", e.g. \"~/somewhere/envs/foo:foo\".",
        )
    host_raw, name = spec.rsplit(":", 1)
    if not host_raw:
        raise InvalidPathError(
            f"conda_existing_envs spec missing host path: {spec!r}",
            "Use \"HOST_ENV_DIR:NAME\".",
        )
    if not name or not _CONDA_ENV_NAME_RE.match(name):
        raise InvalidPathError(
            f"invalid conda_existing_envs name: {name!r}",
            "Names must match ^[A-Za-z0-9_.-]+$ (a single path segment).",
        )
    source = _expand(host_raw)
    if not source.exists():
        raise InvalidPathError(
            f"conda_existing_envs source does not exist: {source}",
            "Pass an existing conda env directory to mount read-only.",
        )
    if not source.is_dir():
        raise InvalidPathError(
            f"conda_existing_envs source is not a directory: {source}",
            "A conda env source must be the env directory.",
        )
    return CondaExistingEnv(source=source, name=name)


def validate_conda_config(
    conda: CondaConfig,
    *,
    persistent_cache: bool,
    project_dir: Path,
    state_root: Path | None,
    kimi_code_home: Path,
    cache_dir: Path | None,
) -> None:
    """Enforce all controlled-conda safety rules (mod_v2 §11).

    Checks the conda root is a real install (``bin/conda`` executable), does not
    live inside any launcher-managed host tree (project/state/profile/cache),
    that its original-prefix compatibility target will not cover a launcher path,
    that ``conda_writable = "cache"`` is backed by ``persistent_cache = true``,
    and that existing-env names are unique and safe. All failures are typed
    :class:`InvalidPathError` with actionable hints.
    """
    root = conda.root
    conda_exe = root / "bin" / "conda"
    if not conda_exe.exists():
        raise InvalidPathError(
            f"conda_root has no bin/conda: {conda_exe}",
            "Point conda_root at a real conda install (anaconda3/miniconda3/...).",
        )
    if not (conda_exe.is_file() and os.access(conda_exe, os.X_OK)):
        raise InvalidPathError(
            f"conda_root/bin/conda is not executable: {conda_exe}",
            "Ensure bin/conda is a runnable executable.",
        )

    # The conda root must not sit inside any tree the launcher owns/writes — a
    # read-only bind of e.g. the project dir would otherwise be self-referential.
    enclosers: list[tuple[str, Path]] = [("project dir", project_dir)]
    if state_root is not None:
        enclosers.append(("state root", state_root))
    enclosers.append(("kimi-code-home", kimi_code_home))
    if cache_dir is not None:
        enclosers.append(("cache dir", cache_dir))
    for label, encloser in enclosers:
        if _is_within(root, encloser):
            raise InvalidPathError(
                f"conda_root is inside the {label}: {root} ⊆ {encloser}",
                "Keep the conda install outside the project/state/cache trees.",
            )

    # The original-prefix compatibility bind recreates the root at its absolute
    # host path inside the sandbox; that path must not clash with a core mount.
    conflict = _conda_compat_target_conflict(conda.sandbox_original_root)
    if conflict is not None:
        raise InvalidPathError(
            f"conda_root original path {conda.sandbox_original_root} collides "
            f"with launcher-managed path {conflict}",
            "Install conda under your home dir (e.g. ~/anaconda3); roots under "
            "/usr, /opt, /etc, ... cannot be safely recreated read-only.",
        )

    if conda.writable_root == SANDBOX_CONDA_CACHE_WRITABLE and not persistent_cache:
        raise InvalidPathError(
            "conda_writable = \"cache\" requires persistent_cache = true",
            "Either set persistent_cache = true, or use conda_writable = \"tmp\" "
            "(new envs/packages then vanish when the sandbox exits).",
        )

    seen: set[str] = set()
    for env in conda.existing_envs:
        if env.name in seen:
            raise InvalidPathError(
                f"duplicate conda_existing_envs name: {env.name!r}",
                "Each conda_existing_envs entry must use a distinct NAME.",
            )
        seen.add(env.name)
        env_conflict = _conda_compat_target_conflict(str(env.source))
        if env_conflict is not None:
            raise InvalidPathError(
                f"conda_existing_envs source {env.source} original path "
                f"collides with launcher-managed path {env_conflict}",
                "Existing envs must live outside launcher-managed trees.",
            )


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


# ---------------------------------------------------------------------------
# Profile read-only sub-mounts under /kimi-code-home (mod_v1 §7, §10.2)
# ---------------------------------------------------------------------------

def resolve_profile_ro_mount(spec: str) -> ProfileMount:
    """Parse and validate a ``HOST:RELATIVE_TARGET`` profile mount (mod_v1 §7).

    The mount is always read-only and always lands at
    ``/kimi-code-home/<RELATIVE_TARGET>``. Unlike a generic extra mount, the
    target may not be an arbitrary absolute path: it is confined to a safe,
    ``..``-free relative path under the profile tree. v1 only supports directory
    sources (file-level profile mounts are intentionally out of scope to avoid
    mountpoint create / file-vs-dir ambiguity).

    Example: ``~/.kimi-code/skills:skills`` ->
    ``ProfileMount(source=<resolved ~/.kimi-code/skills>, relative_target="skills")``.
    """
    if not spec:
        raise InvalidPathError(
            "empty profile mount spec",
            "Use profile_ro_mounts = [\"HOST:RELATIVE_TARGET\"], "
            "e.g. \"~/.kimi-code/skills:skills\".",
        )
    if ":" not in spec:
        raise InvalidPathError(
            f"profile mount spec missing ':': {spec!r}",
            "Use HOST:RELATIVE_TARGET, e.g. \"~/.kimi-code/skills:skills\".",
        )
    # Split on the last ':' so host paths containing a ':' still work; the
    # relative target itself never contains ':'.
    host_raw, target = spec.rsplit(":", 1)
    if not host_raw:
        raise InvalidPathError(
            f"profile mount spec missing host path: {spec!r}",
            "Use HOST:RELATIVE_TARGET, e.g. \"~/.kimi-code/skills:skills\".",
        )
    if not target:
        raise InvalidPathError(
            f"profile mount spec missing relative target: {spec!r}",
            "Use HOST:RELATIVE_TARGET, e.g. \"~/.kimi-code/skills:skills\".",
        )

    source = _expand(host_raw)
    if not source.exists():
        raise InvalidPathError(
            f"profile mount source does not exist: {source}",
            "Pass an existing host directory to mount under /kimi-code-home.",
        )
    if not source.is_dir():
        raise InvalidPathError(
            f"profile mount source is not a directory: {source}",
            "v1 only supports directory profile mounts (file mounts are out of "
            "scope).",
        )

    if target.startswith("/"):
        raise InvalidPathError(
            f"profile mount target must be relative, not absolute: {target!r}",
            "Use a path under /kimi-code-home, e.g. \"skills\" or "
            "\"integrations/skills\".",
        )
    # Reject any '..' (or '.') path segment in the *raw* target before
    # normalisation: posixpath.normpath() would silently collapse
    # "skills/../other" to "other", which would hide a traversal attempt.
    raw_segments = [seg for seg in target.split("/") if seg != ""]
    if any(seg in (".", "..") for seg in raw_segments):
        raise InvalidPathError(
            f"profile mount target may not contain '.'/'..' segments: {target!r}",
            "The target must be a plain relative path with no '..' segments.",
        )
    if not raw_segments:
        raise InvalidPathError(
            f"profile mount target is empty after normalisation: {target!r}",
            "Use a path under /kimi-code-home, e.g. \"skills\".",
        )
    # Normalise with POSIX semantics: the target is an in-sandbox path, so host
    # platform separators must not enter the picture.
    normalized = posixpath.normpath(target)
    if normalized.startswith("/") or normalized in (".", ".."):
        raise InvalidPathError(
            f"profile mount target escapes /kimi-code-home: {target!r}",
            "Use a path under /kimi-code-home, e.g. \"skills\".",
        )

    return ProfileMount(source=source, relative_target=normalized)


def validate_profile_ro_mounts(
    mounts: list[ProfileMount] | tuple[ProfileMount, ...]
) -> None:
    """Cross-check a set of profile mounts (mod_v1 §8).

    At minimum, no two mounts may share the same ``relative_target`` (that would
    be an ambiguous double bind on one mountpoint). If file-level profile mounts
    are added later, the per-target type-conflict check belongs here too.
    """
    seen: dict[str, Path] = {}
    for mount in mounts:
        if mount.relative_target in seen:
            raise InvalidPathError(
                f"duplicate profile mount target: {mount.relative_target!r}",
                "Each profile_ro_mounts entry must use a distinct relative "
                "target (e.g. do not mount two sources at 'skills').",
            )
        seen[mount.relative_target] = mount.source


def prepare_profile_mount_targets(
    kimi_code_home: Path, mounts: tuple[ProfileMount, ...]
) -> None:
    """Create the profile mountpoints inside ``kimi_code_home`` (mod_v1 §10.2).

    Must be called *after* ``ensure_dir(kimi_code_home)``. For each mount this
    walks ``relative_target`` component by component under ``kimi_code_home`` and:

      * rejects a symlink at **any** component (leaf or intermediate), so no
        part of the profile path can be redirected outside the profile tree;
      * creates missing directories (owner-only);
      * accepts an existing directory;
      * rejects a non-directory (raises :class:`SandboxError`);
      * warns when the leaf already exists and is non-empty, because the
        read-only bind will *shadow* that content inside the sandbox (host files
        are not deleted, but the original contents are invisible there).

    As defence-in-depth, the realpath of the final mountpoint is verified to
    still live inside ``kimi_code_home``. This only prepares mountpoints in the
    profile; it never touches the MCP / skill source directories.
    """
    home_real = kimi_code_home.resolve()
    for mount in mounts:
        parts = [p for p in mount.relative_target.split("/") if p]
        current = kimi_code_home
        # Intermediate components: each must be a real (non-symlink) directory.
        for part in parts[:-1]:
            current = current / part
            if current.is_symlink():
                raise SandboxError(
                    f"profile mountpoint path crosses a symlink: {current}",
                    "Refusing to mount through a symlinked profile path. Remove "
                    "or replace it with a real directory.",
                )
            if current.exists():
                if not current.is_dir():
                    raise SandboxError(
                        f"profile mountpoint parent is not a directory: {current}",
                        "Remove the conflicting file or choose another target.",
                    )
            else:
                ensure_dir(current)

        target = current / parts[-1]
        # Leaf: a symlink would let the profile path be redirected; reject it.
        if target.is_symlink():
            raise SandboxError(
                f"profile mountpoint is a symlink: {target}",
                "Refusing to mount onto a symlinked profile path. Remove it or "
                "replace it with a real directory.",
            )
        if target.exists():
            if not target.is_dir():
                raise SandboxError(
                    f"profile mountpoint exists but is not a directory: {target}",
                    "v1 only supports directory profile mounts; remove the file "
                    "or choose a different relative target.",
                )
            try:
                non_empty = any(target.iterdir())
            except OSError:
                non_empty = False
            if non_empty:
                print(
                    f"warning: profile mountpoint {target} is non-empty; its "
                    "contents will be shadowed (hidden) inside the sandbox by "
                    "the read-only bind mount. Host files are not deleted.",
                    file=sys.stderr,
                )
        else:
            ensure_dir(target)

        # Defence-in-depth: the (now-existing) mountpoint must still resolve
        # inside the profile tree.
        if not _is_within(target.resolve(), home_real):
            raise SandboxError(
                f"profile mountpoint escapes the profile tree: {target} -> "
                f"{target.resolve()}",
                "The mountpoint must stay under /kimi-code-home; check for "
                "symlinks in the profile directory.",
            )


# ---------------------------------------------------------------------------
# Kimi config layout discovery (mod_v1 §5.1) — read-only, never writes
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class KimiConfigLayout:
    """Result of a read-only probe of a Kimi home directory (mod_v1 §5.1).

    ``recognized`` is True when a known Kimi layout was detected. The fields
    record which concrete artifacts were found so callers (``doctor`` /
    ``init-integrations``) can report them without guessing or writing anything.
    """

    home: Path
    recognized: bool
    mcp_config: Path | None = None
    skills_dir: Path | None = None
    notes: tuple[str, ...] = field(default_factory=tuple)


# Verified against an installed Kimi Code home: MCP servers live in
# ``mcp.json`` ({"mcpServers": {<name>: {...}}}) and skills under ``skills/``.
_KIMI_MCP_CONFIG_NAMES = ("mcp.json",)
_KIMI_SKILLS_DIRNAME = "skills"


def discover_kimi_config_layout(kimi_code_home: Path) -> KimiConfigLayout:
    """Probe ``kimi_code_home`` for a known Kimi MCP/skill layout (read-only).

    This performs only stat-level inspection: it never reads secret values, and
    never copies or writes anything. When no known artifact is present the
    layout is returned with ``recognized=False`` and an explanatory note, which
    callers should surface as guidance rather than a hard failure (mod_v1 §5.1).

    Recognition deliberately ignores an *empty* ``skills/`` directory: the
    launcher itself pre-creates that path as a bind mountpoint
    (``prepare_profile_mount_targets``), so an empty one proves nothing about a
    real Kimi skill configuration and must not be mistaken for one.
    """
    notes: list[str] = []
    mcp_config: Path | None = None
    for name in _KIMI_MCP_CONFIG_NAMES:
        candidate = kimi_code_home / name
        if candidate.is_file():
            mcp_config = candidate
            break

    skills_dir: Path | None = None
    skills_is_real = False
    candidate_skills = kimi_code_home / _KIMI_SKILLS_DIRNAME
    if candidate_skills.is_dir():
        skills_dir = candidate_skills
        try:
            skills_is_real = any(candidate_skills.iterdir())
        except OSError:
            skills_is_real = False
        if not skills_is_real:
            notes.append(
                f"{candidate_skills} exists but is empty; it is likely just a "
                "bind mountpoint, not a populated Kimi skills directory"
            )

    # A bare empty skills/ mountpoint does not, on its own, constitute a
    # recognized layout (see docstring).
    recognized = mcp_config is not None or skills_is_real
    if not recognized:
        notes.append(
            "no recognized Kimi MCP config (mcp.json) or non-empty skills/ "
            f"directory was found under {kimi_code_home}"
        )
    return KimiConfigLayout(
        home=kimi_code_home,
        recognized=recognized,
        mcp_config=mcp_config,
        skills_dir=skills_dir,
        notes=tuple(notes),
    )


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
    "resolve_conda_root",
    "resolve_conda_existing_env",
    "validate_conda_config",
    "resolve_cache_dir",
    "resolve_profile_ro_mount",
    "validate_profile_ro_mounts",
    "prepare_profile_mount_targets",
    "discover_kimi_config_layout",
    "KimiConfigLayout",
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
