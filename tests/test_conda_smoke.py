"""End-to-end fake-conda smoke against real bubblewrap (mod_v2 §17.2).

Builds a throwaway fake conda root, launches the real sandbox via
``python -m kimi_sandbox.cli`` with conda enabled, and asserts the controlled
behaviour: queries/run/create work, mutations against the read-only host env are
rejected (by the shim) and the host conda tree is never modified.

Skipped automatically when ``bwrap`` is unavailable (e.g. CI without user
namespaces). Everything is created under ``$HOME`` because the launcher rejects
a conda root whose original path sits under a reserved tree such as ``/tmp``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

bwrap = shutil.which("bwrap")
pytestmark = pytest.mark.skipif(bwrap is None, reason="bubblewrap not installed")


_FAKE_CONDA = '''\
#!/usr/bin/env python3
import os, sys
args = [a for a in sys.argv[1:]]
def has(x): return x in args
# `conda shell.bash hook` -> emit a bash `conda` function. activate is handled
# in-shell; everything else dispatches to $CONDA_EXE (the controlled shim).
# Like real conda, activate re-exports CONDA_EXE back to the real binary, so a
# subsequent mutation bypasses the shim and is caught by the read-only FS.
if len(args) >= 2 and args[0] == "shell.bash" and args[1] == "hook":
    print('conda() {')
    print('  if [ "$1" = "activate" ]; then')
    print('    shift')
    print('    if [ -z "$1" ] || [ "$1" = base ]; then')
    print('      export CONDA_PREFIX="$KIMI_SANDBOX_CONDA_ORIGINAL_ROOT"')
    print('    else')
    print('      export CONDA_PREFIX="$KIMI_SANDBOX_CONDA_ORIGINAL_ROOT/envs/$1"')
    print('    fi')
    print('    export CONDA_EXE="$KIMI_SANDBOX_CONDA_ORIGINAL_ROOT/bin/conda"')
    print('    return 0')
    print('  fi')
    print('  "$CONDA_EXE" "$@"')
    print('}')
    sys.exit(0)
# locate first bare subcommand
sub = None
i = 0
while i < len(args):
    a = args[i]
    if a in ("-c", "--channel", "--solver", "--repodata-fn"):
        i += 2; continue
    if a.startswith("-"):
        i += 1; continue
    sub = a; break
if has("--version"):
    print("conda 0.0-fake"); sys.exit(0)
if sub == "env" and "list" in args:
    print("# conda environments:"); print("existing  /opt/kimi-conda/root/envs/existing"); sys.exit(0)
if sub == "run":
    print("RUN-OK"); sys.exit(0)
if sub == "config":
    print("channels: []"); sys.exit(0)
if sub == "clean":
    print("CLEAN-OK"); sys.exit(0)
def _target_prefix():
    name = prefix = None
    for j, a in enumerate(args):
        if a in ("-p", "--prefix") and j + 1 < len(args): prefix = args[j + 1]
        if a in ("-n", "--name") and j + 1 < len(args): name = args[j + 1]
    if prefix: return prefix
    if name == "base": return os.environ.get("KIMI_SANDBOX_CONDA_ORIGINAL_ROOT", "")
    if name:
        for d in os.environ.get("CONDA_ENVS_PATH", "").split(":"):
            cand = os.path.join(d, name)
            if os.path.isdir(cand): return cand
        return os.path.join(os.environ.get("CONDA_ENVS_PATH", "").split(":")[0], name)
    return os.environ.get("CONDA_PREFIX", "")
if sub in ("create",) or (sub == "env" and "create" in args):
    prefix = _target_prefix()
    if prefix:
        try:
            os.makedirs(prefix, exist_ok=True)
            print("CREATE-OK"); sys.exit(0)
        except OSError as e:
            print("CREATE-FAIL", e); sys.exit(1)
    print("CREATE-OK"); sys.exit(0)
if sub in ("install", "update", "upgrade", "remove", "uninstall"):
    # Attempt a real write into the target prefix so the read-only FS layer is
    # genuinely exercised when the shim is bypassed (e.g. post-activate).
    prefix = _target_prefix()
    try:
        marker = os.path.join(prefix, ".fake-mutate")
        with open(marker, "w") as fh:
            fh.write("x")
        os.unlink(marker)
        print("MUTATE-OK"); sys.exit(0)
    except OSError as e:
        print("MUTATE-FAIL", e); sys.exit(1)
print("OK"); sys.exit(0)
'''


def _build_world(base: Path, *, shell_integration: bool, existing_envs=()):
    root = base / "anaconda3"
    (root / "bin").mkdir(parents=True)
    py = root / "bin" / "python"
    py.write_text("#!/bin/sh\nexec python3 \"$@\"\n")
    py.chmod(0o755)
    conda = root / "bin" / "conda"
    conda.write_text(_FAKE_CONDA)
    conda.chmod(0o755)
    (root / "envs" / "existing" / "bin").mkdir(parents=True)

    project = base / "proj"
    project.mkdir()
    state = base / "state"
    state.mkdir()
    kimi = base / "kimi"
    kimi.write_text("#!/bin/sh\necho kimi\n")
    kimi.chmod(0o755)

    cfg_lines = [
        "persistent_cache = true",
        "conda_enabled = true",
        f'conda_root = "{root}"',
        'conda_writable = "cache"',
        f"conda_shell_integration = {'true' if shell_integration else 'false'}",
    ]
    if existing_envs:
        items = ", ".join(f'"{src}:{name}"' for src, name in existing_envs)
        cfg_lines.append(f"conda_existing_envs = [{items}]")
    cfg = base / "config.toml"
    cfg.write_text("\n".join(cfg_lines) + "\n")
    return {
        "base": base, "root": root, "project": project,
        "state": state, "kimi": kimi, "cfg": cfg,
    }


@pytest.fixture
def conda_world():
    base = Path(tempfile.mkdtemp(dir=os.path.expanduser("~"), prefix="kimicondasmoke_"))
    try:
        yield _build_world(base, shell_integration=False)
    finally:
        shutil.rmtree(base, ignore_errors=True)


def _run(world, command):
    proc = subprocess.run(
        [
            sys.executable, "-m", "kimi_sandbox.cli", str(world["project"]),
            "--config", str(world["cfg"]),
            "--state-root", str(world["state"]),
            "--kimi", str(world["kimi"]),
            "--no-seccomp", "--no-pin-mounts",
            "--exec", command,
        ],
        capture_output=True, text=True,
    )
    return proc


def test_conda_version(conda_world):
    p = _run(conda_world, "conda --version")
    assert p.returncode == 0, p.stderr
    assert "conda" in p.stdout


def test_conda_env_list(conda_world):
    p = _run(conda_world, "conda env list")
    assert p.returncode == 0, p.stderr
    assert "existing" in p.stdout


def test_conda_run_existing(conda_world):
    p = _run(conda_world, "conda run -n existing echo hi")
    assert p.returncode == 0, p.stderr
    assert "RUN-OK" in p.stdout


def test_conda_create_lands_in_writable(conda_world):
    p = _run(conda_world, "conda create -y -n newenv")
    assert p.returncode == 0, p.stderr
    # New env materialised in the host cache, not the read-only host root.
    cache_env = (
        conda_world["state"] / "profiles" / "default" / "cache"
        / "conda" / "envs" / "newenv"
    )
    assert cache_env.is_dir()
    assert not (conda_world["root"] / "envs" / "newenv").exists()


def test_conda_install_existing_rejected(conda_world):
    p = _run(conda_world, "conda install -y -n existing numpy")
    assert p.returncode != 0
    assert "refusing" in p.stdout + p.stderr


def test_conda_env_remove_existing_rejected(conda_world):
    p = _run(conda_world, "conda env remove -y -n existing")
    assert p.returncode != 0
    assert "refusing" in p.stdout + p.stderr


def test_conda_json_install_existing_rejected(conda_world):
    p = _run(conda_world, "conda --json install -n existing numpy")
    assert p.returncode != 0
    assert "refusing" in p.stdout + p.stderr


def test_conda_clean_force_pkgs_dirs_rejected(conda_world):
    p = _run(conda_world, "conda clean --force-pkgs-dirs -y")
    assert p.returncode != 0
    assert "refusing" in p.stdout + p.stderr


def test_conda_clean_all_allowed(conda_world):
    p = _run(conda_world, "conda clean --all -y")
    assert p.returncode == 0, p.stderr
    assert "CLEAN-OK" in p.stdout


def test_conda_config_show_allowed(conda_world):
    p = _run(conda_world, "conda config --show")
    assert p.returncode == 0, p.stderr


def test_conda_config_set_rejected(conda_world):
    p = _run(conda_world, "conda config --set channels defaults")
    assert p.returncode != 0
    assert "refusing" in p.stdout + p.stderr


def test_readonly_root_env_rm_fails(conda_world):
    p = _run(conda_world, "rm -rf /opt/kimi-conda/root/envs/existing; "
                          "test -e /opt/kimi-conda/root/envs/existing && echo STILL")
    # The read-only bind makes deletion fail; the env is still present.
    assert "STILL" in p.stdout
    assert (conda_world["root"] / "envs" / "existing").exists()


def test_conda_env_create_readonly_prefix_yaml_rejected(conda_world):
    yml = conda_world["project"] / "environment-readonly.yml"
    yml.write_text(f"prefix: {conda_world['root']}\ndependencies:\n  - python\n")
    p = _run(conda_world, "conda env create -f /workspace/environment-readonly.yml")
    assert p.returncode != 0
    assert "refusing" in (p.stdout + p.stderr).lower() or "read-only" in (p.stdout + p.stderr).lower()


# --- audit #7: activation-path (shell_integration=true) keeps shim authority --

@pytest.fixture
def conda_world_shell():
    base = Path(tempfile.mkdtemp(dir=os.path.expanduser("~"), prefix="kimicondashell_"))
    try:
        yield _build_world(base, shell_integration=True)
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_activate_base_works(conda_world_shell):
    p = _run(conda_world_shell, "conda activate base && echo ACT-OK")
    assert p.returncode == 0, p.stderr
    assert "ACT-OK" in p.stdout


def test_install_without_activate_rejected_by_shim(conda_world_shell):
    # Before any `conda activate`, the bash hook's CONDA_EXE override routes the
    # conda function through the shim, so a read-only mutation is rejected by
    # policy (early, with a clear message).
    p = _run(conda_world_shell, "conda install -n base numpy")
    assert p.returncode != 0
    assert "refusing" in p.stdout + p.stderr
    assert "MUTATE-OK" not in p.stdout


def test_install_after_activate_blocked_by_fs(conda_world_shell):
    # Real conda's `activate` re-exports CONDA_EXE to the real binary, so the
    # shim is bypassed afterwards. The read-only bind mount is the real
    # guarantee: the mutation fails and the host root is untouched.
    p = _run(
        conda_world_shell,
        "conda activate base && conda install -n base numpy",
    )
    assert p.returncode != 0
    assert not (conda_world_shell["root"] / ".fake-mutate").exists()


def test_create_in_activated_shell_allowed(conda_world_shell):
    p = _run(conda_world_shell, "conda activate base && conda create -y -n actcreate")
    assert p.returncode == 0, p.stderr
    cache_env = (
        conda_world_shell["state"] / "profiles" / "default" / "cache"
        / "conda" / "envs" / "actcreate"
    )
    assert cache_env.is_dir()


# --- audit #8: symlinked conda root + conda_existing_envs ------------------

@pytest.fixture
def conda_world_symlink():
    base = Path(tempfile.mkdtemp(dir=os.path.expanduser("~"), prefix="kimicondasym_"))
    try:
        # Real conda root at anaconda3_real; conda_root points at a symlink.
        real = base / "anaconda3_real"
        (real / "bin").mkdir(parents=True)
        py = real / "bin" / "python"
        py.write_text("#!/bin/sh\nexec python3 \"$@\"\n")
        py.chmod(0o755)
        conda = real / "bin" / "conda"
        conda.write_text(_FAKE_CONDA)
        conda.chmod(0o755)
        (real / "envs" / "existing" / "bin").mkdir(parents=True)
        link = base / "anaconda3"
        os.symlink(real, link)

        # An extra existing env mounted by name.
        extra = base / "extra_envs" / "tool"
        (extra / "bin").mkdir(parents=True)

        project = base / "proj"; project.mkdir()
        state = base / "state"; state.mkdir()
        kimi = base / "kimi"; kimi.write_text("#!/bin/sh\necho kimi\n"); kimi.chmod(0o755)
        cfg = base / "config.toml"
        cfg.write_text(
            "persistent_cache = true\n"
            "conda_enabled = true\n"
            f'conda_root = "{link}"\n'
            'conda_writable = "cache"\n'
            "conda_shell_integration = false\n"
            f'conda_existing_envs = ["{extra}:tool"]\n'
        )
        yield {
            "base": base, "real": real, "link": link, "extra": extra,
            "project": project, "state": state, "kimi": kimi, "cfg": cfg,
        }
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_symlinked_root_version_works(conda_world_symlink):
    p = _run(conda_world_symlink, "conda --version")
    assert p.returncode == 0, p.stderr
    assert "conda" in p.stdout


def test_existing_env_run_works(conda_world_symlink):
    p = _run(conda_world_symlink, "conda run -n tool echo hi")
    assert p.returncode == 0, p.stderr
    assert "RUN-OK" in p.stdout


def test_existing_env_install_rejected(conda_world_symlink):
    p = _run(conda_world_symlink, "conda install -y -n tool numpy")
    assert p.returncode != 0
    assert "refusing" in p.stdout + p.stderr
