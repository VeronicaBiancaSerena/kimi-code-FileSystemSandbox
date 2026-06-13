"""Controlled-conda command parsing and mutation policy (mod_v2 §7).

This module is the single source of truth for *which* ``conda`` invocations are
allowed inside the sandbox. It is deliberately **self-contained**: it imports
only the standard library and never imports anything else from
``kimi_sandbox``. That is a hard requirement, because
``conda_integration.render_conda_shim`` inlines this file verbatim into the
generated ``/sandbox/bin/conda`` shim, which runs inside the sandbox with an
empty ``PYTHONPATH`` and no access to the launcher source tree (mod_v2 §7.2).

The contract is "fail closed": any command whose target cannot be *proven* to
live under the writable conda env root is rejected before the real conda is
ever exec'd. The bubblewrap read-only binds are the backstop, but this policy
gives a clear, early error and prevents conda from even attempting a write to
host content.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass

# --- in-sandbox canonical locations (kept in sync with config.py) ----------
DEFAULT_READONLY_ROOT = "/opt/kimi-conda/root"
DEFAULT_EXISTING_ENVS_ROOT = "/opt/kimi-conda/existing-envs"
DEFAULT_WRITABLE_ROOT = "/cache/conda"
CONDARC_PATH = "/sandbox/etc/condarc"

# --- argv classification sets ----------------------------------------------
# Global options that carry a *value* in the following argv token. Skipping
# these correctly is what lets the scanner find the real subcommand even when
# the user writes e.g. ``conda -c conda-forge install -n x numpy``.
_GLOBAL_VALUE_FLAGS = frozenset(
    {"--solver", "--repodata-fn", "--channel", "-c"}
)

_MAIN_COMMANDS = frozenset(
    {
        "create",
        "install",
        "update",
        "upgrade",
        "remove",
        "uninstall",
        "clean",
        "run",
        "env",
        "info",
        "list",
        "search",
        "config",
    }
)
_ENV_SUBCOMMANDS = frozenset(
    {"create", "update", "remove", "delete", "list", "export", "config"}
)
# Commands that may create/modify env or package state.
_MUTATION_MAIN = frozenset(
    {"create", "install", "update", "upgrade", "remove", "uninstall"}
)
_ENV_MUTATION_SUB = frozenset({"create", "update", "remove", "delete"})

# Target-selection flags.
_NAME_FLAGS = frozenset({"-n", "--name"})
_PREFIX_FLAGS = frozenset({"-p", "--prefix"})
_FILE_FLAGS = frozenset({"-f", "--file"})
# Long target flags eligible for abbreviation expansion (mod_v2 §7.2 / audit #2).
_TARGET_LONG_FLAGS = frozenset({"--name", "--prefix", "--file"})

# conda config flags.
_CONFIG_READONLY_FLAGS = frozenset(
    {"--show", "--show-sources", "--describe", "--get"}
)
_CONFIG_WRITE_FLAGS = frozenset(
    {
        "--set",
        "--add",
        "--append",
        "--prepend",
        "--remove",
        "--remove-key",
        "--write-default",
    }
)
# Flags that re-target config writing at an env/root/system scope.
_CONFIG_SCOPE_FLAGS = frozenset({"--env", "--system"})
# All known ``conda config`` long flags, used to resolve abbreviations against
# the right namespace before classifying read-only vs write (audit #2).
_CONFIG_ALL_LONG_FLAGS = (
    _CONFIG_READONLY_FLAGS
    | _CONFIG_WRITE_FLAGS
    | _CONFIG_SCOPE_FLAGS
    | frozenset({"--json", "--file", "--force", "--validate", "--help"})
)

_MAX_ENV_FILE_BYTES = 64 * 1024


def build_conda_envs_path(
    writable_root: str,
    original_root: str,
    existing_envs_root: str = DEFAULT_EXISTING_ENVS_ROOT,
) -> str:
    """Return the controlled ``CONDA_ENVS_PATH`` (mod_v2 §6).

    Single source of truth for the writable-first env search path so the
    launcher env, the generated shim, and the bash hook never drift apart. The
    writable env root is always first (new envs land there); the read-only host
    envs follow via the original-prefix path (so console-script shebangs that
    hard-code that path resolve, §7.6).
    """
    return ":".join(
        (
            f"{writable_root.rstrip('/')}/envs",
            f"{original_root.rstrip('/')}/envs",
            existing_envs_root.rstrip("/"),
        )
    )


def _expand_long_flag(tok: str, candidates: frozenset[str]) -> str:
    """Expand an unambiguous ``--abbrev`` to its full flag (conda allows this).

    conda's argparse accepts unambiguous prefixes of long options (e.g. ``--pre``
    for ``--prefix``). Recognizing them keeps the scanner aligned with conda so
    abbreviated targets are still resolved and abbreviated write-flags are still
    caught. Only ``--`` long options are expanded; an ambiguous or unknown
    prefix is returned unchanged (the policy then fails closed). ``--k=v`` forms
    are supported (only the key is expanded).
    """
    if not tok.startswith("--") or tok == "--":
        return tok
    key, sep, value = tok.partition("=")
    if key in candidates:
        return tok
    matches = [c for c in candidates if c.startswith(key)]
    if len(matches) == 1:
        return matches[0] + (sep + value if sep else "")
    return tok


class CondaPolicyError(Exception):
    """A conda invocation was rejected by the sandbox policy.

    The message is shown to the user (prefixed with ``error:``) and the shim
    exits non-zero without invoking the real conda.
    """


@dataclass(frozen=True)
class CondaCommand:
    """Parsed view of a ``conda`` argv (mod_v2 §7.2).

    ``command`` is the resolved command path: ``("install",)`` or
    ``("env", "create")``. ``env_name`` / ``prefix`` are the ``-n``/``-p``
    targets (``prefix`` wins when both are present). ``file`` is the ``-f``
    environment file (env create/update). ``is_mutation`` marks commands that
    can change env/package state; ``is_clean`` marks ``conda clean``;
    ``allow_readonly_target`` is True for commands (``run`` / queries) that may
    legitimately point at a read-only host env.
    """

    command: tuple[str, ...]
    env_name: str | None
    prefix: str | None
    file: str | None
    is_mutation: bool
    is_clean: bool
    allow_readonly_target: bool


def _find_main_command(argv: list[str]) -> tuple[str | None, int]:
    """Return ``(main_command, index)`` for the first *known* subcommand.

    Leading global options are skipped (value-taking ones consume their value).
    Crucially, only a token in :data:`_MAIN_COMMANDS` is accepted as the command
    (audit #1): this prevents a value belonging to an unknown global option from
    being mistaken for the subcommand. An unknown bare token is skipped and the
    scan continues; if no known command is found, ``(None, -1)`` is returned and
    the caller treats the invocation as a pass-through query.
    """
    i = 0
    n = len(argv)
    while i < n:
        tok = argv[i]
        if tok.startswith("--") and "=" in tok:
            i += 1
            continue
        if tok in _GLOBAL_VALUE_FLAGS:
            i += 2
            continue
        if tok.startswith("-"):
            i += 1
            continue
        if tok in _MAIN_COMMANDS:
            return tok, i
        # Unknown bare token: skip it and keep looking for a known command.
        i += 1
    return None, -1


def _find_env_subcommand(argv: list[str], start: int) -> tuple[str | None, int]:
    """Return the first *known* ``env`` sub-command after ``start`` (audit #1)."""
    j = start
    n = len(argv)
    while j < n:
        tok = argv[j]
        if tok.startswith("--") and "=" in tok:
            j += 1
            continue
        if tok in _GLOBAL_VALUE_FLAGS:
            j += 2
            continue
        if tok.startswith("-"):
            j += 1
            continue
        if tok in _ENV_SUBCOMMANDS:
            return tok, j
        j += 1
    return None, -1


def _scan_targets(
    tokens: list[str], *, is_run: bool
) -> tuple[str | None, str | None, str | None]:
    """Scan ``-n/--name``, ``-p/--prefix`` and ``-f/--file`` from ``tokens``.

    For ``run`` the scan stops at the first positional (the program to execute)
    so a program's own ``-n``/``-c`` cannot be mistaken for a conda option.
    """
    name: str | None = None
    prefix: str | None = None
    filearg: str | None = None
    i = 0
    n = len(tokens)
    while i < n:
        tok = _expand_long_flag(tokens[i], _TARGET_LONG_FLAGS)
        key, eq, val = tok.partition("=")
        if eq:
            if key in _NAME_FLAGS:
                name = val
            elif key in _PREFIX_FLAGS:
                prefix = val
            elif key in _FILE_FLAGS:
                filearg = val
            i += 1
            continue
        if tok in _NAME_FLAGS:
            name = tokens[i + 1] if i + 1 < n else None
            i += 2
            continue
        if tok in _PREFIX_FLAGS:
            prefix = tokens[i + 1] if i + 1 < n else None
            i += 2
            continue
        if tok in _FILE_FLAGS:
            filearg = tokens[i + 1] if i + 1 < n else None
            i += 2
            continue
        if tok in _GLOBAL_VALUE_FLAGS:
            i += 2
            continue
        if tok.startswith("-"):
            i += 1
            continue
        # positional token
        if is_run:
            break
        i += 1
    return name, prefix, filearg


def parse_conda_argv(argv: list[str]) -> CondaCommand:
    """Parse a ``conda`` argv into a :class:`CondaCommand` (no I/O)."""
    main, main_idx = _find_main_command(argv)
    if main is None:
        return CondaCommand(
            command=(),
            env_name=None,
            prefix=None,
            file=None,
            is_mutation=False,
            is_clean=False,
            allow_readonly_target=True,
        )

    if main == "env":
        sub, sub_idx = _find_env_subcommand(argv, main_idx + 1)
        if sub is not None:
            command: tuple[str, ...] = ("env", sub)
            rest = argv[sub_idx + 1 :]
        else:
            command = ("env",)
            rest = argv[main_idx + 1 :]
        is_mutation = sub in _ENV_MUTATION_SUB if sub is not None else False
        # `conda env config vars set/unset` mutates an env's persisted state;
        # `conda env config vars list` is read-only (audit #1).
        if sub == "config" and ("set" in rest or "unset" in rest):
            is_mutation = True
        is_clean = False
        is_run = False
    else:
        command = (main,)
        rest = argv[main_idx + 1 :]
        is_mutation = main in _MUTATION_MAIN
        is_clean = main == "clean"
        is_run = main == "run"

    name, prefix, filearg = _scan_targets(rest, is_run=is_run)
    return CondaCommand(
        command=command,
        env_name=name,
        prefix=prefix,
        file=filearg,
        is_mutation=is_mutation,
        is_clean=is_clean,
        allow_readonly_target=not is_mutation,
    )


# --- path helpers (string-only; no host FS assumptions) --------------------

def _normabs(path: str) -> str:
    """Normalize an absolute path, collapsing ``.``/``..`` without FS access."""
    return os.path.normpath(path)


def _within(child: str, parent: str) -> bool:
    """True if ``child`` is ``parent`` or strictly nested under it."""
    parent = parent.rstrip("/")
    if not parent:
        return True
    return child == parent or child.startswith(parent + "/")


def _readonly_roots(readonly_root: str, env: Mapping[str, str]) -> tuple[str, ...]:
    """All read-only conda root aliases: canonical + original-prefix (§7.6)."""
    roots = [readonly_root.rstrip("/")]
    original = env.get("KIMI_SANDBOX_CONDA_ORIGINAL_ROOT")
    if original:
        original = original.rstrip("/")
        if original not in roots:
            roots.append(original)
    return tuple(roots)


def _scan_env_file_targets(path: str) -> tuple[str | None, str | None]:
    """Conservatively read top-level ``name:``/``prefix:`` from an env file.

    Reads at most 64 KiB and only recognizes simple column-0 scalars. Anything
    ambiguous (multi-doc YAML, templated/complex values, unreadable/oversized
    file) raises :class:`CondaPolicyError` so the caller fails closed (§7.5).
    """
    try:
        with open(path, "rb") as fh:
            raw = fh.read(_MAX_ENV_FILE_BYTES + 1)
    except OSError as exc:
        raise CondaPolicyError(
            f"cannot read conda environment file: {path} ({exc})"
        ) from exc
    if len(raw) > _MAX_ENV_FILE_BYTES:
        raise CondaPolicyError(
            f"conda environment file too large to scan safely: {path}; "
            "pass an explicit -n/-p target instead"
        )
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CondaPolicyError(
            f"conda environment file is not valid UTF-8: {path}"
        ) from exc

    name: str | None = None
    prefix: str | None = None
    seen_content = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped == "...":
            raise CondaPolicyError(
                f"multi-document environment file not supported: {path}; "
                "pass an explicit -n/-p target"
            )
        if stripped == "---":
            # A leading document marker is fine; one appearing after content
            # means a second YAML document — refuse to guess (§7.5).
            if seen_content:
                raise CondaPolicyError(
                    f"multi-document environment file not supported: {path}; "
                    "pass an explicit -n/-p target"
                )
            continue
        if not stripped or stripped[0] == "#":
            continue
        seen_content = True
        if line[0] in (" ", "\t", "-"):
            # nested mapping/list item: ignore.
            continue
        key, sep, val = line.partition(":")
        if not sep:
            continue
        key = key.strip()
        if key not in ("name", "prefix"):
            continue
        # Strip a trailing inline comment, then surrounding quotes.
        val = val.strip()
        if not val:
            raise CondaPolicyError(
                f"ambiguous top-level {key!r} in environment file {path}; "
                "pass an explicit -n/-p target"
            )
        if val[0] in "{[&*!|>":
            raise CondaPolicyError(
                f"complex top-level {key!r} in environment file {path}; "
                "pass an explicit -n/-p target"
            )
        if val[0] in "\"'":
            quote = val[0]
            end = val.find(quote, 1)
            if end == -1:
                raise CondaPolicyError(
                    f"unterminated quoted {key!r} in environment file {path}"
                )
            val = val[1:end]
        else:
            # drop inline comment
            hashpos = val.find(" #")
            if hashpos != -1:
                val = val[:hashpos].strip()
        if key == "name":
            name = val
        else:
            prefix = val
    return name, prefix


def _resolve_name_to_prefix(
    name: str,
    *,
    writable_root: str,
    readonly_roots: tuple[str, ...],
    existing_envs_root: str,
    path_exists: Callable[[str], bool],
) -> tuple[str, bool]:
    """Map an env *name* to ``(prefix, is_writable)``.

    ``base`` resolves to the (read-only) canonical root. Otherwise a writable
    ``<writable>/envs/<name>`` wins if it exists; failing that an existing
    read-only env of the same name (under any readonly root or the extra
    existing-envs root) marks the target read-only. If nothing exists yet the
    target is the *prospective* writable prefix (used by ``create``).
    """
    if name == "base":
        return readonly_roots[0], False

    writable_prefix = f"{writable_root.rstrip('/')}/envs/{name}"
    if path_exists(writable_prefix):
        return writable_prefix, True

    for ro in readonly_roots:
        ro_prefix = f"{ro}/envs/{name}"
        if path_exists(ro_prefix):
            return ro_prefix, False
    existing_prefix = f"{existing_envs_root.rstrip('/')}/{name}"
    if path_exists(existing_prefix):
        return existing_prefix, False

    # Nothing exists yet: prospective writable target.
    return writable_prefix, True


def _require_writable_prefix(prefix: str, *, writable_root: str) -> None:
    """Raise unless ``prefix`` is a clean child of ``<writable>/envs``."""
    if not prefix.startswith("/"):
        raise CondaPolicyError(
            f"refusing conda mutation with non-absolute prefix: {prefix!r}; "
            "pass an absolute prefix under the sandbox writable env root"
        )
    norm = _normabs(prefix)
    writable_envs = f"{writable_root.rstrip('/')}/envs"
    if not norm.startswith(writable_envs + "/"):
        raise CondaPolicyError(
            f"refusing to modify read-only / out-of-sandbox conda prefix: {norm}\n"
            f"hint: sandbox env mutations must target {writable_envs}/<name> "
            "(e.g. conda create -n sandbox-foo ...)"
        )
    # A direct ``<writable>/envs/<name>`` (or deeper) is fine; the writable env
    # root itself, or ``..`` escapes, are not.
    if norm == writable_envs:
        raise CondaPolicyError(
            f"refusing conda mutation that targets the env root itself: {norm}"
        )


def _validate_config(argv: list[str]) -> None:
    """Allow only explicit read-only ``conda config`` queries (§7.4).

    Long-flag abbreviations are expanded first (audit #2) so an abbreviated
    write flag (e.g. ``--se`` for ``--set``) is still caught and an abbreviated
    read-only query (e.g. ``--g`` for ``--get``) is still accepted.
    """
    flags = {
        _expand_long_flag(a, _CONFIG_ALL_LONG_FLAGS).split("=", 1)[0]
        for a in argv
        if a.startswith("-")
    }
    bad = flags & (_CONFIG_WRITE_FLAGS | _CONFIG_SCOPE_FLAGS)
    if bad:
        raise CondaPolicyError(
            "refusing write/scoped 'conda config' "
            f"({', '.join(sorted(bad))}); only read-only queries are allowed "
            "(--show, --show-sources, --describe, --get)"
        )
    if not (flags & _CONFIG_READONLY_FLAGS):
        raise CondaPolicyError(
            "refusing ambiguous 'conda config'; use an explicit read-only query "
            "(--show, --show-sources, --describe, --get)"
        )


def _validate_clean(
    cmd: CondaCommand, argv: list[str], *, writable_root: str, env: Mapping[str, str]
) -> None:
    """Constrain ``conda clean`` to the writable package cache only (§7.3)."""
    if "--force-pkgs-dirs" in argv:
        raise CondaPolicyError(
            "refusing 'conda clean --force-pkgs-dirs'; it is too broad and is "
            "not supported in the sandbox"
        )
    if cmd.env_name is not None or cmd.prefix is not None:
        raise CondaPolicyError(
            "refusing 'conda clean' with -n/-p; clean operates on the package "
            "cache, not an env"
        )
    expected = f"{writable_root.rstrip('/')}/pkgs"
    pkgs = env.get("CONDA_PKGS_DIRS", "")
    if pkgs != expected:
        # In the real shim CONDA_PKGS_DIRS is force-set to the writable pkgs dir
        # before this runs, so this guard is normally satisfied; it still matters
        # because (a) unit tests drive validate_conda_argv with an explicit env,
        # and (b) it documents/enforces that clean may only ever touch the
        # writable package cache, never a read-only host pkgs cache (§7.3).
        raise CondaPolicyError(
            "refusing 'conda clean': CONDA_PKGS_DIRS is not limited to the "
            f"writable package cache ({expected})"
        )


def validate_conda_argv(
    argv: list[str],
    *,
    writable_root: str,
    readonly_root: str,
    existing_envs_root: str,
    env: Mapping[str, str],
    path_exists: Callable[[str], bool],
) -> None:
    """Raise :class:`CondaPolicyError` if ``argv`` is not allowed (mod_v2 §7).

    Read-only queries and ``run`` pass through. ``config`` and ``clean`` have
    dedicated rules. Mutating commands must resolve to a target prefix that is
    provably under ``<writable_root>/envs``; everything else fails closed.
    """
    cmd = parse_conda_argv(argv)
    main = cmd.command[0] if cmd.command else None

    if main == "config":
        _validate_config(argv)
        return
    if cmd.is_clean:
        _validate_clean(cmd, argv, writable_root=writable_root, env=env)
        return
    if not cmd.is_mutation:
        # run, info, list, search, env list/export, bare conda, ...
        return

    readonly_roots = _readonly_roots(readonly_root, env)

    # Reconcile CLI target with any environment-file target (§7.5).
    cli_prefix = cmd.prefix
    cli_name = cmd.env_name
    file_name: str | None = None
    file_prefix: str | None = None
    if cmd.command[:1] == ("env",) and cmd.command[1:2] in (("create",), ("update",)):
        if cmd.file is not None:
            file_name, file_prefix = _scan_env_file_targets(cmd.file)
        elif cli_prefix is None and cli_name is None:
            raise CondaPolicyError(
                "refusing 'conda env create/update' without a target; pass -n or "
                "-p, or a -f environment file with a top-level name:/prefix:"
            )

    # Resolve each declared target to an absolute prefix, then require they all
    # agree and are writable.
    resolved: list[str] = []

    def _add_prefix(prefix: str) -> None:
        resolved.append(_normabs(prefix))

    def _add_name(name: str) -> None:
        target, _writable = _resolve_name_to_prefix(
            name,
            writable_root=writable_root,
            readonly_roots=readonly_roots,
            existing_envs_root=existing_envs_root,
            path_exists=path_exists,
        )
        resolved.append(_normabs(target))

    if cli_prefix is not None:
        _add_prefix(cli_prefix)
    if cli_name is not None:
        _add_name(cli_name)
    if file_prefix is not None:
        _add_prefix(file_prefix)
    if file_name is not None:
        _add_name(file_name)

    if not resolved:
        # No explicit target: only safe if the active env is already writable.
        active = env.get("CONDA_PREFIX")
        if active:
            _require_writable_prefix(active, writable_root=writable_root)
            return
        raise CondaPolicyError(
            "refusing conda mutation without an explicit target env; pass -n or "
            "-p pointing at a sandbox env under "
            f"{writable_root.rstrip('/')}/envs"
        )

    # CLI vs YAML conflict (§7.5): all declared targets must agree.
    if len(set(resolved)) > 1:
        raise CondaPolicyError(
            "conflicting conda targets (CLI vs environment file resolve to "
            f"different prefixes): {sorted(set(resolved))}"
        )

    target = resolved[0]
    _require_writable_prefix(target, writable_root=writable_root)

    # Extra guard for create: a same-named read-only env must not be shadowed
    # silently (§7.2). If the writable target does not yet exist but a readonly
    # env of that name does, reject.
    is_create = cmd.command in (("create",), ("env", "create"))
    if is_create and (cli_name or file_name):
        name = cli_name or file_name
        if name and name != "base" and not path_exists(target):
            for ro in readonly_roots:
                if path_exists(f"{ro}/envs/{name}"):
                    raise CondaPolicyError(
                        f"refusing to create '{name}': a read-only host env of "
                        "the same name exists; choose a different name (e.g. "
                        f"sandbox-{name})"
                    )
            if path_exists(f"{existing_envs_root.rstrip('/')}/{name}"):
                raise CondaPolicyError(
                    f"refusing to create '{name}': a read-only host env of the "
                    "same name exists; choose a different name"
                )


def shim_main(argv: list[str]) -> int:
    """Entry point used by the generated ``/sandbox/bin/conda`` shim.

    Forces the controlled conda environment variables (defense-in-depth, in
    case a sandboxed process tried to redirect env/pkg dirs), validates the
    command, then execs the real conda — preferring the original-prefix
    interpreter so console-script shebangs keep resolving (§7.6).
    """
    readonly_root = os.environ.get("KIMI_SANDBOX_CONDA_ROOT", DEFAULT_READONLY_ROOT)
    original_root = os.environ.get(
        "KIMI_SANDBOX_CONDA_ORIGINAL_ROOT", readonly_root
    )
    writable_root = os.environ.get(
        "KIMI_SANDBOX_CONDA_WRITABLE_ROOT", DEFAULT_WRITABLE_ROOT
    )
    existing_envs_root = DEFAULT_EXISTING_ENVS_ROOT

    writable_root = writable_root.rstrip("/")
    original_root = original_root.rstrip("/")

    # Force-control the conda environment (cannot be redirected from inside).
    os.environ["CONDARC"] = CONDARC_PATH
    os.environ["CONDA_ENVS_PATH"] = build_conda_envs_path(
        writable_root, original_root, existing_envs_root
    )
    os.environ["CONDA_PKGS_DIRS"] = f"{writable_root}/pkgs"
    os.environ["CONDA_ALWAYS_COPY"] = "1"
    os.environ.setdefault("CONDA_AUTO_ACTIVATE_BASE", "false")

    try:
        validate_conda_argv(
            argv,
            writable_root=writable_root,
            readonly_root=readonly_root,
            existing_envs_root=existing_envs_root,
            env=os.environ,
            path_exists=os.path.exists,
        )
    except CondaPolicyError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 1

    py = f"{original_root}/bin/python"
    conda_exe = f"{original_root}/bin/conda"
    if os.path.exists(py) and os.path.exists(conda_exe):
        os.execv(py, [py, conda_exe, *argv])
    fallback = f"{readonly_root}/bin/conda"
    os.execv(fallback, ["conda", *argv])
    return 127  # unreachable; execv replaces the process


__all__ = [
    "CondaCommand",
    "CondaPolicyError",
    "parse_conda_argv",
    "validate_conda_argv",
    "build_conda_envs_path",
    "shim_main",
    "DEFAULT_READONLY_ROOT",
    "DEFAULT_EXISTING_ENVS_ROOT",
    "DEFAULT_WRITABLE_ROOT",
    "CONDARC_PATH",
]


if __name__ == "__main__":
    raise SystemExit(shim_main(sys.argv[1:]))
