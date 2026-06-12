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
- [ハードニング用フラグ](#ハードニング用フラグ)
- [マウント固定（mount pinning）](#マウント固定mount-pinning)
- [設定ファイル](#設定ファイル)
- [オプション](#オプション)
- [サンドボックス内部の様子](#サンドボックス内部の様子)
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
