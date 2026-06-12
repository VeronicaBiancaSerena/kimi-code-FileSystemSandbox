"""Tests for v2 CLI: config file loading, precedence, systemd-run prefix."""

from __future__ import annotations

from pathlib import Path

import pytest

from kimi_sandbox import cli
from kimi_sandbox import seccomp
from kimi_sandbox.config import ResourceLimits
from kimi_sandbox.errors import SandboxError


# --- load_config_file ----------------------------------------------------

def test_load_config_missing_default_ok(tmp_path):
    # Non-explicit (default path) missing file -> empty dict, no error.
    assert cli.load_config_file(tmp_path / "nope.toml", explicit=False) == {}


def test_load_config_missing_explicit_errors(tmp_path):
    with pytest.raises(SandboxError):
        cli.load_config_file(tmp_path / "nope.toml", explicit=True)


def test_load_config_parses_known_keys(tmp_path):
    cfg = tmp_path / "c.toml"
    cfg.write_text(
        'no_network = true\n'
        'read_only = true\n'
        'profile = "work"\n'
        'memory_max = "2G"\n'
        'pids_max = 64\n'
        'ro_mounts = ["/a:/opt/a"]\n'
    )
    data = cli.load_config_file(cfg, explicit=True)
    assert data["no_network"] is True
    assert data["read_only"] is True
    assert data["profile"] == "work"
    assert data["memory_max"] == "2G"
    assert data["pids_max"] == 64
    assert data["ro_mounts"] == ["/a:/opt/a"]


def test_load_config_unknown_key_warns_not_fatal(tmp_path, capsys):
    cfg = tmp_path / "c.toml"
    cfg.write_text('totally_unknown = 1\n')
    data = cli.load_config_file(cfg, explicit=True)
    assert "totally_unknown" not in cli._CONFIG_KNOWN_KEYS
    err = capsys.readouterr().err
    assert "ignoring unknown config key" in err
    # Unknown key is still present in returned dict but unused.
    assert data["totally_unknown"] == 1


def test_load_config_wrong_type_errors(tmp_path):
    cfg = tmp_path / "c.toml"
    cfg.write_text('pids_max = "not-int"\n')
    with pytest.raises(SandboxError):
        cli.load_config_file(cfg, explicit=True)


def test_load_config_bool_not_int(tmp_path):
    # TOML booleans must not be accepted where an int is required and v.v.
    cfg = tmp_path / "c.toml"
    cfg.write_text('pids_max = true\n')
    with pytest.raises(SandboxError):
        cli.load_config_file(cfg, explicit=True)


def test_load_config_bad_toml_errors(tmp_path):
    cfg = tmp_path / "c.toml"
    cfg.write_text('this is = = not toml\n')
    with pytest.raises(SandboxError):
        cli.load_config_file(cfg, explicit=True)


# --- _pick precedence ----------------------------------------------------

def test_pick_cli_wins():
    assert cli._pick("cli", {"k": "cfg"}, "k", "default") == "cli"


def test_pick_config_when_no_cli():
    assert cli._pick(None, {"k": "cfg"}, "k", "default") == "cfg"


def test_pick_default_when_neither():
    assert cli._pick(None, {}, "k", "default") == "default"


def test_pick_cli_false_is_not_none():
    # A real CLI value of 0 / "" should win over config (only None defers).
    assert cli._pick(0, {"k": "cfg"}, "k", "d") == 0


# --- build_systemd_run_prefix --------------------------------------------

def test_systemd_run_prefix_all_limits(tmp_path):
    sr = tmp_path / "systemd-run"
    sr.write_text("x")
    prefix = cli.build_systemd_run_prefix(
        sr, ResourceLimits(memory_max="2G", cpu_quota="150%", pids_max=512)
    )
    assert prefix[0] == str(sr)
    assert "--user" in prefix and "--scope" in prefix
    assert "MemoryMax=2G" in prefix
    assert "CPUQuota=150%" in prefix
    assert "TasksMax=512" in prefix
    assert prefix[-1] == "--"


def test_systemd_run_prefix_partial(tmp_path):
    sr = tmp_path / "systemd-run"
    sr.write_text("x")
    prefix = cli.build_systemd_run_prefix(sr, ResourceLimits(memory_max="1G"))
    assert "MemoryMax=1G" in prefix
    assert not any(p.startswith("CPUQuota") for p in prefix)
    assert not any(p.startswith("TasksMax") for p in prefix)


# --- env allowlist with mode/cache ---------------------------------------

def test_env_read_only_mode_marker():
    env = cli.build_env_allowlist(mode="read-only")
    assert env["KIMI_SANDBOX_MODE"] == "read-only"


def test_env_persistent_cache_points_at_cache():
    env = cli.build_env_allowlist(persistent_cache=True)
    assert env["XDG_CACHE_HOME"] == "/cache"


def test_env_default_cache_in_home():
    env = cli.build_env_allowlist()
    assert env["XDG_CACHE_HOME"] == "/home/sandbox/.cache"


# --- R1: boolean negators override a config-set True ---------------------

@pytest.mark.parametrize(
    "flag, attr",
    [
        ("--writable", "read_only"),
        ("--network", "no_network"),
        ("--seccomp", "no_seccomp"),
        ("--no-persistent-cache", "persistent_cache"),
    ],
)
def test_negator_flag_sets_explicit_false(flag, attr):
    # A negator must produce an explicit False (not None) so _pick can override
    # a config-set True back to False (R1).
    args = cli.build_parser().parse_args([".", flag])
    assert getattr(args, attr) is False


@pytest.mark.parametrize(
    "flag, attr",
    [
        ("--read-only", "read_only"),
        ("--no-network", "no_network"),
        ("--no-seccomp", "no_seccomp"),
        ("--persistent-cache", "persistent_cache"),
    ],
)
def test_positive_flag_sets_true(flag, attr):
    args = cli.build_parser().parse_args([".", flag])
    assert getattr(args, attr) is True


@pytest.mark.parametrize(
    "attr", ["read_only", "no_network", "no_seccomp", "persistent_cache"]
)
def test_bool_flag_unset_is_none(attr):
    # Unset -> None so config/default can apply; shared dest must keep None.
    args = cli.build_parser().parse_args(["."])
    assert getattr(args, attr) is None


@pytest.mark.parametrize(
    "flag, key",
    [
        ("--writable", "read_only"),
        ("--network", "no_network"),
        ("--seccomp", "no_seccomp"),
        ("--no-persistent-cache", "persistent_cache"),
    ],
)
def test_negator_overrides_config_true(flag, key):
    # End-to-end precedence: config sets True, CLI negator wins as False (R1).
    args = cli.build_parser().parse_args([".", flag])
    cfg = {key: True}
    assert cli._pick(getattr(args, key), cfg, key, False) is False


@pytest.mark.parametrize("key", ["read_only", "no_network", "no_seccomp"])
def test_config_true_wins_without_cli(key):
    args = cli.build_parser().parse_args(["."])
    cfg = {key: True}
    assert cli._pick(getattr(args, key), cfg, key, False) is True


# --- C1: --exec shell selection ------------------------------------------

class _ExecArgs:
    def __init__(self, exec_command=None):
        self.exec_command = exec_command


def test_inner_shell_is_absolute_known_shell():
    shell, flags = cli._inner_shell()
    assert shell.startswith("/")
    assert shell.rsplit("/", 1)[-1] in {"bash", "sh"}
    assert flags in {"-lc", "-c"}


def test_inner_command_prefers_bash_when_present(monkeypatch):
    monkeypatch.setattr(cli.os.path, "exists", lambda p: p == "/bin/bash")
    cmd = cli.build_inner_command(_ExecArgs(exec_command="id"), [])
    assert cmd == ["/bin/bash", "-lc", "id"]


def test_inner_command_falls_back_to_sh(monkeypatch):
    # No bash anywhere on the host -> POSIX /bin/sh -c (C1).
    monkeypatch.setattr(cli.os.path, "exists", lambda p: False)
    cmd = cli.build_inner_command(_ExecArgs(exec_command="id"), [])
    assert cmd == ["/bin/sh", "-c", "id"]


# --- R2: --debug mount plan mirrors limits / systemd-run / seccomp -------

def _plan_config(tmp_path, **overrides):
    from kimi_sandbox.config import SandboxConfig

    base = dict(
        project_dir=tmp_path / "proj",
        kimi_code_home=tmp_path / "home",
        kimi_path=tmp_path / "kimi",
        inner_command=["/sandbox/bin/kimi"],
        env={"HOME": "/home/sandbox"},
        state_root=tmp_path / "state",
        profile="default",
    )
    base.update(overrides)
    return SandboxConfig(**base)


def test_debug_plan_shows_limits_and_systemd(tmp_path, capsys):
    config = _plan_config(tmp_path)
    cli.print_mount_plan(
        config,
        kimi_path=tmp_path / "kimi",
        seccomp_active=True,
        limits=ResourceLimits(memory_max="2G", cpu_quota="150%", pids_max=512),
        systemd_run=Path("/usr/bin/systemd-run"),
    )
    err = capsys.readouterr().err
    assert "TIOCSTI filter active" in err
    assert "mem=2G" in err and "cpu=150%" in err and "pids=512" in err
    assert "/usr/bin/systemd-run" in err


def test_debug_plan_shows_seccomp_off_and_no_limits(tmp_path, capsys):
    config = _plan_config(tmp_path)
    cli.print_mount_plan(
        config,
        kimi_path=tmp_path / "kimi",
        seccomp_active=False,
        limits=ResourceLimits(),
        systemd_run=None,
    )
    err = capsys.readouterr().err
    assert "seccomp   : off" in err
    assert "limits    : none" in err


# --- seccomp fd passthrough across the systemd-run wrapper ---------------

def _make_fake_bin(path: Path, content: str = "#!/bin/sh\n") -> Path:
    path.write_text(content)
    path.chmod(0o755)
    return path


def test_seccomp_fd_passes_through_systemd_run(tmp_path, monkeypatch):
    """When resource limits are active the immediate child is ``systemd-run``,
    not ``bwrap``. Verify the seccomp filter fd is still emitted in the bwrap
    portion of the argv AND handed to ``subprocess.run(pass_fds=...)`` so it is
    inherited across the ``systemd-run --user --scope`` wrapper, and that the fd
    is open + inheritable at launch time and closed afterwards.
    """
    if not seccomp.is_supported_arch():
        pytest.skip("seccomp filter not supported on this arch")

    import os

    project = tmp_path / "proj"
    project.mkdir()
    state = tmp_path / "state"
    kimi = _make_fake_bin(tmp_path / "kimi", "\x7fELF")
    bwrap = _make_fake_bin(tmp_path / "bwrap")
    fake_systemd_run = _make_fake_bin(tmp_path / "systemd-run")

    # systemd-run resolution is mocked so the test does not depend on a real
    # user systemd being present in the environment.
    monkeypatch.setattr(cli, "resolve_systemd_run", lambda *a, **k: fake_systemd_run)

    captured: dict = {}

    class _CP:
        returncode = 0

    def fake_run(command, pass_fds=()):
        captured["command"] = command
        captured["pass_fds"] = pass_fds
        # Every passed fd must be open and inheritable at the moment of launch.
        captured["fstat_ok"] = all(os.fstat(fd) is not None for fd in pass_fds)
        captured["inheritable"] = all(os.get_inheritable(fd) for fd in pass_fds)
        return _CP()

    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    rc = cli.main(
        [
            str(project),
            "--kimi", str(kimi),
            "--bwrap", str(bwrap),
            "--state-root", str(state),
            "--memory-max", "256M",
        ]
    )
    assert rc == 0

    cmd = captured["command"]
    # systemd-run wraps the whole bwrap command.
    assert cmd[0] == str(fake_systemd_run)
    assert "MemoryMax=256M" in cmd
    # The bwrap portion still carries --seccomp <fd>.
    assert "--seccomp" in cmd
    fd_in_cmd = int(cmd[cmd.index("--seccomp") + 1])
    # That same fd is among those passed for inheritance (alongside the pinned
    # mount fds), and is open + inheritable at launch time.
    assert fd_in_cmd in captured["pass_fds"]
    assert captured["fstat_ok"] is True
    assert captured["inheritable"] is True

    # And the launcher closes the fd after the run returns.
    import pytest as _pytest

    with _pytest.raises(OSError):
        os.fstat(fd_in_cmd)


def test_seccomp_fd_closed_even_without_systemd_run(tmp_path, monkeypatch):
    """The same fd lifecycle holds on the plain (no-limits) path."""
    if not seccomp.is_supported_arch():
        pytest.skip("seccomp filter not supported on this arch")

    import os

    project = tmp_path / "proj"
    project.mkdir()
    state = tmp_path / "state"
    kimi = _make_fake_bin(tmp_path / "kimi", "\x7fELF")
    bwrap = _make_fake_bin(tmp_path / "bwrap")

    captured: dict = {}

    class _CP:
        returncode = 0

    def fake_run(command, pass_fds=()):
        captured["command"] = command
        captured["pass_fds"] = pass_fds
        return _CP()

    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    rc = cli.main(
        [str(project), "--kimi", str(kimi), "--bwrap", str(bwrap),
         "--state-root", str(state)]
    )
    assert rc == 0
    cmd = captured["command"]
    # No systemd-run wrapper here.
    assert cmd[0] == str(bwrap)
    assert "--seccomp" in cmd
    fd_in_cmd = int(cmd[cmd.index("--seccomp") + 1])
    assert fd_in_cmd in captured["pass_fds"]
    with pytest.raises(OSError):
        os.fstat(fd_in_cmd)


# --- mount pinning (anti-TOCTOU, opt #3): --bind-fd / --ro-bind-fd ---------

def test_pin_mounts_default_uses_bind_fd_and_closes(tmp_path, monkeypatch):
    """By default each host bind source is pinned via an O_PATH fd and the argv
    uses --bind-fd/--ro-bind-fd; every pinned fd is passed for inheritance and
    closed after the run."""
    import os

    project = tmp_path / "proj"
    project.mkdir()
    state = tmp_path / "state"
    kimi = _make_fake_bin(tmp_path / "kimi", "\x7fELF")
    bwrap = _make_fake_bin(tmp_path / "bwrap")
    # Disable seccomp to isolate the mount fds in pass_fds.
    captured: dict = {}

    class _CP:
        returncode = 0

    def fake_run(command, pass_fds=()):
        captured["command"] = command
        captured["pass_fds"] = pass_fds
        captured["all_open"] = all(os.fstat(fd) is not None for fd in pass_fds)
        return _CP()

    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    rc = cli.main(
        [str(project), "--kimi", str(kimi), "--bwrap", str(bwrap),
         "--state-root", str(state), "--no-seccomp"]
    )
    assert rc == 0
    cmd = captured["command"]
    # Project + kimi-home + kimi binary are fd-pinned.
    assert "--bind-fd" in cmd       # /workspace and /kimi-code-home (rw)
    assert "--ro-bind-fd" in cmd    # the kimi binary (ro)
    # No path-based bind of the project remains.
    assert ("--bind", str(project.resolve()), "/workspace") not in [
        (cmd[i], cmd[i + 1], cmd[i + 2])
        for i in range(len(cmd) - 2)
    ]
    # The fd numbers in the argv are exactly those passed for inheritance.
    fd_args: list[int] = []
    for i, tok in enumerate(cmd):
        if tok in ("--bind-fd", "--ro-bind-fd"):
            fd_args.append(int(cmd[i + 1]))
    assert set(fd_args) == set(captured["pass_fds"])
    assert captured["all_open"] is True
    # All pinned fds are closed after the run returns.
    for fd in captured["pass_fds"]:
        with pytest.raises(OSError):
            os.fstat(fd)


def test_no_pin_mounts_uses_path_binds(tmp_path, monkeypatch):
    """--no-pin-mounts falls back to path-based binds and passes no mount fds."""
    project = tmp_path / "proj"
    project.mkdir()
    state = tmp_path / "state"
    kimi = _make_fake_bin(tmp_path / "kimi", "\x7fELF")
    bwrap = _make_fake_bin(tmp_path / "bwrap")
    captured: dict = {}

    class _CP:
        returncode = 0

    def fake_run(command, pass_fds=()):
        captured["command"] = command
        captured["pass_fds"] = pass_fds
        return _CP()

    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    rc = cli.main(
        [str(project), "--kimi", str(kimi), "--bwrap", str(bwrap),
         "--state-root", str(state), "--no-seccomp", "--no-pin-mounts"]
    )
    assert rc == 0
    cmd = captured["command"]
    assert "--bind-fd" not in cmd
    assert "--ro-bind-fd" not in cmd
    assert ("--bind", str(project.resolve()), "/workspace") in [
        (cmd[i], cmd[i + 1], cmd[i + 2]) for i in range(len(cmd) - 2)
    ]
    # No seccomp, no pinning -> no inherited fds at all.
    assert captured["pass_fds"] == ()


def test_dry_run_shows_paths_not_fds(tmp_path, capsys, monkeypatch):
    """--dry-run prints readable path binds (not fd numbers) and notes pinning."""
    monkeypatch.setenv("HOME", str(tmp_path / "realhome"))
    (tmp_path / "realhome").mkdir()
    project = tmp_path / "proj"
    project.mkdir()
    state = tmp_path / "state"
    kimi = _make_fake_bin(tmp_path / "kimi", "\x7fELF")
    bwrap = _make_fake_bin(tmp_path / "bwrap")

    rc = cli.main(
        [str(project), "--kimi", str(kimi), "--bwrap", str(bwrap),
         "--state-root", str(state), "--dry-run"]
    )
    assert rc == 0
    captured = capsys.readouterr()
    assert "--bind-fd" not in captured.out      # readable path form in dry-run
    assert f"--bind {project.resolve()} /workspace" in captured.out
    assert "pinned via --bind-fd" in captured.err
