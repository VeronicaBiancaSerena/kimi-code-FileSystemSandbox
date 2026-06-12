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
import os
import shlex
import subprocess
import sys
import tomllib
from pathlib import Path

from . import __version__
from . import seccomp
from .bwrap import build_bwrap_command
from .config import (
    MODE_READ_ONLY,
    MODE_WORKSPACE_WRITE,
    SANDBOX_CACHE,
    SANDBOX_HOME,
    SANDBOX_KIMI_BIN,
    SANDBOX_KIMI_CODE_HOME,
    SANDBOX_WORKSPACE,
    ExtraMount,
    ResourceLimits,
    SandboxConfig,
)
from .errors import BubblewrapFailedError, SandboxError
from .paths import (
    default_kimi_code_home,
    ensure_dir,
    kimi_is_script,
    resolve_bwrap,
    resolve_cache_dir,
    resolve_extra_mount,
    resolve_kimi_with_source,
    resolve_project_dir,
    resolve_state_root,
    resolve_systemd_run,
    resolve_unsafe_kimi_code_home,
    validate_path_relationships,
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
_CONFIG_BOOL_KEYS = ("no_network", "read_only", "persistent_cache", "no_seccomp")
_CONFIG_STR_KEYS = ("profile", "state_root", "memory_max", "cpu_quota")
_CONFIG_INT_KEYS = ("pids_max",)
_CONFIG_LIST_KEYS = ("ro_mounts", "rw_mounts")
_CONFIG_KNOWN_KEYS = frozenset(
    _CONFIG_BOOL_KEYS + _CONFIG_STR_KEYS + _CONFIG_INT_KEYS + _CONFIG_LIST_KEYS
)


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
    return data


def _pick(cli_value, cfg: dict, key: str, default):
    """CLI value wins if set (not None); else config file; else hardcoded."""
    if cli_value is not None:
        return cli_value
    if key in cfg:
        return cfg[key]
    return default


# ---------------------------------------------------------------------------

def build_env_allowlist(
    *,
    mode: str = MODE_WORKSPACE_WRITE,
    persistent_cache: bool = False,
    extra: dict[str, str] | None = None,
) -> dict[str, str]:
    """Construct the sandbox environment (design 11 / 21).

    Launcher-controlled values are set unconditionally; a short allowlist of
    terminal/locale vars is forwarded from the host when present. Sensitive vars
    (credentials, agents) are never forwarded because we start from --clearenv.
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
    for key in ENV_ALLOWLIST:
        val = os.environ.get(key)
        if val is not None:
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
    _eprint(f"  kimi bin  : {kimi_path} -> {SANDBOX_KIMI_BIN} (ro)")
    if config.cache_dir is not None:
        _eprint(f"  cache     : {config.cache_dir} -> {SANDBOX_CACHE} (rw, persistent)")
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
    for mount in config.extra_mounts:
        acc = "rw" if mount.writable else "ro"
        print(f"  extra mount: {mount.source} -> {mount.target} ({acc})", file=sys.stderr)
    print("  home: isolated tmpfs", file=sys.stderr)
    print("  tmp: isolated tmpfs", file=sys.stderr)
    print(f"  network: {'isolated' if config.no_network else 'enabled'}", file=sys.stderr)
    print(f"  seccomp: {'TIOCSTI filter active' if seccomp_active else 'off'}", file=sys.stderr)
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


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    launcher_argv, kimi_args = split_kimi_args(raw_argv)

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


def _run_main(args: argparse.Namespace, kimi_args: list[str]) -> int:
    # --- config file (lowest precedence; CLI flags override) ---
    if args.no_config:
        cfg: dict = {}
    else:
        cfg_path = Path(os.path.expanduser(args.config)) if args.config else _default_config_path()
        cfg = load_config_file(cfg_path, explicit=bool(args.config))

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
    if cache_dir is not None:
        ensure_dir(cache_dir)

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
        env=build_env_allowlist(mode=mode, persistent_cache=persistent_cache),
        state_root=effective_state_root,
        profile=profile,
        unsafe_kimi_code_home=bool(args.unsafe_kimi_code_home),
        debug=args.debug,
        mode=mode,
        no_network=no_network,
        cache_dir=cache_dir,
        extra_mounts=tuple(extra_mounts),
    )

    if args.debug:
        print_mount_plan(
            config,
            kimi_path=kimi_path,
            seccomp_active=seccomp_active,
            limits=limits,
            systemd_run=systemd_run,
        )

    if args.dry_run:
        # Build without a real fd; annotate the seccomp/limits structure so the
        # printed command faithfully reflects what would run.
        command = build_bwrap_command(config, bwrap_path=bwrap_path)
        if systemd_run is not None:
            command = build_systemd_run_prefix(systemd_run, limits) + command
        print(shlex.join(command))
        if seccomp_active:
            _eprint("note: a TIOCSTI seccomp filter fd would be passed via --seccomp <fd>.")
        return 0

    # --- open the seccomp filter fd (kept open across the run) ---
    seccomp_fd: int | None = None
    if seccomp_active:
        try:
            seccomp_fd = seccomp.open_filter_fd()
        except (OSError, ValueError) as exc:
            _eprint(f"warning: could not prepare seccomp filter ({exc}); continuing without it.")
            seccomp_active = False

    try:
        command = build_bwrap_command(
            config, bwrap_path=bwrap_path, seccomp_fd=seccomp_fd
        )
        if systemd_run is not None:
            command = build_systemd_run_prefix(systemd_run, limits) + command
        print_start_banner(config, seccomp_active=seccomp_active, limits=limits)
        pass_fds = (seccomp_fd,) if seccomp_fd is not None else ()
        return _run(command, pass_fds=pass_fds)
    finally:
        if seccomp_fd is not None:
            os.close(seccomp_fd)


if __name__ == "__main__":
    raise SystemExit(main())
