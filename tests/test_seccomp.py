"""Tests for the pure-stdlib seccomp cBPF builder (v2 §33.1).

Beyond shape checks, this module includes a tiny classic-BPF interpreter so the
*security behaviour* of the filter — including the multi-ABI hardening that
closes the i386/x32/arm bypass — is proven rather than merely inspected.
"""

from __future__ import annotations

import struct

import pytest

from kimi_sandbox import seccomp


def _instructions(program: bytes) -> list[tuple[int, int, int, int]]:
    assert len(program) % 8 == 0
    out = []
    for i in range(0, len(program), 8):
        code, jt, jf, k = struct.unpack("<HBBI", program[i : i + 8])
        out.append((code, jt, jf, k))
    return out


def _simulate(program: bytes, *, arch: int, nr: int, arg1: int) -> int:
    """Execute the cBPF program against a synthetic ``seccomp_data`` and return
    the resulting ``SECCOMP_RET_*`` action value.

    Only the opcodes our generator emits (LD|W|ABS, JEQ|K, RET|K) are handled.
    ``seccomp_data`` is ``{ nr:u32, arch:u32, ip:u64, args[6]:u64 }`` = 64 bytes;
    ``arg1`` is placed in ``args[1]`` (offset 24 for its low word).
    """
    args = [0, arg1, 0, 0, 0, 0]
    data = struct.pack("<IIQ6Q", nr & 0xFFFFFFFF, arch & 0xFFFFFFFF, 0, *args)
    instrs = _instructions(program)
    acc = 0
    pc = 0
    while True:
        code, jt, jf, k = instrs[pc]
        if code == seccomp._LD_ABS:
            (acc,) = struct.unpack_from("<I", data, k)
            pc += 1
        elif code == seccomp._JEQ:
            pc += 1 + (jt if acc == k else jf)
        elif code == seccomp._RET:
            return k
        else:  # pragma: no cover - defensive
            raise AssertionError(f"unexpected opcode {code:#x}")


_ALLOW = seccomp._SECCOMP_RET_ALLOW
_DENY = seccomp._SECCOMP_RET_ERRNO | seccomp._EPERM


# --- shape ---------------------------------------------------------------

def test_x86_64_filter_shape():
    prog = seccomp.build_tiocsti_filter(seccomp._AUDIT_ARCH_X86_64)
    instrs = _instructions(prog)
    # First instruction loads the arch field (BPF_LD|W|ABS at offset 4).
    code, _, _, k = instrs[0]
    assert code == seccomp._LD_ABS
    assert k == seccomp._OFF_ARCH
    ks = [k for (_, _, _, k) in instrs]
    # Both x86_64 and the i386 compat ABI are dispatched on.
    assert seccomp._AUDIT_ARCH_X86_64 in ks
    assert seccomp._AUDIT_ARCH_I386 in ks
    # x86_64, x32 and i386 ioctl numbers are all present.
    assert seccomp._NR_IOCTL_X86_64 in ks
    assert seccomp._NR_IOCTL_X32 in ks
    assert seccomp._NR_IOCTL_I386 in ks


def test_filter_blocks_tiocsti_and_tioclinux_constants():
    prog = seccomp.build_tiocsti_filter(seccomp._AUDIT_ARCH_X86_64)
    instrs = _instructions(prog)
    ks = [k for (_, _, _, k) in instrs]
    assert seccomp._TIOCSTI in ks
    assert seccomp._TIOCLINUX in ks
    # ALLOW and ERRNO(EPERM) return values present.
    assert _ALLOW in ks
    assert _DENY in ks


def test_filter_checks_ioctl_nr_for_arch():
    prog = seccomp.build_tiocsti_filter(seccomp._AUDIT_ARCH_AARCH64)
    instrs = _instructions(prog)
    ks = [k for (_, _, _, k) in instrs]
    assert seccomp._NR_IOCTL_AARCH64 in ks  # 29 on aarch64
    # 32-bit ARM compat ABI is covered too.
    assert seccomp._AUDIT_ARCH_ARM in ks
    assert seccomp._NR_IOCTL_ARM in ks


def test_unsupported_arch_raises():
    with pytest.raises(ValueError):
        seccomp.build_tiocsti_filter(0xDEADBEEF)


# --- behaviour: native x86_64 --------------------------------------------

def test_native_x86_64_blocks_tiocsti():
    prog = seccomp.build_tiocsti_filter(seccomp._AUDIT_ARCH_X86_64)
    assert _simulate(prog, arch=seccomp._AUDIT_ARCH_X86_64,
                     nr=seccomp._NR_IOCTL_X86_64, arg1=seccomp._TIOCSTI) == _DENY
    assert _simulate(prog, arch=seccomp._AUDIT_ARCH_X86_64,
                     nr=seccomp._NR_IOCTL_X86_64, arg1=seccomp._TIOCLINUX) == _DENY


def test_native_x86_64_allows_other_ioctls():
    prog = seccomp.build_tiocsti_filter(seccomp._AUDIT_ARCH_X86_64)
    # TIOCGWINSZ (0x5413) and FIONREAD (0x541B) must pass.
    assert _simulate(prog, arch=seccomp._AUDIT_ARCH_X86_64,
                     nr=seccomp._NR_IOCTL_X86_64, arg1=0x5413) == _ALLOW
    # Non-ioctl syscalls pass too (e.g. nr=1 write).
    assert _simulate(prog, arch=seccomp._AUDIT_ARCH_X86_64,
                     nr=1, arg1=seccomp._TIOCSTI) == _ALLOW


# --- behaviour: compat ABIs (the bypass that v1 left open) ---------------

def test_i386_compat_tiocsti_is_blocked():
    # i386 ABI via int 0x80 on an x86_64 kernel: nr 54, arch I386. The old
    # single-arch filter allowed this outright; it must now be denied.
    prog = seccomp.build_tiocsti_filter(seccomp._AUDIT_ARCH_X86_64)
    assert _simulate(prog, arch=seccomp._AUDIT_ARCH_I386,
                     nr=seccomp._NR_IOCTL_I386, arg1=seccomp._TIOCSTI) == _DENY
    # ...while other i386 ioctls still pass.
    assert _simulate(prog, arch=seccomp._AUDIT_ARCH_I386,
                     nr=seccomp._NR_IOCTL_I386, arg1=0x5413) == _ALLOW


def test_x32_compat_tiocsti_is_blocked():
    prog = seccomp.build_tiocsti_filter(seccomp._AUDIT_ARCH_X86_64)
    assert _simulate(prog, arch=seccomp._AUDIT_ARCH_X86_64,
                     nr=seccomp._NR_IOCTL_X32, arg1=seccomp._TIOCSTI) == _DENY


def test_arm_compat_tiocsti_is_blocked():
    prog = seccomp.build_tiocsti_filter(seccomp._AUDIT_ARCH_AARCH64)
    assert _simulate(prog, arch=seccomp._AUDIT_ARCH_ARM,
                     nr=seccomp._NR_IOCTL_ARM, arg1=seccomp._TIOCSTI) == _DENY


# --- behaviour: foreign / unrecognised arch is denied, never allowed -----

def test_foreign_arch_is_denied_not_allowed():
    # An arch that is not part of the x86_64 family (here: ARM) must be denied,
    # not allowed — this is what closes the "switch ABI to escape" bypass.
    prog = seccomp.build_tiocsti_filter(seccomp._AUDIT_ARCH_X86_64)
    assert _simulate(prog, arch=seccomp._AUDIT_ARCH_ARM,
                     nr=seccomp._NR_IOCTL_ARM, arg1=seccomp._TIOCSTI) == _DENY
    # Even a benign syscall on the foreign arch is denied (no escape hatch).
    assert _simulate(prog, arch=0x4000DEAD, nr=1, arg1=0) == _DENY


# --- fd plumbing ---------------------------------------------------------

def test_open_filter_fd_roundtrip():
    # Only run where the current arch is supported; otherwise skip.
    if not seccomp.is_supported_arch():
        pytest.skip("seccomp filter not supported on this arch")
    import os

    expected = len(seccomp.build_tiocsti_filter())
    fd = seccomp.open_filter_fd()
    try:
        data = os.pread(fd, 4096, 0)
        assert len(data) == expected
        assert len(data) % 8 == 0
        assert os.get_inheritable(fd) is True
    finally:
        os.close(fd)


def test_current_audit_arch_known(monkeypatch):
    monkeypatch.setattr(seccomp.platform, "machine", lambda: "x86_64")
    assert seccomp.current_audit_arch() == seccomp._AUDIT_ARCH_X86_64
    assert seccomp.is_supported_arch() is True


def test_current_audit_arch_unknown(monkeypatch):
    monkeypatch.setattr(seccomp.platform, "machine", lambda: "riscv64")
    assert seccomp.current_audit_arch() is None
    assert seccomp.is_supported_arch() is False
