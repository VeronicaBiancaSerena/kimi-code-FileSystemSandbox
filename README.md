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
- [Hardening flags](#hardening-flags)
- [Mount pinning](#mount-pinning)
- [Config file](#config-file)
- [Options](#options)
- [Inside the sandbox](#what-the-sandbox-looks-like-inside)
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
