"""Tests for the controlled-conda argv parser and mutation policy (mod_v2 §7)."""

from __future__ import annotations

import subprocess
import sys

import pytest

from kimi_sandbox.conda_policy import (
    CondaPolicyError,
    parse_conda_argv,
    validate_conda_argv,
)

WRITABLE = "/cache/conda"
READONLY = "/opt/kimi-conda/root"
EXISTING = "/opt/kimi-conda/existing-envs"
ORIGINAL = "/home/u/anaconda3"


def _env(**extra):
    base = {
        "KIMI_SANDBOX_CONDA_ORIGINAL_ROOT": ORIGINAL,
        "CONDA_PKGS_DIRS": f"{WRITABLE}/pkgs",
    }
    base.update(extra)
    return base


def _exists_factory(existing):
    existing = set(existing)

    def _exists(path):
        return path in existing

    return _exists


def _validate(argv, *, existing=(), env=None):
    validate_conda_argv(
        argv,
        writable_root=WRITABLE,
        readonly_root=READONLY,
        existing_envs_root=EXISTING,
        env=_env() if env is None else env,
        path_exists=_exists_factory(existing),
    )


# --- parser ----------------------------------------------------------------

def test_parse_skips_global_value_flags():
    cmd = parse_conda_argv(["-c", "conda-forge", "install", "-n", "x", "numpy"])
    assert cmd.command == ("install",)
    assert cmd.env_name == "x"
    assert cmd.is_mutation


def test_parse_json_install_name():
    cmd = parse_conda_argv(["--json", "install", "-n", "x", "numpy"])
    assert cmd.command == ("install",)
    assert cmd.env_name == "x"


def test_parse_name_equals():
    cmd = parse_conda_argv(["install", "--name=x", "numpy"])
    assert cmd.env_name == "x"


def test_parse_env_update_file_and_name():
    cmd = parse_conda_argv(["env", "update", "-f", "environment.yml", "-n", "x"])
    assert cmd.command == ("env", "update")
    assert cmd.file == "environment.yml"
    assert cmd.env_name == "x"
    assert cmd.is_mutation


def test_parse_prefix():
    cmd = parse_conda_argv(["remove", "--prefix", "/cache/conda/envs/x", "numpy"])
    assert cmd.prefix == "/cache/conda/envs/x"


def test_parse_run_stops_at_program():
    # The program's own -n/-c must not be read as conda options.
    cmd = parse_conda_argv(["run", "-n", "env", "python", "-c", "code"])
    assert cmd.command == ("run",)
    assert cmd.env_name == "env"
    assert not cmd.is_mutation
    assert cmd.allow_readonly_target


def test_parse_clean_and_config():
    assert parse_conda_argv(["clean", "--all"]).is_clean
    assert parse_conda_argv(["config", "--show"]).command == ("config",)


def test_parse_env_list_not_mutation():
    cmd = parse_conda_argv(["env", "list"])
    assert cmd.command == ("env", "list")
    assert not cmd.is_mutation


# --- mutation policy: allow --------------------------------------------------

def test_create_new_env_allowed():
    _validate(["create", "-n", "newenv", "python=3.11"])


def test_create_prefix_in_writable_allowed():
    _validate(["create", "-p", "/cache/conda/envs/foo", "python"])


def test_install_existing_writable_env_allowed():
    _validate(
        ["install", "-n", "foo", "pandas"],
        existing={f"{WRITABLE}/envs/foo"},
    )


def test_remove_prefix_writable_allowed():
    _validate(["remove", "--prefix", "/cache/conda/envs/x", "numpy"])


def test_run_readonly_env_allowed():
    _validate(["run", "-n", "existing", "pip", "--version"])


def test_query_commands_allowed():
    _validate(["env", "list"])
    _validate(["list", "-n", "existing"])
    _validate(["info"])
    _validate([])


# --- mutation policy: reject -------------------------------------------------

def test_install_readonly_existing_rejected():
    with pytest.raises(CondaPolicyError):
        _validate(
            ["install", "-n", "existing", "pandas"],
            existing={f"{ORIGINAL}/envs/existing"},
        )


def test_install_base_rejected():
    with pytest.raises(CondaPolicyError):
        _validate(["install", "-n", "base", "pip"])


def test_env_remove_readonly_rejected():
    with pytest.raises(CondaPolicyError):
        _validate(
            ["env", "remove", "-n", "existing"],
            existing={f"{ORIGINAL}/envs/existing"},
        )


def test_remove_readonly_prefix_rejected():
    with pytest.raises(CondaPolicyError):
        _validate(["remove", "--prefix", "/opt/kimi-conda/root/envs/x", "numpy"])


def test_json_install_readonly_rejected():
    with pytest.raises(CondaPolicyError):
        _validate(
            ["--json", "install", "-n", "existing", "numpy"],
            existing={f"{ORIGINAL}/envs/existing"},
        )


def test_mutation_without_target_fails_closed():
    with pytest.raises(CondaPolicyError):
        _validate(["install", "numpy"])


def test_mutation_without_target_ok_when_active_writable():
    _validate(
        ["install", "numpy"],
        env=_env(CONDA_PREFIX="/cache/conda/envs/active"),
    )


def test_mutation_without_target_rejected_when_active_readonly():
    with pytest.raises(CondaPolicyError):
        _validate(
            ["install", "numpy"],
            env=_env(CONDA_PREFIX="/opt/kimi-conda/root"),
        )


def test_prefix_escape_rejected():
    with pytest.raises(CondaPolicyError):
        _validate(["create", "-p", "/cache/conda/envs/x/../../../etc", "python"])


def test_create_name_conflict_with_readonly_rejected():
    with pytest.raises(CondaPolicyError):
        _validate(
            ["create", "-n", "existing", "python"],
            existing={f"{ORIGINAL}/envs/existing"},
        )


# --- clean -------------------------------------------------------------------

def test_clean_all_allowed_with_writable_pkgs():
    _validate(["clean", "--all"])


def test_clean_force_pkgs_dirs_rejected():
    with pytest.raises(CondaPolicyError):
        _validate(["clean", "--force-pkgs-dirs"])


def test_clean_with_name_rejected():
    with pytest.raises(CondaPolicyError):
        _validate(["clean", "--all", "-n", "x"])


def test_clean_rejected_when_pkgs_dirs_not_writable():
    with pytest.raises(CondaPolicyError):
        _validate(["clean", "--all"], env=_env(CONDA_PKGS_DIRS="/opt/kimi-conda/root/pkgs"))


# --- config ------------------------------------------------------------------

@pytest.mark.parametrize("flag", ["--show", "--show-sources", "--describe", "--get"])
def test_config_readonly_allowed(flag):
    _validate(["config", flag])


@pytest.mark.parametrize(
    "argv",
    [
        ["config", "--set", "channels", "defaults"],
        ["config", "--add", "channels", "x"],
        ["config", "--remove-key", "channels"],
        ["config", "--write-default"],
        ["config", "--env", "--set", "x", "y"],
        ["config", "--system", "--show"],
    ],
)
def test_config_write_rejected(argv):
    with pytest.raises(CondaPolicyError):
        _validate(argv)


def test_config_bare_rejected():
    with pytest.raises(CondaPolicyError):
        _validate(["config"])


# --- environment file (-f) ---------------------------------------------------

def test_env_create_yaml_readonly_prefix_rejected(tmp_path):
    f = tmp_path / "environment.yml"
    f.write_text("prefix: /home/u/anaconda3\ndependencies:\n  - python\n")
    with pytest.raises(CondaPolicyError):
        _validate(["env", "create", "-f", str(f)])


def test_env_create_yaml_writable_prefix_allowed(tmp_path):
    f = tmp_path / "environment.yml"
    f.write_text("prefix: /cache/conda/envs/fromfile\ndependencies:\n  - python\n")
    _validate(["env", "create", "-f", str(f)])


def test_env_create_yaml_readonly_name_rejected(tmp_path):
    f = tmp_path / "environment.yml"
    f.write_text("name: base\ndependencies:\n  - python\n")
    with pytest.raises(CondaPolicyError):
        _validate(["env", "create", "-f", str(f)])


def test_env_create_yaml_new_name_allowed(tmp_path):
    f = tmp_path / "environment.yml"
    f.write_text("name: sandbox-new\ndependencies:\n  - python\n")
    _validate(["env", "create", "-f", str(f)])


def test_env_create_cli_yaml_conflict_rejected(tmp_path):
    f = tmp_path / "environment.yml"
    f.write_text("name: sandbox-a\n")
    with pytest.raises(CondaPolicyError):
        _validate(["env", "create", "-f", str(f), "-n", "sandbox-b"])


def test_env_create_no_target_fails_closed(tmp_path):
    f = tmp_path / "environment.yml"
    f.write_text("dependencies:\n  - python\n")
    with pytest.raises(CondaPolicyError):
        _validate(["env", "create", "-f", str(f)])


def test_env_file_multidoc_rejected(tmp_path):
    f = tmp_path / "environment.yml"
    f.write_text("name: sandbox-x\n---\nname: other\n")
    with pytest.raises(CondaPolicyError):
        _validate(["env", "create", "-f", str(f)])


def test_env_file_missing_rejected(tmp_path):
    with pytest.raises(CondaPolicyError):
        _validate(["env", "create", "-f", str(tmp_path / "nope.yml")])


# --- generated-shim self-containment (mod_v2 §7.2) --------------------------

def test_policy_module_runs_with_empty_pythonpath(tmp_path):
    """The policy must import and run with no PYTHONPATH (it is inlined into
    the in-sandbox shim, which has no access to the launcher source tree)."""
    import kimi_sandbox.conda_policy as mod

    src = mod.__file__
    script = (
        "import runpy, sys\n"
        f"mod = runpy.run_path({src!r})\n"
        "p = mod['parse_conda_argv'](['install', '-n', 'x', 'numpy'])\n"
        "assert p.command == ('install',), p.command\n"
        "assert p.is_mutation\n"
        "print('ok')\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", script],
        env={"PATH": "/usr/bin:/bin", "PYTHONPATH": ""},
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert "ok" in proc.stdout


# --- audit #1: command whitelist + env config vars --------------------------

def test_unknown_leading_token_skipped_to_known_command():
    # An unknown global option's value must not be mistaken for the subcommand;
    # the scanner skips unknown bare tokens and finds the real command.
    cmd = parse_conda_argv(["--unknownopt", "junk", "install", "-n", "x", "numpy"])
    assert cmd.command == ("install",)
    assert cmd.is_mutation


def test_unknown_command_is_passthrough():
    cmd = parse_conda_argv(["frobnicate", "--weird"])
    assert cmd.command == ()
    assert not cmd.is_mutation


def test_env_config_vars_set_is_mutation():
    cmd = parse_conda_argv(["env", "config", "vars", "set", "-n", "base", "A=B"])
    assert cmd.command == ("env", "config")
    assert cmd.is_mutation


def test_env_config_vars_list_not_mutation():
    cmd = parse_conda_argv(["env", "config", "vars", "list", "-n", "base"])
    assert not cmd.is_mutation


def test_env_config_vars_set_base_rejected():
    with pytest.raises(CondaPolicyError):
        _validate(["env", "config", "vars", "set", "-n", "base", "A=B"])


# --- audit #2: long-option abbreviation -------------------------------------

def test_abbrev_prefix_resolved():
    cmd = parse_conda_argv(["install", "--pre", "/cache/conda/envs/x", "numpy"])
    assert cmd.prefix == "/cache/conda/envs/x"


def test_abbrev_name_resolved_and_rejected_for_readonly():
    with pytest.raises(CondaPolicyError):
        _validate(["install", "--na", "base", "numpy"])


def test_abbrev_config_write_flag_caught():
    # '--se' uniquely abbreviates '--set' -> must be rejected as a write.
    with pytest.raises(CondaPolicyError):
        _validate(["config", "--se", "channels", "x"])


def test_abbrev_config_readonly_accepted():
    _validate(["config", "--g", "channels"])  # --g -> --get


# --- audit #3: shared CONDA_ENVS_PATH builder -------------------------------

def test_build_conda_envs_path_writable_first():
    from kimi_sandbox.conda_policy import build_conda_envs_path

    p = build_conda_envs_path("/cache/conda", "/home/u/anaconda3")
    assert p == (
        "/cache/conda/envs:/home/u/anaconda3/envs:/opt/kimi-conda/existing-envs"
    )
