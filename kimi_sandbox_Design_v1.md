# Kimi Sandbox Design v1

## 0. 文档状态

- 文档名称：`kimi_sandbox_Design_v1.md`
- 设计版本：v1
- 目标阶段：MVP，可用于下一步 vibe coding
- 当前决策：Python + venv + bubblewrap，不使用 Docker，不做网络隔离
- 产物类型：独立 launcher，用外层沙箱启动现有 `kimi` CLI

本文档只描述第一版设计。它的目标是先把 Kimi Code 放进一个文件系统受限的运行环境里跑起来，观察实际体验，再决定是否继续做更强的网络隔离、凭据隔离或 Kimi-native 沙箱改造。

## 1. 背景

Kimi Code 目前已经有权限审批、permission rules、hooks、MCP allowlist/blocklist 等机制，但这些机制主要属于应用层策略：

- 它们可以决定工具调用是否需要询问用户。
- 它们可以拒绝某些显式匹配到的工具调用。
- 它们可以提示或拦截部分高危操作。

但这些机制不是硬沙箱。尤其是：

- `Bash` 一旦被允许，shell 内部可以执行复杂命令。
- hooks 属于辅助拦截，不能作为唯一安全边界。
- 应用层路径检查无法完全替代 OS 层文件系统隔离。
- MCP stdio server 可能在会话启动时执行本地命令。
- YOLO 或误配置可能让写文件和执行命令更宽松。

因此，本项目 v1 不试图重写 Kimi Code，而是在 Kimi Code 外面加一层 launcher。launcher 使用 Linux 的 `bubblewrap` 创建受限文件系统视图，然后在其中启动现有 `kimi` CLI。

## 2. v1 目标

v1 的核心目标是实现一个可用的 filesystem sandbox：

- 用户在真实项目目录中运行 `kimi-sandbox .`。
- launcher 把当前项目挂载到沙箱内 `/workspace`。
- `/workspace` 可读写。
- 系统目录只读。
- 不挂载真实用户 `HOME`。
- 使用独立的 `KIMI_CODE_HOME`。
- `/tmp` 使用沙箱内临时目录。
- 不使用 Docker。
- 不隔离网络。
- 不修改 Kimi Code 源码。
- 尽量保持 Kimi TUI 的正常交互体验。

第一版最重要的安全收益是：减少 Kimi Code 或其执行的 shell 命令误改宿主机其他目录的风险。

## 3. v1 非目标

v1 明确不解决以下问题：

- 不做网络隔离。
- 不限制出站 HTTP/HTTPS。
- 不阻止 `curl`、`npm install`、`pip install`、MCP server 联网。
- 不实现 Kimi 主进程和 Kimi `Bash` 子命令之间的网络权限差异。
- 不实现 secret broker。
- 不保证 Kimi 凭据对 Kimi 启动的 `Bash` 命令不可见。
- 不做 macOS Seatbelt。
- 不做 Windows AppContainer。
- 不做 Docker 镜像。
- 不修改 Kimi Code 的 `BashTool` 或 Kaos 执行层。
- 不实现完整供应链安全。
- 不保证项目目录内敏感文件不可读。

换句话说，v1 是 filesystem damage containment，不是完整安全沙箱。

## 4. 总体结论

推荐第一版技术路线：

```text
Python launcher
  -> build bubblewrap command
  -> mount project at /workspace
  -> mount isolated KIMI_CODE_HOME
  -> mount system directories read-only
  -> create tmpfs for /tmp and HOME
  -> run existing kimi binary
```

不推荐第一版使用 Docker。原因：

- Docker 对交互式 TUI 更重。
- 需要维护 image。
- 需要处理容器内登录和凭据。
- volume ownership 容易变复杂。
- 与用户本机已有 `kimi` CLI 的升级路径不一致。

Python + venv 适合第一版。launcher 逻辑很薄，标准库基本足够：

- `argparse`
- `dataclasses`
- `os`
- `pathlib`
- `shutil`
- `subprocess`
- `sys`

## 5. 预期使用方式

第一版命令建议：

```bash
kimi-sandbox .
```

等价于：

```bash
kimi-sandbox --project . --profile default -- kimi
```

也可以传递 Kimi 参数：

```bash
kimi-sandbox . -- --version
```

或：

```bash
kimi-sandbox . -- -m kimi-code/kimi-for-coding
```

调试 bubblewrap 命令：

```bash
kimi-sandbox . --dry-run
```

运行沙箱内调试命令，不启动 Kimi：

```bash
kimi-sandbox . --exec "pwd && id && touch /workspace/.sandbox-write-test"
```

说明：`--exec` 是建议加入的开发调试参数，用来验证挂载和权限。它可以在正式使用时保留，也可以只作为开发期功能。

### 5.1 启动提示

v1 默认必须在启动 Kimi 前打印一次简短 banner，让用户确认当前确实通过 launcher 进入沙箱。

建议格式：

```text
Kimi Sandbox active
  mode: workspace-write
  project: /host/project -> /workspace (rw)
  kimi home: /host/state/kimi-code-home -> /kimi-code-home (rw)
  home: isolated tmpfs
  tmp: isolated tmpfs
  network: enabled
```

这个 banner 不应打印 API key、token、Kimi config 内容或完整环境变量值。

同时必须在沙箱内设置：

```text
KIMI_SANDBOX=1
KIMI_SANDBOX_MODE=workspace-write
KIMI_SANDBOX_WORKSPACE=/workspace
```

这样用户可以在 Kimi 的 `Bash` 中执行 `echo $KIMI_SANDBOX` 验证自己处于沙箱中。

## 6. 安全模型

### 6.1 v1 想防什么

v1 主要防这些情况：

- Kimi 误写 `/etc`。
- Kimi 误写用户主目录其他项目。
- Kimi 误删非当前项目目录。
- Kimi 的 shell 命令误操作宿主机全局路径。
- 项目内命令试图写系统目录。
- 命令把临时文件写入宿主 `/tmp`。
- Kimi 读取真实 `~/.ssh`、`~/.aws`、`~/.config` 等未挂载目录。

### 6.2 v1 不防什么

v1 不防这些情况：

- Kimi 或子命令联网。
- 当前项目目录内的敏感文件被读取。
- `/workspace` 内文件被删除或篡改。
- Kimi 凭据被 Kimi 启动的 `Bash` 命令读取。
- 恶意依赖通过网络下载更多代码。
- 恶意代码在沙箱内消耗 CPU、内存、磁盘配额。
- 利用 kernel 或 bubblewrap 漏洞逃逸。
- 用户主动把敏感目录挂进沙箱。

### 6.3 凭据边界

这是 v1 最重要的注意点。

Kimi 主进程需要模型凭据。由于 v1 是外层 launcher，Kimi 主进程和它启动的 `Bash` 命令处于同一个沙箱文件系统视图。只要 Kimi 能读某个凭据文件，Kimi 启动的 shell 命令理论上也能读。

因此 v1 不能提供“凭据只给 Kimi 主进程，不给 Bash 子命令”的能力。

v1 的建议策略：

- 不挂载真实 `~/.kimi-code`。
- 不挂载真实 `HOME`。
- 使用独立的 sandbox profile home。
- 使用专用 Kimi account 或低权限 API key。
- 避免把生产凭据放进 sandbox profile。
- 文档明确提示：sandbox profile 内的 Kimi 凭据对沙箱内进程可见。

如果未来要解决这个问题，需要进入 v2 或 v3：

- Kimi-native 工具执行沙箱。
- 凭据 broker。
- 模型请求代理。
- 主进程和工具进程分离权限。

## 7. 运行平台

v1 只支持 Linux。

可接受环境：

- Linux desktop/server
- WSL2，前提是 bubblewrap 可用且 user namespace 可用

暂不支持：

- macOS
- Windows 原生
- 无法使用 user namespace 的 Linux 环境

启动前检查：

```bash
bwrap --version
```

如果没有安装 bubblewrap，launcher 应直接失败并给出提示，例如：

```text
bubblewrap is required.
Install it with your system package manager, for example:
  sudo apt install bubblewrap
```

注意：这个安装提示只是建议，不应由 launcher 自动执行。

## 8. 目录设计

建议仓库结构：

```text
kimi-sandbox/
  pyproject.toml
  README.md
  kimi_sandbox/
    __init__.py
    cli.py
    config.py
    bwrap.py
    paths.py
    errors.py
  tests/
    test_bwrap_command.py
    test_paths.py
```

如果当前只是快速 vibe coding，也可以先更简单：

```text
kimi-sandbox/
  pyproject.toml
  kimi_sandbox/
    __init__.py
    cli.py
```

但建议最晚在第二轮重构时拆出 `bwrap.py`，因为挂载策略会快速变复杂。

## 9. Python 环境

使用 venv：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -U pip
pip install -e .
```

`pyproject.toml` 建议：

```toml
[project]
name = "kimi-sandbox"
version = "0.1.0"
description = "Filesystem sandbox launcher for Kimi Code"
requires-python = ">=3.10"
dependencies = []

[project.scripts]
kimi-sandbox = "kimi_sandbox.cli:main"
```

测试依赖可以先不用。如果希望用 pytest，可以加 optional extra：

```toml
[project.optional-dependencies]
dev = ["pytest"]
```

但第一版核心功能不需要第三方运行时依赖。

## 10. Host 状态目录

v1 需要一个 host 侧状态目录，用来保存 sandbox profile 的 Kimi 数据。

推荐默认：

```text
~/.local/state/kimi-sandbox/profiles/default/kimi-code-home
```

沙箱内挂载为：

```text
/kimi-code-home
```

并设置：

```text
KIMI_CODE_HOME=/kimi-code-home
```

这样做的好处：

- 不挂载真实 `~/.kimi-code`。
- 不把 Kimi profile 默认放进项目目录。
- 多项目可以共享一个 sandbox profile。
- 可以通过 `--profile` 切换不同凭据和配置。

需要明确：

- `/kimi-code-home` 对沙箱内进程可读写。
- 它不等于 secret boundary。
- 不应放入生产高权限凭据。

常规参数：

```bash
kimi-sandbox . --profile work
kimi-sandbox . --state-root ~/.local/state/kimi-sandbox
```

高级危险参数：

```bash
kimi-sandbox . --unsafe-kimi-code-home /some/host/path
```

`--unsafe-kimi-code-home` 不应作为普通使用路径。它只用于调试、迁移或明确接受凭据暴露风险的场景。只要使用该参数，launcher 必须在启动前打印醒目警告，说明该目录会被沙箱内 Kimi 和所有子进程读写。

优先级建议：

```text
--unsafe-kimi-code-home 显式路径
  > --state-root + --profile 推导路径
  > 默认 ~/.local/state/kimi-sandbox/profiles/default/kimi-code-home
```

硬性校验规则：

- 默认 `kimi_code_home` 必须位于 `state_root/profiles/<profile>/kimi-code-home`。
- 默认不允许把真实 `~/.kimi-code` 挂载为 `/kimi-code-home`。
- 默认不允许把真实 `HOME` 或其父目录挂载为 `/kimi-code-home`。
- 默认不允许 `project_dir` 等于、包含或位于 `kimi_code_home` 内。
- 默认不允许 `state_root` 位于 `project_dir` 内。
- 默认不允许 `project_dir` 位于 `state_root` 内。
- 使用 `--unsafe-kimi-code-home` 时仍应拒绝 `/`、`/home`、真实 `HOME`、`/etc`、`/usr`、`/var`、`/tmp` 这类宽泛或系统目录。
- 如果 `--unsafe-kimi-code-home` 指向真实 `~/.kimi-code`，默认应拒绝；除非未来增加更明确的 `--i-know-this-exposes-real-kimi-home` 之类强制参数，v1 不提供这种绕过。

## 11. 沙箱内路径布局

推荐沙箱内布局：

```text
/workspace          当前项目，可读写
/kimi-code-home     Kimi Code 专用状态目录，可读写
/home/sandbox       临时 HOME，tmpfs
/tmp                临时目录，tmpfs
/run                临时运行目录，tmpfs
/proc               procfs
/dev                最小设备视图
/usr                宿主只读挂载
/bin                宿主只读挂载或 /usr/bin 兼容
/lib                宿主只读挂载
/lib64              宿主只读挂载，如果存在
/etc                只读或最小必要文件只读
```

环境变量：

```text
HOME=/home/sandbox
KIMI_CODE_HOME=/kimi-code-home
KIMI_SANDBOX=1
KIMI_SANDBOX_MODE=workspace-write
KIMI_SANDBOX_WORKSPACE=/workspace
PWD=/workspace
PATH=/sandbox/bin:/usr/local/bin:/usr/bin:/bin
TMPDIR=/tmp
XDG_CACHE_HOME=/home/sandbox/.cache
XDG_CONFIG_HOME=/home/sandbox/.config
XDG_DATA_HOME=/home/sandbox/.local/share
```

是否需要持久化 cache，v1 默认不需要。让 cache 走 tmpfs 更干净。后续如果用户反馈频繁下载太慢，可以加：

```text
~/.cache/kimi-sandbox/profiles/default -> /cache
XDG_CACHE_HOME=/cache
```

## 12. bubblewrap 挂载策略

### 12.1 基本原则

不要简单使用：

```bash
--dev-bind / /
```

因为这样会把宿主根文件系统过度暴露。v1 应该明确挂载必要目录。

基本原则：

- 项目目录 read-write。
- Kimi profile 目录 read-write。
- 系统运行时目录 read-only。
- HOME tmpfs。
- `/tmp` tmpfs。
- 不挂载真实 `/home`。
- 不挂载真实 SSH、cloud credentials、browser profile。
- 不使用 `--unshare-net`，因为 v1 不做网络隔离。

namespace 策略：

- 必须使用独立 mount namespace；这是 bubblewrap 挂载隔离的基础。
- 必须使用 `--unshare-pid`，让沙箱内进程处于独立 PID namespace。
- 建议使用 `--unshare-ipc`，隔离 System V IPC 和 POSIX message queues。
- 建议使用 `--unshare-uts`，隔离 hostname/domainname 视图。
- 不使用 `--unshare-net`，因为 v1 明确不隔离网络。
- 不依赖 user namespace 之外的特权能力；如果宿主禁用 unprivileged user namespace，launcher 应失败并提示。

### 12.2 推荐 bwrap 骨架

以下是概念示例，实际实现要根据路径存在性动态拼装：

```bash
bwrap \
  --unshare-pid \
  --unshare-ipc \
  --unshare-uts \
  --die-with-parent \
  --proc /proc \
  --dev /dev \
  --tmpfs /tmp \
  --tmpfs /run \
  --tmpfs /home \
  --dir /home/sandbox \
  --dir /etc \
  --dir /sandbox \
  --dir /sandbox/bin \
  --ro-bind /usr /usr \
  --ro-bind /bin /bin \
  --ro-bind /lib /lib \
  --ro-bind /lib64 /lib64 \
  --ro-bind /etc/resolv.conf /etc/resolv.conf \
  --ro-bind /etc/hosts /etc/hosts \
  --ro-bind /etc/nsswitch.conf /etc/nsswitch.conf \
  --ro-bind /etc/ssl /etc/ssl \
  --ro-bind /etc/ca-certificates /etc/ca-certificates \
  --bind /host/project /workspace \
  --bind /host/state/kimi-code-home /kimi-code-home \
  --ro-bind /host/path/to/kimi /sandbox/bin/kimi \
  --clearenv \
  --setenv HOME /home/sandbox \
  --setenv KIMI_CODE_HOME /kimi-code-home \
  --setenv KIMI_SANDBOX 1 \
  --setenv KIMI_SANDBOX_MODE workspace-write \
  --setenv KIMI_SANDBOX_WORKSPACE /workspace \
  --setenv PATH /sandbox/bin:/usr/local/bin:/usr/bin:/bin \
  --setenv TMPDIR /tmp \
  --setenv TERM "$TERM" \
  --chdir /workspace \
  /sandbox/bin/kimi
```

实际实现细节：

- 如果 `/bin` 是 `/usr/bin` 的 symlink，`--ro-bind /usr /usr` 可能已经足够，但显式处理更稳。
- 如果 `/lib64` 不存在，不要挂载它。
- 如果某个 `/etc/*` 路径不存在，不要挂载它。
- v1 默认不整体挂载 `/etc`，而是只读挂载 DNS、hosts、NSS 和 CA certificates 所需路径。
- 如果最小 `/etc` 挂载导致某些发行版兼容问题，后续可以提供显式 `--compat-bind-etc`，但不应作为默认行为。
- `--compat-bind-etc` 的语义必须明确标注为“扩大 host 配置暴露面以换取兼容性”。

### 12.3 网络相关挂载

v1 不隔离网络，因此不使用：

```bash
--unshare-net
```

为了让 DNS 和 HTTPS 正常工作，需要让沙箱看到必要配置：

- `/etc/resolv.conf`
- `/etc/hosts`
- `/etc/nsswitch.conf`
- CA certificates

v1 默认不应直接只读挂载整个 `/etc`。如果最小挂载清单导致 DNS、TLS 或 locale 兼容问题，可以通过显式兼容选项扩大挂载范围，但这不应成为默认安全基线。

### 12.4 TUI 相关

Kimi 是交互式 TUI。为了尽量保证体验：

- 不要断开 stdin/stdout/stderr。
- 继承当前终端。
- 传递 `TERM`。
- 可以传递 `COLORTERM`。
- 可以传递 `LANG` 和 `LC_*`。
- 不要强制 `TERM=dumb`。

环境变量建议 allowlist，而不是完整继承：

```text
TERM
COLORTERM
LANG
LC_ALL
LC_CTYPE
NO_COLOR
```

不要默认继承：

```text
SSH_AUTH_SOCK
AWS_ACCESS_KEY_ID
AWS_SECRET_ACCESS_KEY
GITHUB_TOKEN
OPENAI_API_KEY
KIMI_API_KEY
ANTHROPIC_API_KEY
GOOGLE_API_KEY
```

如果用户确实想传递某个环境变量，后续可以加：

```bash
--env NAME=value
--pass-env NAME
```

v1 可以先不实现 `--pass-env`，避免误传凭据。

## 13. Kimi binary 发现策略

launcher 需要找到宿主机上的 `kimi` 可执行文件。

默认逻辑：

1. 如果用户传了 `--kimi /path/to/kimi`，使用该路径。
2. 否则使用 `shutil.which("kimi")`。
3. 对找到的路径做 `realpath()`。
4. 检查文件存在且可执行。
5. 将该文件只读挂载到沙箱内 `/sandbox/bin/kimi`。

示例：

```text
host: /home/user/.kimi-code/bin/kimi
sandbox: /sandbox/bin/kimi
```

注意点：

- 如果 `kimi` 是 symlink，需要解析到真实文件。
- 如果 `kimi` 是 shell wrapper，单文件挂载可能不够。
- 如果发现是 symlink 或脚本，第一版可以给出警告。
- 如果执行失败，再考虑只读挂载其父目录。

更稳的 fallback：

```text
--ro-bind <real_kimi_parent> /sandbox/kimi-bin
command = /sandbox/kimi-bin/<basename>
```

但挂载父目录可能暴露更多文件。第一版可以先单文件挂载，遇到兼容问题再放宽。

fallback 约束：

- 只能挂载包含 Kimi executable 的最窄目录。
- 不允许把真实 `~/.kimi-code` 整体作为 fallback 挂载。
- 不允许把真实 HOME、`/usr`、`/opt` 这类宽泛目录作为 fallback 挂载，除非它们本来已经作为只读系统运行时目录挂载。
- 如果 fallback 挂载目录超出最窄 executable 目录，必须在 `--debug` 输出中醒目标注。

## 14. 项目目录策略

用户传入项目目录：

```bash
kimi-sandbox /path/to/project
```

处理规则：

1. 展开 `~`。
2. 转成绝对路径。
3. 调用 `resolve()`，解析 symlink。
4. 检查路径存在。
5. 检查是目录。
6. 拒绝明显危险目录。

默认拒绝：

```text
/
/home
/home/<user>
/etc
/usr
/var
/tmp
```

理由：如果用户把整个 HOME 当项目挂进去，沙箱就失去主要价值。

如确实需要，后续可以加强制参数：

```bash
--allow-broad-project-root
```

v1 可以先不加这个参数，直接拒绝并提示用户选择更具体的项目目录。

## 15. 权限模式

v1 只实现一种实际模式：

```text
workspace-write
```

语义：

- `/workspace` 可读写。
- `/kimi-code-home` 可读写。
- `/tmp`、`/run`、`/home/sandbox` 可写但临时。
- 系统目录只读。
- 其他宿主用户目录不可见。

未来可以扩展：

```text
read-only
workspace-write
danger-full-access
```

但 v1 不需要实现完整 mode matrix。为了避免过度设计，第一版 CLI 可以只提供默认 workspace-write。

## 16. 配置文件

v1 可以不引入配置文件，只靠 CLI 参数。

如果需要配置文件，建议后续使用：

```text
~/.config/kimi-sandbox/config.toml
```

但这会引入 TOML 解析。Python 3.11 有 `tomllib` 只能读，Python 3.10 没有内置 TOML。为了保持 v1 标准库无依赖，建议暂不做配置文件。

v1 参数足够：

```bash
kimi-sandbox PROJECT
kimi-sandbox PROJECT --profile NAME
kimi-sandbox PROJECT --state-root PATH
kimi-sandbox PROJECT --kimi PATH
kimi-sandbox PROJECT --dry-run
kimi-sandbox PROJECT --debug
kimi-sandbox PROJECT --exec COMMAND
kimi-sandbox PROJECT --unsafe-kimi-code-home PATH
kimi-sandbox PROJECT -- KIMI_ARGS...
```

## 17. CLI 参数设计

建议：

```text
usage:
  kimi-sandbox [PROJECT] [options] [-- KIMI_ARGS...]

positional:
  PROJECT
    Host project directory to mount as /workspace.
    Default: current directory.

options:
  --profile NAME
    Sandbox profile name. Default: default.

  --state-root PATH
    Host state root. Default: ~/.local/state/kimi-sandbox.

  --kimi PATH
    Explicit host path to kimi executable.

  --dry-run
    Print the generated bwrap command and exit.

  --debug
    Print resolved paths and mount plan before running.

  --exec COMMAND
    Run COMMAND inside the sandbox instead of kimi. Useful for smoke tests.

  --unsafe-kimi-code-home PATH
    Use a custom host path for /kimi-code-home.
    Dangerous: this exposes the directory to all sandboxed processes.
    Must not accept broad system paths or the real ~/.kimi-code in v1.

  --version
    Print launcher version.
```

关于 `--`：

- `kimi-sandbox . -- --version` 把 `--version` 传给 Kimi。
- `kimi-sandbox . -- -m model` 把 `-m model` 传给 Kimi。

如果没有 `KIMI_ARGS`，默认运行：

```text
/sandbox/bin/kimi
```

如果有 `KIMI_ARGS`：

```text
/sandbox/bin/kimi <KIMI_ARGS>
```

如果使用 `--exec COMMAND`：

```text
/bin/bash -lc <COMMAND>
```

> v2 注（§39.11 C1）：实际实现优先 `/bin/bash`、`/usr/bin/bash`（`-lc`），
> 宿主无 bash 时回退 `/bin/sh -c`。

## 18. 命令构建伪代码

```python
def main(argv=None) -> int:
    args = parse_args(argv)

    project_dir = resolve_project_dir(args.project)
    state_root = resolve_state_root(args.state_root)
    profile_dir = state_root / "profiles" / args.profile
    if args.unsafe_kimi_code_home:
        kimi_code_home = resolve_unsafe_kimi_code_home(args.unsafe_kimi_code_home)
    else:
        kimi_code_home = profile_dir / "kimi-code-home"
    validate_path_relationships(
        project_dir=project_dir,
        state_root=state_root,
        kimi_code_home=kimi_code_home,
        unsafe_kimi_code_home=bool(args.unsafe_kimi_code_home),
    )
    kimi_path = resolve_kimi(args.kimi)
    bwrap_path = resolve_bwrap()

    ensure_dir(kimi_code_home)

    config = SandboxConfig(
        project_dir=project_dir,
        kimi_code_home=kimi_code_home,
        kimi_path=kimi_path,
        env=build_env_allowlist(),
        command=build_inner_command(args),
    )

    command = build_bwrap_command(config)

    if args.debug:
        print_mount_plan(config)

    if args.dry_run:
        print_shell_escaped(command)
        return 0

    print_start_banner(config)
    return run(command)
```

## 19. 数据结构建议

```python
from dataclasses import dataclass
from pathlib import Path

@dataclass(frozen=True)
class SandboxConfig:
    project_dir: Path
    kimi_code_home: Path
    kimi_path: Path
    inner_command: list[str]
    env: dict[str, str]
    debug: bool = False

@dataclass(frozen=True)
class Mount:
    kind: str  # "bind", "ro-bind", "tmpfs", "proc", "dev", "dir"
    source: Path | None
    target: str
```

`build_bwrap_command(config)` 只负责把 config 转成 argv list，不执行命令。

这样测试可以直接断言 argv：

```python
cmd = build_bwrap_command(config)
assert "--bind" in cmd
assert "/workspace" in cmd
```

## 20. bwrap argv 构建原则

必须用 `subprocess.run(argv)`，不要拼 shell 字符串。

正确：

```python
subprocess.run(command)
```

错误：

```python
subprocess.run(" ".join(command), shell=True)
```

原因：

- 避免 shell quoting 问题。
- 避免路径中空格导致错误。
- 避免命令注入。

`--dry-run` 打印时可以使用：

```python
shlex.join(command)
```

## 21. 环境变量策略

使用 `--clearenv`，然后显式传递。

基础 env：

```text
HOME=/home/sandbox
KIMI_CODE_HOME=/kimi-code-home
KIMI_SANDBOX=1
KIMI_SANDBOX_MODE=workspace-write
KIMI_SANDBOX_WORKSPACE=/workspace
PATH=/sandbox/bin:/usr/local/bin:/usr/bin:/bin
TMPDIR=/tmp
XDG_CACHE_HOME=/home/sandbox/.cache
XDG_CONFIG_HOME=/home/sandbox/.config
XDG_DATA_HOME=/home/sandbox/.local/share
```

从宿主 allowlist 传递：

```text
TERM
COLORTERM
LANG
LC_ALL
LC_CTYPE
NO_COLOR
```

不要默认传：

```text
HOME
PATH
SSH_AUTH_SOCK
GPG_AGENT_INFO
AWS_*
GCP_*
GOOGLE_*
AZURE_*
GITHUB_*
OPENAI_*
ANTHROPIC_*
KIMI_*
MOONSHOT_*
```

其中 `KIMI_CODE_HOME` 和 `KIMI_SANDBOX*` 由 launcher 自己设置，不继承宿主环境。

## 22. Kimi profile 初始化

第一次运行时，`kimi-code-home` 是空目录。Kimi 会要求登录或配置。

launcher 可以做最小初始化：

```text
mkdir -p <kimi-code-home>
```

不要自动复制真实 `~/.kimi-code`。

可选初始化：

如果 `<kimi-code-home>/tui.toml` 不存在，可以写入一个保守配置：

```toml
[upgrade]
auto_install = false
```

但这会让 launcher 写 Kimi 配置文件。v1 可以先不写，保持 launcher 简单。

如果后续需要减少自动升级干扰，再加 `--init-profile` 或默认初始化。

## 23. MCP 与 hooks 策略

v1 不特殊处理 Kimi MCP 和 hooks。

实际效果：

- user-level MCP 位于 `/kimi-code-home/mcp.json`，会在沙箱内生效。
- project-level MCP 位于 `/workspace/.kimi-code/mcp.json`，会在沙箱内生效。
- hooks 位于 `/kimi-code-home/config.toml` 中声明，执行时也在沙箱内。

风险：

- project-level stdio MCP server 仍然可以执行本地命令。
- 因为 v1 不隔离网络，MCP server 可以联网。
- 因为 `/workspace` 可写，MCP server 可以改项目。

收益：

- MCP 和 hooks 只能看到沙箱挂载出来的文件系统。
- 它们默认看不到真实 HOME。
- 它们不能写系统目录。

建议文档提示：

- 不要在不信任项目中启用 project-level MCP。
- 保持 Kimi permission mode 为 `manual`。
- 不建议在 v1 sandbox 中使用 YOLO，除非项目目录可以被完全重建。

## 24. Kimi permission 建议

launcher 不应强行修改 Kimi permission mode。

但 README 应建议用户在 sandbox profile 内使用：

```toml
default_permission_mode = "manual"
```

理由：

- sandbox 是硬边界。
- permission 是交互确认层。
- 两者结合更稳。

不建议 v1 默认 YOLO。即使有沙箱，YOLO 仍可能删除或大规模改写 `/workspace`。

## 25. 资源限制

v1 不实现资源限制。

不限制：

- CPU
- memory
- disk usage
- process count
- network bandwidth

原因：

- bubblewrap 本身主要做 namespace 和 mount 隔离。
- 资源限制需要 cgroup、systemd-run、ulimit 或外部 supervisor。
- 第一版先保证 TUI 可用和文件系统边界。

未来可选：

```bash
systemd-run --user --scope -p MemoryMax=4G ...
```

或：

```bash
ulimit -n
ulimit -u
```

但不进入 v1。

## 26. 日志与审计

v1 不记录完整命令日志，避免记录用户输入和敏感内容。

可以在 `--debug` 下打印：

- resolved project path
- resolved Kimi binary
- resolved KIMI_CODE_HOME
- mount plan
- environment variable names
- final bwrap command

不要默认打印：

- 环境变量值，除非是 launcher 自己设置的非敏感值。
- Kimi config 内容。
- API key。
- token。

## 27. 错误处理

推荐错误类型：

```text
SandboxError
  MissingDependencyError
  InvalidProjectError
  KimiNotFoundError
  BubblewrapFailedError
```

用户可读错误示例：

```text
error: bubblewrap not found

Install bubblewrap with your system package manager.
On Ubuntu/Debian:
  sudo apt install bubblewrap
```

```text
error: refusing to mount broad project root: /home/user

Choose a specific project directory instead, for example:
  kimi-sandbox ~/work/my-project
```

```text
error: kimi executable not found

Install Kimi Code first or pass:
  kimi-sandbox . --kimi /path/to/kimi
```

`subprocess.run` 返回 Kimi 的退出码。launcher 不应把 Kimi 非零退出统一改成自己的错误码。

## 28. 验收标准

第一版完成后，应满足以下验收条件。

### 28.1 基础启动

```bash
kimi-sandbox . -- --version
```

应能在沙箱内启动 Kimi 并输出版本。

### 28.2 项目可写

```bash
kimi-sandbox . --exec "touch /workspace/.sandbox-write-test && test -f /workspace/.sandbox-write-test"
```

应成功，并且宿主项目目录能看到 `.sandbox-write-test`。

### 28.3 系统目录只读

```bash
kimi-sandbox . --exec "touch /etc/kimi-sandbox-test"
```

应失败。

### 28.4 真实 HOME 不可见

```bash
kimi-sandbox . --exec "test ! -d /home/$USER/.ssh"
```

应成功。更稳的测试是：

```bash
kimi-sandbox . --exec "ls /home"
```

输出应只包含或主要包含 `sandbox`，不应暴露真实用户 home。

### 28.5 `/tmp` 隔离

```bash
kimi-sandbox . --exec "touch /tmp/kimi-sandbox-test"
```

应成功，但宿主 `/tmp/kimi-sandbox-test` 不应出现。

### 28.6 网络未隔离

```bash
kimi-sandbox . --exec "python3 - <<'PY'
import urllib.request
print(urllib.request.urlopen('https://example.com', timeout=10).status)
PY"
```

如果宿主网络正常，应成功。这用于确认 v1 的预期：网络没有隔离。

### 28.7 KIMI_CODE_HOME 隔离

```bash
kimi-sandbox . --exec "test \"$KIMI_CODE_HOME\" = /kimi-code-home && touch \"$KIMI_CODE_HOME/test-file\""
```

应成功，并且宿主 state profile 目录中能看到 `test-file`。

### 28.8 dry run

```bash
kimi-sandbox . --dry-run
```

应打印 bwrap argv，并且不启动 Kimi。

### 28.9 启动 banner

```bash
kimi-sandbox . --exec "true"
```

启动前应打印类似以下提示：

```text
Kimi Sandbox active
  mode: workspace-write
  network: enabled
```

banner 不应包含 API key、token、Kimi config 内容或敏感环境变量值。

### 28.10 沙箱环境标识

```bash
kimi-sandbox . --exec "test \"$KIMI_SANDBOX\" = 1 && test \"$KIMI_SANDBOX_MODE\" = workspace-write"
```

应成功。

### 28.11 `/etc` 最小挂载

```bash
kimi-sandbox . --dry-run
```

输出中不应包含：

```text
--ro-bind /etc /etc
```

输出中应包含 DNS、hosts、NSS 或 CA certificates 的最小挂载项。不存在的路径可以跳过，但不能退化为默认整体挂载 `/etc`。

## 29. 单元测试建议

不用真的运行 bwrap 的测试：

- `resolve_project_dir` 正确解析相对路径。
- `resolve_project_dir` 拒绝 `/`。
- `resolve_project_dir` 拒绝真实 HOME。
- `validate_path_relationships` 拒绝 `project_dir` 包含 `kimi_code_home`。
- `validate_path_relationships` 拒绝 `kimi_code_home` 位于 `project_dir` 内。
- `validate_path_relationships` 拒绝 `state_root` 位于 `project_dir` 内。
- `resolve_unsafe_kimi_code_home` 拒绝真实 `~/.kimi-code`。
- `resolve_kimi` 在显式路径不存在时失败。
- `build_bwrap_command` 包含 `--clearenv`。
- `build_bwrap_command` 包含 `/workspace` bind。
- `build_bwrap_command` 不包含 `--unshare-net`。
- `build_bwrap_command` 不包含 `--dev-bind / /`。
- `build_bwrap_command` 不包含 `--ro-bind /etc /etc`。
- `build_bwrap_command` 包含 `--unshare-pid`。
- `build_bwrap_command` 包含 `--unshare-ipc`。
- `build_bwrap_command` 包含 `--unshare-uts`。
- `build_env` 不继承敏感变量。
- `build_env` 设置 `KIMI_SANDBOX=1` 和 `KIMI_SANDBOX_MODE=workspace-write`。

需要真实 bubblewrap 的 smoke tests 可以单独放在：

```text
tests/smoke/
```

或者写进 README 手工执行。

## 30. 第一轮 vibe coding 任务拆分

建议按以下顺序实现。

### Step 1：创建 Python package

文件：

```text
pyproject.toml
kimi_sandbox/__init__.py
kimi_sandbox/cli.py
```

目标：

- `pip install -e .`
- `kimi-sandbox --version`
- `kimi-sandbox --help`

### Step 2：实现路径解析

实现：

- `resolve_project_dir`
- `resolve_state_root`
- `resolve_kimi`
- `resolve_bwrap`
- `resolve_unsafe_kimi_code_home`
- `validate_path_relationships`

重点：

- 拒绝危险 project root。
- 不自动复制真实 Kimi home。
- 拒绝真实 `~/.kimi-code` 作为默认 Kimi profile。
- 拒绝 project/state/profile 互相包含的危险路径关系。
- `kimi` 找不到时错误清晰。

### Step 3：实现 bwrap command builder

实现：

- `build_bwrap_command(config) -> list[str]`
- mount 存在性检查。
- env allowlist。
- `--dry-run`。

重点：

- 不使用 `shell=True`。
- 不使用 `--dev-bind / /`。
- 不使用 `--unshare-net`。
- 不整体挂载 `/etc`。
- 设置 `KIMI_SANDBOX*` 环境变量。

### Step 4：实现运行逻辑

实现：

- `subprocess.run(command)`
- `print_start_banner(config)`
- 返回 Kimi exit code。
- Ctrl-C 行为自然传递。

重点：

- 默认启动前显示 `Kimi Sandbox active`。
- banner 不打印 secrets。
- 不吞掉 TUI stdin/stdout/stderr。
- 不捕获输出。

### Step 5：实现 `--exec`

实现：

```text
--exec COMMAND -> /bin/bash -lc COMMAND
```

> v2 注（§39.11 C1）：宿主无 bash 时回退 `/bin/sh -c`（见 `_inner_shell()`）。

用途：

- 快速测试沙箱权限。
- 不依赖 Kimi 登录。

### Step 6：补 smoke checklist

添加 README 或文档：

- 安装 venv。
- 安装 bubblewrap。
- 运行 `kimi-sandbox . -- --version`。
- 运行写入测试。
- 运行系统只读测试。
- 说明网络未隔离。

## 31. 关键实现细节

### 31.1 创建目录

launcher 可以创建：

```text
state_root
profile_dir
kimi_code_home
```

不要创建：

```text
project_dir
```

项目目录必须已经存在。

### 31.2 chmod

v1 在创建新的 state/profile 目录时，应尽量使用仅当前用户可访问的权限：

```text
0700
```

如果 `kimi_code_home` 已经存在且权限过宽，可以警告：

```text
warning: KIMI_CODE_HOME is readable by other users
```

v1 不建议静默修改已有目录权限，避免意外改变用户已有配置。可以在警告中给出修复建议：

```bash
chmod 700 <kimi-code-home>
```

### 31.3 symlink

项目路径应 `resolve()`。

如果项目内存在 symlink：

- 指向未挂载宿主路径时，多数会不可访问。
- 指向 `/etc` 时，可读但只读。
- 指向 `/tmp` 时，指向沙箱 tmpfs。
- 指向 `/kimi-code-home` 时，可以访问 profile，因为 profile 本来就在沙箱内。

v1 不需要额外处理项目内部 symlink。

### 31.4 package manager

`npm install`、`pip install`、`cargo build` 等命令可能：

- 在 `/workspace` 写依赖目录。
- 在 HOME 写 cache。
- 联网下载依赖。

v1 HOME 是 tmpfs，所以 cache 不持久。优点是干净，缺点是重复下载。

后续如有需要，可加：

```bash
--persistent-cache
```

### 31.5 Git

由于 HOME 是临时的：

- global git config 默认不可见。
- SSH key 默认不可见。
- private repo 操作可能失败。

这符合 v1 安全预期。

如果用户需要 GitHub 操作，建议通过 Kimi MCP 或显式临时配置，而不是默认挂载 SSH agent。

## 32. README 中必须写清楚的警告

建议 README 直接写：

```text
This is a filesystem sandbox MVP.
It does not isolate network access.
It does not protect credentials placed inside KIMI_CODE_HOME from commands run inside the sandbox.
It does not protect sensitive files inside the mounted project directory.
Do not use YOLO mode unless the project directory is disposable or backed up.
```

中文版本：

```text
这是文件系统沙箱 MVP，不是完整安全沙箱。
它不隔离网络。
它不能防止沙箱内命令读取 /kimi-code-home 中的 Kimi 凭据。
它不能防止 Kimi 读取当前项目目录中的敏感文件。
除非项目目录可随时恢复，否则不建议使用 YOLO 模式。
```

## 33. 后续 v2 方向

v1 用一段时间后，根据反馈选择方向。

### 33.1 `/etc` 兼容和收敛

v1 默认已经使用最小 `/etc` 挂载清单：

```text
/etc/resolv.conf
/etc/hosts
/etc/nsswitch.conf
/etc/ssl
/etc/ca-certificates
```

后续可以继续做两类改进：

- 针对不同发行版补充最小 CA、locale、NSS 路径。
- 增加显式 `--compat-bind-etc` 作为兼容 fallback，但默认仍保持最小挂载。

### 33.2 增加 read-only 模式

增加：

```bash
kimi-sandbox . --read-only
```

语义：

- `/workspace` 只读。
- `/tmp` 可写。
- `/kimi-code-home` 可写。

用途：

- 只让 Kimi 分析项目，不修改项目。

### 33.3 增加 extra mount

增加：

```bash
--ro-mount HOST:SANDBOX
--rw-mount HOST:SANDBOX
```

默认仍然不挂载真实 HOME。

### 33.4 增加网络隔离

可选：

```bash
--no-network
```

技术上添加：

```text
--unshare-net
```

但这样 Kimi 主进程也不能访问模型 API。因此这个选项只适合：

- 离线测试。
- `--exec` 调试。
- 将来配合模型代理或 Kimi-native 分进程沙箱。

### 33.5 Kimi-native sandbox

长期正确方向：

- Kimi 主进程保留模型网络。
- `Bash`、MCP stdio、hooks 分别在更小沙箱内运行。
- 工具进程默认无网络。
- approval 只能请求放宽，不直接绕过沙箱。

这需要修改 Kimi Code 源码，不属于 v1。

## 34. 风险清单

### 34.1 Kimi binary 兼容风险

如果 Kimi 不是真正单文件 binary，单独挂载 executable 可能失败。

缓解：

- `--kimi` 支持显式路径。
- fallback 到挂载 Kimi binary 父目录。
- 文档记录不同安装方式。

### 34.2 bubblewrap 权限风险

某些系统禁用 unprivileged user namespace，bwrap 可能失败。

缓解：

- 启动前运行 `bwrap --version`。
- 失败时输出原始错误。
- README 写明系统要求。

### 34.3 TUI 兼容风险

终端能力、颜色、按键可能异常。

缓解：

- 继承 stdin/stdout/stderr。
- allowlist 传 `TERM`、`COLORTERM`、`LANG`。
- 不捕获输出。

### 34.4 凭据误解风险

用户可能误以为 sandbox 能保护 Kimi profile 凭据。

缓解：

- README 和 CLI 首次运行提示写清楚。
- 不挂载真实 `~/.kimi-code`。
- 推荐专用低权限 profile。

### 34.5 项目误删风险

沙箱允许写 `/workspace`，所以 Kimi 仍可删项目文件。

缓解：

- 建议 Git 工作区。
- 建议 manual permission。
- 不默认 YOLO。
- 可后续加 read-only 模式。

## 35. 最小可运行版本定义

最小可运行版本只需要这些能力：

- `kimi-sandbox --help`
- `kimi-sandbox . --dry-run`
- `kimi-sandbox . --exec "pwd"`
- `kimi-sandbox . -- --version`
- `/workspace` 可写
- `/etc` 不可写
- 不整体挂载 `/etc`
- 真实 HOME 不挂载
- `KIMI_CODE_HOME=/kimi-code-home`
- `KIMI_SANDBOX=1`
- 默认显示 `Kimi Sandbox active` banner
- 使用 `--unshare-pid`
- 使用 `--unshare-ipc`
- 使用 `--unshare-uts`
- 拒绝真实 `~/.kimi-code` 作为默认 profile
- 拒绝 project/state/profile 互相包含的危险路径关系
- 不使用 Docker
- 不隔离网络

不要求：

- 完整测试套件
- 配置文件
- read-only 模式
- persistent cache
- extra mounts
- network toggle
- credential broker

## 36. 推荐第一版实现顺序

第一天可以这样做：

1. 建 package 和 CLI。
2. 实现 `--help`、`--version`。
3. 实现 `--dry-run`，打印 bwrap command。
4. 实现 `--exec`，先跑 `pwd`。
5. 验证 `/workspace` 写入。
6. 验证 `/etc` 只读。
7. 验证 HOME 隔离。
8. 跑 `kimi --version`。
9. 进入 Kimi TUI。

不要第一天就做：

- Docker。
- 网络隔离。
- 配置文件。
- 多平台。
- 自动迁移 Kimi 配置。
- 大量抽象。

## 37. 设计底线

第一版实现时必须守住这些底线：

- 不挂载真实 HOME。
- 不挂载真实 `~/.kimi-code`。
- 不使用 `--dev-bind / /`。
- 不默认传递敏感环境变量。
- 不使用 `shell=True` 执行 bwrap。
- 不自动安装系统依赖。
- 不自动复制用户凭据。
- 不声称这是完整安全沙箱。

## 38. 最终建议

v1 应该做成一个小而清楚的工具：

```text
kimi-sandbox = Python CLI + bubblewrap mount plan
```

它的价值不是解决所有安全问题，而是先把 Kimi Code 的文件系统影响面从“整个用户环境”收缩到“当前项目 + 独立 Kimi profile + 临时目录”。

这个版本足够轻，能快速落地，也足够有用，适合先用一段时间观察真实痛点。后续如果发现最大痛点是网络、凭据或 MCP，再分别进入 v2/v3。

---

## 39. v2 实现补遗（Addendum）

> 本节记录 v1 落地后实际实现的 v2 增强。v1 主体（§1–§32 的文件系统隔离）保持不变；
> 以下功能均为**附加项**，默认行为尽量与 v1 兼容（除 TIOCSTI seccomp 默认开启外）。
> 版本号随之升至 `0.2.0`，`requires-python` 升至 `>=3.11`（因使用标准库 `tomllib`）。

### 39.1 TIOCSTI seccomp 过滤器（对应 §6.2 残留项 / §33 安全方向）

v1 文档诚实记录了一个残留风险：为保持 TUI 可用而不使用 `--new-session`，沙箱内进程
理论上可通过 `TIOCSTI`/`TIOCLINUX` ioctl 向宿主控制终端注入按键（CVE-2017-5226 类）。

v2 用一个**纯标准库**构建的 classic-BPF（cBPF）程序关闭该残留，且不破坏 TUI：

- 模块 `kimi_sandbox/seccomp.py` 直接用 `struct.pack("<HBBI", …)` 拼出
  `struct sock_filter` 序列，无 libseccomp 依赖。
- 过滤器逻辑：校验 `seccomp_data.arch` → 校验 syscall 号为 `ioctl` →
  比较 `args[1]` 低 32 位，命中 `TIOCSTI(0x5412)`/`TIOCLINUX(0x541c)` 返回
  `SECCOMP_RET_ERRNO|EPERM`，其余 `SECCOMP_RET_ALLOW`。
- 支持架构：`x86_64`、`aarch64`（ioctl 号分别为 16、29）。未知架构时**降级为不安装**
  并打印 note（而非拒绝启动）。
- 通过 `bwrap --seccomp <fd>` 传入；fd 由 `mkstemp`→`open`→`unlink` 得到，
  `set_inheritable(True)` 后经 `subprocess.run(..., pass_fds=(fd,))` 继承。
- 默认开启；`--no-seccomp` 关闭。已验证：开启时 ioctl 返回 `EPERM`，关闭时不再拦截。

### 39.2 网络隔离 `--no-network`（对应 §33.4）

`--no-network` 添加 `--unshare-net`。文档明确：这是**全有或全无**——主进程也会失去
模型 API 连接，故默认关闭。真正的「主进程保留模型网络 / 工具子进程断网」需要
§33.5 的 credential broker（需改 Kimi 源码），v2 不实现，README 与本节如实标注。

### 39.3 资源限制（对应 §34 风险：资源耗尽）

`--memory-max SIZE` / `--cpu-quota PCT` / `--pids-max N` 通过把整条 bwrap 命令包进
`systemd-run --user --scope --quiet -p MemoryMax= -p CPUQuota= -p TasksMax=` 实现。
仅在请求了限制时才需要 `systemd-run`；缺失时给出明确报错而非静默忽略。已验证
`pass_fds`（seccomp）能穿过 `systemd-run --user --scope` 正常继承。

### 39.4 read-only 模式（对应 §33.2）

`--read-only` 将 `/workspace` 由 `--bind` 改为 `--ro-bind`；`/kimi-code-home`、`/tmp`
仍可写。环境标记 `KIMI_SANDBOX_MODE=read-only`，与 §15 的权限模式语义一致。

### 39.5 持久化缓存 `--persistent-cache`（对应 §31.4）

`--persistent-cache` 在状态根下按 profile 建立 `profiles/<profile>/cache/`，
`--bind` 到 `/cache`，并令 `XDG_CACHE_HOME=/cache`，使 pip/npm 等缓存跨次复用；
不开启时 `XDG_CACHE_HOME` 仍指向临时 HOME 内（与 v1 相同，运行结束即丢弃）。

### 39.6 额外挂载 `--ro-mount` / `--rw-mount`（对应 §33.3）

- 形式：`HOST`（裸路径，落在 `/mnt/<basename>`）或 `HOST:TARGET`（显式绝对目标）。
  裸路径**不**采用恒等映射，因为多数宿主路径在 `/home` 下（保留树），恒等映射必被拒。
- 目标做保留路径冲突校验：禁止等于、包含或被包含于
  `/workspace`、`/kimi-code-home`、`/cache`、`/etc`、`/usr`、`/home`、`/sandbox` 等
  沙箱自有挂载点；根 `/` 单独拒绝。
- `--rw-mount` 会打印告警（暴露可写宿主目录给所有沙箱进程）。

### 39.7 配置文件（对应 §33 / “v1 不做配置文件” 的后续松绑）

默认读取 `~/.config/kimi-sandbox/config.toml`（`tomllib`，Python 3.11+），
可用 `--config PATH` 指定、`--no-config` 跳过。优先级：**命令行 > 配置文件 > 内置默认**。
已知键做类型校验（错误类型硬报错，未知键告警但不致命）。支持键：
`profile`、`state_root`、`no_network`、`read_only`、`no_seccomp`、`persistent_cache`、
`memory_max`、`cpu_quota`、`pids_max`、`ro_mounts`、`rw_mounts`。

每个布尔项均提供配对反向标志（`--writable`/`--network`/`--seccomp`/`--no-persistent-cache`），
使配置文件里被置为 `true` 的硬化项可在命令行**两个方向**覆盖（详见 §39.11 R1）。

### 39.8 非 merged-/usr 兼容（对应 §12.2 / §34）

`_system_mount_args()` 对每个系统目录区分：宿主为 symlink（merged-/usr，如 `/bin→usr/bin`）
则用 `--symlink` 重建；为真实目录（传统布局，如 Alpine 风格）则直接 `--ro-bind`；
缺失则跳过。两种布局均有单元测试覆盖（monkeypatch `Path`/`os.readlink`）。

### 39.9 测试现状

- 单元测试 136 项全绿（新增 `test_seccomp.py`、`test_paths_v2.py`、`test_bwrap_v2.py`、
  `test_cli_v2.py`；含第二轮评审 R1/R2/C1 的回归用例）。
- smoke 21 项全绿（真实 bubblewrap 0.11.1：原 §28 验收 + 9 项 v2 功能
  + 1 项 R1 反向覆盖回归；网络可达性探测对外部依赖做有限重试）。

### 39.10 仍未触碰的 v1 底线（§37）

v2 全程遵守 §37：不挂真实 HOME、不挂真实 `~/.kimi-code`、不 `--dev-bind / /`、
不默认传敏感环境变量、不 `shell=True`、不自动装系统依赖、不自动复制凭据、
不声称是完整安全沙箱。`--rw-mount`/`--unsafe-kimi-code-home` 等危险项均显式告警。

### 39.11 第二轮独立评审整改（R1 / R2 / C1）

第二轮独立评审给出 87/100，并列三项可整改点。本节记录其落地：

- **R1（配置/CLI 取反不对称）** —— 原先布尔硬化项仅有 `store_const=True, default=None`，
  配置文件里置 `true` 后命令行无法改回 `false`，与"命令行 > 配置文件"承诺相悖。
  整改：为每个布尔项加配对反向标志，共享 `dest` 且 `const=False`
  （`--read-only`/`--writable`、`--no-network`/`--network`、`--no-seccomp`/`--seccomp`、
  `--persistent-cache`/`--no-persistent-cache`）。argparse 对共享 `dest` 只取首个
  `default`，故反向标志不重复声明 `default`，未设时仍为 `None`，`_pick()` 据此判定"未设"。
  验证：`--network`/`--writable` 等可把配置里的 `true` 覆盖为 `false`，
  而无命令行标志时配置 `true` 仍生效（`test_cli_v2.py` 参数化回归）。
- **R2（`--debug` 计划与实跑不一致）** —— `print_mount_plan` 原先不打印 seccomp / 资源限制 /
  `systemd-run` 包装，与启动 banner、`--dry-run` 不一致。整改：新增
  `seccomp_active`/`limits`/`systemd_run` 入参，输出 `seccomp`、`limits`（含
  `systemd-run` 路径）行，与实跑一致。
- **C1（`--exec` 硬编码 `/bin/bash`）** —— 新增 `_inner_shell()`：优先
  `/bin/bash`、`/usr/bin/bash`（`-lc`），均不存在时回退 `/bin/sh -c`，
  兼容只带 dash/sh 的宿主。`--exec` 仅为开发/冒烟便利项。

其余次要项（D1 TIOCSTI 文案、S1 rw-mount source 广度、Q1 machine name 双来源、
D2 README 漏 `state_root` 等）评级为信息/装饰级，未在本轮改动，留待后续。本轮整改
不触碰 §37 任一底线。
