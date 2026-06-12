# kimi-sandbox

A filesystem sandbox launcher for [Kimi Code](https://github.com/MoonshotAI),
built on [bubblewrap](https://github.com/containers/bubblewrap). It runs the
existing `kimi` CLI inside a restricted filesystem view: your project mounted
read-write at `/workspace`, an isolated `KIMI_CODE_HOME`, read-only system
directories, and tmpfs `HOME`/`/tmp`.

The core is a **filesystem damage-containment** layer. On top of that, v2 adds
opt-in hardening: a TIOCSTI-blocking seccomp filter (on by default), network
isolation, read-only mode, cgroup resource limits, a persistent cache, and
extra mounts. None of these turn it into a complete security sandbox — see
[Security model](#security-model) for exactly what it does and does not protect.

> ⚠️ **Read this first**
>
> - This is a filesystem sandbox, **not** a complete security sandbox.
> - By default it does **not** isolate network access (use `--no-network`).
> - It does **not** protect credentials placed inside `KIMI_CODE_HOME` from
>   commands run inside the sandbox.
> - It does **not** protect sensitive files inside the mounted project dir.
> - Do **not** use YOLO mode unless the project directory is disposable or
>   backed up.
>
> 这是文件系统沙箱，不是完整安全沙箱。默认不隔离网络（可用 `--no-network` 开启隔离）；
> 不能防止沙箱内命令读取 `/kimi-code-home` 中的 Kimi 凭据；不能防止 Kimi 读取当前
> 项目目录中的敏感文件。除非项目目录可随时恢复，否则不建议使用 YOLO 模式。

## Requirements

- Linux (or WSL2) with **unprivileged user namespaces** enabled.
- `bubblewrap` (`bwrap`) installed and on `PATH`.
- Python ≥ 3.11 (for the stdlib `tomllib` config parser).
- The `kimi` CLI installed (or pass `--kimi /path/to/kimi`).
- Optional: user `systemd` (for `--memory-max`/`--cpu-quota`/`--pids-max`).

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

### Hardening flags (v2)

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
```

### Config file

Defaults can live in `~/.config/kimi-sandbox/config.toml` (override the path
with `--config`, or skip with `--no-config`). Any CLI flag overrides the file —
including turning a config-enabled boolean back **off**: `--network` overrides
`no_network = true`, `--writable` overrides `read_only = true`, `--seccomp`
overrides `no_seccomp = true`, and `--no-persistent-cache` overrides
`persistent_cache = true`.

```toml
# ~/.config/kimi-sandbox/config.toml
profile        = "work"
no_network     = true
read_only      = false
persistent_cache = true
memory_max     = "2G"
cpu_quota      = "150%"
pids_max       = 512
ro_mounts      = ["~/reference:/opt/reference"]
rw_mounts      = []
```

### Options

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

Environment markers set inside the sandbox:

```
KIMI_SANDBOX=1
KIMI_SANDBOX_MODE=workspace-write   # or read-only with --read-only
KIMI_SANDBOX_WORKSPACE=/workspace
KIMI_CODE_HOME=/kimi-code-home
HOME=/home/sandbox
```

Verify from within Kimi's `Bash`: `echo $KIMI_SANDBOX`.

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
  on the native, x32 *and* i386 (`int 0x80`) syscall ABIs, and on aarch64 on the
  native and 32-bit ARM ABIs; any other architecture value is denied rather than
  allowed, so a process cannot escape by switching ABI. See the residual note
  below for the exact caveats.

### Opt-in hardening

- **Network isolation** (`--no-network`): adds `--unshare-net`, so `curl`,
  `pip`, `npm`, and MCP servers cannot reach the network. *Off by default*
  because Kimi itself needs network for model calls — see the credential note.
- **Read-only project** (`--read-only`): `/workspace` is mounted read-only;
  Kimi can read and reason about the code but cannot modify it.
- **Resource limits** (`--memory-max`/`--cpu-quota`/`--pids-max`): the sandbox
  is wrapped in a transient `systemd-run --user --scope` with the corresponding
  cgroup `MemoryMax`/`CPUQuota`/`TasksMax` properties — containing runaway
  memory, CPU, and fork-bomb behaviour. Requires user systemd.

### Does NOT protect against

- **Network access by default** — without `--no-network` there is no network
  isolation. Even with it, the *main Kimi process* loses model connectivity
  too (v1/v2 do not split tool traffic from model traffic — see below).
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
  undefended state rather than refusing to run). On a supported architecture the
  filter covers every syscall ABI the kernel accepts for that machine (native +
  32-bit/x32 compat) and denies foreign ABIs, so there is no "switch ABI to
  bypass" escape. `--no-seccomp` disables it entirely. Many modern kernels also
  restrict TIOCSTI independently (`dev.tty.legacy_tiocsti_restrict`).
- **Time-of-check/time-of-use (TOCTOU) on resolved paths.** Host paths
  (`PROJECT`, `KIMI_CODE_HOME`, extra mounts) are resolved to symlink-free
  absolute paths and validated, then handed to `bwrap` as bind sources. If an
  attacker can swap one of those paths for a symlink in the narrow window
  between resolution and the `bwrap` mount, the mount could target a different
  directory. This is inherent to launching an external mounter and is low risk
  on a single-user host; do not point the sandbox at directories writable by
  untrusted users mid-launch.
- **No model/tool network split.** `--no-network` is all-or-nothing. A future
  version could keep model connectivity in the main process while denying it to
  tool subprocesses via a credential broker (see the design addendum, §33.5);
  v2 does not implement this.
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
python -m pytest            # unit tests (no real bwrap needed)
bash tests/smoke/run_smoke.sh   # smoke tests (requires real bwrap + kimi)
```

The smoke script maps to the acceptance criteria in the design document
(section 28) plus the v2 hardening features (`--no-network`, `--read-only`,
seccomp, persistent cache, extra mounts, resource limits, config file, and the
CLI-overrides-config negators).

Continuous integration (`.github/workflows/ci.yml`) runs the unit suite on
Python 3.11–3.13 and a `bwrap` dry-run sanity check on every push and PR.

## License

MIT
