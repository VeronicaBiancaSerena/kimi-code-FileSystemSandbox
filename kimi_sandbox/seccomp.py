"""Minimal seccomp-BPF filter to block terminal-injection ioctls.

v1 left a documented residual: without ``--new-session`` (which would break the
TUI) a sandboxed process could call the ``TIOCSTI`` ioctl to push keystrokes
into the host's controlling terminal, to be run un-sandboxed after Kimi exits
(CVE-2017-5226). bubblewrap can install a seccomp filter from a compiled cBPF
program passed on a file descriptor (``--seccomp FD``); this module builds that
program with the standard library only — no libseccomp dependency.

The filter denies ``ioctl(_, TIOCSTI, _)`` and ``ioctl(_, TIOCLINUX, _)`` with
``EPERM`` and allows everything else.

**Multi-ABI hardening.** A classic seccomp pitfall is to check only the native
``seccomp_data.arch`` and *allow* every other architecture: on an x86_64 kernel
with IA32 emulation a process can issue ``ioctl`` through the i386 ABI
(``int 0x80``) or the x32 ABI, whose syscall numbers differ from x86_64's, and
sail straight past a single-arch filter. To close that, the filter enumerates
**every** ABI the kernel will accept for the detected machine and blocks the
terminal ioctls on each:

* ``x86_64``  -> x86_64 (nr 16), x32 (nr ``0x40000000|16``), and i386 (nr 54).
* ``aarch64`` -> aarch64 (nr 29) and 32-bit ARM compat (nr 54).

Any ``seccomp_data.arch`` value that is *not* one of the enumerated ABIs for the
machine is treated as anomalous (it cannot occur on a correctly-detected host)
and the syscall is denied with ``EPERM`` rather than allowed — so there is no
foreign-ABI escape hatch. Genuinely unknown *machines* (neither x86_64 nor
aarch64) still degrade to "filter not installed" via :func:`is_supported_arch`,
keeping the v1 "degrade rather than refuse to run" behaviour.

cBPF wire format (``struct sock_filter``): ``{ u16 code; u8 jt; u8 jf; u32 k }``
=> ``struct.pack("<HBBI", ...)``. The classic seccomp data layout matches
``struct seccomp_data``: syscall nr at byte offset 0, arch at offset 4, and the
syscall arguments as 64-bit little-endian values starting at offset 16 (so the
low 32 bits of ``args[1]`` — the ioctl request — sit at offset 24). The args are
always 64-bit slots regardless of ABI, so the shared request-comparison block
works for every ABI above.
"""

from __future__ import annotations

import os
import platform
import struct
import tempfile

# --- BPF opcode constants (linux/bpf_common.h) ---
_BPF_LD = 0x00
_BPF_W = 0x00
_BPF_ABS = 0x20
_BPF_JMP = 0x05
_BPF_JEQ = 0x10
_BPF_RET = 0x06
_BPF_K = 0x00

_LD_ABS = _BPF_LD | _BPF_W | _BPF_ABS  # 0x20
_JEQ = _BPF_JMP | _BPF_JEQ | _BPF_K    # 0x15
_RET = _BPF_RET | _BPF_K               # 0x06

# --- seccomp return actions (linux/seccomp.h) ---
_SECCOMP_RET_ALLOW = 0x7FFF0000
_SECCOMP_RET_ERRNO = 0x00050000
_EPERM = 1

# --- seccomp_data field offsets ---
_OFF_NR = 0
_OFF_ARCH = 4
# args start at offset 16; each arg is 8 bytes little-endian. args[1] low word:
_OFF_ARG1_LO = 16 + 8 * 1  # = 24

# --- AUDIT_ARCH values (linux/audit.h) ---
_AUDIT_ARCH_X86_64 = 0xC000003E
_AUDIT_ARCH_I386 = 0x40000003
_AUDIT_ARCH_AARCH64 = 0xC00000B7
_AUDIT_ARCH_ARM = 0x40000028

# --- ioctl request numbers (asm-generic/ioctls.h) ---
# TIOCSTI/TIOCLINUX share these values across the architectures we support.
_TIOCSTI = 0x5412
_TIOCLINUX = 0x541C

# --- the x32 syscall-number bit (asm/unistd.h, __X32_SYSCALL_BIT) ---
_X32_SYSCALL_BIT = 0x40000000

# --- ioctl syscall numbers, per ABI ---
_NR_IOCTL_X86_64 = 16
_NR_IOCTL_X32 = _X32_SYSCALL_BIT | _NR_IOCTL_X86_64
_NR_IOCTL_I386 = 54
_NR_IOCTL_AARCH64 = 29
_NR_IOCTL_ARM = 54

# Native AUDIT_ARCH -> ioctl syscall number, kept for direct lookups/tests.
_NR_IOCTL = {
    _AUDIT_ARCH_X86_64: _NR_IOCTL_X86_64,
    _AUDIT_ARCH_AARCH64: _NR_IOCTL_AARCH64,
}

# Native AUDIT_ARCH -> the full set of (arch, (ioctl_nrs...)) ABIs the kernel may
# accept for that machine. The filter blocks the terminal ioctls on each ABI and
# denies any arch outside this set. See the module docstring for the rationale.
_ARCH_FAMILY: dict[int, tuple[tuple[int, tuple[int, ...]], ...]] = {
    _AUDIT_ARCH_X86_64: (
        (_AUDIT_ARCH_X86_64, (_NR_IOCTL_X86_64, _NR_IOCTL_X32)),
        (_AUDIT_ARCH_I386, (_NR_IOCTL_I386,)),
    ),
    _AUDIT_ARCH_AARCH64: (
        (_AUDIT_ARCH_AARCH64, (_NR_IOCTL_AARCH64,)),
        (_AUDIT_ARCH_ARM, (_NR_IOCTL_ARM,)),
    ),
}

# Map platform.machine() -> native AUDIT_ARCH.
_MACHINE_TO_ARCH = {
    "x86_64": _AUDIT_ARCH_X86_64,
    "amd64": _AUDIT_ARCH_X86_64,
    "aarch64": _AUDIT_ARCH_AARCH64,
    "arm64": _AUDIT_ARCH_AARCH64,
}


def _stmt(code: int, k: int) -> bytes:
    return struct.pack("<HBBI", code, 0, 0, k)


def _jump(code: int, k: int, jt: int, jf: int) -> bytes:
    return struct.pack("<HBBI", code, jt, jf, k)


def current_audit_arch() -> int | None:
    """Return the native AUDIT_ARCH for the running machine, or None if unknown."""
    return _MACHINE_TO_ARCH.get(platform.machine().lower())


def is_supported_arch() -> bool:
    """True if we can build a TIOCSTI filter for the current machine."""
    arch = current_audit_arch()
    return arch is not None and arch in _ARCH_FAMILY


def build_tiocsti_filter(arch: int | None = None) -> bytes:
    """Build a compiled cBPF program blocking TIOCSTI/TIOCLINUX for ``arch``.

    ``arch`` is the *native* AUDIT_ARCH of the host; the program it produces
    additionally covers that machine's 32-bit/x32 compat ABIs and denies any
    other architecture (see module docstring). Raises :class:`ValueError` if the
    machine is unsupported (callers should check :func:`is_supported_arch`
    first and skip gracefully).
    """
    if arch is None:
        arch = current_audit_arch()
    family = _ARCH_FAMILY.get(arch) if arch is not None else None
    if family is None:
        raise ValueError("unsupported architecture for seccomp filter")

    # Build the instruction list with symbolic jump labels, then resolve the
    # (forward-only) relative offsets in a second pass. This keeps the program
    # correct as the per-ABI structure grows, instead of hand-counting offsets.
    #
    # Layout:
    #   load arch
    #   per ABI i: JEQ arch_i -> abi_i ; (last ABI's miss -> errno = deny)
    #   abi_i: load nr; JEQ each ioctl nr -> argcheck ; (miss -> allow)
    #   argcheck: load args[1] low; JEQ TIOCSTI/TIOCLINUX -> errno ; else allow
    #   allow:  RET ALLOW
    #   errno:  RET ERRNO(EPERM)   (also the deny target for foreign arches)
    code: list[tuple] = []
    labels: dict[str, int] = {}

    def add(instr: tuple, label: str | None = None) -> None:
        if label is not None:
            labels[label] = len(code)
        code.append(instr)

    n = len(family)

    # arch dispatch
    add(("stmt", _LD_ABS, _OFF_ARCH))
    for i in range(n):
        arch_val = family[i][0]
        # A non-matching last ABI falls through to the EPERM (deny) leaf, so no
        # unrecognised architecture is ever allowed.
        jf = "errno" if i == n - 1 else f"archchk{i + 1}"
        add(("jeq", arch_val, f"abi{i}", jf), label=f"archchk{i}")

    # per-arch ioctl-number blocks
    for i in range(n):
        nrs = family[i][1]
        add(("stmt", _LD_ABS, _OFF_NR), label=f"abi{i}")
        m = len(nrs)
        for j in range(m):
            jf = "allow" if j == m - 1 else f"nrchk{i}_{j + 1}"
            add(("jeq", nrs[j], "argcheck", jf), label=f"nrchk{i}_{j}")

    # shared ioctl-request check (args[1] low 32 bits)
    add(("stmt", _LD_ABS, _OFF_ARG1_LO), label="argcheck")
    add(("jeq", _TIOCSTI, "errno", "stinext"))
    add(("jeq", _TIOCLINUX, "errno", "allow"), label="stinext")

    # terminals
    add(("stmt", _RET, _SECCOMP_RET_ALLOW), label="allow")
    add(("stmt", _RET, _SECCOMP_RET_ERRNO | _EPERM), label="errno")

    out: list[bytes] = []
    for idx, instr in enumerate(code):
        if instr[0] == "stmt":
            _, op, k = instr
            out.append(_stmt(op, k))
        else:
            _, k, jt_label, jf_label = instr
            jt = labels[jt_label] - idx - 1
            jf = labels[jf_label] - idx - 1
            if not (0 <= jt <= 255 and 0 <= jf <= 255):  # pragma: no cover
                raise ValueError("seccomp jump offset out of range")
            out.append(_jump(_JEQ, k, jt, jf))
    return b"".join(out)


def open_filter_fd() -> int:
    """Write the TIOCSTI filter to a temp file and return an inheritable fd.

    The temp file is unlinked immediately after opening, so the program lives
    only as long as the open fd. The caller is responsible for closing the fd
    after the child process has started (or letting process exit reap it) and
    must pass it through ``subprocess.run(..., pass_fds=(fd,))``.
    """
    program = build_tiocsti_filter()
    fd_tmp, path = tempfile.mkstemp(prefix="ks-seccomp-")
    try:
        os.write(fd_tmp, program)
    finally:
        os.close(fd_tmp)
    fd = os.open(path, os.O_RDONLY)
    os.unlink(path)
    os.set_inheritable(fd, True)
    return fd


__all__ = [
    "is_supported_arch",
    "current_audit_arch",
    "build_tiocsti_filter",
    "open_filter_fd",
]
