"""bubblewrap command construction.

``build_bwrap_command`` turns a resolved :class:`SandboxConfig` into a complete
``bwrap`` argv list. It performs only read-only filesystem probing (does this
host path exist? is it a symlink?) and never launches anything, so it is fully
unit-testable.

Mount strategy notes (validated empirically against bubblewrap 0.11):

* System runtime (``/usr``, ``/lib*``) is bound read-only. On merged-/usr
  distros ``/bin``, ``/sbin``, ``/lib`` are symlinks into ``/usr``; we recreate
  them as ``--symlink`` entries rather than ro-binding the link targets twice.
* ``/etc`` is **not** mounted as a writable ``--dir`` (that would leave it
  writable and let ``touch /etc/x`` succeed). Instead we lay down a ``--tmpfs``
  at ``/etc``, ro-bind only the minimal DNS/TLS files into it, then
  ``--remount-ro /etc`` so the whole tree — including the tmpfs root — is
  read-only. This satisfies "system dirs read-only" and "no whole /etc bind".
* No ``--dev-bind / /``. Network is shared by default; ``--unshare-net`` is
  added only when ``config.no_network`` is set (v2 §33.4).
* ``--seccomp <fd>`` is emitted when the caller supplies a ``seccomp_fd`` (the
  TIOCSTI-blocking filter, v2 §33.1). The fd must be inheritable and passed via
  ``subprocess.run(..., pass_fds=...)``; building the argv does not open it.
"""

from __future__ import annotations

import os
from pathlib import Path

from .config import (
    ETC_MIN_DIRS,
    ETC_MIN_FILES,
    MODE_READ_ONLY,
    SANDBOX_CACHE,
    SANDBOX_HOME,
    SANDBOX_KIMI_BIN_DIR,
    SANDBOX_KIMI_CODE_HOME,
    SANDBOX_WORKSPACE,
    SandboxConfig,
)

# Top-level system runtime trees mounted read-only when they are real dirs.
_SYSTEM_RO_DIRS = ("/usr", "/lib", "/lib32", "/lib64", "/libx32", "/bin", "/sbin")


def _system_mount_args() -> list[str]:
    """Build read-only mounts for system runtime, symlink-aware.

    For each entry: if it is a symlink on the host (merged-/usr layout), recreate
    the symlink inside the sandbox; if it is a real directory, ro-bind it; if it
    is missing, skip it.
    """
    args: list[str] = []
    for d in _SYSTEM_RO_DIRS:
        p = Path(d)
        if p.is_symlink():
            # e.g. /bin -> usr/bin ; preserve the (relative) link target.
            target = os.readlink(d)
            args += ["--symlink", target, d]
        elif p.is_dir():
            args += ["--ro-bind", d, d]
        # missing -> skip
    return args


def _etc_mount_args() -> list[str]:
    """Build the minimal, read-only ``/etc`` (design 12.3, 28.11).

    A tmpfs at /etc gives an empty writable root; we ro-bind only the allowlisted
    files/dirs that actually exist, then remount the whole /etc read-only so
    nothing under it (including new paths) can be created.
    """
    args: list[str] = ["--tmpfs", "/etc"]
    for f in ETC_MIN_FILES:
        # ro-bind-try: skip silently if the source does not exist on this host.
        args += ["--ro-bind-try", f, f]
    for d in ETC_MIN_DIRS:
        args += ["--ro-bind-try", d, d]
    args += ["--remount-ro", "/etc"]
    return args


def _bind_args(
    *,
    source: str,
    dest: str,
    writable: bool,
    path_fds: dict[str, int] | None,
) -> list[str]:
    """Emit a single bind, fd-pinned when an fd is available for ``source``.

    If ``path_fds`` carries an fd for ``source`` we use ``--bind-fd`` /
    ``--ro-bind-fd`` (pinning the resolved inode); otherwise we fall back to the
    plain path-based ``--bind`` / ``--ro-bind``.
    """
    fd = path_fds.get(source) if path_fds else None
    if fd is not None:
        flag = "--bind-fd" if writable else "--ro-bind-fd"
        return [flag, str(fd), dest]
    flag = "--bind" if writable else "--ro-bind"
    return [flag, source, dest]


def build_bwrap_command(
    config: SandboxConfig,
    *,
    bwrap_path: str | os.PathLike[str] = "bwrap",
    seccomp_fd: int | None = None,
    path_fds: dict[str, int] | None = None,
) -> list[str]:
    """Return the full ``bwrap`` argv for ``config`` (no execution).

    The returned list is suitable for ``subprocess.run(cmd)`` directly. When
    ``seccomp_fd`` is given, ``--seccomp <fd>`` is appended; the caller is
    responsible for keeping that fd open and inheritable across the exec
    (``subprocess.run(..., pass_fds=(seccomp_fd,))``).

    When ``path_fds`` is given (mapping a host source path string -> an already
    open ``O_PATH`` fd), the corresponding bind uses ``--bind-fd`` /
    ``--ro-bind-fd`` instead of ``--bind`` / ``--ro-bind``. This pins each bind
    to the exact inode that was resolved and validated, closing the TOCTOU
    window where the path could be swapped for a symlink between validation and
    mount. The caller owns those fds (open, ``set_inheritable(True)``, pass via
    ``pass_fds=``, and close after the run). Sources absent from ``path_fds``
    fall back to path-based binds (e.g. ``--dry-run``, which prints readable
    paths rather than fd numbers).
    """
    cmd: list[str] = [os.fspath(bwrap_path)]

    # --- namespaces & lifetime (design 12.1) ---
    # We deliberately do NOT pass --new-session. It calls setsid() and detaches
    # the controlling terminal, which bubblewrap's own docs warn "breaks some
    # programs" — interactive TUIs in particular. Design §12.4 prioritises
    # inheriting the current terminal.
    #
    # The TIOCSTI terminal-injection residual that --new-session would also
    # close is instead handled by an opt-in seccomp filter (see seccomp_fd
    # below and seccomp.py): it blocks the TIOCSTI/TIOCLINUX ioctls without
    # detaching the terminal, so the TUI keeps working. When no filter is
    # installed (unsupported arch or --no-seccomp) the residual remains, and is
    # documented honestly in the README. Many modern kernels also restrict
    # TIOCSTI by default (dev.tty.legacy_tiocsti_restrict).
    cmd += [
        "--unshare-pid",
        "--unshare-ipc",
        "--unshare-uts",
        "--die-with-parent",
    ]
    # Network isolation is opt-in (v2 §33.4); v1 default keeps the host network.
    if config.no_network:
        cmd += ["--unshare-net"]
    # A stable hostname avoids leaking the host name via --unshare-uts default.
    cmd += ["--hostname", "kimi-sandbox"]

    # --- core virtual filesystems ---
    cmd += [
        "--proc", "/proc",
        "--dev", "/dev",
        "--tmpfs", "/tmp",
        "--tmpfs", "/run",
        "--tmpfs", "/home",
        "--dir", SANDBOX_HOME,
    ]

    # --- system runtime (read-only, symlink-aware) ---
    cmd += _system_mount_args()

    # --- sandbox bin dir for the kimi binary ---
    cmd += ["--dir", "/sandbox", "--dir", SANDBOX_KIMI_BIN_DIR]

    # --- minimal read-only /etc ---
    cmd += _etc_mount_args()

    # --- project (rw or ro) and kimi profile home (rw) ---
    # read-only mode (§33.2) mounts /workspace with --ro-bind so the project
    # tree cannot be modified; the profile home stays writable for Kimi state.
    # Each host source is fd-pinned when ``path_fds`` provides an fd (closing the
    # validation->mount TOCTOU window); see _bind_args.
    cmd += _bind_args(
        source=str(config.project_dir),
        dest=SANDBOX_WORKSPACE,
        writable=config.mode != MODE_READ_ONLY,
        path_fds=path_fds,
    )
    cmd += _bind_args(
        source=str(config.kimi_code_home),
        dest=SANDBOX_KIMI_CODE_HOME,
        writable=True,
        path_fds=path_fds,
    )

    # --- optional persistent cache (design 31.4) ---
    if config.cache_dir is not None:
        cmd += _bind_args(
            source=str(config.cache_dir),
            dest=SANDBOX_CACHE,
            writable=True,
            path_fds=path_fds,
        )

    # --- extra user mounts (§33.3) ---
    for mount in config.extra_mounts:
        cmd += _bind_args(
            source=str(mount.source),
            dest=mount.target,
            writable=mount.writable,
            path_fds=path_fds,
        )

    # --- kimi binary (read-only single file) ---
    cmd += _bind_args(
        source=str(config.kimi_path),
        dest=config.sandbox_kimi_target,
        writable=False,
        path_fds=path_fds,
    )

    # --- environment (clear then explicit allowlist; design 21) ---
    cmd += ["--clearenv"]
    for key in sorted(config.env):
        cmd += ["--setenv", key, config.env[key]]

    # --- working directory ---
    cmd += ["--chdir", SANDBOX_WORKSPACE]

    # --- seccomp filter (v2 §33.1), if the launcher prepared one ---
    if seccomp_fd is not None:
        cmd += ["--seccomp", str(seccomp_fd)]

    # --- separator then the in-sandbox command ---
    cmd += ["--"]
    cmd += list(config.inner_command)
    return cmd


__all__ = ["build_bwrap_command"]
