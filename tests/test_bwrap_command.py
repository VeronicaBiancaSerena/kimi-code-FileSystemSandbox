"""Tests for the bwrap command builder and env allowlist (design 29)."""

from __future__ import annotations

from pathlib import Path

import pytest

from kimi_sandbox.bwrap import build_bwrap_command
from kimi_sandbox.cli import build_env_allowlist, build_inner_command, split_kimi_args
from kimi_sandbox.config import SandboxConfig


def make_config(tmp_path: Path, inner=None, env=None) -> SandboxConfig:
    project = tmp_path / "proj"
    project.mkdir(exist_ok=True)
    kimi_home = tmp_path / "home"
    kimi_home.mkdir(exist_ok=True)
    kimi = tmp_path / "kimi"
    kimi.write_text("x")
    return SandboxConfig(
        project_dir=project.resolve(),
        kimi_code_home=kimi_home.resolve(),
        kimi_path=kimi.resolve(),
        inner_command=inner or ["/sandbox/bin/kimi"],
        env=env or {"HOME": "/home/sandbox", "KIMI_SANDBOX": "1"},
    )


def _pairs(cmd: list[str], flag: str) -> list[tuple[str, str]]:
    """Return (src, dest) pairs following each occurrence of ``flag``."""
    out = []
    for i, tok in enumerate(cmd):
        if tok == flag:
            out.append((cmd[i + 1], cmd[i + 2]))
    return out


# --- required flags ------------------------------------------------------

def test_contains_clearenv(tmp_path):
    cmd = build_bwrap_command(make_config(tmp_path))
    assert "--clearenv" in cmd


def test_contains_workspace_bind(tmp_path):
    cfg = make_config(tmp_path)
    cmd = build_bwrap_command(cfg)
    binds = _pairs(cmd, "--bind")
    assert (str(cfg.project_dir), "/workspace") in binds


def test_contains_kimi_home_bind(tmp_path):
    cfg = make_config(tmp_path)
    cmd = build_bwrap_command(cfg)
    binds = _pairs(cmd, "--bind")
    assert (str(cfg.kimi_code_home), "/kimi-code-home") in binds


def test_contains_namespace_flags(tmp_path):
    cmd = build_bwrap_command(make_config(tmp_path))
    assert "--unshare-pid" in cmd
    assert "--unshare-ipc" in cmd
    assert "--unshare-uts" in cmd
    assert "--die-with-parent" in cmd


def test_no_unshare_net(tmp_path):
    cmd = build_bwrap_command(make_config(tmp_path))
    assert "--unshare-net" not in cmd


def test_no_dev_bind_root(tmp_path):
    cmd = build_bwrap_command(make_config(tmp_path))
    # No --dev-bind with source "/" and dest "/".
    for i, tok in enumerate(cmd):
        if tok == "--dev-bind" and cmd[i + 1 : i + 3] == ["/", "/"]:
            pytest.fail("--dev-bind / / present")


def test_no_whole_etc_bind(tmp_path):
    cmd = build_bwrap_command(make_config(tmp_path))
    # /etc must be a tmpfs, not ro-bind /etc /etc.
    for i, tok in enumerate(cmd):
        if tok in ("--ro-bind", "--bind", "--ro-bind-try") and cmd[i + 1 : i + 3] == ["/etc", "/etc"]:
            pytest.fail("whole /etc bind present")
    assert ("--tmpfs", "/etc") in [(cmd[i], cmd[i + 1]) for i in range(len(cmd) - 1)]


def test_etc_is_remounted_ro(tmp_path):
    cmd = build_bwrap_command(make_config(tmp_path))
    assert "--remount-ro" in cmd
    idx = cmd.index("--remount-ro")
    assert cmd[idx + 1] == "/etc"


def test_etc_minimal_files_present(tmp_path):
    cmd = build_bwrap_command(make_config(tmp_path))
    tries = _pairs(cmd, "--ro-bind-try")
    srcs = {s for s, _ in tries}
    assert "/etc/resolv.conf" in srcs
    assert "/etc/hosts" in srcs


def test_kimi_binary_ro_bound(tmp_path):
    cfg = make_config(tmp_path)
    cmd = build_bwrap_command(cfg)
    robinds = _pairs(cmd, "--ro-bind")
    assert (str(cfg.kimi_path), "/sandbox/bin/kimi") in robinds


def test_chdir_workspace(tmp_path):
    cmd = build_bwrap_command(make_config(tmp_path))
    idx = cmd.index("--chdir")
    assert cmd[idx + 1] == "/workspace"


def test_inner_command_after_separator(tmp_path):
    cfg = make_config(tmp_path, inner=["/sandbox/bin/kimi", "--version"])
    cmd = build_bwrap_command(cfg)
    sep = cmd.index("--")
    assert cmd[sep + 1 :] == ["/sandbox/bin/kimi", "--version"]


def test_bwrap_path_is_argv0(tmp_path):
    cmd = build_bwrap_command(make_config(tmp_path), bwrap_path="/usr/bin/bwrap")
    assert cmd[0] == "/usr/bin/bwrap"


def test_env_setenv_pairs_sorted(tmp_path):
    cfg = make_config(tmp_path, env={"B": "2", "A": "1"})
    cmd = build_bwrap_command(cfg)
    # Collect --setenv NAME VALUE triples.
    names = [cmd[i + 1] for i, t in enumerate(cmd) if t == "--setenv"]
    assert names == sorted(names)
    assert "A" in names and "B" in names


# --- env allowlist (cli) -------------------------------------------------

def test_build_env_sets_sandbox_markers():
    env = build_env_allowlist()
    assert env["KIMI_SANDBOX"] == "1"
    assert env["KIMI_SANDBOX_MODE"] == "workspace-write"
    assert env["KIMI_SANDBOX_WORKSPACE"] == "/workspace"
    assert env["HOME"] == "/home/sandbox"
    assert env["KIMI_CODE_HOME"] == "/kimi-code-home"


def test_build_env_does_not_inherit_secrets(monkeypatch):
    for var in ("KIMI_API_KEY", "AWS_SECRET_ACCESS_KEY", "GITHUB_TOKEN",
                "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "SSH_AUTH_SOCK"):
        monkeypatch.setenv(var, "secret-value")
    env = build_env_allowlist()
    for var in ("KIMI_API_KEY", "AWS_SECRET_ACCESS_KEY", "GITHUB_TOKEN",
                "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "SSH_AUTH_SOCK"):
        assert var not in env


def test_build_env_forwards_term(monkeypatch):
    monkeypatch.setenv("TERM", "xterm-256color")
    env = build_env_allowlist()
    assert env["TERM"] == "xterm-256color"


def test_build_env_does_not_inherit_host_path(monkeypatch):
    monkeypatch.setenv("PATH", "/host/evil/bin")
    env = build_env_allowlist()
    assert env["PATH"] == "/sandbox/bin:/usr/local/bin:/usr/bin:/bin"


# --- inner command / arg splitting --------------------------------------

def test_split_kimi_args():
    launcher, kimi = split_kimi_args([".", "--profile", "x", "--", "--version"])
    assert launcher == [".", "--profile", "x"]
    assert kimi == ["--version"]


def test_split_kimi_args_no_separator():
    launcher, kimi = split_kimi_args([".", "--dry-run"])
    assert launcher == [".", "--dry-run"]
    assert kimi == []


class _Args:
    def __init__(self, exec_command=None):
        self.exec_command = exec_command


def test_build_inner_command_exec():
    # The shell is host-dependent (bash preferred, /bin/sh fallback — C1), but
    # the flags+command tail are fixed and the shell path must be absolute.
    cmd = build_inner_command(_Args(exec_command="pwd"), [])
    assert len(cmd) == 3
    assert cmd[0].startswith("/")
    assert cmd[0].rsplit("/", 1)[-1] in {"bash", "sh"}
    assert cmd[1] in {"-lc", "-c"}
    assert cmd[2] == "pwd"


def test_build_inner_command_kimi_with_args():
    cmd = build_inner_command(_Args(), ["-m", "model"])
    assert cmd == ["/sandbox/bin/kimi", "-m", "model"]
