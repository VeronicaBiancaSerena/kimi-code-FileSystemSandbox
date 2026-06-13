"""Tests for generated conda helper files (mod_v2 §7.2, §8, §9)."""

from __future__ import annotations

import os
import subprocess
import sys

from kimi_sandbox.conda_integration import (
    conda_generated_dir,
    prepare_conda_generated_files,
    render_conda_bash_env,
    render_conda_shim,
    render_condarc,
)
from kimi_sandbox.config import (
    SANDBOX_CONDA_BASH_ENV,
    SANDBOX_CONDA_SHIM,
    SANDBOX_CONDARC,
)


def test_shim_is_self_contained_and_inlines_policy():
    shim = render_conda_shim("/home/u/anaconda3")
    # Shebang points at the conda root's own python (audit #10).
    assert shim.startswith("#!/home/u/anaconda3/bin/python")
    # Policy logic must be inlined (no import of kimi_sandbox at runtime).
    assert "def validate_conda_argv" in shim
    assert "def shim_main" in shim
    assert "import kimi_sandbox" not in shim


def test_condarc_lists_writable_first_and_no_readonly_pkgs():
    rc = render_condarc("/cache/conda", "/home/u/anaconda3")
    assert "/cache/conda/envs" in rc
    assert "/home/u/anaconda3/envs" in rc
    assert "/cache/conda/pkgs" in rc
    assert "always_copy: true" in rc
    assert "auto_activate_base: false" in rc
    # readonly root package cache must never be exposed.
    assert "/opt/kimi-conda/root/pkgs" not in rc
    # writable envs dir comes before the read-only host envs dir.
    assert rc.index("/cache/conda/envs") < rc.index("/home/u/anaconda3/envs")


def test_bash_env_loads_hook_and_routes_through_shim():
    hook = render_conda_bash_env()
    assert "KIMI_SANDBOX_CONDA_ORIGINAL_ROOT" in hook
    assert "shell.bash" in hook
    # CONDA_EXE is redirected to the controlled shim (audit #7 / shim authority).
    assert 'export CONDA_EXE="/sandbox/bin/conda"' in hook
    # The hook no longer re-declares env vars the launcher already injects
    # (single source of truth, audit #3).
    assert "CONDA_ENVS_PATH" not in hook
    assert "CONDA_PKGS_DIRS" not in hook


def test_prepare_writes_files_with_perms(tmp_path):
    mounts = prepare_conda_generated_files(
        state_root=tmp_path,
        profile="default",
        writable_root="/cache/conda",
        original_root="/home/u/anaconda3",
        shell_integration=True,
    )
    targets = {m.target: m for m in mounts}
    assert SANDBOX_CONDA_SHIM in targets
    assert SANDBOX_CONDARC in targets
    assert SANDBOX_CONDA_BASH_ENV in targets

    shim_mount = targets[SANDBOX_CONDA_SHIM]
    assert shim_mount.executable
    assert os.access(shim_mount.source, os.X_OK)
    assert oct(os.stat(shim_mount.source).st_mode)[-3:] == "755"
    assert oct(os.stat(targets[SANDBOX_CONDARC].source).st_mode)[-3:] == "644"


def test_prepare_without_shell_integration_skips_bash_env(tmp_path):
    mounts = prepare_conda_generated_files(
        state_root=tmp_path,
        profile="default",
        writable_root="/tmp/kimi-conda",
        original_root="/home/u/anaconda3",
        shell_integration=False,
    )
    targets = {m.target for m in mounts}
    assert SANDBOX_CONDA_BASH_ENV not in targets
    assert SANDBOX_CONDA_SHIM in targets


def test_generated_shim_runs_with_empty_pythonpath(tmp_path):
    """The generated shim must parse/validate with no PYTHONPATH and no access
    to the launcher source tree (mod_v2 §7.2, §17.1)."""
    gen = conda_generated_dir(tmp_path, "default")
    prepare_conda_generated_files(
        state_root=tmp_path,
        profile="default",
        writable_root="/cache/conda",
        original_root="/home/u/anaconda3",
        shell_integration=False,
    )
    shim = gen / "conda"
    # Drive the inlined policy directly (avoid exec'ing a real conda):
    script = (
        "import runpy\n"
        f"ns = runpy.run_path({str(shim)!r}, run_name='not_main')\n"
        "p = ns['parse_conda_argv'](['install', '-n', 'x', 'numpy'])\n"
        "assert p.is_mutation and p.command == ('install',)\n"
        "err = ns['CondaPolicyError']\n"
        "ok = False\n"
        "try:\n"
        "    ns['validate_conda_argv'](['install','-n','base','x'],"
        " writable_root='/cache/conda', readonly_root='/opt/kimi-conda/root',"
        " existing_envs_root='/opt/kimi-conda/existing-envs', env={},"
        " path_exists=lambda p: False)\n"
        "except err:\n"
        "    ok = True\n"
        "assert ok, 'base mutation should be rejected'\n"
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
