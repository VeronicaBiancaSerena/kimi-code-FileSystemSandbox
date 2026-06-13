"""Tests for the mod_v1 MCP/skill read-only integration feature.

Covers: profile read-only sub-mounts (resolve/validate/prepare), the Kimi config
layout probe, env_keep/env_set with reserved-key protection, the bwrap profile
sub-mount ordering + compat symlink, mount pinning of profile sources, and the
doctor / init-integrations subcommands.
"""

from __future__ import annotations

import os

import pytest

from kimi_sandbox import cli
from kimi_sandbox.bwrap import build_bwrap_command
from kimi_sandbox.config import ProfileMount, SandboxConfig
from kimi_sandbox.errors import InvalidPathError, SandboxError
from kimi_sandbox.paths import (
    discover_kimi_config_layout,
    prepare_profile_mount_targets,
    resolve_profile_ro_mount,
    validate_profile_ro_mounts,
)

# ===========================================================================
# resolve_profile_ro_mount (mod_v1 §7)
# ===========================================================================

def test_profile_mount_basic(tmp_path):
    src = tmp_path / "skills"
    src.mkdir()
    m = resolve_profile_ro_mount(f"{src}:skills")
    assert isinstance(m, ProfileMount)
    assert m.source == src.resolve()
    assert m.relative_target == "skills"


def test_profile_mount_nested_target(tmp_path):
    src = tmp_path / "skills"
    src.mkdir()
    m = resolve_profile_ro_mount(f"{src}:integrations/skills")
    assert m.relative_target == "integrations/skills"


def test_profile_mount_tilde_expands(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".kimi-code" / "skills").mkdir(parents=True)
    m = resolve_profile_ro_mount("~/.kimi-code/skills:skills")
    assert m.source == (tmp_path / ".kimi-code" / "skills").resolve()


def test_profile_mount_empty_spec():
    with pytest.raises(InvalidPathError):
        resolve_profile_ro_mount("")


def test_profile_mount_missing_colon(tmp_path):
    src = tmp_path / "skills"
    src.mkdir()
    with pytest.raises(InvalidPathError):
        resolve_profile_ro_mount(str(src))


def test_profile_mount_missing_source(tmp_path):
    with pytest.raises(InvalidPathError):
        resolve_profile_ro_mount(f"{tmp_path / 'nope'}:skills")


def test_profile_mount_missing_target(tmp_path):
    src = tmp_path / "skills"
    src.mkdir()
    with pytest.raises(InvalidPathError):
        resolve_profile_ro_mount(f"{src}:")


def test_profile_mount_source_is_file(tmp_path):
    f = tmp_path / "file"
    f.write_text("x")
    with pytest.raises(InvalidPathError):
        resolve_profile_ro_mount(f"{f}:skills")


def test_profile_mount_missing_host_before_colon():
    with pytest.raises(InvalidPathError):
        resolve_profile_ro_mount(":skills")


@pytest.mark.parametrize("target", ["/skills", ".", "..", "../skills", "skills/../other"])
def test_profile_mount_bad_targets(tmp_path, target):
    src = tmp_path / "skills"
    src.mkdir()
    with pytest.raises(InvalidPathError):
        resolve_profile_ro_mount(f"{src}:{target}")


# ===========================================================================
# validate_profile_ro_mounts (mod_v1 §8)
# ===========================================================================

def test_validate_unique_ok(tmp_path):
    a = tmp_path / "a"; a.mkdir()
    b = tmp_path / "b"; b.mkdir()
    validate_profile_ro_mounts(
        [ProfileMount(a, "skills"), ProfileMount(b, "integrations/skills")]
    )


def test_validate_duplicate_target(tmp_path):
    a = tmp_path / "a"; a.mkdir()
    b = tmp_path / "b"; b.mkdir()
    with pytest.raises(InvalidPathError):
        validate_profile_ro_mounts(
            [ProfileMount(a, "skills"), ProfileMount(b, "skills")]
        )


# ===========================================================================
# prepare_profile_mount_targets (mod_v1 §10.2)
# ===========================================================================

def test_prepare_creates_missing_dir(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    src = tmp_path / "skills"; src.mkdir()
    prepare_profile_mount_targets(home, (ProfileMount(src, "skills"),))
    assert (home / "skills").is_dir()


def test_prepare_accepts_existing_dir(tmp_path):
    home = tmp_path / "home"
    (home / "skills").mkdir(parents=True)
    src = tmp_path / "skills"; src.mkdir()
    prepare_profile_mount_targets(home, (ProfileMount(src, "skills"),))  # no raise


def test_prepare_rejects_file_target(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / "skills").write_text("x")
    src = tmp_path / "skills"; src.mkdir()
    with pytest.raises(SandboxError):
        prepare_profile_mount_targets(home, (ProfileMount(src, "skills"),))


def test_prepare_rejects_symlink_target(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (tmp_path / "elsewhere").mkdir()
    (home / "skills").symlink_to(tmp_path / "elsewhere")
    src = tmp_path / "skills"; src.mkdir()
    with pytest.raises(SandboxError):
        prepare_profile_mount_targets(home, (ProfileMount(src, "skills"),))


def test_prepare_warns_nonempty(tmp_path, capsys):
    home = tmp_path / "home"
    (home / "skills").mkdir(parents=True)
    (home / "skills" / "leftover").write_text("x")
    src = tmp_path / "skills"; src.mkdir()
    prepare_profile_mount_targets(home, (ProfileMount(src, "skills"),))
    assert "shadowed" in capsys.readouterr().err


def test_prepare_creates_nested(tmp_path):
    home = tmp_path / "home"; home.mkdir()
    src = tmp_path / "skills"; src.mkdir()
    prepare_profile_mount_targets(home, (ProfileMount(src, "integrations/skills"),))
    assert (home / "integrations" / "skills").is_dir()


def test_prepare_rejects_intermediate_symlink(tmp_path):
    # Opt#4: a symlink at an *intermediate* path component must be rejected,
    # not just the leaf.
    home = tmp_path / "home"
    home.mkdir()
    (tmp_path / "elsewhere").mkdir()
    (home / "integrations").symlink_to(tmp_path / "elsewhere")
    src = tmp_path / "skills"; src.mkdir()
    with pytest.raises(SandboxError):
        prepare_profile_mount_targets(home, (ProfileMount(src, "integrations/skills"),))


def test_prepare_rejects_intermediate_file(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / "integrations").write_text("x")  # file where a dir is needed
    src = tmp_path / "skills"; src.mkdir()
    with pytest.raises(SandboxError):
        prepare_profile_mount_targets(home, (ProfileMount(src, "integrations/skills"),))


# ===========================================================================
# discover_kimi_config_layout (mod_v1 §5.1)
# ===========================================================================

def test_discover_recognized_mcp_and_skills(tmp_path):
    (tmp_path / "mcp.json").write_text('{"mcpServers": {}}')
    (tmp_path / "skills").mkdir()
    layout = discover_kimi_config_layout(tmp_path)
    assert layout.recognized
    assert layout.mcp_config == tmp_path / "mcp.json"
    assert layout.skills_dir == tmp_path / "skills"


def test_discover_recognized_skills_only(tmp_path):
    skills = tmp_path / "skills"
    skills.mkdir()
    (skills / "a-skill").mkdir()  # non-empty -> a real skills dir
    layout = discover_kimi_config_layout(tmp_path)
    assert layout.recognized
    assert layout.mcp_config is None
    assert layout.skills_dir == skills


def test_discover_empty_skills_not_recognized(tmp_path):
    # Opt#1: an empty skills/ is just a bind mountpoint, not a real config.
    (tmp_path / "skills").mkdir()
    layout = discover_kimi_config_layout(tmp_path)
    assert not layout.recognized
    assert layout.skills_dir == tmp_path / "skills"  # path still reported
    assert any("empty" in n for n in layout.notes)


def test_discover_empty_skills_but_mcp_recognized(tmp_path):
    # mcp.json alone is enough even with an empty skills/ mountpoint.
    (tmp_path / "skills").mkdir()
    (tmp_path / "mcp.json").write_text('{"mcpServers": {}}')
    layout = discover_kimi_config_layout(tmp_path)
    assert layout.recognized
    assert layout.mcp_config == tmp_path / "mcp.json"


def test_discover_not_recognized(tmp_path):
    layout = discover_kimi_config_layout(tmp_path)
    assert not layout.recognized
    assert layout.notes  # explanatory note present


# ===========================================================================
# build_env_allowlist: env_keep / env_set + reserved keys (mod_v1 §9)
# ===========================================================================

def test_env_keep_passes_through(monkeypatch):
    monkeypatch.setenv("MY_TOKEN", "secret-value")
    env = cli.build_env_allowlist(env_keep=["MY_TOKEN"])
    assert env["MY_TOKEN"] == "secret-value"


def test_env_keep_absent_not_created(monkeypatch):
    monkeypatch.delenv("MY_ABSENT", raising=False)
    env = cli.build_env_allowlist(env_keep=["MY_ABSENT"])
    assert "MY_ABSENT" not in env


def test_env_keep_dedup_preserves_order(monkeypatch):
    monkeypatch.setenv("A_VAR", "1")
    monkeypatch.setenv("B_VAR", "2")
    env = cli.build_env_allowlist(env_keep=["A_VAR", "B_VAR", "A_VAR"])
    assert env["A_VAR"] == "1" and env["B_VAR"] == "2"


@pytest.mark.parametrize("key", sorted(cli._RESERVED_ENV_KEYS))
def test_env_keep_reserved_rejected(key):
    with pytest.raises(SandboxError):
        cli.build_env_allowlist(env_keep=[key])


@pytest.mark.parametrize("bad", ["1BAD", "has space", "has=eq", "", "a-b"])
def test_env_keep_invalid_name_rejected(bad):
    with pytest.raises(SandboxError):
        cli.build_env_allowlist(env_keep=[bad])


def test_env_set_non_reserved_ok():
    env = cli.build_env_allowlist(env_set={"PYTHONDONTWRITEBYTECODE": "1"})
    assert env["PYTHONDONTWRITEBYTECODE"] == "1"


@pytest.mark.parametrize("key", ["PATH", "KIMI_CODE_HOME", "XDG_CACHE_HOME", "HOME"])
def test_env_set_reserved_rejected(key):
    with pytest.raises(SandboxError):
        cli.build_env_allowlist(env_set={key: "x"})


def test_env_set_invalid_name_rejected():
    with pytest.raises(SandboxError):
        cli.build_env_allowlist(env_set={"bad name": "x"})


def test_sensitive_not_forwarded_by_default(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_xxx")
    env = cli.build_env_allowlist()
    assert "GITHUB_TOKEN" not in env


# ===========================================================================
# Config schema parsing (mod_v1 §8)
# ===========================================================================

def test_config_accepts_new_keys(tmp_path):
    cfg = tmp_path / "c.toml"
    cfg.write_text(
        'compat_kimi_home = true\n'
        'profile_ro_mounts = ["~/x:skills"]\n'
        'env_keep = ["GITHUB_TOKEN"]\n'
        '[env_set]\n'
        'PYTHONDONTWRITEBYTECODE = "1"\n'
    )
    data = cli.load_config_file(cfg, explicit=True)
    assert data["compat_kimi_home"] is True
    assert data["profile_ro_mounts"] == ["~/x:skills"]
    assert data["env_keep"] == ["GITHUB_TOKEN"]
    assert data["env_set"] == {"PYTHONDONTWRITEBYTECODE": "1"}


def test_config_env_set_must_be_dict(tmp_path):
    cfg = tmp_path / "c.toml"
    cfg.write_text('env_set = ["not-a-dict"]\n')
    with pytest.raises(SandboxError):
        cli.load_config_file(cfg, explicit=True)


def test_config_env_set_values_must_be_str(tmp_path):
    cfg = tmp_path / "c.toml"
    cfg.write_text('[env_set]\nX = 1\n')
    with pytest.raises(SandboxError):
        cli.load_config_file(cfg, explicit=True)


def test_config_profile_ro_mounts_must_be_str_list(tmp_path):
    cfg = tmp_path / "c.toml"
    cfg.write_text('profile_ro_mounts = [1, 2]\n')
    with pytest.raises(SandboxError):
        cli.load_config_file(cfg, explicit=True)


def test_config_compat_kimi_home_must_be_bool(tmp_path):
    cfg = tmp_path / "c.toml"
    cfg.write_text('compat_kimi_home = "yes"\n')
    with pytest.raises(SandboxError):
        cli.load_config_file(cfg, explicit=True)


# ===========================================================================
# CLI compat flags
# ===========================================================================

def test_compat_flag_true():
    args = cli.build_parser().parse_args([".", "--compat-kimi-home"])
    assert args.compat_kimi_home is True


def test_compat_flag_false():
    args = cli.build_parser().parse_args([".", "--no-compat-kimi-home"])
    assert args.compat_kimi_home is False


def test_compat_flag_unset_none():
    args = cli.build_parser().parse_args(["."])
    assert args.compat_kimi_home is None


# ===========================================================================
# bwrap: profile sub-mounts, ordering, compat symlink (mod_v1 §10)
# ===========================================================================

def _make_config(tmp_path, **overrides) -> SandboxConfig:
    project = tmp_path / "proj"; project.mkdir(exist_ok=True)
    home = tmp_path / "home"; home.mkdir(exist_ok=True)
    kimi = tmp_path / "kimi"; kimi.write_text("x")
    base = dict(
        project_dir=project.resolve(),
        kimi_code_home=home.resolve(),
        kimi_path=kimi.resolve(),
        inner_command=["/sandbox/bin/kimi"],
        env={"HOME": "/home/sandbox"},
    )
    base.update(overrides)
    return SandboxConfig(**base)


def _index_of_pair(cmd, flag, dest):
    for i, tok in enumerate(cmd):
        if tok == flag and i + 2 < len(cmd) and cmd[i + 2] == dest:
            return i
    return -1


def test_profile_ro_after_kimi_home_rw(tmp_path):
    src = tmp_path / "skills"; src.mkdir()
    cfg = _make_config(
        tmp_path, profile_ro_mounts=(ProfileMount(src.resolve(), "skills"),)
    )
    cmd = build_bwrap_command(cfg)
    rw_idx = _index_of_pair(cmd, "--bind", "/kimi-code-home")
    ro_idx = _index_of_pair(cmd, "--ro-bind", "/kimi-code-home/skills")
    assert rw_idx != -1 and ro_idx != -1
    assert rw_idx < ro_idx


def test_profile_ro_uses_ro_bind(tmp_path):
    src = tmp_path / "skills"; src.mkdir()
    cfg = _make_config(
        tmp_path, profile_ro_mounts=(ProfileMount(src.resolve(), "skills"),)
    )
    cmd = build_bwrap_command(cfg)
    assert _index_of_pair(cmd, "--ro-bind", "/kimi-code-home/skills") != -1


def test_profile_ro_uses_ro_bind_fd_when_pinned(tmp_path):
    src = tmp_path / "skills"; src.mkdir()
    cfg = _make_config(
        tmp_path, profile_ro_mounts=(ProfileMount(src.resolve(), "skills"),)
    )
    cmd = build_bwrap_command(cfg, path_fds={str(src.resolve()): 21})
    # --ro-bind-fd 21 /kimi-code-home/skills
    found = any(
        cmd[i] == "--ro-bind-fd" and cmd[i + 1] == "21"
        and cmd[i + 2] == "/kimi-code-home/skills"
        for i in range(len(cmd) - 2)
    )
    assert found


def test_compat_symlink_present_by_default(tmp_path):
    cfg = _make_config(tmp_path)  # compat default True
    cmd = build_bwrap_command(cfg)
    found = any(
        cmd[i] == "--symlink" and cmd[i + 1] == "/kimi-code-home"
        and cmd[i + 2] == "/home/sandbox/.kimi-code"
        for i in range(len(cmd) - 2)
    )
    assert found


def test_compat_symlink_absent_when_false(tmp_path):
    cfg = _make_config(tmp_path, compat_kimi_home=False)
    cmd = build_bwrap_command(cfg)
    found = any(
        cmd[i] == "--symlink" and cmd[i + 2] == "/home/sandbox/.kimi-code"
        for i in range(len(cmd) - 2)
    )
    assert not found


# ===========================================================================
# open_bind_fds includes profile mount sources (mod_v1 §10.4)
# ===========================================================================

def test_open_bind_fds_includes_profile_source(tmp_path):
    src = tmp_path / "skills"; src.mkdir()
    cfg = _make_config(
        tmp_path, profile_ro_mounts=(ProfileMount(src.resolve(), "skills"),)
    )
    fds = cli.open_bind_fds(cfg)
    try:
        assert str(src.resolve()) in fds
    finally:
        for fd in fds.values():
            os.close(fd)


# ===========================================================================
# doctor / init-integrations subcommands (mod_v1 §13/§14)
# ===========================================================================

def test_doctor_missing_config_is_not_failure(tmp_path):
    rc = cli.main(["doctor", "--config-check", "--config", str(tmp_path / "none.toml")])
    assert rc == 0


def test_doctor_validates_good_config(tmp_path, monkeypatch):
    skills = tmp_path / "skills"; skills.mkdir()
    (skills / "marker").write_text("x")
    state = tmp_path / "state"
    cfg = tmp_path / "c.toml"
    cfg.write_text(f'profile_ro_mounts = ["{skills}:skills"]\n')
    rc = cli.main(["doctor", "--config-check", "--config", str(cfg),
                   "--state-root", str(state)])
    assert rc == 0


def test_doctor_fails_on_missing_source(tmp_path):
    cfg = tmp_path / "c.toml"
    cfg.write_text(f'profile_ro_mounts = ["{tmp_path / "nope"}:skills"]\n')
    rc = cli.main(["doctor", "--config-check", "--config", str(cfg),
                   "--state-root", str(tmp_path / "state")])
    assert rc == cli.LAUNCHER_ERROR_EXIT


def test_doctor_scan_symlink_bounded(tmp_path, monkeypatch):
    # Opt#3: the advisory symlink scan is bounded. With a tiny budget and many
    # entries it stops early and reports truncation.
    monkeypatch.setattr(cli, "_SYMLINK_SCAN_MAX_ENTRIES", 5)
    src = tmp_path / "skills"
    src.mkdir()
    for i in range(50):
        (src / f"f{i}").write_text("x")
    msgs: list[str] = []
    cli._scan_source_symlinks(src, lambda ok, m, warn=False: msgs.append(m))
    assert any("truncated" in m for m in msgs)


def test_doctor_scan_symlink_deep_not_truncated(tmp_path, monkeypatch):
    # --deep lifts the caps: even with a tiny default budget, deep=True scans
    # everything and never reports truncation.
    monkeypatch.setattr(cli, "_SYMLINK_SCAN_MAX_ENTRIES", 5)
    src = tmp_path / "skills"
    src.mkdir()
    for i in range(50):
        (src / f"f{i}").write_text("x")
    (src / "evil").symlink_to("/cache/x")
    msgs: list[str] = []
    cli._scan_source_symlinks(src, lambda ok, m, warn=False: msgs.append(m), deep=True)
    assert not any("truncated" in m for m in msgs)
    assert any("writable area" in m for m in msgs)


def test_doctor_deep_flag_parsed(tmp_path, monkeypatch, capsys):
    # End-to-end: `doctor --deep` is accepted and still returns a clean result.
    monkeypatch.setattr(cli, "_SYMLINK_SCAN_MAX_ENTRIES", 2)
    skills = tmp_path / "skills"
    skills.mkdir()
    for i in range(10):
        (skills / f"f{i}").write_text("x")
    cfg = tmp_path / "c.toml"
    cfg.write_text(f'profile_ro_mounts = ["{skills}:skills"]\n')
    rc = cli.main(["doctor", "--config-check", "--deep", "--config", str(cfg),
                   "--state-root", str(tmp_path / "state")])
    assert rc == 0
    assert "truncated" not in capsys.readouterr().err


def test_doctor_scan_symlink_flags_writable_target(tmp_path):
    src = tmp_path / "skills"
    src.mkdir()
    (src / "link").symlink_to("/cache/somewhere")
    msgs: list[str] = []
    cli._scan_source_symlinks(src, lambda ok, m, warn=False: msgs.append(m))
    assert any("writable area" in m for m in msgs)


def test_doctor_scan_symlink_ignores_benign(tmp_path):
    src = tmp_path / "skills"
    src.mkdir()
    (src / "ok").symlink_to("/opt/kimi-mcp/whatever")
    msgs: list[str] = []
    cli._scan_source_symlinks(src, lambda ok, m, warn=False: msgs.append(m))
    assert not any("writable area" in m for m in msgs)


# --- MCP path check judged against configured mount targets (follow-up #1) ---

def _collect(mcp_config, targets):
    msgs: list[tuple[str, bool]] = []
    cli._check_mcp_uses_sandbox_paths(
        mcp_config, targets, lambda ok, m, warn=False: msgs.append((m, warn))
    )
    return msgs


def test_mcp_check_flags_uncovered_host_path(tmp_path):
    mcp = tmp_path / "mcp.json"
    mcp.write_text('{"mcpServers": {"x": {"command": "/home/ningyuxie/foo/python",'
                   ' "args": ["/home/ningyuxie/foo/server.py"]}}}')
    msgs = _collect(mcp, [])  # no mount targets cover it
    assert any(w and "not covered" in m for m, w in msgs)


def test_mcp_check_allows_opt_paths(tmp_path):
    mcp = tmp_path / "mcp.json"
    mcp.write_text('{"mcpServers": {"x": {"command": "/opt/kimi-runtime/py/bin/python",'
                   ' "args": ["/opt/kimi-mcp/x/server.py"]}}}')
    msgs = _collect(mcp, [])
    assert any("sandbox/mounted paths" in m for m, _ in msgs)
    assert not any(w for _, w in msgs)


def test_mcp_check_home_path_covered_by_mount_target_not_flagged(tmp_path):
    # follow-up #1: an MCP mounted under /home/... *inside the sandbox* (a
    # configured mount target) must NOT be mis-flagged as a host path.
    mcp = tmp_path / "mcp.json"
    mcp.write_text('{"mcpServers": {"x": {"command": "/home/sandbox-mcp/bin/python",'
                   ' "args": ["/home/sandbox-mcp/server.py"]}}}')
    msgs = _collect(mcp, ["/home/sandbox-mcp"])
    assert any("sandbox/mounted paths" in m for m, _ in msgs)
    assert not any(w for _, w in msgs)


# --- doctor rw_mounts heads-up (follow-up #2) -------------------------------

def test_doctor_warns_on_rw_mounts(tmp_path, capsys):
    rw = tmp_path / "data"; rw.mkdir()
    state = tmp_path / "state"
    cfg = tmp_path / "c.toml"
    cfg.write_text(f'rw_mounts = ["{rw}:/srv/data"]\n')
    rc = cli.main(["doctor", "--config-check", "--config", str(cfg),
                   "--state-root", str(state)])
    assert rc == 0  # writable mount is a warning, not a failure
    err = capsys.readouterr().err
    assert "WRITABLE" in err
    assert "disposable" in err


def test_doctor_rw_mount_missing_source_fails(tmp_path):
    cfg = tmp_path / "c.toml"
    cfg.write_text(f'rw_mounts = ["{tmp_path / "nope"}:/srv/data"]\n')
    rc = cli.main(["doctor", "--config-check", "--config", str(cfg),
                   "--state-root", str(tmp_path / "state")])
    assert rc == cli.LAUNCHER_ERROR_EXIT


def test_init_integrations_dry_run_prints_template(tmp_path, capsys):
    rc = cli.main(["init-integrations", "--config", str(tmp_path / "new.toml")])
    assert rc == 0
    out = capsys.readouterr().out
    assert "profile_ro_mounts" in out
    assert not (tmp_path / "new.toml").exists()  # dry-run does not write


def test_init_integrations_write_creates_file(tmp_path):
    target = tmp_path / "new.toml"
    rc = cli.main(["init-integrations", "--config", str(target), "--write"])
    assert rc == 0
    assert target.exists()
    data = cli.load_config_file(target, explicit=True)
    assert data["compat_kimi_home"] is True


def test_init_integrations_existing_no_overwrite(tmp_path, capsys):
    target = tmp_path / "c.toml"
    target.write_text('profile = "custom"\ncompat_kimi_home = false\n'
                      'persistent_cache = false\nprofile_ro_mounts = ["~/x:skills"]\n')
    before = target.read_text()
    rc = cli.main(["init-integrations", "--config", str(target)])
    assert rc == 0
    assert target.read_text() == before  # untouched in dry-run


def test_init_integrations_write_backs_up_existing(tmp_path):
    target = tmp_path / "c.toml"
    target.write_text('profile = "custom"\n')  # missing compat/persistent keys
    rc = cli.main(["init-integrations", "--config", str(target), "--write"])
    assert rc == 0
    backups = list(tmp_path.glob("c.toml.bak.*"))
    assert backups, "expected a timestamped backup"
    # Newly appended keys are valid and parseable.
    data = cli.load_config_file(target, explicit=True)
    assert data["compat_kimi_home"] is True
    assert data["profile"] == "custom"  # original untouched


def test_init_integrations_existing_complete_profile_ro_no_change(tmp_path, capsys, monkeypatch):
    # Opt#2: profile_ro_mounts already contains the suggested item -> no manual
    # hint, no change, even if other scalar keys are present.
    monkeypatch.setattr(cli, "detect_conda_root", lambda: None)
    target = tmp_path / "c.toml"
    target.write_text(
        'compat_kimi_home = true\npersistent_cache = true\n'
        'profile_ro_mounts = ["~/.kimi-code/skills:skills"]\n'
    )
    before = target.read_text()
    rc = cli.main(["init-integrations", "--config", str(target)])
    assert rc == 0
    assert target.read_text() == before
    err = capsys.readouterr().err
    assert "no missing keys" in err


def test_init_integrations_incomplete_list_prints_manual_hint(tmp_path, monkeypatch, capsys):
    # Opt#2: profile_ro_mounts exists but is missing the suggested item. The
    # tool must NOT rewrite the array; it prints a precise manual hint and does
    # not modify the file, even with --write.
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".kimi-code" / "skills").mkdir(parents=True)
    target = tmp_path / "c.toml"
    target.write_text(
        'compat_kimi_home = true\npersistent_cache = true\n'
        'profile_ro_mounts = ["~/other:other"]\n'
    )
    before = target.read_text()
    rc = cli.main(["init-integrations", "--config", str(target), "--write"])
    assert rc == 0
    # File unchanged (no safe append possible for an existing array).
    assert target.read_text() == before
    assert not list(tmp_path.glob("c.toml.bak.*"))  # nothing appended -> no backup
    err = capsys.readouterr().err
    assert "cannot safely auto-append" in err
    assert "~/.kimi-code/skills:skills" in err



# --- TOML string escaping in init-integrations renderers (cleanup #2) -------

import tomllib  # noqa: E402


def test_toml_str_escapes_quotes_and_backslash():
    assert cli._toml_str('a"b') == '"a\\"b"'
    assert cli._toml_str("a\\b") == '"a\\\\b"'
    assert cli._toml_str("a\tb") == '"a\\tb"'


def test_render_snippet_with_special_chars_is_valid_toml():
    snippet = cli._render_toml_snippet(
        {"compat_kimi_home": True, "profile_ro_mounts": ['~/we"ird\\path:skills']}
    )
    # The rendered snippet must parse back cleanly and preserve the value.
    data = tomllib.loads(snippet)
    assert data["compat_kimi_home"] is True
    assert data["profile_ro_mounts"] == ['~/we"ird\\path:skills']


def test_render_init_template_is_valid_toml(tmp_path):
    template = cli._render_init_template(['~/.kimi-code/skills:skills'])
    data = tomllib.loads(template)
    assert data["profile"] == "default"
    assert data["compat_kimi_home"] is True
    assert data["profile_ro_mounts"] == ['~/.kimi-code/skills:skills']
