"""Tests for CLI orchestration behaviors (exit codes, warnings, validation)."""

from __future__ import annotations

import pytest

from kimi_sandbox import cli
from kimi_sandbox.errors import InvalidPathError
from kimi_sandbox.paths import validate_path_relationships


def test_launcher_error_exit_is_distinct():
    # Must not be 2 (argparse / common child code) per design §27.
    assert cli.LAUNCHER_ERROR_EXIT != 2
    assert cli.LAUNCHER_ERROR_EXIT != 0


def test_unsafe_mode_skips_state_root_checks(tmp_path):
    # In unsafe mode, a project living under the (unused) state_root must NOT
    # be rejected on that basis. kimi_code_home is elsewhere and disjoint.
    state_root = tmp_path / "state"
    project = state_root / "proj"  # project inside default state_root
    kimi_home = tmp_path / "custom-home"
    # Should not raise because unsafe mode ignores state_root relationships.
    validate_path_relationships(
        project_dir=project,
        state_root=state_root,
        kimi_code_home=kimi_home,
        unsafe_kimi_code_home=True,
    )


def test_unsafe_mode_still_enforces_kimi_home_vs_project(tmp_path):
    # The project-vs-kimi_code_home rules still apply in unsafe mode.
    project = tmp_path / "proj"
    kimi_home = project / "inside"
    with pytest.raises(InvalidPathError):
        validate_path_relationships(
            project_dir=project,
            state_root=tmp_path / "state",
            kimi_code_home=kimi_home,
            unsafe_kimi_code_home=True,
        )


def test_main_returns_launcher_code_on_invalid_project(capsys):
    rc = cli.main(["/"])  # broad root rejected
    assert rc == cli.LAUNCHER_ERROR_EXIT
    err = capsys.readouterr().err
    assert "error:" in err


def test_main_version_exits_zero(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--version"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "kimi-sandbox" in out


def test_dry_run_emits_unsafe_warning(tmp_path, capsys, monkeypatch):
    # The §10 unsafe warning must appear even under --dry-run (banner skipped).
    monkeypatch.setenv("HOME", str(tmp_path / "realhome"))
    (tmp_path / "realhome").mkdir()
    project = tmp_path / "proj"
    project.mkdir()
    custom_home = tmp_path / "exposed"
    # Provide a fake kimi + bwrap so resolution succeeds.
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    kimi = fake_bin / "kimi"
    kimi.write_text("\x7fELF")
    kimi.chmod(0o755)
    bwrap = fake_bin / "bwrap"
    bwrap.write_text("#!/bin/sh\n")
    bwrap.chmod(0o755)

    rc = cli.main(
        [
            str(project),
            "--unsafe-kimi-code-home",
            str(custom_home),
            "--kimi",
            str(kimi),
            "--bwrap",
            str(bwrap),
            "--dry-run",
        ]
    )
    assert rc == 0
    captured = capsys.readouterr()
    assert "WARNING: --unsafe-kimi-code-home in use." in captured.err
    # The bwrap command still printed to stdout.
    assert "--bind" in captured.out
