"""Command-line entry point for kimi-sandbox.

Parses arguments, loads an optional config file, resolves and validates host
paths, builds the bubblewrap command (optionally wrapped in ``systemd-run`` for
resource limits and carrying a seccomp filter fd), prints the start banner, and
runs the sandbox. The launcher returns Kimi's own exit code unchanged
(design 27); launcher-level failures print ``error:`` lines and use a distinct
non-zero code.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import time
import tomllib
from pathlib import Path

from . import __version__, seccomp
from .bwrap import build_bwrap_command
from .conda_integration import prepare_conda_generated_files
from .conda_policy import build_conda_envs_path
from .config import (
    CONDA_ALIAS_FD_SUFFIX,
    MODE_READ_ONLY,
    MODE_WORKSPACE_WRITE,
    SANDBOX_CACHE,
    SANDBOX_CONDA_BASH_ENV,
    SANDBOX_CONDA_CACHE_WRITABLE,
    SANDBOX_CONDA_EXISTING_ENVS,
    SANDBOX_CONDA_ROOT,
    SANDBOX_CONDA_SHIM,
    SANDBOX_CONDA_TMP_WRITABLE,
    SANDBOX_CONDARC,
    SANDBOX_HOME,
    SANDBOX_KIMI_BIN,
    SANDBOX_KIMI_CODE_HOME,
    SANDBOX_WORKSPACE,
    CondaConfig,
    ExtraMount,
    GeneratedFileMount,
    ProfileMount,
    ResourceLimits,
    SandboxConfig,
)
from .errors import BubblewrapFailedError, SandboxError
from .paths import (
    default_kimi_code_home,
    discover_kimi_config_layout,
    ensure_dir,
    kimi_is_script,
    prepare_profile_mount_targets,
    resolve_bwrap,
    resolve_cache_dir,
    resolve_conda_existing_env,
    resolve_conda_root,
    resolve_extra_mount,
    resolve_kimi_with_source,
    resolve_profile_ro_mount,
    resolve_project_dir,
    resolve_state_root,
    resolve_systemd_run,
    resolve_unsafe_kimi_code_home,
    validate_conda_config,
    validate_path_relationships,
    validate_profile_ro_mounts,
    validate_resource_limits,
    world_or_group_readable,
)

# Launcher-internal failure exit code. Chosen as 125 (the convention used by
# `env`/`docker` for "the wrapper itself failed, before/around the child")
# rather than 2, which collides with argparse's own usage errors and with the
# very common child exit code 2 — defeating §27's "distinct code" intent.
LAUNCHER_ERROR_EXIT = 125

# Host env vars forwarded verbatim into the sandbox (design 12.4 / 21).
ENV_ALLOWLIST = (
    "TERM",
    "COLORTERM",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "NO_COLOR",
)

# Config-file keys the launcher understands (design v2 §33.3 config file).
# Values here become defaults; matching CLI flags override them.
_CONFIG_BOOL_KEYS = (
    "no_network",
    "read_only",
    "persistent_cache",
    "no_seccomp",
    "compat_kimi_home",
    "conda_enabled",
    "conda_shell_integration",
)
_CONFIG_STR_KEYS = (
    "profile",
    "state_root",
    "memory_max",
    "cpu_quota",
    "conda_root",
    "conda_writable",
)
_CONFIG_INT_KEYS = ("pids_max",)
_CONFIG_LIST_KEYS = (
    "ro_mounts",
    "rw_mounts",
    "profile_ro_mounts",
    "env_keep",
    "conda_existing_envs",
)
_CONFIG_DICT_KEYS = ("env_set",)
_CONFIG_KNOWN_KEYS = frozenset(
    _CONFIG_BOOL_KEYS
    + _CONFIG_STR_KEYS
    + _CONFIG_INT_KEYS
    + _CONFIG_LIST_KEYS
    + _CONFIG_DICT_KEYS
)

# Sandbox env vars the launcher owns; env_keep/env_set may never override these
# (mod_v1 §9). Keeping this list in lockstep with build_env_allowlist's
# launcher-controlled keys is what makes the reserved-key guard meaningful.
_RESERVED_ENV_KEYS = frozenset(
    {
        "HOME",
        "KIMI_CODE_HOME",
        "KIMI_SANDBOX",
        "KIMI_SANDBOX_MODE",
        "KIMI_SANDBOX_WORKSPACE",
        "PATH",
        "TMPDIR",
        "XDG_CACHE_HOME",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
    }
)

# Additional launcher-controlled env vars reserved only when conda is enabled
# (mod_v2 §6). These steer where new envs/packages are created and how the
# conda shell hook is loaded; letting a user override them would defeat the
# read-only/writable separation, so they are rejected in env_keep/env_set.
_CONDA_RESERVED_ENV_KEYS = frozenset(
    {
        "CONDARC",
        "CONDA_ENVS_PATH",
        "CONDA_PKGS_DIRS",
        "CONDA_ALWAYS_COPY",
        "CONDA_AUTO_ACTIVATE_BASE",
        "KIMI_SANDBOX_CONDA_ROOT",
        "KIMI_SANDBOX_CONDA_ORIGINAL_ROOT",
        "KIMI_SANDBOX_CONDA_WRITABLE_ROOT",
    }
)

# Valid POSIX-ish environment variable name (mod_v1 §9).
_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Bounds for doctor's advisory symlink scan of a profile-mount source so a huge
# or pathological skill tree cannot make the check hang (mod_v1 §14 optional).
_SYMLINK_SCAN_MAX_ENTRIES = 5000
_SYMLINK_SCAN_MAX_DEPTH = 8


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kimi-sandbox",
        description="Filesystem sandbox launcher for Kimi Code (bubblewrap).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  kimi-sandbox .\n"
            "  kimi-sandbox . -- --version\n"
            "  kimi-sandbox ~/work/proj --profile work\n"
            "  kimi-sandbox . --read-only --no-network\n"
            "  kimi-sandbox . --memory-max 2G --cpu-quota 150%%\n"
            "  kimi-sandbox . --ro-mount /opt/refdata\n"
            "  kimi-sandbox . --dry-run\n"
            '  kimi-sandbox . --exec "pwd && id"\n'
        ),
    )
    parser.add_argument(
        "project",
        nargs="?",
        default=".",
        help="Host project directory to mount as /workspace (default: cwd).",
    )
    parser.add_argument(
        "--profile",
        default=None,
        help="Sandbox profile name (default: default).",
    )
    parser.add_argument(
        "--state-root",
        default=None,
        help="Host state root (default: ~/.local/state/kimi-sandbox).",
    )
    parser.add_argument(
        "--kimi",
        default=None,
        help="Explicit host path to the kimi executable.",
    )
    parser.add_argument(
        "--bwrap",
        default=None,
        help="Explicit host path to the bwrap executable.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the generated bwrap command and exit (does not run Kimi).",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Print resolved paths and the mount plan before running.",
    )
    parser.add_argument(
        "--exec",
        dest="exec_command",
        metavar="COMMAND",
        default=None,
        help="Run COMMAND (via bash -lc, or /bin/sh -c) inside the sandbox instead of kimi.",
    )
    parser.add_argument(
        "--unsafe-kimi-code-home",
        default=None,
        metavar="PATH",
        help=(
            "Use a custom host path for /kimi-code-home. DANGEROUS: this exposes "
            "the directory to all sandboxed processes. Broad system paths and the "
            "real ~/.kimi-code are still rejected."
        ),
    )
    # --- v2 hardening / engineering flags ---
    # Each boolean has an explicit negator so a value set in the config file can
    # be overridden from the CLI in *both* directions (design v2 §39.11). The
    # primary flag carries default=None; argparse applies only the first-added
    # default for a shared dest, so the negators deliberately omit it (their
    # store_const default is None too). _pick() then treats None as "unset".
    parser.add_argument(
        "--read-only",
        dest="read_only",
        action="store_const",
        const=True,
        default=None,
        help="Mount /workspace read-only (project cannot be modified).",
    )
    parser.add_argument(
        "--writable",
        dest="read_only",
        action="store_const",
        const=False,
        help="Mount /workspace read-write (override read_only from config).",
    )
    parser.add_argument(
        "--no-network",
        dest="no_network",
        action="store_const",
        const=True,
        default=None,
        help="Isolate the network namespace (adds --unshare-net).",
    )
    parser.add_argument(
        "--network",
        dest="no_network",
        action="store_const",
        const=False,
        help="Keep host network access (override no_network from config).",
    )
    parser.add_argument(
        "--no-seccomp",
        dest="no_seccomp",
        action="store_const",
        const=True,
        default=None,
        help="Do not install the TIOCSTI-blocking seccomp filter.",
    )
    parser.add_argument(
        "--seccomp",
        dest="no_seccomp",
        action="store_const",
        const=False,
        help="Install the TIOCSTI seccomp filter (override no_seccomp from config).",
    )
    parser.add_argument(
        "--persistent-cache",
        dest="persistent_cache",
        action="store_const",
        const=True,
        default=None,
        help="Bind a per-profile host cache dir at /cache (XDG_CACHE_HOME).",
    )
    parser.add_argument(
        "--no-persistent-cache",
        dest="persistent_cache",
        action="store_const",
        const=False,
        help="Use an ephemeral cache (override persistent_cache from config).",
    )
    parser.add_argument(
        "--compat-kimi-home",
        dest="compat_kimi_home",
        action="store_const",
        const=True,
        default=None,
        help="Symlink /home/sandbox/.kimi-code -> /kimi-code-home (default on).",
    )
    parser.add_argument(
        "--no-compat-kimi-home",
        dest="compat_kimi_home",
        action="store_const",
        const=False,
        help="Do not create the ~/.kimi-code compat symlink.",
    )
    parser.add_argument(
        "--memory-max",
        default=None,
        metavar="SIZE",
        help="cgroup memory cap via systemd-run, e.g. 2G or 512M.",
    )
    parser.add_argument(
        "--cpu-quota",
        default=None,
        metavar="PCT",
        help="cgroup CPU quota via systemd-run, e.g. 150%% (1.5 cores).",
    )
    parser.add_argument(
        "--pids-max",
        type=int,
        default=None,
        metavar="N",
        help="cgroup process/thread cap via systemd-run (TasksMax).",
    )
    parser.add_argument(
        "--ro-mount",
        dest="ro_mounts",
        action="append",
        default=None,
        metavar="HOST[:TARGET]",
        help="Mount an extra host path read-only (repeatable).",
    )
    parser.add_argument(
        "--rw-mount",
        dest="rw_mounts",
        action="append",
        default=None,
        metavar="HOST[:TARGET]",
        help="Mount an extra host path read-write (repeatable). DANGEROUS.",
    )
    parser.add_argument(
        "--no-pin-mounts",
        dest="no_pin_mounts",
        action="store_true",
        help=(
            "Do not pin host bind sources via --bind-fd. Pinning is on by "
            "default and closes a path-swap (TOCTOU) window; disable only for "
            "bubblewrap older than 0.5 (no --bind-fd)."
        ),
    )
    parser.add_argument(
        "--config",
        default=None,
        metavar="PATH",
        help="Config file (default: ~/.config/kimi-sandbox/config.toml).",
    )
    parser.add_argument(
        "--no-config",
        action="store_true",
        help="Do not read any config file.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"kimi-sandbox {__version__}",
        help="Print launcher version.",
    )
    return parser


# ---------------------------------------------------------------------------
# Config file (design v2 §33.3)
# ---------------------------------------------------------------------------

def _default_config_path() -> Path:
    """Return the default config path honoring XDG_CONFIG_HOME."""
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path(os.path.expanduser("~")) / ".config"
    return base / "kimi-sandbox" / "config.toml"


def load_config_file(path: Path, *, explicit: bool) -> dict:
    """Load and type-check a TOML config file.

    ``explicit`` means the user passed ``--config``: a missing file is then an
    error. The default path being absent is silently ignored. Unknown keys warn
    but do not fail; wrong-typed known keys are a hard error.
    """
    if not path.exists():
        if explicit:
            raise SandboxError(
                f"config file not found: {path}",
                "Check the --config path, or omit it to use the default.",
            )
        return {}
    try:
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise SandboxError(
            f"failed to read config file {path}: {exc}",
            "Fix the TOML syntax or pass --no-config to skip it.",
        ) from exc

    for key in data:
        if key not in _CONFIG_KNOWN_KEYS:
            _eprint(f"warning: ignoring unknown config key: {key!r} ({path})")

    def _require(key: str, ok: bool) -> None:
        if not ok:
            raise SandboxError(
                f"config key {key!r} has the wrong type in {path}",
                "See the README for the config file schema.",
            )

    for key in _CONFIG_BOOL_KEYS:
        if key in data:
            _require(key, isinstance(data[key], bool))
    for key in _CONFIG_STR_KEYS:
        if key in data:
            _require(key, isinstance(data[key], str))
    for key in _CONFIG_INT_KEYS:
        if key in data:
            _require(key, isinstance(data[key], int) and not isinstance(data[key], bool))
    for key in _CONFIG_LIST_KEYS:
        if key in data:
            _require(
                key,
                isinstance(data[key], list)
                and all(isinstance(x, str) for x in data[key]),
            )
    for key in _CONFIG_DICT_KEYS:
        if key in data:
            _require(
                key,
                isinstance(data[key], dict)
                and all(
                    isinstance(k, str) and isinstance(v, str)
                    for k, v in data[key].items()
                ),
            )
    return data


def _pick(cli_value, cfg: dict, key: str, default):
    """CLI value wins if set (not None); else config file; else hardcoded."""
    if cli_value is not None:
        return cli_value
    if key in cfg:
        return cfg[key]
    return default


# ---------------------------------------------------------------------------

def _validate_env_key(
    key: str,
    *,
    origin: str,
    reserved: frozenset[str] | set[str] = _RESERVED_ENV_KEYS,
) -> None:
    """Reject malformed or reserved env keys (mod_v1 §9, mod_v2 §6).

    ``origin`` is "env_keep" or "env_set" for the error message. A bad name
    (empty, containing ``=`` / whitespace, not matching the POSIX-ish pattern)
    or any launcher-reserved key is a hard :class:`SandboxError`, never a silent
    drop. ``reserved`` is the effective reserved set (the base set, plus the
    conda-controlled keys when conda is enabled).
    """
    if not _ENV_KEY_RE.match(key):
        raise SandboxError(
            f"invalid environment variable name in {origin}: {key!r}",
            "Names must match ^[A-Za-z_][A-Za-z0-9_]*$ (no spaces, no '=').",
        )
    if key in reserved:
        raise SandboxError(
            f"{origin} may not override the launcher-reserved variable {key!r}",
            "Reserved keys (HOME, PATH, KIMI_CODE_HOME, XDG_*, conda CONDARC/"
            "CONDA_ENVS_PATH/..., BASH_ENV) are managed by the launcher.",
        )


def build_env_allowlist(
    *,
    mode: str = MODE_WORKSPACE_WRITE,
    persistent_cache: bool = False,
    env_keep: list[str] | tuple[str, ...] = (),
    env_set: dict[str, str] | None = None,
    conda: CondaConfig | None = None,
    extra: dict[str, str] | None = None,
) -> dict[str, str]:
    """Construct the sandbox environment (design 11 / 21; mod_v1 §9, mod_v2 §6).

    Launcher-controlled values are set unconditionally; a short allowlist of
    terminal/locale vars is forwarded from the host when present. Sensitive vars
    (credentials, agents) are never forwarded because we start from --clearenv.

    ``env_keep`` forwards *explicitly named* host variables that actually exist
    (duplicates de-duplicated, declared order preserved; no globbing). ``env_set``
    sets fixed values declared in config. Both reject malformed names and refuse
    to override any launcher-reserved key (see ``_RESERVED_ENV_KEYS`` plus the
    conda-controlled keys when ``conda`` is given).

    When ``conda`` is set, the controlled conda variables (CONDARC, the
    writable-first CONDA_ENVS_PATH / CONDA_PKGS_DIRS, CONDA_ALWAYS_COPY, the
    KIMI_SANDBOX_CONDA_* anchors, and BASH_ENV when shell integration is on) are
    injected here and protected from override (mod_v2 §6).
    """
    cache_home = SANDBOX_CACHE if persistent_cache else f"{SANDBOX_HOME}/.cache"
    env: dict[str, str] = {
        "HOME": SANDBOX_HOME,
        "KIMI_CODE_HOME": SANDBOX_KIMI_CODE_HOME,
        "KIMI_SANDBOX": "1",
        "KIMI_SANDBOX_MODE": mode,
        "KIMI_SANDBOX_WORKSPACE": SANDBOX_WORKSPACE,
        "PATH": "/sandbox/bin:/usr/local/bin:/usr/bin:/bin",
        "TMPDIR": "/tmp",
        "XDG_CACHE_HOME": cache_home,
        "XDG_CONFIG_HOME": f"{SANDBOX_HOME}/.config",
        "XDG_DATA_HOME": f"{SANDBOX_HOME}/.local/share",
    }

    reserved: set[str] = set(_RESERVED_ENV_KEYS)
    if conda is not None:
        reserved |= _CONDA_RESERVED_ENV_KEYS
        writable = conda.writable_root.rstrip("/")
        original = conda.sandbox_original_root.rstrip("/")
        env["CONDARC"] = SANDBOX_CONDARC
        env["CONDA_ENVS_PATH"] = build_conda_envs_path(
            writable, original, SANDBOX_CONDA_EXISTING_ENVS
        )
        env["CONDA_PKGS_DIRS"] = f"{writable}/pkgs"
        env["CONDA_ALWAYS_COPY"] = "1"
        env["CONDA_AUTO_ACTIVATE_BASE"] = "false"
        env["KIMI_SANDBOX_CONDA_ROOT"] = conda.sandbox_root
        env["KIMI_SANDBOX_CONDA_ORIGINAL_ROOT"] = original
        env["KIMI_SANDBOX_CONDA_WRITABLE_ROOT"] = writable
        if conda.shell_integration:
            reserved.add("BASH_ENV")
            env["BASH_ENV"] = SANDBOX_CONDA_BASH_ENV

    for key in ENV_ALLOWLIST:
        val = os.environ.get(key)
        if val is not None:
            env[key] = val

    # env_keep: forward declared host vars (deduped, order preserved).
    seen: set[str] = set()
    for key in env_keep:
        if key in seen:
            continue
        seen.add(key)
        _validate_env_key(key, origin="env_keep", reserved=reserved)
        val = os.environ.get(key)
        if val is not None:
            env[key] = val

    # env_set: fixed values from config.
    if env_set:
        for key, val in env_set.items():
            _validate_env_key(key, origin="env_set", reserved=reserved)
            env[key] = val

    if extra:
        env.update(extra)
    return env


def _inner_shell() -> tuple[str, str]:
    """Pick the shell used by ``--exec`` inside the sandbox.

    The sandbox ro-binds the host system trees, so the shells available inside
    match the host's. Prefer bash (with a login shell, ``-lc``); fall back to
    POSIX ``/bin/sh`` with ``-c`` on hosts that ship only dash/sh (C1). Returns
    ``(shell_path, flags)``.

    Note: ``-lc`` requests a *login* shell, but inside the sandbox this sources
    effectively nothing — ``/etc`` is a minimal read-only tmpfs without
    ``/etc/profile``, and ``HOME`` is an empty tmpfs without ``~/.profile`` —
    so it does not pull in or override the launcher-controlled environment.
    """
    for bash in ("/bin/bash", "/usr/bin/bash"):
        if os.path.exists(bash):
            return bash, "-lc"
    return "/bin/sh", "-c"


def build_inner_command(args: argparse.Namespace, kimi_args: list[str]) -> list[str]:
    """Decide what runs inside the sandbox (design 17)."""
    if args.exec_command is not None:
        shell, flags = _inner_shell()
        return [shell, flags, args.exec_command]
    return [SANDBOX_KIMI_BIN, *kimi_args]


def split_kimi_args(argv: list[str]) -> tuple[list[str], list[str]]:
    """Split argv at the first ``--`` into (launcher args, kimi passthrough)."""
    if "--" in argv:
        idx = argv.index("--")
        return argv[:idx], argv[idx + 1 :]
    return argv, []


def build_systemd_run_prefix(
    systemd_run: Path, limits: ResourceLimits
) -> list[str]:
    """Build the ``systemd-run --user --scope`` prefix for resource limits."""
    prefix = [str(systemd_run), "--user", "--scope", "--quiet"]
    if limits.memory_max is not None:
        prefix += ["-p", f"MemoryMax={limits.memory_max}"]
    if limits.cpu_quota is not None:
        prefix += ["-p", f"CPUQuota={limits.cpu_quota}"]
    if limits.pids_max is not None:
        prefix += ["-p", f"TasksMax={limits.pids_max}"]
    prefix += ["--"]
    return prefix


def _eprint(msg: str = "") -> None:
    print(msg, file=sys.stderr)


def print_mount_plan(
    config: SandboxConfig,
    *,
    kimi_path: Path,
    seccomp_active: bool,
    limits: ResourceLimits,
    systemd_run: Path | None,
    pin_mounts: bool = False,
) -> None:
    """Print resolved paths and a human-readable mount plan (design 26).

    Mirrors exactly what will run: the seccomp filter, the resource limits, and
    the ``systemd-run`` wrapper are all surfaced here so ``--debug`` does not
    diverge from the start banner / ``--dry-run`` (R2).
    """
    ws_access = "ro" if config.mode == MODE_READ_ONLY else "rw"
    _eprint("kimi-sandbox: mount plan")
    _eprint(f"  project   : {config.project_dir} -> {SANDBOX_WORKSPACE} ({ws_access})")
    _eprint(f"  kimi home : {config.kimi_code_home} -> {SANDBOX_KIMI_CODE_HOME} (rw)")
    for mount in config.profile_ro_mounts:
        _eprint(
            f"  profile ro: {mount.source} -> "
            f"{SANDBOX_KIMI_CODE_HOME}/{mount.relative_target} (ro)"
        )
    if config.compat_kimi_home:
        _eprint(f"  compat    : {SANDBOX_HOME}/.kimi-code -> {SANDBOX_KIMI_CODE_HOME}")
    _eprint(f"  kimi bin  : {kimi_path} -> {SANDBOX_KIMI_BIN} (ro)")
    if config.cache_dir is not None:
        _eprint(f"  cache     : {config.cache_dir} -> {SANDBOX_CACHE} (rw, persistent)")
    if config.conda is not None:
        _eprint(
            f"  conda root: {config.conda.root} -> {SANDBOX_CONDA_ROOT} (ro) "
            f"+ {config.conda.sandbox_original_root} (ro, compat)"
        )
        _eprint(
            f"  conda rw  : {config.conda.writable_root}/envs, "
            f"{config.conda.writable_root}/pkgs"
        )
        _eprint(f"  conda shim: {SANDBOX_CONDA_SHIM} (ro)")
        for env in config.conda.existing_envs:
            _eprint(
                f"  conda env : {env.source} -> "
                f"{SANDBOX_CONDA_EXISTING_ENVS}/{env.name} (ro)"
            )
    for mount in config.extra_mounts:
        acc = "rw" if mount.writable else "ro"
        _eprint(f"  extra     : {mount.source} -> {mount.target} ({acc})")
    if config.state_root is not None:
        _eprint(f"  state root: {config.state_root}")
    _eprint(f"  profile   : {config.profile}")
    _eprint("  home      : isolated tmpfs (/home/sandbox)")
    _eprint("  tmp       : isolated tmpfs")
    _eprint("  /etc      : minimal read-only (DNS/TLS only)")
    _eprint(f"  network   : {'isolated' if config.no_network else 'enabled'}")
    _eprint(f"  seccomp   : {'TIOCSTI filter active' if seccomp_active else 'off'}")
    _eprint(f"  pin mounts: {'on (--bind-fd)' if pin_mounts else 'off'}")
    if not limits.is_empty():
        parts = []
        if limits.memory_max is not None:
            parts.append(f"mem={limits.memory_max}")
        if limits.cpu_quota is not None:
            parts.append(f"cpu={limits.cpu_quota}")
        if limits.pids_max is not None:
            parts.append(f"pids={limits.pids_max}")
        wrapper = f" via {systemd_run}" if systemd_run is not None else ""
        _eprint(f"  limits    : {', '.join(parts)} (systemd-run{wrapper})")
    else:
        _eprint("  limits    : none")
    _eprint(f"  env keys  : {', '.join(sorted(config.env))}")
    _eprint(f"  command   : {shlex.join(config.inner_command)}")
    _eprint("")


def print_start_banner(
    config: SandboxConfig,
    *,
    seccomp_active: bool,
    limits: ResourceLimits,
    pin_mounts: bool = False,
) -> None:
    """Print the start banner (design 5.1). Never prints secrets.

    Written to stderr so it never pollutes stdout — important for ``--exec``
    output and for piping the launcher's output.
    """
    print("Kimi Sandbox active", file=sys.stderr)
    print(f"  mode: {config.mode}", file=sys.stderr)
    ws_access = "ro" if config.mode == MODE_READ_ONLY else "rw"
    print(
        f"  project: {config.project_dir} -> {SANDBOX_WORKSPACE} ({ws_access})",
        file=sys.stderr,
    )
    print(
        f"  kimi home: {config.kimi_code_home} -> {SANDBOX_KIMI_CODE_HOME} (rw)",
        file=sys.stderr,
    )
    if config.cache_dir is not None:
        print(f"  cache: persistent ({SANDBOX_CACHE})", file=sys.stderr)
    if config.conda is not None:
        integ = "on" if config.conda.shell_integration else "off"
        print(
            f"  conda: {config.conda.root} (ro) -> writable "
            f"{config.conda.writable_root}; shell-integration {integ}",
            file=sys.stderr,
        )
    for mount in config.profile_ro_mounts:
        print(
            f"  skill/profile ro: {SANDBOX_KIMI_CODE_HOME}/{mount.relative_target}",
            file=sys.stderr,
        )
    if config.compat_kimi_home:
        print(
            f"  compat: {SANDBOX_HOME}/.kimi-code -> {SANDBOX_KIMI_CODE_HOME}",
            file=sys.stderr,
        )
    for mount in config.extra_mounts:
        acc = "rw" if mount.writable else "ro"
        print(f"  extra mount: {mount.source} -> {mount.target} ({acc})", file=sys.stderr)
    print("  home: isolated tmpfs", file=sys.stderr)
    print("  tmp: isolated tmpfs", file=sys.stderr)
    print(f"  network: {'isolated' if config.no_network else 'enabled'}", file=sys.stderr)
    print(f"  seccomp: {'TIOCSTI filter active' if seccomp_active else 'off'}", file=sys.stderr)
    print(f"  pin mounts: {'on' if pin_mounts else 'off'}", file=sys.stderr)
    if not limits.is_empty():
        parts = []
        if limits.memory_max is not None:
            parts.append(f"mem={limits.memory_max}")
        if limits.cpu_quota is not None:
            parts.append(f"cpu={limits.cpu_quota}")
        if limits.pids_max is not None:
            parts.append(f"pids={limits.pids_max}")
        print(f"  limits: {', '.join(parts)} (systemd-run)", file=sys.stderr)
    if config.unsafe_kimi_code_home:
        print("  (unsafe-kimi-code-home active; see warning above)", file=sys.stderr)


def open_bind_fds(config: SandboxConfig) -> dict[str, int]:
    """Open ``O_PATH`` fds for every host bind source, for fd-pinned mounts.

    Returns a mapping ``{source_path_str: fd}`` covering the project dir, the
    profile kimi-code-home, the persistent cache (if any), the kimi binary, and
    every extra-mount source. Each fd is marked inheritable so it can be passed
    to ``bwrap`` via ``subprocess.run(pass_fds=...)`` and used with
    ``--bind-fd`` / ``--ro-bind-fd``. The caller must close the fds after the
    run. Duplicate source paths share one fd.

    ``O_PATH`` pins the already-resolved, symlink-free inode without needing
    read permission, so the only residual race is the single ``open()`` walk
    itself (rather than the whole window until bwrap mounts).
    """
    sources: list[str] = [
        str(config.project_dir),
        str(config.kimi_code_home),
        str(config.kimi_path),
    ]
    if config.cache_dir is not None:
        sources.append(str(config.cache_dir))
    for mount in config.extra_mounts:
        sources.append(str(mount.source))
    for mount in config.profile_ro_mounts:
        sources.append(str(mount.source))
    # Controlled-conda sources (mod_v2 §7 fd pinning): the read-only host conda
    # root, any extra existing envs, and every launcher-generated helper file.
    # The conda root / existing envs are each bound twice (canonical + original
    # path); the alias bind needs its own fd (bwrap closes a bind fd after use),
    # pinned under a NUL-suffixed synthetic key (audit #6).
    alias_sources: list[str] = []
    if config.conda is not None:
        sources.append(str(config.conda.root))
        alias_sources.append(str(config.conda.root))
        for env in config.conda.existing_envs:
            sources.append(str(env.source))
            alias_sources.append(str(env.source))
    for mount in config.generated_file_mounts:
        sources.append(str(mount.source))

    fds: dict[str, int] = {}
    current = ""
    try:
        for src in sources:
            if src in fds:
                continue
            current = src
            fd = os.open(src, os.O_PATH)
            os.set_inheritable(fd, True)
            fds[src] = fd
        # Distinct second fd for each twice-bound conda source (alias bind).
        for src in alias_sources:
            key = src + CONDA_ALIAS_FD_SUFFIX
            if key in fds:
                continue
            current = src
            fd = os.open(src, os.O_PATH)
            os.set_inheritable(fd, True)
            fds[key] = fd
    except OSError as exc:
        for fd in fds.values():
            os.close(fd)
        raise SandboxError(
            f"failed to pin bind source {current!r}: {exc}",
            "Retry, or pass --no-pin-mounts to disable mount pinning.",
        ) from exc
    return fds


def _run(command: list[str], *, pass_fds: tuple[int, ...] = ()) -> int:
    """Run the sandbox, inheriting stdio, and return the child's exit code."""
    try:
        completed = subprocess.run(command, pass_fds=pass_fds)  # noqa: S603
    except FileNotFoundError as exc:
        raise BubblewrapFailedError(
            f"failed to execute bubblewrap: {exc}",
            "Is bubblewrap installed and on PATH?",
        ) from exc
    except KeyboardInterrupt:
        # Mirror the conventional 128+SIGINT exit code; child already saw it.
        return 130
    return completed.returncode


# ---------------------------------------------------------------------------
# Integration-config hint (mod_v1 §5/§8) and subcommands (§13/§14)
# ---------------------------------------------------------------------------

def _host_integration_clues() -> list[str]:
    """Return human-readable host clues that MCP/skill integration is intended.

    Currently just the presence of a real ``~/.kimi-code/skills`` directory.
    Used to decide whether to surface the (otherwise silent) "integration not
    configured" hint when the default config is absent.
    """
    clues: list[str] = []
    skills = Path(os.path.expanduser("~")) / ".kimi-code" / "skills"
    if skills.is_dir():
        clues.append(str(skills))
    return clues


_HINTED_MISSING_INTEGRATIONS = False


# Conda root candidates probed by init-integrations (mod_v2 §14), in order.
_CONDA_ROOT_CANDIDATES = (
    "~/anaconda3",
    "~/miniconda3",
    "~/miniforge3",
    "~/mambaforge",
)


def detect_conda_root() -> str | None:
    """Return the first host conda-root candidate that has ``bin/conda``.

    Read-only probing only; returns the ``~``-relative spec string (e.g.
    ``"~/anaconda3"``) so it can be written verbatim into a config suggestion.
    """
    for candidate in _CONDA_ROOT_CANDIDATES:
        conda_exe = Path(os.path.expanduser(candidate)) / "bin" / "conda"
        if conda_exe.is_file() and os.access(conda_exe, os.X_OK):
            return candidate
    return None


def _maybe_hint_missing_integrations(*, debug: bool) -> None:
    """Print at most one restrained hint about unconfigured MCP/skill support.

    Fires only under ``--debug`` or when a host integration clue is detected
    (mod_v1 §5). Says only that integration is not yet configured and points at
    the init path; it never nags repeatedly and never blocks a plain run.
    """
    global _HINTED_MISSING_INTEGRATIONS
    if _HINTED_MISSING_INTEGRATIONS:
        return
    clues = _host_integration_clues()
    if not debug and not clues:
        return
    _HINTED_MISSING_INTEGRATIONS = True
    _eprint(
        "note: MCP/skill integration is not configured "
        f"(no {_default_config_path()})."
    )
    if clues:
        _eprint(f"  detected host integration clue(s): {', '.join(clues)}")
    _eprint(
        "  run 'kimi-sandbox init-integrations' to scaffold a config, "
        "or see the README 'MCP and Skills' section."
    )


def _doctor_status(ok: bool, *, warn: bool = False) -> str:
    if warn:
        return "WARN"
    return "OK  " if ok else "FAIL"


def _cmd_doctor(argv: list[str]) -> int:
    """`kimi-sandbox doctor --config-check`: validate config + mount plan (§14).

    Read-only and launcher-only: it never starts bubblewrap. Hard problems
    (unparseable config, missing/invalid mount sources) fail with
    ``LAUNCHER_ERROR_EXIT``; advisory issues print WARN and do not fail.
    """
    p = argparse.ArgumentParser(
        prog="kimi-sandbox doctor",
        description="Validate the kimi-sandbox config and mount plan.",
    )
    p.add_argument("--config", default=None, metavar="PATH")
    p.add_argument("--profile", default=None)
    p.add_argument("--state-root", default=None)
    p.add_argument(
        "--config-check",
        action="store_true",
        help="Validate config and dry-run mount plan (default check in v1).",
    )
    p.add_argument(
        "--deep",
        action="store_true",
        help=(
            "Scan profile-mount sources for symlinks into writable areas "
            "exhaustively (no entry/depth cap). Slower on large skill trees."
        ),
    )
    args = p.parse_args(argv)

    cfg_path = (
        Path(os.path.expanduser(args.config)) if args.config else _default_config_path()
    )
    fails = 0
    warns = 0

    def line(ok: bool, msg: str, *, warn: bool = False) -> None:
        nonlocal fails, warns
        if warn:
            warns += 1
        elif not ok:
            fails += 1
        _eprint(f"  [{_doctor_status(ok, warn=warn)}] {msg}")

    _eprint(f"kimi-sandbox doctor: config-check ({cfg_path})")

    if not cfg_path.exists():
        line(True, f"default config not found ({cfg_path}); nothing to validate",
             warn=True)
        _eprint("")
        _eprint("  hint: run 'kimi-sandbox init-integrations' to scaffold one.")
        return 0

    # 1) config parses.
    try:
        cfg = load_config_file(cfg_path, explicit=True)
        line(True, "config parses")
    except SandboxError as exc:
        line(False, f"config parse error: {exc.message}")
        return LAUNCHER_ERROR_EXIT

    profile = _pick(args.profile, cfg, "profile", "default")
    state_root = resolve_state_root(_pick(args.state_root, cfg, "state_root", None))
    kimi_code_home = default_kimi_code_home(state_root, profile)
    line(kimi_code_home.exists(),
         f"profile kimi-code-home {kimi_code_home} "
         f"{'exists' if kimi_code_home.exists() else 'will be created on first run'}",
         warn=not kimi_code_home.exists())

    # 2) profile_ro_mounts source existence + dir + mountpoint/shadow.
    profile_mounts: list[ProfileMount] = []
    for spec in cfg.get("profile_ro_mounts", []):
        try:
            m = resolve_profile_ro_mount(spec)
            profile_mounts.append(m)
            line(True, f"profile_ro_mount source ok: {m.source} -> "
                       f"{SANDBOX_KIMI_CODE_HOME}/{m.relative_target}")
            # symlink-to-writable scan inside the source (recommended check).
            _scan_source_symlinks(m.source, line, deep=args.deep)
        except SandboxError as exc:
            line(False, f"profile_ro_mount invalid ({spec!r}): {exc.message}")
    try:
        validate_profile_ro_mounts(profile_mounts)
        if profile_mounts:
            line(True, "profile_ro_mounts targets are unique")
    except SandboxError as exc:
        line(False, exc.message)

    # mountpoint state in the profile.
    for m in profile_mounts:
        target = kimi_code_home / m.relative_target
        if target.is_symlink():
            line(False, f"profile mountpoint is a symlink: {target}")
        elif target.exists() and not target.is_dir():
            line(False, f"profile mountpoint exists but is not a directory: {target}")
        elif target.is_dir():
            try:
                non_empty = any(target.iterdir())
            except OSError:
                non_empty = False
            if non_empty:
                line(True, f"profile mountpoint {target} is non-empty "
                           "(will be shadowed read-only)", warn=True)
            else:
                line(True, f"profile mountpoint ready: {target}")

    # 3) ro_mounts source existence.
    extra: list[ExtraMount] = []
    for spec in cfg.get("ro_mounts", []):
        try:
            mm = resolve_extra_mount(spec, writable=False)
            extra.append(mm)
            line(True, f"ro_mount source ok: {mm.source} -> {mm.target}")
            _warn_runtime_mount_cache(mm, cfg, line)
        except SandboxError as exc:
            line(False, f"ro_mount invalid ({spec!r}): {exc.message}")

    # 3b) rw_mounts: validate sources and warn that they are writable. The
    # integration design is read-only, so a writable mount is always worth a
    # heads-up (it is not a failure — the user may have a deliberate reason).
    rw_specs = list(cfg.get("rw_mounts", []))
    rw_targets: list[str] = []
    for spec in rw_specs:
        try:
            mm = resolve_extra_mount(spec, writable=True)
            rw_targets.append(mm.target)
            line(True, f"rw_mount (WRITABLE) source ok: {mm.source} -> "
                       f"{mm.target}; only mount disposable/non-sensitive paths",
                 warn=True)
        except SandboxError as exc:
            line(False, f"rw_mount invalid ({spec!r}): {exc.message}")

    # 4) env_keep host vars exist.
    for key in cfg.get("env_keep", []):
        present = os.environ.get(key) is not None
        line(present, f"env_keep {key} "
                      f"{'present in host env' if present else 'NOT set on host'}",
             warn=not present)

    # 5) env_set reserved-key / validity (build_env_allowlist enforces).
    try:
        build_env_allowlist(env_set=dict(cfg.get("env_set", {})),
                            env_keep=list(cfg.get("env_keep", [])))
        line(True, "env_keep/env_set names valid, no reserved-key overrides")
    except SandboxError as exc:
        line(False, f"env config rejected: {exc.message}")

    # 6) Kimi MCP/skill layout recognition (advisory).
    layout = discover_kimi_config_layout(kimi_code_home)
    if layout.recognized:
        bits = []
        if layout.mcp_config:
            bits.append(f"mcp config: {layout.mcp_config.name}")
        if layout.skills_dir:
            bits.append("skills/ dir")
        line(True, "Kimi config layout recognized (" + ", ".join(bits) + ")")
        if layout.mcp_config is not None:
            # Judge MCP command paths against the *actual* configured mount
            # targets, not a bare /home heuristic, so an MCP legitimately
            # mounted under e.g. /home/... in the sandbox is not mis-flagged.
            # When conda is enabled the host conda root is also bound at its
            # original absolute path, so MCP commands using that path (e.g. a
            # conda env interpreter) are legitimately reachable too.
            covered = [m.target for m in extra] + rw_targets
            if cfg.get("conda_enabled", False) and cfg.get("conda_root"):
                try:
                    covered.append(str(resolve_conda_root(cfg["conda_root"])))
                except SandboxError:
                    pass
            _check_mcp_uses_sandbox_paths(
                layout.mcp_config,
                covered,
                line,
            )
    else:
        line(True, "Kimi MCP/skill config layout not recognized; configure "
                   "MCP/skills manually (see README)", warn=True)
        for note in layout.notes:
            _eprint(f"         - {note}")

    # 6b) controlled conda integration checks (mod_v2 §15).
    _doctor_conda(
        cfg,
        line,
        state_root=state_root,
        profile=profile,
        kimi_code_home=kimi_code_home,
        mcp_config=layout.mcp_config,
    )

    # 7) dry-run bwrap argv contains the expected profile ro mounts.
    if profile_mounts:
        # Explicit, clearly-labelled placeholders: doctor never launches bwrap
        # and the assertion below only inspects the profile-mount targets, so
        # these paths are never resolved or validated. Using sentinels (rather
        # than reusing state_root / kimi_code_home) keeps that intent obvious.
        doctor_placeholder = Path("/doctor-config-check-placeholder")
        plan_cfg = SandboxConfig(
            project_dir=doctor_placeholder,
            kimi_code_home=kimi_code_home,
            kimi_path=doctor_placeholder / "kimi",
            inner_command=[SANDBOX_KIMI_BIN],
            env=build_env_allowlist(),
            profile=profile,
            profile_ro_mounts=tuple(profile_mounts),
            compat_kimi_home=bool(_pick(None, cfg, "compat_kimi_home", True)),
        )
        command = build_bwrap_command(plan_cfg, bwrap_path="bwrap")
        all_present = all(
            f"{SANDBOX_KIMI_CODE_HOME}/{m.relative_target}" in command
            for m in profile_mounts
        )
        line(all_present, "dry-run mount plan includes all profile ro mounts")

    _eprint("")
    _eprint(f"doctor: {fails} failed, {warns} warning(s)")
    return LAUNCHER_ERROR_EXIT if fails else 0


def _scan_source_symlinks(source: Path, line, *, deep: bool = False) -> None:
    """Warn if a profile-mount source has symlinks into writable sandbox areas.

    A skill dir may legitimately use symlinks (the host's ~/.kimi-code/skills
    often does). We only flag links whose target *resolves into* a writable
    in-sandbox tree name, which could let a read-only mount be written through.
    This is best-effort and advisory.

    By default the walk is bounded: it does not descend past
    ``_SYMLINK_SCAN_MAX_DEPTH`` and stops after visiting
    ``_SYMLINK_SCAN_MAX_ENTRIES`` entries, so a huge or pathological skill tree
    cannot make ``doctor`` hang. ``deep=True`` (``doctor --deep``) lifts both
    caps for an exhaustive scan. It never follows directory symlinks
    (``os.walk(followlinks=False)``), avoiding cycles. When a bounded scan
    exhausts its budget a single WARN notes that it was truncated.
    """
    writable_names = ("/workspace", "/cache", "/tmp", "/kimi-code-home")
    max_entries = float("inf") if deep else _SYMLINK_SCAN_MAX_ENTRIES
    max_depth = float("inf") if deep else _SYMLINK_SCAN_MAX_DEPTH
    visited = 0
    truncated = False
    base_depth = len(source.parts)
    for root, dirs, files in os.walk(source, followlinks=False):
        depth = len(Path(root).parts) - base_depth
        if depth >= max_depth:
            # Do not descend further; clear dirs to prune the walk.
            dirs[:] = []
        for name in list(dirs) + files:
            visited += 1
            if visited > max_entries:
                truncated = True
                break
            entry = Path(root) / name
            if entry.is_symlink():
                try:
                    target = os.readlink(entry)
                except OSError:
                    continue
                if any(target.startswith(w) for w in writable_names):
                    line(True, f"symlink in {source} points at writable area: "
                               f"{entry} -> {target}", warn=True)
        if truncated:
            break
    if truncated:
        line(True, f"symlink scan of {source} truncated at "
                   f"{_SYMLINK_SCAN_MAX_ENTRIES} entries / depth "
                   f"{_SYMLINK_SCAN_MAX_DEPTH}; re-run with 'doctor --deep' or "
                   "review large skill trees by hand",
             warn=True)


def _warn_runtime_mount_cache(mount: ExtraMount, cfg: dict, line) -> None:
    """Advisory: runtime mounts may try to write caches into read-only source."""
    target = mount.target
    looks_runtime = "/opt/kimi-runtime" in target or "runtime" in target
    if not looks_runtime:
        return
    env_set = cfg.get("env_set", {})
    has_cache_redirect = any(
        k in env_set
        for k in ("PYTHONDONTWRITEBYTECODE", "PIP_CACHE_DIR", "HF_HOME",
                  "NPM_CONFIG_CACHE", "UV_CACHE_DIR")
    )
    if not has_cache_redirect:
        line(True, f"runtime mount {target} is read-only; consider redirecting "
                   "caches (PYTHONDONTWRITEBYTECODE=1, PIP_CACHE_DIR=/cache/..., "
                   "etc.) via [env_set]", warn=True)


def _check_mcp_uses_sandbox_paths(
    mcp_config: Path, mount_targets: list[str], line
) -> None:
    """Advisory: warn if the Kimi MCP config references host (not sandbox) paths.

    A token is treated as a *host* path only when it looks home-rooted
    (``~``-real-home or ``/home/...``) **and** is not covered by any configured
    in-sandbox mount target (``mount_targets`` = resolved ``ro_mounts`` +
    ``rw_mounts`` targets) nor by a launcher-owned sandbox tree. This avoids
    mis-flagging an MCP that is legitimately mounted under e.g.
    ``/home/<x>`` inside the sandbox, instead of the previous bare-``/home``
    heuristic.
    """
    try:
        data = json.loads(mcp_config.read_text())
    except (OSError, ValueError):
        line(True, f"could not parse {mcp_config} for path check", warn=True)
        return
    real_home = os.path.expanduser("~")
    # In-sandbox prefixes that are legitimately present: configured mount
    # targets plus the launcher-owned trees. Empty/relative targets are ignored.
    allowed = [t for t in mount_targets if t.startswith("/")] + [
        "/opt", "/cache", "/tmp", "/kimi-code-home", "/workspace",
        "/sandbox", "/usr", "/bin", "/sbin", "/lib", "/lib64", "/etc",
    ]

    def _covered(token: str) -> bool:
        return any(token == p or token.startswith(p.rstrip("/") + "/") for p in allowed)

    servers = data.get("mcpServers", {}) if isinstance(data, dict) else {}
    flagged = []
    for name, spec in servers.items():
        if not isinstance(spec, dict):
            continue
        tokens = []
        if isinstance(spec.get("command"), str):
            tokens.append(spec["command"])
        if isinstance(spec.get("args"), list):
            tokens.extend(t for t in spec["args"] if isinstance(t, str))
        host_like = [
            t for t in tokens
            if (t.startswith(real_home) or t.startswith("/home/")) and not _covered(t)
        ]
        if host_like:
            flagged.append(name)
    if flagged:
        line(True, "MCP server(s) reference host paths not covered by any mount "
                   f"target (use /opt/... or a configured mount): {', '.join(flagged)}",
             warn=True)
    else:
        line(True, "MCP server commands use sandbox/mounted paths")


def _check_mcp_conda(mcp_config: Path, line, *, shell_integration: bool) -> None:
    """Advisory: warn about conda anti-patterns in the Kimi MCP config (§15).

    Flags servers that invoke a *host* conda path (should be ``conda`` /
    ``/sandbox/bin/conda``), use ``sh -c "conda activate ..."`` (needs bash), or
    rely on ``conda activate`` while shell integration is disabled.
    """
    try:
        data = json.loads(mcp_config.read_text())
    except (OSError, ValueError):
        return
    servers = data.get("mcpServers", {}) if isinstance(data, dict) else {}
    host_conda: list[str] = []
    sh_activate: list[str] = []
    activate_without_integration: list[str] = []
    host_markers = ("anaconda3", "miniconda", "miniforge", "mambaforge")
    # Non-bash POSIX shells do not load BASH_ENV, so `conda activate` will not be
    # set up under them. Match by basename so /bin/sh, /usr/bin/dash, etc. are
    # all caught (audit #12).
    non_bash_shells = {"sh", "dash", "ash", "busybox"}
    for name, spec in servers.items():
        if not isinstance(spec, dict):
            continue
        command = spec.get("command") if isinstance(spec.get("command"), str) else ""
        tokens = [command] if command else []
        if isinstance(spec.get("args"), list):
            tokens.extend(t for t in spec["args"] if isinstance(t, str))
        joined = " ".join(tokens)
        if any(
            t.endswith("/bin/conda") and any(m in t for m in host_markers)
            for t in tokens
        ):
            host_conda.append(name)
        if "conda activate" in joined:
            if any(os.path.basename(t) in non_bash_shells for t in tokens):
                sh_activate.append(name)
            if not shell_integration:
                activate_without_integration.append(name)
    if host_conda:
        line(True, "MCP server(s) call a host conda path; use 'conda' or "
                   f"/sandbox/bin/conda: {', '.join(host_conda)}", warn=True)
    if sh_activate:
        line(True, "MCP server(s) use sh -c \"conda activate\"; prefer bash -lc "
                   f"or 'conda run': {', '.join(sh_activate)}", warn=True)
    if activate_without_integration:
        line(True, "MCP server(s) use 'conda activate' but "
                   "conda_shell_integration is false: "
                   f"{', '.join(activate_without_integration)}", warn=True)


def _doctor_conda(
    cfg: dict,
    line,
    *,
    state_root: Path,
    profile: str,
    kimi_code_home: Path,
    mcp_config: Path | None,
) -> None:
    """Validate the controlled-conda config and surface advisories (mod_v2 §15)."""
    if not cfg.get("conda_enabled", False):
        return

    persistent_cache = bool(cfg.get("persistent_cache", False))
    writable = cfg.get("conda_writable", "cache")
    if writable not in ("cache", "tmp"):
        line(False, f"conda_writable invalid: {writable!r} (use 'cache' or 'tmp')")
        return
    line(True, f"conda enabled (writable={writable})")
    if writable == "tmp":
        line(True, "conda_writable=tmp: new envs/packages are NOT persistent",
             warn=True)
    if writable == "cache" and not persistent_cache:
        line(False, "conda_writable='cache' requires persistent_cache=true")

    root_spec = cfg.get("conda_root")
    if not root_spec:
        line(False, "conda_enabled is true but conda_root is not set")
        return
    try:
        root = resolve_conda_root(root_spec)
        line(True, f"conda_root exists: {root}")
    except SandboxError as exc:
        line(False, f"conda_root invalid: {exc.message}")
        return

    conda_exe = root / "bin" / "conda"
    ok_exe = conda_exe.is_file() and os.access(conda_exe, os.X_OK)
    line(ok_exe, f"conda_root/bin/conda "
                 f"{'executable' if ok_exe else 'MISSING / not executable'}: "
                 f"{conda_exe}")

    writable_root = (
        SANDBOX_CONDA_CACHE_WRITABLE if writable == "cache"
        else SANDBOX_CONDA_TMP_WRITABLE
    )
    existing_envs: list = []
    for spec in cfg.get("conda_existing_envs", []):
        try:
            existing_envs.append(resolve_conda_existing_env(spec))
            line(True, f"conda_existing_env source ok: {spec}")
        except SandboxError as exc:
            line(False, f"conda_existing_envs invalid ({spec!r}): {exc.message}")

    conda = CondaConfig(
        root=root,
        sandbox_original_root=str(root),
        writable_root=writable_root,
        shell_integration=bool(cfg.get("conda_shell_integration", True)),
        existing_envs=tuple(existing_envs),
    )
    cache_dir = resolve_cache_dir(state_root, profile) if persistent_cache else None
    try:
        validate_conda_config(
            conda,
            persistent_cache=persistent_cache,
            project_dir=Path("/doctor-config-check-placeholder"),
            state_root=state_root,
            kimi_code_home=kimi_code_home,
            cache_dir=cache_dir,
        )
        line(True, "conda config validates (read-only host root, compat target "
                   "safe, writable/cache coupling ok)")
    except SandboxError as exc:
        line(False, f"conda config rejected: {exc.message}")
        return

    line(True, f"conda root mounts read-only at {SANDBOX_CONDA_ROOT} + "
               f"original-prefix compat bind {conda.sandbox_original_root}")
    line(True, f"conda writable envs/pkgs dirs: {writable_root}/envs, "
               f"{writable_root}/pkgs")
    line(True, "CONDARC / CONDA_ENVS_PATH / CONDA_PKGS_DIRS set by launcher; "
               f"{SANDBOX_CONDA_SHIM} is first on PATH")
    line(True, f"conda clean scope: writable package cache only "
               f"({writable_root}/pkgs)")
    integ = conda.shell_integration
    line(True, f"conda shell integration: BASH_ENV "
               f"{'enabled' if integ else 'disabled'}")

    try:
        build_env_allowlist(
            conda=conda,
            env_set=dict(cfg.get("env_set", {})),
            env_keep=list(cfg.get("env_keep", [])),
        )
        line(True, "env_keep/env_set do not override conda reserved keys")
    except SandboxError as exc:
        line(False, f"env config conflicts with conda reserved keys: {exc.message}")

    if cfg.get("no_network", False):
        line(True, "no_network=true: conda create/install can only use already "
                   "available local channels/cache", warn=True)

    if mcp_config is not None:
        _check_mcp_conda(mcp_config, line, shell_integration=integ)


def _cmd_init_integrations(argv: list[str]) -> int:
    """`kimi-sandbox init-integrations`: scaffold the default config (§13).

    Default behaviour is dry-run: it prints a suggested config (or, for an
    existing config, the additions it would make) without writing. ``--write``
    creates a missing config, or for an existing config appends only entirely
    absent top-level keys after taking a timestamped backup. It never rewrites
    user content, and never touches the Kimi MCP config.

    Scope note (mod_v1 §13): when a *list* key (e.g. ``profile_ro_mounts``)
    already exists but is missing a suggested item, the tool does **not** edit
    the existing array — appending a second ``key = [...]`` would be invalid
    TOML, and an in-place, comment-preserving array merge is deliberately
    deferred to v2. Instead it prints the exact item(s) to add by hand. This
    matches the "no unsafe merge; print a manual snippet" rule.
    """
    p = argparse.ArgumentParser(
        prog="kimi-sandbox init-integrations",
        description="Scaffold ~/.config/kimi-sandbox/config.toml for MCP/skills.",
    )
    p.add_argument("--config", default=None, metavar="PATH")
    p.add_argument(
        "--write",
        action="store_true",
        help="Write the suggested config (creates a backup of any existing file).",
    )
    args = p.parse_args(argv)

    cfg_path = (
        Path(os.path.expanduser(args.config)) if args.config else _default_config_path()
    )

    # Build suggestions from host clues (read-only probing only).
    skills = Path(os.path.expanduser("~")) / ".kimi-code" / "skills"
    suggested_profile_ro: list[str] = []
    if skills.is_dir():
        suggested_profile_ro.append("~/.kimi-code/skills:skills")

    conda_root = detect_conda_root()
    template = _render_init_template(suggested_profile_ro, conda_root=conda_root)

    if not cfg_path.exists():
        if args.write:
            ensure_dir(cfg_path.parent)
            cfg_path.write_text(template)
            _eprint(f"wrote new config: {cfg_path}")
            return 0
        _eprint(f"# no config at {cfg_path}; suggested contents "
                "(re-run with --write to create it):")
        print(template)
        return 0

    # Existing config: never overwrite. Compute absent top-level keys.
    try:
        existing = load_config_file(cfg_path, explicit=True)
    except SandboxError as exc:
        raise SandboxError(
            f"existing config is invalid, refusing to modify it: {exc.message}",
            "Fix the TOML by hand, then re-run init-integrations.",
        ) from exc

    suggestions: dict[str, object] = {}
    if "compat_kimi_home" not in existing:
        suggestions["compat_kimi_home"] = True
    if "persistent_cache" not in existing:
        suggestions["persistent_cache"] = True

    # Controlled conda integration (mod_v2 §14): suggest only absent scalar
    # keys; never enumerate envs or touch the MCP config.
    if conda_root is not None:
        if "conda_enabled" not in existing:
            suggestions["conda_enabled"] = True
        if "conda_root" not in existing:
            suggestions["conda_root"] = conda_root
        if "conda_writable" not in existing:
            suggestions["conda_writable"] = "cache"
        if "conda_shell_integration" not in existing:
            suggestions["conda_shell_integration"] = True

    # List keys need care: appending a second `key = [...]` to a TOML file that
    # already defines `key` produces a duplicate-key error. So we only *append*
    # a list key when it is entirely absent; when it exists but is missing some
    # suggested items we never rewrite it — we print a precise, copy-pasteable
    # manual hint for the exact items to add (mod_v1 §13).
    manual_list_items: dict[str, list[str]] = {}
    if suggested_profile_ro:
        if "profile_ro_mounts" not in existing:
            suggestions["profile_ro_mounts"] = list(suggested_profile_ro)
        else:
            current_items = existing.get("profile_ro_mounts", [])
            missing = [s for s in suggested_profile_ro if s not in current_items]
            if missing:
                manual_list_items["profile_ro_mounts"] = missing

    if not suggestions and not manual_list_items:
        _eprint(f"config already present at {cfg_path}; no missing keys to add.")
        if "profile_ro_mounts" in existing:
            _eprint("  profile_ro_mounts already set; leaving it untouched.")
        return 0

    def _print_manual_list_hints() -> None:
        for key, items in manual_list_items.items():
            _eprint(
                f"note: {key} already exists; cannot safely auto-append "
                "(in-place array merge is deferred to v2). Add these item(s) to "
                "the existing array by hand:"
            )
            for item in items:
                _eprint(f'    "{item}",')

    if not suggestions:
        # Only incomplete list(s): purely a manual edit, nothing to append.
        _print_manual_list_hints()
        return 0

    snippet = _render_toml_snippet(suggestions)
    if not args.write:
        _eprint(f"# {cfg_path} exists; suggested additions "
                "(re-run with --write to append, or edit by hand):")
        print(snippet)
        _print_manual_list_hints()
        return 0

    # --write on an existing file: back up, then append absent keys only.
    backup = cfg_path.with_suffix(cfg_path.suffix + f".bak.{int(time.time())}")
    backup.write_text(cfg_path.read_text())
    with open(cfg_path, "a", encoding="utf-8") as fh:
        fh.write("\n# --- added by kimi-sandbox init-integrations ---\n")
        fh.write(snippet)
    _eprint(f"appended {len(suggestions)} key(s) to {cfg_path} (backup: {backup})")
    _print_manual_list_hints()
    return 0


def _toml_str(value: str) -> str:
    """Render ``value`` as a TOML basic string with the required escapes.

    Escapes backslash, double-quote and the control characters TOML forbids in
    a bare basic string (tab/newline/carriage-return). Items written by
    ``init-integrations`` are launcher-controlled today, but escaping keeps the
    output valid TOML if a path/spec ever contains one of these characters.
    """
    out = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\t", "\\t")
        .replace("\n", "\\n")
        .replace("\r", "\\r")
    )
    return f'"{out}"'


def _render_init_template(profile_ro: list[str], *, conda_root: str | None = None) -> str:
    """Render a full starter config.toml (mod_v1 §5, mod_v2 §14 formats)."""
    pro_lines = "\n".join(f"  {_toml_str(spec)}," for spec in profile_ro) or \
        '  # "~/.kimi-code/skills:skills",'
    if conda_root is not None:
        conda_block = (
            "\n"
            "# Controlled conda: host conda root is mounted read-only; new envs\n"
            "# go to /cache/conda. 'conda install/remove' against host envs fail.\n"
            "conda_enabled = true\n"
            f"conda_root = {_toml_str(conda_root)}\n"
            'conda_writable = "cache"\n'
            "conda_shell_integration = true\n"
        )
    else:
        conda_block = (
            "\n"
            "# Controlled conda (uncomment after pointing conda_root at your\n"
            "# install; requires persistent_cache = true for conda_writable=cache):\n"
            '# conda_enabled = true\n'
            '# conda_root = "~/anaconda3"\n'
            '# conda_writable = "cache"\n'
            '# conda_shell_integration = true\n'
        )
    return (
        'profile = "default"\n'
        "persistent_cache = true\n"
        "compat_kimi_home = true\n"
        "\n"
        "profile_ro_mounts = [\n"
        f"{pro_lines}\n"
        "]\n"
        + conda_block
        + "\n"
        "# Mount MCP source + runtime read-only under /opt (edit to taste):\n"
        "ro_mounts = [\n"
        '  # "~/MLWorksPlaces_MCP/github_mcp:/opt/kimi-mcp/github_mcp",\n'
        '  # "~/anaconda3/envs/github-mcp:/opt/kimi-runtime/github-mcp",\n'
        "]\n"
        "\n"
        "# Only forward secrets you actually need (never globbed):\n"
        "env_keep = [\n"
        '  # "GITHUB_TOKEN",\n'
        "]\n"
        "\n"
        "[env_set]\n"
        'PYTHONDONTWRITEBYTECODE = "1"\n'
    )


def _render_toml_snippet(values: dict[str, object]) -> str:
    """Render a minimal TOML snippet for a few scalar/list keys (append-safe)."""
    lines: list[str] = []
    # scalars first, then lists/tables, so the snippet stays valid TOML.
    for key, val in values.items():
        if isinstance(val, bool):
            lines.append(f"{key} = {'true' if val else 'false'}")
        elif isinstance(val, str):
            lines.append(f"{key} = {_toml_str(val)}")
    for key, val in values.items():
        if isinstance(val, list):
            lines.append(f"{key} = [")
            for item in val:
                lines.append(f"  {_toml_str(item)},")
            lines.append("]")
    return "\n".join(lines) + "\n"


_SUBCOMMANDS = {
    "doctor": _cmd_doctor,
    "init-integrations": _cmd_init_integrations,
}


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    launcher_argv, kimi_args = split_kimi_args(raw_argv)

    # Subcommands are dispatched before the main parser so the bare-positional
    # form `kimi-sandbox PROJECT [flags] -- [kimi args]` keeps working: a leading
    # `doctor` / `init-integrations` token would otherwise be read as a project.
    if launcher_argv and launcher_argv[0] in _SUBCOMMANDS:
        name = launcher_argv[0]
        try:
            return _SUBCOMMANDS[name](launcher_argv[1:])
        except SandboxError as exc:
            _eprint(f"error: {exc.message}")
            if exc.hint:
                _eprint()
                _eprint(exc.hint)
            return LAUNCHER_ERROR_EXIT

    parser = build_parser()
    args = parser.parse_args(launcher_argv)

    try:
        return _run_main(args, kimi_args)
    except SandboxError as exc:
        _eprint(f"error: {exc.message}")
        if exc.hint:
            _eprint()
            _eprint(exc.hint)
        return LAUNCHER_ERROR_EXIT


def build_conda_runtime(
    cfg: dict,
    *,
    project_dir: Path,
    state_root: Path,
    kimi_code_home: Path,
    cache_dir: Path | None,
    persistent_cache: bool,
    profile: str,
) -> tuple[CondaConfig | None, tuple[GeneratedFileMount, ...]]:
    """Resolve, validate and materialise the controlled-conda runtime (mod_v2).

    Returns ``(None, ())`` when conda is disabled. Otherwise it resolves the
    host conda root and any extra existing envs, validates the whole config
    (read-only host root, writable/cache coupling, name safety, compat-target
    collisions), creates the host writable conda dirs in "cache" mode, and
    writes the generated shim/condarc/bash-hook, returning the resulting
    :class:`CondaConfig` and its read-only generated-file mounts.
    """
    if not bool(_pick(None, cfg, "conda_enabled", False)):
        return None, ()

    conda_root_spec = _pick(None, cfg, "conda_root", None)
    if not conda_root_spec:
        raise SandboxError(
            "conda_enabled is true but conda_root is not set",
            "Add conda_root = \"~/anaconda3\" to your config.",
        )

    writable = _pick(None, cfg, "conda_writable", "cache")
    if writable == "cache":
        writable_root = SANDBOX_CONDA_CACHE_WRITABLE
    elif writable == "tmp":
        writable_root = SANDBOX_CONDA_TMP_WRITABLE
    else:
        raise SandboxError(
            f"invalid conda_writable: {writable!r}",
            "Use conda_writable = \"cache\" (persistent) or \"tmp\" (ephemeral).",
        )

    shell_integration = bool(_pick(None, cfg, "conda_shell_integration", True))
    root = resolve_conda_root(conda_root_spec)
    existing_envs = tuple(
        resolve_conda_existing_env(spec)
        for spec in cfg.get("conda_existing_envs", [])
    )

    conda = CondaConfig(
        root=root,
        sandbox_original_root=str(root),
        writable_root=writable_root,
        shell_integration=shell_integration,
        existing_envs=existing_envs,
    )
    validate_conda_config(
        conda,
        persistent_cache=persistent_cache,
        project_dir=project_dir,
        state_root=state_root,
        kimi_code_home=kimi_code_home,
        cache_dir=cache_dir,
    )

    # The "cache" writable area lives inside the persistent /cache mount; make
    # the host dirs so conda has somewhere to create envs/packages.
    if writable_root == SANDBOX_CONDA_CACHE_WRITABLE and cache_dir is not None:
        ensure_dir(cache_dir / "conda")
        ensure_dir(cache_dir / "conda" / "envs")
        ensure_dir(cache_dir / "conda" / "pkgs")

    generated = prepare_conda_generated_files(
        state_root=state_root,
        profile=profile,
        writable_root=writable_root,
        original_root=conda.sandbox_original_root,
        shell_integration=shell_integration,
    )
    return conda, generated


def _run_main(args: argparse.Namespace, kimi_args: list[str]) -> int:
    # --- config file (lowest precedence; CLI flags override) ---
    if args.no_config:
        cfg: dict = {}
    else:
        cfg_path = Path(os.path.expanduser(args.config)) if args.config else _default_config_path()
        cfg = load_config_file(cfg_path, explicit=bool(args.config))
        # Restrained integration hint (mod_v1 §5/§8): only when the *default*
        # config is absent, and only under --debug or when host integration
        # clues exist. Never nag a plain sandbox user on every run.
        if not args.config and not cfg_path.exists():
            _maybe_hint_missing_integrations(debug=args.debug)

    profile = _pick(args.profile, cfg, "profile", "default")
    state_root_raw = _pick(args.state_root, cfg, "state_root", None)
    read_only = bool(_pick(args.read_only, cfg, "read_only", False))
    no_network = bool(_pick(args.no_network, cfg, "no_network", False))
    no_seccomp = bool(_pick(args.no_seccomp, cfg, "no_seccomp", False))
    persistent_cache = bool(_pick(args.persistent_cache, cfg, "persistent_cache", False))
    mode = MODE_READ_ONLY if read_only else MODE_WORKSPACE_WRITE

    limits = ResourceLimits(
        memory_max=_pick(args.memory_max, cfg, "memory_max", None),
        cpu_quota=_pick(args.cpu_quota, cfg, "cpu_quota", None),
        pids_max=_pick(args.pids_max, cfg, "pids_max", None),
    )
    validate_resource_limits(limits)

    # Extra mounts: config-file baseline first, then CLI additions.
    extra_mounts: list[ExtraMount] = []
    for spec in cfg.get("ro_mounts", []):
        extra_mounts.append(resolve_extra_mount(spec, writable=False))
    for spec in cfg.get("rw_mounts", []):
        extra_mounts.append(resolve_extra_mount(spec, writable=True))
    for spec in (args.ro_mounts or []):
        extra_mounts.append(resolve_extra_mount(spec, writable=False))
    for spec in (args.rw_mounts or []):
        extra_mounts.append(resolve_extra_mount(spec, writable=True))

    # Profile read-only sub-mounts under /kimi-code-home (mod_v1 §7/§8).
    profile_ro_mounts: list[ProfileMount] = [
        resolve_profile_ro_mount(spec) for spec in cfg.get("profile_ro_mounts", [])
    ]
    validate_profile_ro_mounts(profile_ro_mounts)

    # Environment passthrough/overrides (mod_v1 §9). Key validation and
    # reserved-key protection happen inside build_env_allowlist.
    env_keep = list(cfg.get("env_keep", []))
    env_set = dict(cfg.get("env_set", {}))
    compat_kimi_home = bool(_pick(args.compat_kimi_home, cfg, "compat_kimi_home", True))

    # --- resolve everything (pure) ---
    project_dir = resolve_project_dir(args.project)
    state_root = resolve_state_root(state_root_raw)

    if args.unsafe_kimi_code_home:
        kimi_code_home = resolve_unsafe_kimi_code_home(args.unsafe_kimi_code_home)
        effective_state_root: Path | None = None
        # Emit the §10-mandated prominent warning at resolution time so it shows
        # even under --dry-run (the start banner is skipped there).
        _eprint("WARNING: --unsafe-kimi-code-home in use.")
        _eprint(f"  {kimi_code_home}")
        _eprint("  is readable and writable by ALL sandboxed processes,")
        _eprint("  including any Bash/MCP/hook command Kimi runs.")
    else:
        kimi_code_home = default_kimi_code_home(state_root, profile)
        effective_state_root = state_root

    if args.exec_command is not None and kimi_args:
        _eprint(
            "warning: --exec is set, so the arguments after '--' "
            f"({shlex.join(kimi_args)}) are ignored."
        )

    if args.rw_mounts or cfg.get("rw_mounts"):
        _eprint(
            "warning: --rw-mount exposes a host directory writable to all "
            "sandboxed processes; only mount disposable/non-sensitive paths."
        )

    validate_path_relationships(
        project_dir=project_dir,
        state_root=state_root,
        kimi_code_home=kimi_code_home,
        unsafe_kimi_code_home=bool(args.unsafe_kimi_code_home),
    )

    kimi_path, kimi_source = resolve_kimi_with_source(args.kimi)
    bwrap_path = resolve_bwrap(args.bwrap)

    # Resource limits require systemd-run; fail loud if requested but missing.
    systemd_run: Path | None = None
    if not limits.is_empty():
        systemd_run = resolve_systemd_run()
        if systemd_run is None:
            raise SandboxError(
                "resource limits requested but systemd-run was not found",
                "Install systemd (provides systemd-run) or drop "
                "--memory-max/--cpu-quota/--pids-max.",
            )

    # --- persistent cache dir (host) ---
    cache_dir: Path | None = None
    if persistent_cache:
        cache_dir = resolve_cache_dir(state_root, profile)

    # --- create host state dirs (never the project) ---
    if effective_state_root is not None:
        ensure_dir(effective_state_root)
    ensure_dir(kimi_code_home)
    # Profile mountpoints must exist before bwrap binds onto them (mod_v1 §11).
    prepare_profile_mount_targets(kimi_code_home, tuple(profile_ro_mounts))
    if cache_dir is not None:
        ensure_dir(cache_dir)

    # --- controlled conda runtime (mod_v2): resolve/validate/generate ---
    conda_config, generated_file_mounts = build_conda_runtime(
        cfg,
        project_dir=project_dir,
        state_root=state_root,
        kimi_code_home=kimi_code_home,
        cache_dir=cache_dir,
        persistent_cache=persistent_cache,
        profile=profile,
    )

    if world_or_group_readable(kimi_code_home):
        _eprint(f"warning: KIMI_CODE_HOME is readable by other users: {kimi_code_home}")
        _eprint(f"  consider: chmod 700 {kimi_code_home}")

    if kimi_is_script(kimi_path):
        _eprint(
            f"warning: {kimi_path} looks like a script/wrapper, not a single "
            "binary; single-file mount may be incomplete."
        )
    # §13: warn when the discovered kimi was a symlink (we mount the resolved
    # real file, but a multi-file install behind the link may be incomplete).
    # ``kimi_source`` is the pre-resolution discovery path, so we avoid a second
    # shutil.which("kimi") lookup here.
    if kimi_source and os.path.islink(kimi_source):
        _eprint(
            f"note: {kimi_source} is a symlink; mounting its resolved target "
            f"{kimi_path}."
        )

    # --- seccomp: decide whether a TIOCSTI filter can/should be installed ---
    want_seccomp = not no_seccomp
    seccomp_supported = seccomp.is_supported_arch()
    seccomp_active = want_seccomp and seccomp_supported
    if want_seccomp and not seccomp_supported:
        _eprint(
            "note: TIOCSTI seccomp filter unavailable on this architecture "
            f"({os.uname().machine}); continuing without it."
        )

    # --- assemble config ---
    config = SandboxConfig(
        project_dir=project_dir,
        kimi_code_home=kimi_code_home,
        kimi_path=kimi_path,
        inner_command=build_inner_command(args, kimi_args),
        env=build_env_allowlist(
            mode=mode,
            persistent_cache=persistent_cache,
            env_keep=env_keep,
            env_set=env_set,
            conda=conda_config,
        ),
        state_root=effective_state_root,
        profile=profile,
        unsafe_kimi_code_home=bool(args.unsafe_kimi_code_home),
        debug=args.debug,
        mode=mode,
        no_network=no_network,
        cache_dir=cache_dir,
        extra_mounts=tuple(extra_mounts),
        profile_ro_mounts=tuple(profile_ro_mounts),
        compat_kimi_home=compat_kimi_home,
        conda=conda_config,
        generated_file_mounts=generated_file_mounts,
    )

    pin_mounts = not args.no_pin_mounts

    if args.debug:
        print_mount_plan(
            config,
            kimi_path=kimi_path,
            seccomp_active=seccomp_active,
            limits=limits,
            systemd_run=systemd_run,
            pin_mounts=pin_mounts,
        )

    if args.dry_run:
        # Build without real fds; annotate the seccomp/limits/pinning structure
        # so the printed command faithfully reflects what would run (paths are
        # shown rather than fd numbers for readability).
        command = build_bwrap_command(config, bwrap_path=bwrap_path)
        if systemd_run is not None:
            command = build_systemd_run_prefix(systemd_run, limits) + command
        print(shlex.join(command))
        if seccomp_active:
            _eprint("note: a TIOCSTI seccomp filter fd would be passed via --seccomp <fd>.")
        if pin_mounts:
            _eprint(
                "note: host bind sources (project, kimi-code-home, cache, kimi "
                "binary, extra mounts, profile ro mounts) would be pinned via "
                "--bind-fd/--ro-bind-fd."
            )
        return 0

    # --- open the seccomp filter fd (kept open across the run) ---
    seccomp_fd: int | None = None
    if seccomp_active:
        try:
            seccomp_fd = seccomp.open_filter_fd()
        except (OSError, ValueError) as exc:
            _eprint(f"warning: could not prepare seccomp filter ({exc}); continuing without it.")
            seccomp_active = False

    # --- open O_PATH fds to pin each host bind source (anti-TOCTOU) ---
    bind_fds: dict[str, int] = {}
    try:
        if pin_mounts:
            bind_fds = open_bind_fds(config)
        command = build_bwrap_command(
            config,
            bwrap_path=bwrap_path,
            seccomp_fd=seccomp_fd,
            path_fds=bind_fds or None,
        )
        if systemd_run is not None:
            command = build_systemd_run_prefix(systemd_run, limits) + command
        print_start_banner(
            config,
            seccomp_active=seccomp_active,
            limits=limits,
            pin_mounts=pin_mounts,
        )
        fd_list: list[int] = []
        if seccomp_fd is not None:
            fd_list.append(seccomp_fd)
        fd_list.extend(bind_fds.values())
        return _run(command, pass_fds=tuple(fd_list))
    finally:
        if seccomp_fd is not None:
            os.close(seccomp_fd)
        for fd in bind_fds.values():
            os.close(fd)


if __name__ == "__main__":
    raise SystemExit(main())
