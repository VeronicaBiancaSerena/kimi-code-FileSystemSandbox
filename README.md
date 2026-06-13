# kimi-sandbox

English | [日本語](README.ja.md)

A filesystem sandbox launcher for [Kimi Code](https://github.com/MoonshotAI),
built on [bubblewrap](https://github.com/containers/bubblewrap). It runs the
existing `kimi` CLI inside a restricted filesystem view: your project mounted
read-write at `/workspace`, an isolated `KIMI_CODE_HOME`, read-only system
directories, and tmpfs `HOME` / `/tmp`.

The core is a **filesystem damage-containment** layer. On top of that it adds
opt-in and on-by-default hardening: a multi-ABI TIOCSTI-blocking seccomp filter,
inode-pinned bind mounts, network isolation, a read-only project mode, cgroup
resource limits, a persistent cache, and extra mounts. None of these turn it
into a complete security sandbox — see [Security model](#security-model) for
exactly what it does and does not protect.

> ⚠️ **Read this first**
>
> - This is a **filesystem** sandbox, not a complete security sandbox.
> - By default it does **not** isolate network access (use `--no-network`).
> - It does **not** protect credentials placed inside `KIMI_CODE_HOME` from
>   commands run inside the sandbox.
> - It does **not** protect sensitive files inside the mounted project dir.
> - Do **not** use YOLO mode unless the project directory is disposable or
>   backed up.

## Contents

- [How it works](#how-it-works)
- [Requirements](#requirements)
- [Install](#install)
- [Usage](#usage)
- [Quick verification](#quick-verification)
- [Hardening flags](#hardening-flags)
- [Mount pinning](#mount-pinning)
- [Config file](#config-file)
- [Options](#options)
- [Inside the sandbox](#what-the-sandbox-looks-like-inside)
- [MCP and Skills](#mcp-and-skills)
- [Conda support](#conda-support)
- [Security model](#security-model)
- [Recommended Kimi configuration](#recommended-kimi-configuration)
- [Development](#development)
- [License](#license)

## How it works

`kimi-sandbox` is a thin **launcher**: it resolves and validates the host paths
you give it, constructs a single `bubblewrap` (`bwrap`) command, and execs it
with `kimi` (or your `--exec` command) inside. There is no daemon and no
persistent privilege; the launcher runs entirely as your unprivileged user and
relies on bubblewrap's unprivileged user namespaces.

The pipeline is deliberately split into pure, unit-testable stages:

1. **Resolve & validate** (`paths.py`) — expand `~`/vars, resolve symlinks,
   reject broad/system roots and dangerous path relationships.
2. **Build the argv** (`bwrap.py`) — turn a fully-resolved `SandboxConfig` into
   a `bwrap` argv. This stage performs only read-only filesystem probing and
   never launches anything, so the exact command is testable.
3. **Run** (`cli.py`) — open the seccomp filter fd and the inode-pinning fds,
   optionally wrap in `systemd-run` for cgroup limits, print the banner, and
   exec. The launcher returns Kimi's own exit code unchanged; launcher-level
   failures print `error:` lines and use a distinct code (`125`).

## Requirements

- Linux (or WSL2) with **unprivileged user namespaces** enabled.
- `bubblewrap` (`bwrap`) installed and on `PATH`. Version **≥ 0.5** is required
  for inode-pinned mounts (`--bind-fd`); pass `--no-pin-mounts` for older
  builds. Validated against bubblewrap 0.11.
- Python ≥ 3.11 (for the stdlib `tomllib` config parser).
- The `kimi` CLI installed (or pass `--kimi /path/to/kimi`).
- Optional: user `systemd` (for `--memory-max` / `--cpu-quota` / `--pids-max`).

Check bubblewrap:

```bash
bwrap --version
```

If it is missing, install it with your system package manager (the launcher
will **not** do this for you):

```bash
sudo apt install bubblewrap     # Debian/Ubuntu
```

## Install

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -U pip
pip install -e .
```

### Use it from any directory (no activation)

The editable install puts a `kimi-sandbox` entry point in the venv whose
shebang already points at the venv's Python, so symlinking it onto your `PATH`
lets you call it from anywhere without activating the venv:

```bash
# Assumes ~/.local/bin is on your PATH (it usually is).
ln -sf "$(pwd)/.venv/bin/kimi-sandbox" ~/.local/bin/kimi-sandbox
kimi-sandbox --version        # works from any directory
```

Alternatively, install it as an isolated tool with
[pipx](https://pipx.pypa.io/):

```bash
pipx install -e .             # adds kimi-sandbox to ~/.local/bin automatically
```

Either way the launcher still uses the host's `bwrap` and `kimi` from `PATH`
(or whatever you pass via `--bwrap` / `--kimi`).

## Usage

```bash
# Sandbox the current directory and start Kimi's TUI.
kimi-sandbox .

# Pass arguments through to kimi (everything after -- goes to kimi).
kimi-sandbox . -- --version
kimi-sandbox . -- -m kimi-code/kimi-for-coding

# Use a named profile (separate KIMI_CODE_HOME and credentials).
kimi-sandbox ~/work/project --profile work

# Inspect the generated bwrap command without running anything.
kimi-sandbox . --dry-run

# Print the resolved paths and mount plan (to stderr) before running.
kimi-sandbox . --debug

# Run a shell command inside the sandbox instead of kimi (smoke testing).
kimi-sandbox . --exec "pwd && id && touch /workspace/.write-test"
```

`--dry-run` prints the `bwrap` argv with **paths** (not fd numbers) for
readability, and notes on stderr which fds (seccomp, pinned mounts) would be
passed at run time. `--debug` prints a mount plan that mirrors the real run
(network, seccomp, pinning, resource limits, and the `systemd-run` wrapper).

### Quick verification

Start Kimi from the project directory:

```bash
kimi-sandbox .
```

Then ask Kimi to run this command inside the sandbox:

```bash
echo "HOME=$HOME"; echo x > /tmp/ks_test; echo y > "$HOME/ks_test"; echo z > /workspace/ks_test; touch /etc/ks_test || echo ETC_BLOCKED
```

Exit Kimi, return to the same project directory on the host, and check the
actual host filesystem:

```bash
[ -e /tmp/ks_test ] && echo "BAD: host /tmp leaked" || echo "OK: host /tmp clean"; [ -e "$HOME/ks_test" ] && echo "BAD: host HOME leaked" || echo "OK: host HOME clean"; [ -e ./ks_test ] && echo "OK: workspace write visible" || echo "BAD: ./ks_test not found"
```

The expected result is `HOME=/home/sandbox` inside Kimi; host `/tmp` and host
`$HOME` stay clean; and `./ks_test` exists in the project. `/workspace` is
writable by default; use `--read-only` when you want Kimi to review without
modifying the project.

### Hardening flags

```bash
# Cut off the network entirely (curl/pip/npm/MCP cannot reach out).
kimi-sandbox . --no-network

# Read-only review: /workspace cannot be modified.
kimi-sandbox . --read-only

# Cap resources via systemd-run (requires user systemd).
kimi-sandbox . --memory-max 2G --cpu-quota 150% --pids-max 512

# Persist the package/build cache across runs (per profile).
kimi-sandbox . --persistent-cache

# Mount extra host paths. Bare HOST lands at /mnt/<basename>.
kimi-sandbox . --ro-mount ~/reference-data        # -> /mnt/reference-data (ro)
kimi-sandbox . --ro-mount ~/data:/opt/data        # explicit target (ro)
kimi-sandbox . --rw-mount ~/scratch:/srv/scratch  # writable (DANGEROUS)

# The TIOCSTI seccomp filter is ON by default; disable only if needed.
kimi-sandbox . --no-seccomp

# Inode-pinned mounts are ON by default; disable for bubblewrap < 0.5.
kimi-sandbox . --no-pin-mounts
```

## Mount pinning

By default, every **host bind source** — the project dir, the profile
`kimi-code-home`, the persistent cache, the `kimi` binary, and any
`--ro/--rw-mount` source — is bound via an `O_PATH` file descriptor
(`--bind-fd` / `--ro-bind-fd`) instead of by path. The launcher opens each fd
once, after the path has been resolved and validated, and `bwrap` mounts that
exact inode.

This closes the time-of-check/time-of-use (TOCTOU) window in which a path could
be swapped for a symlink between validation and mount: the fd refers to the
inode that was checked, regardless of any later path changes. The only residual
race is the single `open()` walk itself (see [Residuals](#residuals-and-caveats)).

Pinning requires bubblewrap ≥ 0.5 (`--bind-fd`). On older builds, pass
`--no-pin-mounts` to fall back to path-based binds. `--dry-run` always prints
the readable path form regardless of this setting.

## Config file

Defaults can live in `~/.config/kimi-sandbox/config.toml` (override the path
with `--config`, or skip with `--no-config`). Any CLI flag overrides the file —
including turning a config-enabled boolean back **off**: `--network` overrides
`no_network = true`, `--writable` overrides `read_only = true`, `--seccomp`
overrides `no_seccomp = true`, and `--no-persistent-cache` overrides
`persistent_cache = true`.

Precedence is always **CLI flag > config file > built-in default**. Known keys
are type-checked (a wrong type is a hard error); unknown keys warn but do not
fail.

```toml
# ~/.config/kimi-sandbox/config.toml
profile          = "work"
no_network       = true
read_only        = false
persistent_cache = true
memory_max       = "2G"
cpu_quota        = "150%"
pids_max         = 512
ro_mounts        = ["~/reference:/opt/reference"]
rw_mounts        = []
```

## Options

| Option | Meaning |
| --- | --- |
| `PROJECT` | Host project dir mounted at `/workspace` (default: cwd). |
| `--profile NAME` | Sandbox profile name (default: `default`). |
| `--state-root PATH` | Host state root (default: `~/.local/state/kimi-sandbox`). |
| `--kimi PATH` | Explicit host path to the `kimi` executable. |
| `--bwrap PATH` | Explicit host path to the `bwrap` executable. |
| `--dry-run` | Print the bwrap command and exit. |
| `--debug` | Print resolved paths / mount plan to stderr, then run. |
| `--exec COMMAND` | Run `COMMAND` inside the sandbox (`bash -lc`, or `/bin/sh -c` if bash is absent). |
| `--read-only` / `--writable` | Mount `/workspace` read-only / read-write (override config). |
| `--no-network` / `--network` | Isolate (`--unshare-net`) / keep host network (override config). |
| `--no-seccomp` / `--seccomp` | Disable / install the TIOCSTI-blocking seccomp filter. |
| `--no-pin-mounts` | Disable inode-pinned (`--bind-fd`) mounts; use path binds (bwrap < 0.5). |
| `--persistent-cache` / `--no-persistent-cache` | Enable / disable the per-profile `/cache` bind. |
| `--memory-max SIZE` | cgroup memory cap via systemd-run (e.g. `2G`). |
| `--cpu-quota PCT` | cgroup CPU quota via systemd-run (e.g. `150%`). |
| `--pids-max N` | cgroup process/thread cap via systemd-run (`TasksMax`). |
| `--ro-mount HOST[:TARGET]` | Extra read-only mount (repeatable). |
| `--rw-mount HOST[:TARGET]` | Extra read-write mount (repeatable, **dangerous**). |
| `--config PATH` | Config file (default: `~/.config/kimi-sandbox/config.toml`). |
| `--no-config` | Ignore any config file. |
| `--unsafe-kimi-code-home PATH` | Custom host path for `/kimi-code-home` (**dangerous**). |
| `--version` | Print launcher version. |

Everything after a `--` separator is passed through to `kimi` unchanged.

## What the sandbox looks like inside

| Sandbox path | Backing | Access |
| --- | --- | --- |
| `/workspace` | your project | read-write (read-only with `--read-only`) |
| `/kimi-code-home` | profile state dir | read-write |
| `/kimi-code-home/skills` | host skill dir (via `profile_ro_mounts`) | read-only |
| `/home/sandbox/.kimi-code` | symlink → `/kimi-code-home` (`compat_kimi_home`) | — |
| `/cache` | profile cache dir | read-write (only with `--persistent-cache`) |
| `/home/sandbox` | tmpfs (`HOME`) | read-write, ephemeral |
| `/tmp`, `/run` | tmpfs | read-write, ephemeral |
| `/usr`, `/lib*`, `/bin`, `/sbin` | host | read-only |
| `/etc` | tmpfs + minimal binds | read-only (DNS/TLS only) |
| `/proc`, `/dev` | virtual | minimal |
| `/mnt/*`, `/opt/*`, … | extra `--ro/--rw-mount` | as requested |

On merged-`/usr` distros (`/bin → usr/bin`, etc.) the symlinks are recreated
inside the sandbox; on traditional layouts the real directories are ro-bound.
`/etc` is a tmpfs with only the DNS/TLS-relevant files bound read-only and then
remounted read-only, so no whole-`/etc` is exposed and nothing under it can be
created. `/etc/passwd` and `/etc/group` are deliberately **not** bound (to avoid
leaking host usernames); the only cost is a cosmetic `id` warning.

Environment markers set inside the sandbox:

```
KIMI_SANDBOX=1
KIMI_SANDBOX_MODE=workspace-write   # or read-only with --read-only
KIMI_SANDBOX_WORKSPACE=/workspace
KIMI_CODE_HOME=/kimi-code-home
HOME=/home/sandbox
```

The environment starts from `--clearenv`; only a short allowlist of
terminal/locale variables (`TERM`, `COLORTERM`, `LANG`, `LC_*`, `NO_COLOR`) is
forwarded, plus the launcher-controlled markers and a fixed `PATH`. Credentials
and agent sockets (`*_API_KEY`, `AWS_*`, `GITHUB_TOKEN`, `SSH_AUTH_SOCK`, …) are
never forwarded. Verify from within Kimi's `Bash`: `echo $KIMI_SANDBOX`.

## MCP and Skills

The sandbox can make your existing MCP servers and Kimi skills available
**without copying anything into the project**. Their source, scripts and
runtimes are bind-mounted **read-only** from their current host locations;
inside the sandbox Kimi, Bash, hooks and MCP subprocesses can read and execute
them but cannot modify them. Writable state stays separate: profile config and
credentials in `/kimi-code-home`, caches in `/cache`, scratch in `/tmp`.

Everything is driven from the default config so that, after a one-time setup,
plain `kimi-sandbox .` "just works":

```bash
kimi-sandbox init-integrations          # print a suggested config (dry-run)
kimi-sandbox init-integrations --write  # create ~/.config/kimi-sandbox/config.toml
kimi-sandbox doctor --config-check      # validate config + mount plan
kimi-sandbox .                          # run with MCP + skills mounted
```

### Config keys

- `profile_ro_mounts` — read-only sub-mounts under `/kimi-code-home`, written as
  `HOST:RELATIVE_TARGET`. Used to expose a skill directory at
  `/kimi-code-home/skills`. The target is confined to a `..`-free relative path;
  it can never escape the profile tree.
- `ro_mounts` — read-only mounts for MCP source and language runtimes, mounted
  under `/opt/...` (e.g. `/opt/kimi-mcp/...`, `/opt/kimi-runtime/...`).
- `env_keep` — host variables to forward **explicitly** (e.g. a token). Nothing
  sensitive is forwarded by default; no globbing.
- `env_set` — fixed environment values from config.
- `compat_kimi_home` — when true (default), `/home/sandbox/.kimi-code` is a
  symlink to `/kimi-code-home`, so tools that probe `~/.kimi-code` still resolve
  to the persistent profile. Toggle with `--compat-kimi-home` /
  `--no-compat-kimi-home`.
- `conda_enabled` / `conda_root` / `conda_writable` / `conda_shell_integration`
  / `conda_existing_envs` — controlled conda integration (see
  [Conda support](#conda-support)). The host conda root is mounted read-only;
  new envs are created in `/cache/conda` (or `/tmp/kimi-conda`).

`env_keep` / `env_set` may **not** override launcher-reserved variables (`HOME`,
`PATH`, `KIMI_CODE_HOME`, `TMPDIR`, `XDG_*`, `KIMI_SANDBOX*`, and — when conda is
enabled — `CONDARC`, `CONDA_ENVS_PATH`, `CONDA_PKGS_DIRS`, `BASH_ENV`, the
`KIMI_SANDBOX_CONDA_*` anchors); attempting to do so is a hard error. For the
cache location use `persistent_cache = true`, not an `XDG_CACHE_HOME` override.

### Example `config.toml`

```toml
profile = "default"
persistent_cache = true
compat_kimi_home = true

profile_ro_mounts = [
  "~/.kimi-code/skills:skills",
]

ro_mounts = [
  "~/mcp/github_mcp:/opt/kimi-mcp/github_mcp",
  "~/miniconda3/envs/github-mcp:/opt/kimi-runtime/github-mcp",
]

env_keep = [
  "GITHUB_TOKEN",
]

[env_set]
PYTHONDONTWRITEBYTECODE = "1"
KIMI_SANDBOX_MCP_ROOT = "/opt/kimi-mcp"
```

### Skill dotenv files

Do not put API keys or other secret values directly in a skill's `SKILL.md` or
in Kimi prompt text. If a skill-backed tool reads a dotenv file from its source
tree or from a host config directory, expose that file to the sandbox with a
read-only mount and point the tool at the in-sandbox path with `env_set`.

This is useful for tools such as `imagegencli_codex`: the sandbox uses an
isolated `HOME=/home/sandbox`, so a host file like
`~/.config/imagegencli_codex/.env` is not visible unless you mount it explicitly.
Mount the dotenv file under `/opt` and set the tool's env-file variable:

```toml
ro_mounts = [
  "~/skills/imagegencli_codex/.env:/opt/imagegencli_codex.env",
]

[env_set]
IMAGEGENCLI_CODEX_ENV_FILE = "/opt/imagegencli_codex.env"
```

Keep the mount read-only, avoid logging the file contents, and verify presence
without printing the secret:

```bash
kimi-sandbox doctor --config-check
kimi-sandbox . --exec 'test -r /opt/imagegencli_codex.env'
kimi-sandbox . --exec 'conda run -n imagegencli_codex imagegencli_codex doctor'
```

### MCP server config must use in-sandbox paths

The Kimi MCP config (verified layout: `~/.kimi-code/mcp.json`, schema
`{"mcpServers": {<name>: {...}}}`) must reference **sandbox** paths, not host
paths, unless the host path is mounted to the same location:

```json
{
  "mcpServers": {
    "github": {
      "command": "/opt/kimi-runtime/github-mcp/bin/python",
      "args": ["/opt/kimi-mcp/github_mcp/server.py"],
      "env": {
        "PYTHONDONTWRITEBYTECODE": "1",
        "MCP_CACHE_DIR": "/cache/github-mcp"
      }
    }
  }
}
```

The launcher does **not** auto-write this file: confirm your installed Kimi's
MCP config format, then edit `mcp.json` in the sandbox profile by hand
(`~/.local/state/kimi-sandbox/profiles/<profile>/kimi-code-home/mcp.json`).
`doctor` reports whether it can recognize the layout but never treats an
unrecognized one as a launcher failure.

### Write conventions (so read-only mounts stay read-only)

- MCP source (`/opt/kimi-mcp/...`) and runtimes (`/opt/kimi-runtime/...`) must
  not be written to: no logs, caches, databases or `.pyc`.
- Set `PYTHONDONTWRITEBYTECODE=1` to stop Python writing `__pycache__` into the
  read-only source.
- Redirect runtime caches into `/cache/<server>` (or `/tmp`), never into the
  runtime mount. Choose only what each server needs:

```toml
persistent_cache = true

[env_set]
PYTHONDONTWRITEBYTECODE = "1"
PIP_CACHE_DIR = "/cache/pip"
UV_CACHE_DIR = "/cache/uv"
NPM_CONFIG_CACHE = "/cache/npm"
HF_HOME = "/cache/huggingface"
TORCH_HOME = "/cache/torch"
```

- Server-level persistent cache → `/cache/<server>`; profile-level state →
  `/kimi-code-home/mcp-state/<server>`; scratch → `/tmp`.

Use `/cache/<server>` for data you are happy to lose/rebuild, and
`/kimi-code-home/mcp-state/<server>` for state that must survive across runs and
travel with the profile (small databases, indexes, registration files). Both
live on writable mounts, so the read-only MCP source stays untouched. Point each
server at these from its MCP `env` block:

```json
{
  "mcpServers": {
    "github": {
      "command": "/opt/kimi-runtime/github-mcp/bin/python",
      "args": ["/opt/kimi-mcp/github_mcp/server.py"],
      "env": {
        "PYTHONDONTWRITEBYTECODE": "1",
        "MCP_CACHE_DIR": "/cache/github-mcp",
        "MCP_STATE_DIR": "/kimi-code-home/mcp-state/github-mcp"
      }
    }
  }
}
```

`/cache` is only present when `persistent_cache = true`; `mcp-state` lives under
the always-writable profile home, so it needs no extra flag. Create the
subdirectory from the server itself (e.g. `os.makedirs(..., exist_ok=True)`) —
the launcher does not pre-create per-server cache/state dirs.

### Verify

```bash
# Skill dir is readable but not writable inside the sandbox:
kimi-sandbox . --exec \
  'test -r /kimi-code-home/skills && ! touch /kimi-code-home/skills/.w'

# A minimal MCP source runs, stays read-only, and writes its cache to /cache:
kimi-sandbox . --exec \
  'python3 /opt/kimi-mcp/fake/fake_server.py && ! touch /opt/kimi-mcp/fake/.w'
```

Do **not** mount your real `~/.kimi-code` writable into the sandbox; use a
`profile_ro_mounts` entry for just the skill directory instead.

### `doctor` and `init-integrations` notes

- `doctor --config-check` validates the config and the dry-run mount plan. It
  treats advisory issues as `WARN` (never a failure): a recognized-but-unknown
  Kimi layout, an empty `skills/` mountpoint, runtime mounts without a cache
  redirect, MCP commands whose paths are not covered by any configured mount
  target, and any `rw_mounts` (writable mounts are flagged because the
  integration model is read-only). Hard problems (unparseable config, missing
  or invalid mount sources) fail with a non-zero exit. The advisory symlink
  scan of skill sources is bounded by default; pass `doctor --deep` for an
  exhaustive scan of very large skill trees.
- `init-integrations` is dry-run by default; `--write` creates a missing config
  or appends only **entirely absent** top-level keys (after a timestamped
  backup). When a list key such as `profile_ro_mounts` already exists but is
  missing a suggested item, it does not edit the array — it prints the exact
  item(s) to add by hand. A comment-preserving in-place array merge is deferred
  to v2.

### Worked example: bringing existing MCP servers + skills into the sandbox

The sandbox runs Kimi with its **own** profile home, so its MCP servers and
skills must be described in terms of *in-sandbox* paths. The recipe below is the
exact process used to wire a real multi-server setup; replace `~/...` and the
example names with your own.

**1. Install the launcher activation-free** (see [Install](#install)) and confirm
`kimi-sandbox --version` works from any directory:

```bash
ln -s "$(pwd)/.venv/bin/kimi-sandbox" ~/.local/bin/kimi-sandbox   # or use pipx
kimi-sandbox --version
```

**2. Inventory what your host Kimi uses.** Look at `~/.kimi-code/mcp.json`
(server commands + any path-valued `env`) and `~/.kimi-code/skills/` (usually
symlinks to real skill dirs). For each server, note its **interpreter** and
**source** — that determines how to relocate it (see the cheat sheet below).

**3. Write `~/.config/kimi-sandbox/config.toml`.** Mount every host path a server
needs read-only under `/opt`, enable conda if any server uses a conda env, and
expose skills under `/kimi-code-home/skills`:

```toml
profile = "default"
persistent_cache = true
compat_kimi_home = true

# Enable if any MCP server runs from a conda env. Binds the host conda root
# read-only at its real path, so a "conda env interpreter" command resolves.
conda_enabled = true
conda_root = "~/anaconda3"
conda_writable = "cache"
conda_shell_integration = true

ro_mounts = [
  # MCP source trees (each contains the server's venv + code)
  "~/mcp/servers:/opt/kimi-mcp/servers",
  # an editable package's real source (cheat sheet: "editable install")
  "~/projects/math-mcp/src:/opt/kimi-mcp/math-mcp-src",
  # a non-system interpreter that a venv points at (cheat sheet: "uv / pyenv")
  "~/.local/share/uv/python:/opt/kimi-mcp/uv-python",
]

# Skills exposed at /kimi-code-home/skills/<name> (read-only). Point at the REAL
# skill directories, not the ~/.kimi-code/skills symlinks.
profile_ro_mounts = [
  "~/skills/bizplan-writer:skills/bizplan-writer",
  "~/skills/canvas:skills/canvas",
]

[env_set]
PYTHONDONTWRITEBYTECODE = "1"   # do not write __pycache__ into read-only /opt
```

> If the sandbox profile's `skills/` already contains stale symlinks from a
> previous setup, remove them first (`find ~/.local/state/kimi-sandbox/profiles/\
> default/kimi-code-home/skills -maxdepth 1 -type l -delete`) — the launcher
> must create real-directory mountpoints, and `doctor` flags a symlinked
> mountpoint as a failure.

**4. Generate the sandbox `mcp.json`.** It lives in the sandbox **profile home**,
not your real `~/.kimi-code`, and must use in-sandbox paths. The simplest way is
to transform the host file: rewrite host prefixes to their `/opt` targets, add
`PYTHONPATH` for editable installs, and keep secret env values verbatim:

```python
# write_sandbox_mcp.py  (run with any python3; prints no secret values)
import json, os
HOST = os.path.expanduser("~/.kimi-code/mcp.json")
SBX  = os.path.expanduser(
    "~/.local/state/kimi-sandbox/profiles/default/kimi-code-home/mcp.json")
os.makedirs(os.path.dirname(SBX), mode=0o700, exist_ok=True)

# host path prefix -> in-sandbox ro_mounts target
REWRITE = {os.path.expanduser("~/mcp/servers"): "/opt/kimi-mcp/servers"}
# servers installed with `pip install -e .` need their source on PYTHONPATH
# (the .pth in the venv hard-codes a host path that is absent in the sandbox)
PYTHONPATH = {"math": "/opt/kimi-mcp/math-mcp-src"}

def rw(s):
    if isinstance(s, str):
        for h, o in REWRITE.items():
            s = s.replace(h, o)
    return s

cfg = json.load(open(HOST))
out = {"mcpServers": {}}
for name, spec in cfg["mcpServers"].items():
    spec = dict(spec)
    spec["command"] = rw(spec.get("command"))
    spec["args"] = [rw(a) for a in spec.get("args", [])]
    env = {k: rw(v) for k, v in (spec.get("env") or {}).items()}  # keeps secrets
    if name in PYTHONPATH:
        env["PYTHONPATH"] = PYTHONPATH[name]
    if env:
        spec["env"] = env
    out["mcpServers"][name] = spec

json.dump(out, open(SBX, "w"), indent=2)
os.chmod(SBX, 0o600)
```

**5. Validate, then probe each server inside the sandbox.**

```bash
kimi-sandbox doctor          # expect: 0 failed, 0 warning(s)

# Confirm every server's interpreter + deps import inside the box, e.g.:
mkdir -p /tmp/probe
kimi-sandbox /tmp/probe --exec \
  'PYTHONPATH=/opt/kimi-mcp/math-mcp-src \
   conda run -n math python -c "import math_mcp; print(\"ok\")"'
kimi-sandbox /tmp/probe --exec 'ls /kimi-code-home/skills'   # skills present
```

**6. Run it.** `kimi-sandbox .` now starts Kimi with the servers and skills. The
sandbox profile is separate from your real `~/.kimi-code`, so log in once inside
the sandbox; that login persists across runs in the per-profile state dir.

#### Interpreter relocation cheat sheet

The one thing that breaks "it works on the host but not in the sandbox" is a path
baked into a venv/interpreter that is not mounted. Match your server to a row:

| Server's interpreter / install | What it needs in the sandbox |
|---|---|
| venv on the **system** `python3` (`/usr/bin/python3`) | Mount the source tree under `/opt`; the venv runs from its `/opt` path as-is (system python is already mounted read-only). |
| venv on a **uv / pyenv** python | Also mount that interpreter (e.g. `~/.local/share/uv/python`) and run it **directly** (`<interp> -m <module>`) with `PYTHONPATH` = venv `site-packages` + source — the venv's own `python` symlink points at an unmounted host path. |
| **conda env** interpreter | Set `conda_enabled = true`; the conda root is bound at its real path, so `<conda>/envs/<env>/bin/python` (or `conda run -n <env>`) resolves. New envs go to `/cache/conda`. |
| **editable** install (`pip install -e`) | Mount the real source and add it to that server's `PYTHONPATH`; the `.pth` in site-packages hard-codes a host path that is absent in the sandbox. |
| command/args/env naming **host paths** | Rewrite them to the `/opt` mount targets in the sandbox `mcp.json`. |

`doctor` confirms each mount source exists, that the recognized Kimi layout uses
sandbox (`/opt/...`) paths, and (when conda is enabled) that the conda setup is
valid — all without launching the Kimi TUI. Your real `~/.kimi-code` is never
mounted writable.

## Conda support

`kimi-sandbox` can expose your existing conda to the sandbox while keeping every
pre-existing host conda env strictly read-only. Enable it in the config:

```toml
persistent_cache = true
conda_enabled = true
conda_root = "~/anaconda3"
conda_writable = "cache"          # new envs/pkgs go to /cache/conda; "tmp" = ephemeral
conda_shell_integration = true    # enables `conda activate` in bash -lc

# Optional extra existing envs that live outside conda_root/envs:
# conda_existing_envs = ["~/somewhere/envs/foo:foo"]
```

Inside the sandbox, `conda` then works directly:

```bash
conda --version
conda env list
conda run -n math-mcp python -m math_mcp
conda activate math-mcp && python ...        # needs conda_shell_integration = true
conda create -n sandbox-dev python=3.11      # -> /cache/conda/envs/sandbox-dev
```

Recommended MCP server forms:

```json
{ "command": "conda", "args": ["run", "-n", "math-mcp", "python", "-m", "math_mcp"] }
```

```json
{ "command": "bash", "args": ["-lc", "conda activate math-mcp && python -m math_mcp"] }
```

### What is read-only vs writable

| Content | Mode |
|---|---|
| Host conda root + existing envs/packages | read-only (`/opt/kimi-conda/root` + original path) |
| New envs / downloaded packages | writable (`/cache/conda` or `/tmp/kimi-conda`) |
| `conda` entry point | launcher-generated shim at `/sandbox/bin/conda` |

These therefore **fail** by design (host envs are read-only):

```bash
conda install -n existing-env some-package   # rejected by the shim; FS is read-only
conda env remove -n existing-env             # rejected
rm -rf /opt/kimi-conda/root/envs/existing    # read-only filesystem
```

The shim parses the full conda argv (including `--json`, `--name=`, `--prefix`,
`env update -f environment.yml`, and unambiguous option abbreviations like
`--pre`) and refuses any mutation that does not provably target
`/cache/conda/envs/<name>`. `conda config` allows only read-only queries
(`--show`/`--get`/...); `conda clean` is limited to the writable package cache
(`--force-pkgs-dirs` is refused). The original host conda root is also bound at
its real absolute path so console-script shebangs (`#!/home/you/anaconda3/...`)
keep resolving. Run `kimi-sandbox doctor` to validate the whole conda setup.

> **The shim is a convenience layer, not the security boundary.** Host conda
> content is protected by the read-only bind mounts plus the launcher-forced
> `CONDA_ENVS_PATH`/`CONDA_PKGS_DIRS`, which hold even if the shim is bypassed
> (e.g. by invoking the real conda binary directly, or after `conda activate`,
> which re-exports `CONDA_EXE` back to the real binary). A `conda install`
> against a host env therefore *fails* either way — early via the shim, or at
> the filesystem layer — and never modifies host content.

> New envs created with `conda_writable = "tmp"` do **not** persist across
> sandbox runs. With `no_network = true`, `conda create/install` can only use
> already-available local channels/cache.

## Security model

### Protects against (filesystem damage containment)
- Kimi or its shell commands writing to `/etc` or other system dirs.
- Kimi touching other projects in your home directory.
- Commands writing to the host `/tmp`.
- Kimi reading your real `~/.ssh`, `~/.aws`, `~/.config`, `~/.kimi-code`, etc.
  (the real `HOME` is never mounted).
- **Terminal injection (TIOCSTI/TIOCLINUX).** A seccomp filter is installed by
  default that makes those ioctls fail with `EPERM`, so a sandboxed process
  cannot push keystrokes into your controlling terminal to be run un-sandboxed
  after Kimi exits (CVE-2017-5226 class). The filter keeps the TUI working
  (unlike `--new-session`). It is **multi-ABI**: on x86_64 it blocks the ioctl
  on the native, x32 *and* i386 (`int 0x80`) syscall ABIs; on aarch64 on the
  native and 32-bit ARM ABIs. Any other architecture value is **denied** rather
  than allowed, so a process cannot escape by switching syscall ABI.
- **Bind-source swap (TOCTOU).** Host bind sources are pinned to the validated
  inode via `--bind-fd` (see [Mount pinning](#mount-pinning)), so a path cannot
  be redirected via a symlink between validation and mount.

### Opt-in hardening

- **Network isolation** (`--no-network`): adds `--unshare-net`, so `curl`,
  `pip`, `npm`, and MCP servers cannot reach the network. *Off by default*
  because Kimi itself needs network for model calls — see the credential note.
  It is all-or-nothing: the main Kimi process also loses model connectivity.
- **Read-only project** (`--read-only`): `/workspace` is mounted read-only;
  Kimi can read and reason about the code but cannot modify it.
- **Resource limits** (`--memory-max` / `--cpu-quota` / `--pids-max`): the
  sandbox is wrapped in a transient `systemd-run --user --scope` with the
  corresponding cgroup `MemoryMax` / `CPUQuota` / `TasksMax` properties —
  containing runaway memory, CPU, and fork-bomb behaviour. Requires user
  systemd. (The seccomp filter fd and pinned-mount fds are inherited correctly
  across this wrapper.)

### Does NOT protect against

- **Network access by default** — without `--no-network` there is no network
  isolation. Even with it, the *main Kimi process* loses model connectivity too
  (there is no model/tool network split — see below).
- Reading or deleting files **inside** the mounted project (`/workspace` is rw
  unless `--read-only`).
- Credentials placed in `/kimi-code-home` being read by sandboxed `Bash`
  commands — the main process and its child commands share one filesystem view.
- Kernel or bubblewrap escapes, or a user who deliberately `--rw-mount`s a
  sensitive directory in.

### Residuals and caveats

- **TIOCSTI seccomp filter scope.** The filter is a small classic-BPF program
  built in-process (no libseccomp dependency). It is installed only on
  architectures it knows (`x86_64`, `aarch64`); on any other architecture the
  launcher prints a note and continues *without* it (degrading to the
  undefended state rather than refusing to run). On a supported architecture it
  covers every syscall ABI the kernel accepts for that machine (native +
  32-bit/x32 compat) and denies foreign ABIs, so there is no "switch ABI to
  bypass" escape. `--no-seccomp` disables it entirely. Many modern kernels also
  restrict TIOCSTI independently (`dev.tty.legacy_tiocsti_restrict`).
- **Mount-pinning residual.** Inode pinning collapses the TOCTOU window to the
  launcher's single `open(O_PATH)` walk of the already-resolved, symlink-free
  path; after that the fd is fixed. With `--no-pin-mounts` (or bubblewrap < 0.5)
  the wider validation→mount window returns. Either way, do not point the
  sandbox at directories that untrusted users can modify mid-launch.
- **No model/tool network split.** `--no-network` is all-or-nothing. A future
  version could keep model connectivity in the main process while denying it to
  tool subprocesses via a credential broker; this is not implemented.
- `/dev/shm` inside the sandbox is a writable (but per-namespace, ephemeral)
  tmpfs from bubblewrap's default device set; it cannot affect the host.
- **Conda mutation policy is not the boundary.** When conda is enabled, the
  generated `/sandbox/bin/conda` shim rejects mutations of read-only host envs
  *early* and with a clear message, but it is a convenience/early-rejection
  layer only. The real conda binary stays directly reachable inside the sandbox
  (e.g. `/opt/kimi-conda/root/bin/conda`), so the guarantee that host envs and
  packages cannot be modified comes from the **read-only bind mounts** plus the
  launcher-forced `CONDA_ENVS_PATH`/`CONDA_PKGS_DIRS`/`CONDARC` (which steer all
  writes into the sandbox-writable area). Those hold even if the shim is bypassed
  entirely. Do not rely on the shim's argv parsing for isolation.
- **Privileges and capabilities.** The launcher runs unprivileged and relies on
  bubblewrap's unprivileged user namespace. Any capabilities a sandboxed process
  holds exist only inside that user namespace and map to *nobody* on the host,
  so they confer no host privilege; the sandbox is not setuid and does not need
  root. The launcher does not add `--cap-add`, and there is no need to drop
  capabilities explicitly because bubblewrap already clears the ambient/bounding
  set for the sandboxed process by default. The real boundary is the kernel's
  user-namespace and bubblewrap implementation (see the escape caveat above).

### Credential boundary

The Kimi main process and any `Bash`/MCP/hook commands it spawns run in the
**same** sandbox filesystem. Anything Kimi can read, those commands can read.
Therefore:

- Use a **dedicated, low-privilege** Kimi account or API key for the profile.
- Do not put production credentials in a sandbox profile.
- `--unsafe-kimi-code-home` exposes the chosen directory to every sandboxed
  process; only use it knowingly. Broad/system paths and the real `~/.kimi-code`
  are always rejected.

## Recommended Kimi configuration

In the sandbox profile's Kimi config, prefer manual approvals — the sandbox is a
hard boundary, permissions are the interactive confirmation layer, and the two
together are stronger:

```toml
default_permission_mode = "manual"
```

Avoid YOLO mode: even sandboxed, it can delete or rewrite all of `/workspace`.

## Development

```bash
pip install -e ".[dev]"
python -m pytest                 # unit tests (no real bwrap needed)
bash tests/smoke/run_smoke.sh    # smoke tests (requires real bwrap + kimi)
```

The unit suite covers path validation, the `bwrap` argv (including inode
pinning, merged- vs non-merged-`/usr`, and the minimal `/etc`), config-file
precedence, seccomp-fd plumbing across the `systemd-run` wrapper, and the
seccomp filter itself — the seccomp tests include a small in-process cBPF
interpreter that *proves* the multi-ABI block behaviour (native/x32/i386/ARM
denied, other ioctls allowed, foreign arch denied) rather than only inspecting
the bytecode.

The smoke script maps to the design document's acceptance criteria (section 28)
plus the hardening features (`--no-network`, `--read-only`, multi-ABI seccomp,
mount pinning, persistent cache, extra mounts, resource limits, config file, and
the CLI-overrides-config negators).

Continuous integration (`.github/workflows/ci.yml`) runs the unit suite on
Python 3.11–3.13 and a `bwrap` dry-run sanity check on every push and PR; the
full smoke suite (real `bwrap`, fake `kimi` stub, best-effort user-systemd
resource limits) is available via the manual `workflow_dispatch` trigger.

## License

Released under the MIT License. See the [`LICENSE`](LICENSE) file for the full
license text.
