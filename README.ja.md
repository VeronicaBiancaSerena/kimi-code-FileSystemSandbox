# kimi-sandbox

[English](README.md) | 日本語

[bubblewrap](https://github.com/containers/bubblewrap) をベースにした
[Kimi Code](https://github.com/MoonshotAI) 向けのファイルシステム・サンドボックス
ランチャーです。既存の `kimi` CLI を制限されたファイルシステムビューの中で実行します。
プロジェクトは `/workspace` に読み書き可能でマウントされ、`KIMI_CODE_HOME` は分離され、
システムディレクトリは読み取り専用、`HOME` / `/tmp` は tmpfs になります。

中核は **ファイルシステムの破壊を封じ込める** レイヤーです。その上に、オプトインおよび
デフォルト有効のハードニングを追加しています。マルチ ABI 対応の TIOCSTI ブロック
seccomp フィルタ、inode 固定のバインドマウント、ネットワーク分離、読み取り専用
プロジェクトモード、cgroup リソース制限、永続キャッシュ、追加マウントなどです。
ただし、これらは本ツールを **完全なセキュリティサンドボックスにするものではありません**。
正確に何を守り何を守らないかは [セキュリティモデル](#セキュリティモデル) を参照してください。

> ⚠️ **最初に必ずお読みください**
>
> - これは **ファイルシステム** サンドボックスであり、完全なセキュリティサンドボックスでは
>   ありません。
> - デフォルトではネットワークアクセスを分離 **しません**（`--no-network` を使用）。
> - `KIMI_CODE_HOME` 内に置いた認証情報を、サンドボックス内で実行されるコマンドから
>   **保護しません**。
> - マウントしたプロジェクトディレクトリ内の機微なファイルを **保護しません**。
> - プロジェクトディレクトリが破棄可能、またはバックアップ済みでない限り、YOLO モードは
>   **使用しないでください**。

## 目次

- [仕組み](#仕組み)
- [必要要件](#必要要件)
- [インストール](#インストール)
- [使い方](#使い方)
- [クイック検証](#クイック検証)
- [ハードニング用フラグ](#ハードニング用フラグ)
- [マウント固定（mount pinning）](#マウント固定mount-pinning)
- [設定ファイル](#設定ファイル)
- [オプション](#オプション)
- [サンドボックス内部の様子](#サンドボックス内部の様子)
- [MCP とスキル](#mcp-とスキル)
- [Conda サポート](#conda-サポート)
- [セキュリティモデル](#セキュリティモデル)
- [推奨する Kimi 設定](#推奨する-kimi-設定)
- [開発](#開発)
- [ライセンス](#ライセンス)

## 仕組み

`kimi-sandbox` は薄い **ランチャー** です。与えられたホストのパスを解決・検証し、
単一の `bubblewrap`（`bwrap`）コマンドを組み立て、その内部で `kimi`（または `--exec`
で指定したコマンド）を exec します。デーモンや永続的な特権はありません。ランチャーは
完全に非特権ユーザーとして動作し、bubblewrap の非特権ユーザー名前空間に依存します。

パイプラインは意図的に、純粋でユニットテスト可能な段階に分割されています。

1. **解決・検証**（`paths.py`）— `~`/変数を展開し、シンボリックリンクを解決し、
   広範な／システムのルートや危険なパス関係を拒否します。
2. **argv の構築**（`bwrap.py`）— 完全に解決済みの `SandboxConfig` を `bwrap` の argv に
   変換します。この段階は読み取り専用のファイルシステム探査のみを行い、何も起動しないため、
   生成されるコマンドはテスト可能です。
3. **実行**（`cli.py`）— seccomp フィルタの fd と inode 固定用の fd を開き、必要に応じて
   cgroup 制限のために `systemd-run` でラップし、バナーを表示して exec します。ランチャーは
   Kimi 自身の終了コードをそのまま返します。ランチャーレベルの失敗は `error:` 行を表示し、
   区別可能なコード（`125`）を使用します。

## 必要要件

- **非特権ユーザー名前空間** が有効な Linux（または WSL2）。
- `bubblewrap`（`bwrap`）がインストールされ `PATH` にあること。inode 固定マウント
  （`--bind-fd`）にはバージョン **0.5 以上** が必要です。古いビルドでは
  `--no-pin-mounts` を指定してください。bubblewrap 0.11 で検証済みです。
- Python 3.11 以上（標準ライブラリの `tomllib` 設定パーサーのため）。
- `kimi` CLI がインストールされていること（または `--kimi /path/to/kimi` を指定）。
- 任意：ユーザー `systemd`（`--memory-max` / `--cpu-quota` / `--pids-max` 用）。

bubblewrap の確認:

```bash
bwrap --version
```

見つからない場合は、システムのパッケージマネージャーでインストールしてください
（ランチャーは **自動ではインストールしません**）:

```bash
sudo apt install bubblewrap     # Debian/Ubuntu
```

## インストール

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -U pip
pip install -e .
```

### 任意のディレクトリから（venv を有効化せずに）使う

editable インストールでは、venv 内に `kimi-sandbox` のエントリポイントが作成されます。
その shebang はすでに venv の Python を指しているため、`PATH` 上にシンボリックリンクを
張れば、venv を有効化しなくても任意の場所から呼び出せます。

```bash
# ~/.local/bin が PATH 上にあることを前提とします（通常は含まれています）。
ln -sf "$(pwd)/.venv/bin/kimi-sandbox" ~/.local/bin/kimi-sandbox
kimi-sandbox --version        # 任意のディレクトリで動作します
```

あるいは、[pipx](https://pipx.pypa.io/) で独立したツールとしてインストールします:

```bash
pipx install -e .             # kimi-sandbox を ~/.local/bin に自動追加します
```

どちらの場合でも、ランチャーは引き続き `PATH` 上の `bwrap` と `kimi`（または
`--bwrap` / `--kimi` で渡したもの）を使用します。

## 使い方

```bash
# カレントディレクトリをサンドボックス化して Kimi の TUI を起動する。
kimi-sandbox .

# 引数を kimi へそのまま渡す（-- 以降はすべて kimi に渡される）。
kimi-sandbox . -- --version
kimi-sandbox . -- -m kimi-code/kimi-for-coding

# 名前付きプロファイルを使う（KIMI_CODE_HOME と認証情報を分離）。
kimi-sandbox ~/work/project --profile work

# 何も実行せず、生成される bwrap コマンドを確認する。
kimi-sandbox . --dry-run

# 実行前に、解決済みパスとマウント計画を（stderr へ）表示する。
kimi-sandbox . --debug

# kimi の代わりにサンドボックス内でシェルコマンドを実行する（スモークテスト用）。
kimi-sandbox . --exec "pwd && id && touch /workspace/.write-test"
```

`--dry-run` は読みやすさのため、`bwrap` の argv を **パス**（fd 番号ではなく）で表示し、
実行時に渡される fd（seccomp、固定マウント）を stderr に注記します。`--debug` は実際の
実行を反映したマウント計画（ネットワーク、seccomp、マウント固定、リソース制限、
`systemd-run` ラッパー）を表示します。

### クイック検証

プロジェクトディレクトリから Kimi を起動します。

```bash
kimi-sandbox .
```

次に、サンドボックス内の Kimi にこのコマンドを実行させます。

```bash
echo "HOME=$HOME"; echo x > /tmp/ks_test; echo y > "$HOME/ks_test"; echo z > /workspace/ks_test; touch /etc/ks_test || echo ETC_BLOCKED
```

Kimi を終了し、ホスト側で同じプロジェクトディレクトリに戻って、実際のホストファイル
システムを確認します。

```bash
[ -e /tmp/ks_test ] && echo "BAD: host /tmp leaked" || echo "OK: host /tmp clean"; [ -e "$HOME/ks_test" ] && echo "BAD: host HOME leaked" || echo "OK: host HOME clean"; [ -e ./ks_test ] && echo "OK: workspace write visible" || echo "BAD: ./ks_test not found"
```

期待される結果は、Kimi 内では `HOME=/home/sandbox` と表示され、ホストの `/tmp` と
ホストの `$HOME` は汚れず、プロジェクト内の `./ks_test` だけが存在することです。
`/workspace` はデフォルトで書き込み可能です。プロジェクトを変更させずにレビューさせたい
場合は `--read-only` を使ってください。

### ハードニング用フラグ

```bash
# ネットワークを完全に遮断する（curl/pip/npm/MCP は外部に到達できない）。
kimi-sandbox . --no-network

# 読み取り専用レビュー：/workspace を変更できない。
kimi-sandbox . --read-only

# systemd-run でリソースを制限する（ユーザー systemd が必要）。
kimi-sandbox . --memory-max 2G --cpu-quota 150% --pids-max 512

# 実行間でパッケージ／ビルドキャッシュを永続化する（プロファイル単位）。
kimi-sandbox . --persistent-cache

# 追加のホストパスをマウントする。HOST のみの場合は /mnt/<basename> に置かれる。
kimi-sandbox . --ro-mount ~/reference-data        # -> /mnt/reference-data (ro)
kimi-sandbox . --ro-mount ~/data:/opt/data        # 明示的なターゲット (ro)
kimi-sandbox . --rw-mount ~/scratch:/srv/scratch  # 書き込み可能（危険）

# TIOCSTI seccomp フィルタはデフォルトで有効。必要な場合のみ無効化する。
kimi-sandbox . --no-seccomp

# inode 固定マウントはデフォルトで有効。bubblewrap < 0.5 では無効化する。
kimi-sandbox . --no-pin-mounts
```

## マウント固定（mount pinning）

デフォルトでは、**すべてのホスト側バインド元** — プロジェクトディレクトリ、プロファイルの
`kimi-code-home`、永続キャッシュ、`kimi` バイナリ、および任意の `--ro/--rw-mount` 元 — は、
パスではなく `O_PATH` ファイルディスクリプタ（`--bind-fd` / `--ro-bind-fd`）を介して
バインドされます。ランチャーはパスを解決・検証した **後** に各 fd を一度だけ開き、`bwrap`
はその正確な inode をマウントします。

これにより、検証からマウントまでの間にパスがシンボリックリンクへすり替えられる
TOCTOU（time-of-check / time-of-use）の隙を塞ぎます。fd は検証された inode を参照するため、
その後のパス変更に左右されません。残る競合は `open()` の一度のパス走査のみです
（[残存リスクと注意点](#残存リスクと注意点) を参照）。

固定には bubblewrap 0.5 以上（`--bind-fd`）が必要です。古いビルドでは `--no-pin-mounts`
を指定するとパスベースのバインドにフォールバックします。`--dry-run` はこの設定に関わらず、
常に読みやすいパス形式で表示します。

## 設定ファイル

デフォルト値は `~/.config/kimi-sandbox/config.toml` に置けます（パスは `--config` で上書き、
`--no-config` でスキップ）。CLI フラグは常にファイルを上書きします。設定で有効化された
ブール値を **無効へ戻す** ことも可能です。`--network` は `no_network = true` を、`--writable`
は `read_only = true` を、`--seccomp` は `no_seccomp = true` を、`--no-persistent-cache` は
`persistent_cache = true` を、それぞれ上書きします。

優先順位は常に **CLI フラグ > 設定ファイル > 組み込みデフォルト** です。既知のキーは型検査
されます（型が誤っていればハードエラー）。未知のキーは警告されますが失敗にはなりません。

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

## オプション

| オプション | 意味 |
| --- | --- |
| `PROJECT` | `/workspace` にマウントするホストのプロジェクトディレクトリ（デフォルト: cwd）。 |
| `--profile NAME` | サンドボックスのプロファイル名（デフォルト: `default`）。 |
| `--state-root PATH` | ホストの状態ルート（デフォルト: `~/.local/state/kimi-sandbox`）。 |
| `--kimi PATH` | `kimi` 実行ファイルへの明示的なホストパス。 |
| `--bwrap PATH` | `bwrap` 実行ファイルへの明示的なホストパス。 |
| `--dry-run` | bwrap コマンドを表示して終了する。 |
| `--debug` | 解決済みパス／マウント計画を stderr に表示してから実行する。 |
| `--exec COMMAND` | サンドボックス内で `COMMAND` を実行する（`bash -lc`、bash が無ければ `/bin/sh -c`）。 |
| `--read-only` / `--writable` | `/workspace` を読み取り専用／読み書きでマウント（設定を上書き）。 |
| `--no-network` / `--network` | ネットワークを分離（`--unshare-net`）／ホストのネットワークを維持（設定を上書き）。 |
| `--no-seccomp` / `--seccomp` | TIOCSTI ブロック seccomp フィルタを無効化／インストール。 |
| `--no-pin-mounts` | inode 固定（`--bind-fd`）マウントを無効化し、パスバインドを使う（bwrap < 0.5）。 |
| `--persistent-cache` / `--no-persistent-cache` | プロファイル単位の `/cache` バインドを有効化／無効化。 |
| `--memory-max SIZE` | systemd-run による cgroup メモリ上限（例: `2G`）。 |
| `--cpu-quota PCT` | systemd-run による cgroup CPU クォータ（例: `150%`）。 |
| `--pids-max N` | systemd-run による cgroup プロセス／スレッド上限（`TasksMax`）。 |
| `--ro-mount HOST[:TARGET]` | 追加の読み取り専用マウント（繰り返し指定可）。 |
| `--rw-mount HOST[:TARGET]` | 追加の読み書きマウント（繰り返し指定可、**危険**）。 |
| `--config PATH` | 設定ファイル（デフォルト: `~/.config/kimi-sandbox/config.toml`）。 |
| `--no-config` | 設定ファイルを読まない。 |
| `--unsafe-kimi-code-home PATH` | `/kimi-code-home` 用のカスタムホストパス（**危険**）。 |
| `--version` | ランチャーのバージョンを表示する。 |

`--` 区切り以降はすべて、そのまま `kimi` に渡されます。

## サンドボックス内部の様子

| サンドボックス内パス | 実体 | アクセス |
| --- | --- | --- |
| `/workspace` | あなたのプロジェクト | 読み書き（`--read-only` で読み取り専用） |
| `/kimi-code-home` | プロファイルの状態ディレクトリ | 読み書き |
| `/kimi-code-home/skills` | ホストのスキルディレクトリ（`profile_ro_mounts` 経由） | 読み取り専用 |
| `/home/sandbox/.kimi-code` | シンボリックリンク → `/kimi-code-home`（`compat_kimi_home`） | — |
| `/cache` | プロファイルのキャッシュディレクトリ | 読み書き（`--persistent-cache` 時のみ） |
| `/home/sandbox` | tmpfs（`HOME`） | 読み書き、揮発性 |
| `/tmp`, `/run` | tmpfs | 読み書き、揮発性 |
| `/usr`, `/lib*`, `/bin`, `/sbin` | ホスト | 読み取り専用 |
| `/etc` | tmpfs + 最小バインド | 読み取り専用（DNS/TLS のみ） |
| `/proc`, `/dev` | 仮想 | 最小限 |
| `/mnt/*`, `/opt/*`, … | 追加の `--ro/--rw-mount` | 指定どおり |

merged-`/usr` のディストリビューション（`/bin → usr/bin` など）では、シンボリックリンクが
サンドボックス内で再作成されます。従来型レイアウトでは実ディレクトリが ro バインドされます。
`/etc` は tmpfs で、DNS/TLS に関係するファイルのみを読み取り専用でバインドし、その後
全体を読み取り専用で再マウントします。したがって `/etc` 全体は公開されず、その配下に
新規作成もできません。`/etc/passwd` と `/etc/group` は意図的にバインド **しません**
（ホストのユーザー名漏洩を避けるため）。代償は `id` の見た目上の警告のみです。

サンドボックス内で設定される環境マーカー:

```
KIMI_SANDBOX=1
KIMI_SANDBOX_MODE=workspace-write   # --read-only の場合は read-only
KIMI_SANDBOX_WORKSPACE=/workspace
KIMI_CODE_HOME=/kimi-code-home
HOME=/home/sandbox
```

環境は `--clearenv` から始まります。端末／ロケール変数（`TERM`, `COLORTERM`, `LANG`,
`LC_*`, `NO_COLOR`）の短い許可リストのみが引き継がれ、加えてランチャー制御のマーカーと
固定 `PATH` が設定されます。認証情報やエージェントソケット（`*_API_KEY`, `AWS_*`,
`GITHUB_TOKEN`, `SSH_AUTH_SOCK` など）は決して引き継がれません。Kimi の `Bash` から
`echo $KIMI_SANDBOX` で確認できます。

## MCP とスキル

サンドボックスは、既存の MCP サーバーや Kimi スキルを **プロジェクトに何もコピーせず**
利用可能にできます。ソース・スクリプト・ランタイムは現在のホスト上の場所から **読み取り
専用** でバインドマウントされます。サンドボックス内の Kimi・Bash・フック・MCP 子プロセスは
それらを読み取り・実行できますが、変更はできません。書き込み可能な状態は分離されます。
プロファイル設定や認証情報は `/kimi-code-home`、キャッシュは `/cache`、一時ファイルは
`/tmp` です。

すべて既定の設定ファイルで駆動するため、一度セットアップすれば素の `kimi-sandbox .` が
そのまま動きます。

```bash
kimi-sandbox init-integrations          # 推奨設定を表示（ドライラン）
kimi-sandbox init-integrations --write  # ~/.config/kimi-sandbox/config.toml を作成
kimi-sandbox doctor --config-check      # 設定とマウント計画を検証
kimi-sandbox .                          # MCP + スキルをマウントして実行
```

### 設定キー

- `profile_ro_mounts` — `/kimi-code-home` 配下への読み取り専用サブマウント。
  `HOST:RELATIVE_TARGET` 形式で記述し、スキルディレクトリを `/kimi-code-home/skills`
  に公開するために使います。ターゲットは `..` を含まない相対パスに限定され、プロファイル
  ツリーの外には決して出られません。
- `ro_mounts` — MCP ソースと言語ランタイムを `/opt/...`（例: `/opt/kimi-mcp/...`,
  `/opt/kimi-runtime/...`）へ読み取り専用でマウントします。
- `env_keep` — **明示的に** 転送するホスト変数（例: トークン）。既定では機微な変数は
  一切転送されず、ワイルドカードもありません。
- `env_set` — 設定ファイルで指定する固定の環境値。
- `compat_kimi_home` — true（既定）のとき `/home/sandbox/.kimi-code` を
  `/kimi-code-home` へのシンボリックリンクにします。`~/.kimi-code` を参照するツールでも
  永続プロファイルに解決されます。`--compat-kimi-home` / `--no-compat-kimi-home` で切替。
- `conda_enabled` / `conda_root` / `conda_writable` / `conda_shell_integration`
  / `conda_existing_envs` — 制御された conda 連携（[Conda サポート](#conda-サポート)
  を参照）。ホストの conda root は読み取り専用でマウントされ、新しい env は
  `/cache/conda`（または `/tmp/kimi-conda`）に作成されます。

`env_keep` / `env_set` はランチャー予約変数（`HOME`, `PATH`, `KIMI_CODE_HOME`,
`TMPDIR`, `XDG_*`, `KIMI_SANDBOX*`、および conda 有効時は `CONDARC`,
`CONDA_ENVS_PATH`, `CONDA_PKGS_DIRS`, `BASH_ENV`, `KIMI_SANDBOX_CONDA_*`）を
上書き **できません**。試みるとエラーになります。
キャッシュ位置は `XDG_CACHE_HOME` の上書きではなく `persistent_cache = true` を使います。

### `config.toml` の例

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

### スキルの dotenv ファイル

API キーなどの秘密値を、スキルの `SKILL.md` や Kimi へのプロンプトに直接書かないで
ください。スキルから呼び出すツールが、ソースツリーやホスト側の設定ディレクトリにある
dotenv ファイルを読む場合は、そのファイルを読み取り専用でサンドボックスへマウントし、
`env_set` でツールにサンドボックス内パスを指定します。

たとえば `imagegencli_codex` のようなツールで有効です。サンドボックス内では
`HOME=/home/sandbox` になるため、ホスト側の `~/.config/imagegencli_codex/.env` のような
ファイルは、明示的にマウントしない限り見えません。dotenv ファイルを `/opt` 配下へ
マウントし、ツールの env-file 変数を設定します。

```toml
ro_mounts = [
  "~/skills/imagegencli_codex/.env:/opt/imagegencli_codex.env",
]

[env_set]
IMAGEGENCLI_CODEX_ENV_FILE = "/opt/imagegencli_codex.env"
```

マウントは読み取り専用のままにし、ファイル内容をログへ出さないでください。秘密値を
表示せずに存在確認できます。

```bash
kimi-sandbox doctor --config-check
kimi-sandbox . --exec 'test -r /opt/imagegencli_codex.env'
kimi-sandbox . --exec 'conda run -n imagegencli_codex imagegencli_codex doctor'
```

### MCP サーバー設定はサンドボックス内パスを使うこと

Kimi の MCP 設定（検証済みのレイアウト: `~/.kimi-code/mcp.json`、スキーマ
`{"mcpServers": {<name>: {...}}}`）は、ホストパスではなく **サンドボックス内** パスを
参照する必要があります（同じ場所にマウントされている場合を除く）。

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

ランチャーはこのファイルを **自動生成しません**。お使いの Kimi の MCP 設定形式を確認の
うえ、サンドボックスプロファイル内の `mcp.json` を手で編集してください
（`~/.local/state/kimi-sandbox/profiles/<profile>/kimi-code-home/mcp.json`）。
`doctor` はレイアウトを認識できたかを報告しますが、認識できなくてもランチャーの失敗とは
みなしません。

### 書き込みの約束（読み取り専用マウントを守るため）

- MCP ソース（`/opt/kimi-mcp/...`）とランタイム（`/opt/kimi-runtime/...`）へは
  書き込まないこと: ログ・キャッシュ・データベース・`.pyc` のいずれも不可。
- `PYTHONDONTWRITEBYTECODE=1` を設定し、Python が読み取り専用ソースへ `__pycache__` を
  書き込むのを防ぎます。
- ランタイムのキャッシュは `/cache/<server>`（または `/tmp`）へリダイレクトし、ランタイム
  マウントには書き込みません。各サーバーが必要とするものだけを選びます:

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

- サーバー単位の永続キャッシュ → `/cache/<server>`。プロファイル単位の状態 →
  `/kimi-code-home/mcp-state/<server>`。一時ファイル → `/tmp`。

`/cache/<server>` は再生成して構わないデータに、`/kimi-code-home/mcp-state/<server>`
は実行をまたいで残し、プロファイルと一緒に移動すべき状態（小さな DB、インデックス、
登録ファイル）に使います。どちらも書き込み可能なマウント上にあるため、読み取り専用の
MCP ソースは無傷のままです。各サーバーの MCP `env` ブロックから指定します:

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

`/cache` は `persistent_cache = true` のときのみ存在します。`mcp-state` は常に書き込み
可能なプロファイルホーム配下にあるため追加フラグは不要です。サブディレクトリはサーバー
自身が作成してください（例: `os.makedirs(..., exist_ok=True)`）。ランチャーはサーバー
単位のキャッシュ／状態ディレクトリを事前作成しません。

### 検証

```bash
# スキルディレクトリはサンドボックス内で読めるが書けない:
kimi-sandbox . --exec \
  'test -r /kimi-code-home/skills && ! touch /kimi-code-home/skills/.w'

# 最小の MCP ソースが動作し、読み取り専用のまま /cache にキャッシュを書く:
kimi-sandbox . --exec \
  'python3 /opt/kimi-mcp/fake/fake_server.py && ! touch /opt/kimi-mcp/fake/.w'
```

実際の `~/.kimi-code` を書き込み可能でサンドボックスにマウントしないでください。
スキルディレクトリだけを `profile_ro_mounts` で公開します。

### `doctor` と `init-integrations` の注意

- `doctor --config-check` は設定とドライランのマウント計画を検証します。助言的な事項は
  `WARN`（失敗ではない）として扱います: 認識できない Kimi レイアウト、空の `skills/`
  マウントポイント、キャッシュリダイレクトのないランタイムマウント、どのマウントターゲット
  にも含まれない MCP コマンドパス、そして `rw_mounts`（統合モデルは読み取り専用のため
  書き込み可能マウントは指摘されます）。重大な問題（解析不能な設定、欠落／不正なマウント
  ソース）は非ゼロ終了で失敗します。スキルソースの助言的シンボリックリンク走査は既定で
  上限付きです。非常に大きなスキルツリーを網羅的に走査するには `doctor --deep` を使います。
- `init-integrations` は既定でドライランです。`--write` は欠落している設定を作成するか、
  **完全に存在しない** トップレベルキーのみを（タイムスタンプ付きバックアップの後で）
  追記します。`profile_ro_mounts` のようなリストキーが既に存在し、推奨項目が欠けている
  場合は配列を編集せず、手で追加すべき項目を表示します。コメントを保持したままの配列の
  インプレースマージは v2 に持ち越します。

### 実例: 既存の MCP サーバー + スキルをサンドボックスへ取り込む

サンドボックスは **独自の** プロファイルホームで Kimi を実行するため、MCP サーバーと
スキルは *サンドボックス内パス* で記述する必要があります。以下は実際の複数サーバー構成を
配線した手順そのものです。`~/...` と例の名前はご自身のものに置き換えてください。

**1. ランチャーを venv 有効化不要で導入**（[インストール](#インストール) 参照）。任意の
ディレクトリから `kimi-sandbox --version` が動くことを確認します:

```bash
ln -s "$(pwd)/.venv/bin/kimi-sandbox" ~/.local/bin/kimi-sandbox   # または pipx を使用
kimi-sandbox --version
```

**2. ホストの Kimi が使うものを棚卸し。** `~/.kimi-code/mcp.json`（サーバーのコマンドと
パスを含む `env`）と `~/.kimi-code/skills/`（通常は実スキルディレクトリへのシンボリック
リンク）を確認します。各サーバーの **インタプリタ** と **ソース** を把握してください。
これが再配置（relocation）方法を決めます（後述のチートシート参照）。

**3. `~/.config/kimi-sandbox/config.toml` を書く。** サーバーが必要とするホストパスを
すべて `/opt` 配下に読み取り専用でマウントし、conda 環境を使うサーバーがあれば conda を
有効化し、スキルを `/kimi-code-home/skills` 配下に公開します:

```toml
profile = "default"
persistent_cache = true
compat_kimi_home = true

# conda 環境から動く MCP サーバーがある場合に有効化。ホストの conda root を実パスで
# 読み取り専用にバインドするので、「conda 環境のインタプリタ」コマンドが解決できます。
conda_enabled = true
conda_root = "~/anaconda3"
conda_writable = "cache"
conda_shell_integration = true

ro_mounts = [
  # MCP ソースツリー（各サーバーの venv + コードを含む）
  "~/mcp/servers:/opt/kimi-mcp/servers",
  # editable パッケージの実ソース（チートシート「editable install」）
  "~/projects/math-mcp/src:/opt/kimi-mcp/math-mcp-src",
  # venv が指す非システムインタプリタ（チートシート「uv / pyenv」）
  "~/.local/share/uv/python:/opt/kimi-mcp/uv-python",
]

# スキルを /kimi-code-home/skills/<name> に読み取り専用で公開。~/.kimi-code/skills の
# シンボリックリンクではなく、実スキルディレクトリを指してください。
profile_ro_mounts = [
  "~/skills/bizplan-writer:skills/bizplan-writer",
  "~/skills/canvas:skills/canvas",
]

[env_set]
PYTHONDONTWRITEBYTECODE = "1"   # 読み取り専用 /opt へ __pycache__ を書かない
```

> サンドボックスプロファイルの `skills/` に以前の設定で作られた古いシンボリックリンクが
> 残っている場合は先に削除してください（`find ~/.local/state/kimi-sandbox/profiles/\
> default/kimi-code-home/skills -maxdepth 1 -type l -delete`）。ランチャーは実ディレクトリ
> のマウントポイントを作る必要があり、`doctor` はシンボリックリンクのマウントポイントを
> 失敗として報告します。

**4. サンドボックス用 `mcp.json` を生成。** これは実際の `~/.kimi-code` ではなく
サンドボックス **プロファイルホーム** に置き、サンドボックス内パスを使います。最も簡単な
方法はホストのファイルを変換することです。ホストの接頭辞を `/opt` ターゲットへ書き換え、
editable インストールには `PYTHONPATH` を追加し、秘密の env 値はそのまま保持します:

```python
# write_sandbox_mcp.py  （任意の python3 で実行。秘密値は一切出力しない）
import json, os
HOST = os.path.expanduser("~/.kimi-code/mcp.json")
SBX  = os.path.expanduser(
    "~/.local/state/kimi-sandbox/profiles/default/kimi-code-home/mcp.json")
os.makedirs(os.path.dirname(SBX), mode=0o700, exist_ok=True)

# ホストパス接頭辞 -> サンドボックス内 ro_mounts ターゲット
REWRITE = {os.path.expanduser("~/mcp/servers"): "/opt/kimi-mcp/servers"}
# `pip install -e .` のサーバーはソースを PYTHONPATH に載せる必要があります
# （venv 内の .pth はサンドボックスに存在しないホストパスをハードコードしているため）
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
    env = {k: rw(v) for k, v in (spec.get("env") or {}).items()}  # 秘密値は保持
    if name in PYTHONPATH:
        env["PYTHONPATH"] = PYTHONPATH[name]
    if env:
        spec["env"] = env
    out["mcpServers"][name] = spec

json.dump(out, open(SBX, "w"), indent=2)
os.chmod(SBX, 0o600)
```

**5. 検証し、サンドボックス内で各サーバーをプローブ。**

```bash
kimi-sandbox doctor          # 期待値: 0 failed, 0 warning(s)

# 各サーバーのインタプリタ + 依存がサンドボックス内で import できることを確認。例:
mkdir -p /tmp/probe
kimi-sandbox /tmp/probe --exec \
  'PYTHONPATH=/opt/kimi-mcp/math-mcp-src \
   conda run -n math python -c "import math_mcp; print(\"ok\")"'
kimi-sandbox /tmp/probe --exec 'ls /kimi-code-home/skills'   # スキルの存在確認
```

**6. 実行。** これで `kimi-sandbox .` はサーバーとスキルを伴って Kimi を起動します。
サンドボックスプロファイルは実際の `~/.kimi-code` とは別なので、サンドボックス内で一度
ログインしてください。そのログインはプロファイルごとの状態ディレクトリに永続します。

#### インタプリタ再配置チートシート

「ホストでは動くのにサンドボックスで動かない」の唯一の原因は、マウントされていないパスが
venv／インタプリタに焼き込まれていることです。サーバーを次の行に当てはめてください:

| サーバーのインタプリタ／インストール形態 | サンドボックスで必要なこと |
|---|---|
| **システム** `python3`（`/usr/bin/python3`）の venv | ソースツリーを `/opt` にマウントするだけ。システム python は既に読み取り専用でマウント済みなので venv はその `/opt` パスからそのまま動く。 |
| **uv / pyenv** python の venv | そのインタプリタ（例: `~/.local/share/uv/python`）も併せてマウントし、**直接** 実行（`<interp> -m <module>`）。`PYTHONPATH` に venv の `site-packages` ＋ ソースを設定。venv 自身の `python` シンボリックリンクは未マウントのホストパスを指すため。 |
| **conda 環境** のインタプリタ | `conda_enabled = true` を設定。conda root が実パスにバインドされ、`<conda>/envs/<env>/bin/python`（または `conda run -n <env>`）が解決する。新規 env は `/cache/conda` へ。 |
| **editable** インストール（`pip install -e`） | 実ソースをマウントし、そのサーバーの `PYTHONPATH` に追加。site-packages の `.pth` はサンドボックスに存在しないホストパスをハードコードしているため。 |
| ホストパスを含む command／args／env | サンドボックスの `mcp.json` で `/opt` マウントターゲットへ書き換える。 |

`doctor` は各マウントソースの存在、認識された Kimi レイアウトがサンドボックス
（`/opt/...`）パスを使っていること、そして（conda 有効時は）conda 構成が妥当なことを、
Kimi の TUI を起動せずに確認します。実際の `~/.kimi-code` が書き込み可能でマウントされる
ことはありません。

## Conda サポート

既存の conda をサンドボックスへ公開しつつ、ホストの既存 conda 環境はすべて
読み取り専用に保てます。設定で有効化します:

```toml
persistent_cache = true
conda_enabled = true
conda_root = "~/anaconda3"
conda_writable = "cache"          # 新しい env/pkg は /cache/conda へ。"tmp" は揮発
conda_shell_integration = true    # bash -lc での `conda activate` を有効化

# conda_root/envs の外にある追加の既存 env（任意・読み取り専用）:
# conda_existing_envs = ["~/somewhere/envs/foo:foo"]
```

サンドボックス内では `conda` が直接使えます:

```bash
conda --version
conda env list
conda run -n math-mcp python -m math_mcp
conda activate math-mcp && python ...        # conda_shell_integration = true が必要
conda create -n sandbox-dev python=3.11      # -> /cache/conda/envs/sandbox-dev
```

推奨する MCP サーバの書き方:

```json
{ "command": "conda", "args": ["run", "-n", "math-mcp", "python", "-m", "math_mcp"] }
```

```json
{ "command": "bash", "args": ["-lc", "conda activate math-mcp && python -m math_mcp"] }
```

### 読み取り専用と書き込み可能の区別

| 対象 | モード |
|---|---|
| ホストの conda root と既存 env／パッケージ | 読み取り専用（`/opt/kimi-conda/root` ＋ 元の絶対パス） |
| 新しい env／ダウンロードしたパッケージ | 書き込み可能（`/cache/conda` または `/tmp/kimi-conda`） |
| `conda` の入口 | ランチャ生成 shim（`/sandbox/bin/conda`） |

したがって設計上、次は**失敗**します（ホスト env は読み取り専用）:

```bash
conda install -n existing-env some-package   # shim が拒否。FS も読み取り専用
conda env remove -n existing-env             # 拒否
rm -rf /opt/kimi-conda/root/envs/existing    # 読み取り専用ファイルシステム
```

shim は conda の引数全体（`--json`、`--name=`、`--prefix`、
`env update -f environment.yml`、および `--pre` のような曖昧でない省略形を含む）を
解析し、`/cache/conda/envs/<name>` を確実に対象としない変更操作はすべて拒否します。
`conda config` は読み取り専用クエリのみ、`conda clean` は書き込み可能なパッケージ
キャッシュに限定（`--force-pkgs-dirs` は拒否）されます。ホストの conda root は元の
絶対パスにも読み取り専用でバインドされるため、コンソールスクリプトの
shebang（`#!/home/you/anaconda3/...`）も解決できます。設定全体は
`kimi-sandbox doctor` で検証できます。

> **shim はセキュリティ境界ではなく利便性のための層です。** ホストの conda
> 内容は読み取り専用バインドマウントと、ランチャが強制する
> `CONDA_ENVS_PATH`／`CONDA_PKGS_DIRS` によって保護されます。これは shim を
> 迂回しても（実体の conda バイナリを直接呼ぶ、あるいは `conda activate` 後に
> `CONDA_EXE` が実体へ再設定された後でも）有効です。したがってホスト env への
> `conda install` は、shim による早期拒否か FS 層のいずれかで必ず**失敗**し、
> ホストの内容を変更することはありません。

> `conda_writable = "tmp"` で作成した env はサンドボックス実行をまたいで永続
> **しません**。`no_network = true` の場合、`conda create/install` は利用可能な
> ローカルチャネル／キャッシュのみ使用できます。

## セキュリティモデル

### 防げること（ファイルシステムの破壊封じ込め）

- Kimi やそのシェルコマンドが `/etc` などのシステムディレクトリへ書き込むこと。
- Kimi がホームディレクトリ内の他のプロジェクトに触れること。
- コマンドがホストの `/tmp` に書き込むこと。
- Kimi が実際の `~/.ssh`, `~/.aws`, `~/.config`, `~/.kimi-code` などを読み取ること
  （実際の `HOME` は決してマウントされません）。
- **端末インジェクション（TIOCSTI/TIOCLINUX）。** デフォルトで seccomp フィルタが
  インストールされ、これらの ioctl を `EPERM` で失敗させます。これにより、サンドボックス内の
  プロセスが、Kimi 終了後にサンドボックス外で実行させるためのキー入力を制御端末へ
  押し込むこと（CVE-2017-5226 系）を防ぎます。このフィルタは（`--new-session` と異なり）
  TUI を動作させたままにします。さらに **マルチ ABI** 対応です。x86_64 ではネイティブ、
  x32、**および** i386（`int 0x80`）のシステムコール ABI で ioctl をブロックし、aarch64 では
  ネイティブと 32 ビット ARM の ABI でブロックします。それ以外のアーキテクチャ値は許可ではなく
  **拒否** されるため、システムコール ABI を切り替えて回避することはできません。
- **バインド元のすり替え（TOCTOU）。** ホスト側バインド元は `--bind-fd` により検証済みの
  inode に固定されます（[マウント固定](#マウント固定mount-pinning) を参照）。そのため、
  検証からマウントまでの間にシンボリックリンクでパスを差し替えることはできません。

### オプトインのハードニング

- **ネットワーク分離**（`--no-network`）: `--unshare-net` を追加し、`curl`、`pip`、`npm`、
  MCP サーバーがネットワークに到達できないようにします。Kimi 自体がモデル呼び出しに
  ネットワークを必要とするため *デフォルトは無効* です（認証情報に関する注記を参照）。
  これは全か無かであり、Kimi 本体プロセスもモデルへの接続を失います。
- **読み取り専用プロジェクト**（`--read-only`）: `/workspace` を読み取り専用でマウントします。
  Kimi はコードを読んで推論できますが変更はできません。
- **リソース制限**（`--memory-max` / `--cpu-quota` / `--pids-max`）: サンドボックスを一時的な
  `systemd-run --user --scope` でラップし、対応する cgroup の `MemoryMax` / `CPUQuota` /
  `TasksMax` プロパティを設定します。暴走するメモリ・CPU・fork 爆弾を封じ込めます。
  ユーザー systemd が必要です。（seccomp フィルタの fd と固定マウントの fd は、この
  ラッパーを越えて正しく継承されます。）

### 防げないこと

- **デフォルトのネットワークアクセス** — `--no-network` を付けない限りネットワーク分離は
  ありません。付けた場合でも、*Kimi 本体プロセス* もモデルへの接続を失います
  （モデル用とツール用のネットワーク分割はありません。下記参照）。
- マウントされたプロジェクト **内** のファイルの読み取りや削除（`--read-only` でない限り
  `/workspace` は読み書き可能）。
- `/kimi-code-home` に置かれた認証情報が、サンドボックス内の `Bash` コマンドから
  読まれること — 本体プロセスとその子コマンドは同一のファイルシステムビューを共有します。
- カーネルや bubblewrap の脱獄、または機微なディレクトリを意図的に `--rw-mount` する
  ユーザー。

### 残存リスクと注意点

- **TIOCSTI seccomp フィルタの適用範囲。** このフィルタは（libseccomp 依存なしで）
  プロセス内で構築される小さな classic-BPF プログラムです。既知のアーキテクチャ
  （`x86_64`, `aarch64`）でのみインストールされます。その他のアーキテクチャでは、ランチャーは
  注記を表示し、それ **なし** で続行します（実行を拒否せず、未防御の状態に縮退します）。
  対応アーキテクチャでは、そのマシンでカーネルが受け付けるすべてのシステムコール ABI
  （ネイティブ + 32 ビット/x32 互換）をカバーし、外来 ABI を拒否するため、「ABI を切り替えて
  回避」する余地はありません。`--no-seccomp` で完全に無効化できます。多くの最近のカーネルは
  TIOCSTI を独立して制限しています（`dev.tty.legacy_tiocsti_restrict`）。
- **マウント固定の残存リスク。** inode 固定により、TOCTOU の隙はランチャーが
  解決済みでシンボリックリンクを含まないパスを `open(O_PATH)` で一度走査する間だけに
  縮小されます。それ以降 fd は固定です。`--no-pin-mounts`（または bubblewrap < 0.5）では、
  検証→マウントのより広い窓が戻ります。いずれにせよ、起動中に信頼できないユーザーが
  変更できるディレクトリをサンドボックスの対象にしないでください。
- **モデル用/ツール用のネットワーク分割なし。** `--no-network` は全か無かです。将来的には
  認証情報ブローカーにより、本体プロセスのモデル接続を維持しつつツールのサブプロセスには
  拒否する、ということも考えられますが、現状では未実装です。
- サンドボックス内の `/dev/shm` は、bubblewrap のデフォルトデバイスセット由来の書き込み
  可能（ただし名前空間ごとに揮発性）な tmpfs です。ホストには影響しません。
- **conda の変更ポリシーは境界ではありません。** conda を有効化すると、生成された
  `/sandbox/bin/conda` shim はホスト env への変更を**早期**に明確なメッセージで拒否
  しますが、これは利便性／早期拒否の層にすぎません。実体の conda バイナリは
  サンドボックス内から直接到達可能（例: `/opt/kimi-conda/root/bin/conda`）なので、
  ホストの env／パッケージが変更されない保証は、**読み取り専用バインドマウント**と
  ランチャが強制する `CONDA_ENVS_PATH`／`CONDA_PKGS_DIRS`／`CONDARC`（すべての書き込みを
  サンドボックスの書き込み可能領域へ誘導）に由来します。これらは shim を完全に迂回
  しても（実体の conda 直接呼び出し、または `conda activate` が `CONDA_EXE` を実体へ
  再設定した後でも）有効です。隔離のために shim の引数解析に依存しないでください。
- **特権とケイパビリティ。** ランチャーは非特権で動作し、bubblewrap の非特権ユーザー
  名前空間に依存します。サンドボックス内プロセスが保持するケイパビリティはその
  ユーザー名前空間の内側にのみ存在し、ホスト上では *nobody* にマップされるため、ホストの
  特権を付与しません。サンドボックスは setuid ではなく root も必要としません。ランチャーは
  `--cap-add` を追加せず、bubblewrap がデフォルトでサンドボックス内プロセスの ambient/bounding
  セットをクリアするため、明示的にケイパビリティを落とす必要はありません。真の境界は
  カーネルのユーザー名前空間と bubblewrap の実装です（上記の脱獄に関する注意点を参照）。

### 認証情報の境界

Kimi 本体プロセスと、それが起動する `Bash`/MCP/フックの各コマンドは、**同一** の
サンドボックスファイルシステムで動作します。Kimi が読めるものは、それらのコマンドも
読めます。したがって:

- プロファイルには **専用かつ低権限** の Kimi アカウントまたは API キーを使用してください。
- 本番の認証情報をサンドボックスプロファイルに置かないでください。
- `--unsafe-kimi-code-home` は指定したディレクトリをすべてのサンドボックス内プロセスに
  公開します。理解したうえでのみ使用してください。広範な／システムのパスと実際の
  `~/.kimi-code` は常に拒否されます。

## 推奨する Kimi 設定

サンドボックスプロファイルの Kimi 設定では、手動承認を推奨します。サンドボックスは
強固な境界であり、パーミッションは対話的な確認レイヤーであって、両者を組み合わせると
より強固になります。

```toml
default_permission_mode = "manual"
```

YOLO モードは避けてください。サンドボックス内であっても `/workspace` のすべてを削除・
書き換えできてしまいます。

## 開発

```bash
pip install -e ".[dev]"
python -m pytest                 # ユニットテスト（実際の bwrap は不要）
bash tests/smoke/run_smoke.sh    # スモークテスト（実際の bwrap + kimi が必要）
```

ユニットスイートは、パス検証、`bwrap` の argv（inode 固定、merged- と非 merged-`/usr`、
最小 `/etc` を含む）、設定ファイルの優先順位、`systemd-run` ラッパー越しの seccomp-fd の
受け渡し、そして seccomp フィルタ自体をカバーします。seccomp テストには小さなプロセス内
cBPF インタプリタが含まれ、バイトコードを目視確認するだけでなく、マルチ ABI のブロック挙動
（native/x32/i386/ARM を拒否、その他の ioctl を許可、外来 arch を拒否）を *証明* します。

スモークスクリプトは、設計ドキュメントの受け入れ基準（セクション 28）に加え、ハードニング
機能（`--no-network`、`--read-only`、マルチ ABI seccomp、マウント固定、永続キャッシュ、
追加マウント、リソース制限、設定ファイル、および CLI が設定を上書きする否定フラグ）に
対応しています。

継続的インテグレーション（`.github/workflows/ci.yml`）は、すべての push と PR で Python
3.11〜3.13 のユニットスイートと `bwrap` の dry-run 健全性チェックを実行します。完全な
スモークスイート（実際の `bwrap`、ダミーの `kimi` スタブ、ベストエフォートのユーザー
systemd リソース制限）は、手動の `workflow_dispatch` トリガーで利用できます。

## ライセンス

MIT ライセンスで公開されています。ライセンス全文は [`LICENSE`](LICENSE) ファイルを
参照してください。
