# wtm — Issue 駆動の worktree / AI エージェント管理 CLI

Git worktree で Claude Code や Codex を並列実行するための個人向け CLI です。
GitHub または Gitea の Issue を作業の正本とし、「何のための worktree か」「どこまで進んだか」を失わないようにします。

> **Phase 1〜5** を実装済みです。進捗と未確認の事項は [docs/implementation-plan.md](docs/implementation-plan.md)、設計は [docs/architecture.md](docs/architecture.md)、日々の使い方は [docs/workflow-best-practices.md](docs/workflow-best-practices.md) を参照してください。

## 必要なもの

- Python 3.12 以上
- Git
- （任意）[gh](https://cli.github.com/) — GitHub 連携（既定）。`gh auth login` 済みであること
- （任意）[tea](https://gitea.com/gitea/tea) — Gitea 連携（`wtm init --forge gitea`）
- どちらも無い場合は `--offline --title` で利用可能
- （任意）Claude Code CLI (`claude`) / Codex CLI (`codex`)

## インストール

```bash
# uv
uv tool install .

# または pip
python -m venv .venv
.venv/Scripts/activate      # macOS/Linux: source .venv/bin/activate
pip install -e .
```

### PATH の設定（Windows）

コマンド名は **`wtm`** です（`wt` は Windows Terminal の `wt.exe` と衝突するため使いません）。
別のディレクトリからも使えるよう、venv の `Scripts` を PATH に追加します。

```powershell
# venv の Scripts を PATH に追加（新しいシェルから有効）
[Environment]::SetEnvironmentVariable("Path",
  [Environment]::GetEnvironmentVariable("Path", "User") + ";C:\path\to\wt-python-cli\.venv\Scripts", "User")
wtm --help
```

環境変数 `WT_CONFIG`、Git 設定 `wt.config`、`wt.db`、`wt.lock`、既定のブランチ名 `wt-<番号>-<識別子>`、Python パッケージ名 `wt` は、既存の環境との互換性のため変更していません。

## 使い方

### 1. 初期化

管理対象リポジトリ（main worktree）で実行します。既存の worktree は変更しません。

```bash
cd C:/work/myproject/main
wtm init                                  # GitHub（gh）連携。基準ブランチ等を対話で確認
wtm init --forge gitea                    # Gitea（tea）連携
wtm init --forge gitea --forge-login my   # tea のログインを指定
wtm init --forge-host ghe.example.com     # GitHub Enterprise Server
wtm init --yes --no-forge                 # 既定値で、Issue/PR 連携なし
wtm init --dry-run                        # 作成内容の確認のみ
```

| オプション | 意味 |
|---|---|
| `--forge github\|gitea` | Issue/PR の連携先。省略時は `github`（gh CLI） |
| `--forge-host` | github: GitHub Enterprise Server のホスト名。省略時は github.com（gh で認証済みの GHE ホストがリモートと一致すれば自動設定） |
| `--forge-login` | gitea: 使用する tea のログイン名 |
| `--forge-cli` | CLI の実行ファイル（既定 `gh` / `tea`） |
| `--no-forge` | 連携を無効にする（旧 `--no-gitea` も利用可） |

標準構成ではリポジトリの隣に `worktrees/` と `management/` を作成します。

```text
workspace/
├── main/            # リポジトリ
├── worktrees/       # wt-101-login-form/, wt-102-fix-api/ ...
└── management/      # config.toml, wt.db, checkpoints/, sessions/, logs/
```

リポジトリのディレクトリ名が `main` でない場合は `<repo>.wt/` 配下に作成します（`--worktree-root` / `--management-dir` で変更可）。
設定の場所は `git config --local wt.config` に記録されるため、どの worktree からでも `wtm` を実行できます。

### 2. 作業開始

```bash
wtm start --issue 101 --agent claude   # worktree 作成後に Claude Code を起動
wtm start --issue 102 --agent codex
wtm start --issue 103 --no-launch      # worktree のみ作成
wtm start --issue 104 --dry-run        # 計画の表示のみ
wtm start --issue 105 --offline --title "ログイン画面の修正" --slug login-form --no-launch   # → wt-105-login-form
wtm start --new --title "API の追加" --body-file goal.md --slug api   # Issue を作成してから開始（作成前に確認）
```

- Issue 情報は `gh api`（GitHub）または `tea api`（Gitea）で取得します（クローズ済み Issue は `--allow-closed` が必要）。
- ブランチ `wt-<番号>-<識別子>` と、同名の worktree フォルダ（`worktrees/wt-<番号>-<識別子>`）を作成します。
  識別子は `--slug` で指定します。省略時はタイトルから英数字で生成し、日本語のみのタイトルでは `task` になります。
  形式は config.toml の `[naming]` で変更できます。
- 既に `wt-<番号>-*` ブランチがあればリセットせずに再利用し（フォルダ名もそのブランチの識別子を使います）、他の worktree で使用中なら中断します。
- worktree に `TASK.md` を生成し、`.git/info/exclude` に登録して Git の追跡対象外にします（`.gitignore` は変更しません）。
- 途中で失敗した場合は、作成途中の worktree・ブランチ・DB 登録を戻します。

### 3. 一覧と状態確認

```bash
wtm list              # 一覧（7 日以上確認していないものは黄色）
wtm list --stale      # 放置されているもののみ
wtm list --wide       # パスも表示
wtm list --json       # 他ツール向け（schema_version: 1）
wtm list --offline    # GitHub / Gitea に問い合わせない（PR 列は空）
wtm list --archived   # 整理済みの作業と記録の場所
wtm status 101        # Issue の目的・Git 状態・Checkpoint・次のアクション
wtm status 101 --offline --json
```

`wtm list` / `wtm status` は Gitea からブランチに対応する PR（open / draft / merged / closed）を取得して表示します。
`wtm status` を実行すると「最終確認日」が更新されます。

### 4. Checkpoint（作業経過の記録）

```bash
wtm checkpoint 101                  # 完了事項・未完了事項・次のアクション・ブロッカーを対話入力
wtm checkpoint 101 --completed "API 実装" --remaining "テスト" --next "レビュー依頼" --yes
wtm checkpoint 101 --publish        # Issue に進捗をコメント（投稿内容を表示して確認）
wtm checkpoints 101                 # 記録した Checkpoint の履歴を新しい順に一覧（--limit N / --json）
wtm checkpoints 101 --show 12       # Checkpoint id=12 の内容を全文表示
```

HEAD・差分統計・未コミットファイル一覧も自動で記録します。ファイルの変更やコミットは行いません。
Issue へのコメントには AI セッションの内容・ID を含めません。
`wtm checkpoints` は worktree を整理（ARCHIVED）した後も参照できます。

AI が書いた Checkpoint ファイルも取り込めます（Markdown または JSON、UTF-8、1 MiB まで）。

```bash
wtm checkpoint 101 --from-file C:/work/myproject/worktrees/wt-101-login-form/.wtm/checkpoint.md
```

```markdown
## Completed
完了した事項

## Remaining
未完了の事項

## Next Action
次に実施すること

## Blockers
作業を止めている要因
```

JSON の場合のキーは `completed` / `remaining` / `next_action` / `blockers` です（値は文字列または文字列の配列）。

`[management].auto_checkpoint = true` にすると、AI の終了時に Checkpoint を自動で記録します。AI がそのセッション中に `.wtm/checkpoint.md`（または `.json`）を書いていれば取り込み、なければ Git のメタデータだけを記録します。

### 5. AI エージェントへの引き継ぎと再開

`wtm start` と `wtm resume` は、エージェントを起動する前に `.wtm/handoff.md` を生成します。

- **内容:** 共通の作業ルール、Issue、現在の状態、最新の Checkpoint です。AI の会話内容とセッション ID は含めません。
- **Git との関係:** `.wtm/` は `info/exclude` で追跡対象外にします。既存の `CLAUDE.md` や `AGENTS.md` は変更しません。
- **Claude Code:** `--append-system-prompt-file` で自動で渡し、`--session-id` で割り当てたセッション ID を記録します。
- **Codex:** オプションを未確認のため、自動では渡しません。起動後に「`.wtm/handoff.md` を読んで作業してください」と伝えてください。
- **共通ルールの変更:** 管理ディレクトリに `agent-instructions.md` を置くと差し替えられます。`{issue}`、`{task_file}`、`{checkpoint_file}` が置換されます。

```bash
wtm resume 101                      # 最後に使ったセッションを再開
wtm resume 101 --agent codex        # 別のエージェントで起動
wtm resume 101 --session <id>       # セッションを指定して再開
wtm resume 101 --new-session        # 再開せず、引き継ぎ情報を渡して新規に起動
wtm resume 101 --dry-run            # 計画の表示のみ
wtm handoff 101 --stdout            # 引き継ぎ情報の生成と表示のみ
```

- 再開の直後（15 秒以内）にエージェントがエラー終了した場合は再開失敗とみなし、確認のうえ新規セッションで起動します（`--yes` で確認を省略）。
- worktree が削除されている場合は再作成せず、復元コマンドを表示して終了します。
- 作業状態が REVIEW の worktree でエージェントを起動すると、WORKING に戻ります。

### 6. レビュー（継続・分割・終了の判断材料）

```bash
wtm review 101                      # 端末に表示
wtm review 101 --format md          # Markdown（AI に渡す場合など）
wtm review 101 --format json        # JSON（schema_version: 1）
wtm review 101 --format md -o r.md  # ファイルに保存（既存ファイルは上書きしない）
wtm review 101 --offline            # GitHub / Gitea に問い合わせない
```

次の項目を規則で判定し、「継続」「分割を検討」「終了を検討」のいずれかを示します。Git と Gitea には書き込みません。

- Issue 本文のチェックリスト（`- [ ]`）の未完了
- 7 日以上 Checkpoint なし、ブロッカーの記録、AI の動作中
- 未コミット変更、コンフリクト
- PR の状態（作成済み・マージ済み・クローズ）
- 基準ブランチからの乖離、変更ファイル数（`[management].diverged_commits` / `large_change_files`、既定 50）
- 他の worktree との変更ファイルの重複

### 7. 作業の終了（レビューへ）

```bash
wtm finish 101 --dry-run            # 検査結果のみ表示
wtm finish 101                      # 検査後、PR を作るか確認して REVIEW へ
wtm finish 101 --push --create-pr   # push と PR 作成をまとめて実行
wtm finish 101 --no-pr              # PR は作らず REVIEW へ遷移のみ
```

- AI が動作中、未コミット変更あり（`--allow-dirty` で続行可）、Checkpoint なし（`--skip-checkpoint` で続行可）の場合は中断します。
- 既存の PR があれば作成せずに表示します。PR は `gh api` / `tea pulls create` で作成し、本文は `Closes #<Issue>` で始まります（マージ時に Issue が自動クローズされます）。
- push・PR 作成は確認するか、`--push` / `--create-pr` を指定した場合のみ実行します（`--force` push はしません）。

### 8. 作業状態の手動変更

```bash
wtm state 101                                   # 現在の状態と変更履歴を表示（--json 可）
wtm state 101 BLOCKED --reason "API の承認待ち"
wtm state 101 WORKING                           # BLOCKED を解除
wtm state 101 DONE                              # 検査して完了にする
wtm state 101 DONE --dry-run                    # 検査結果の表示のみ
wtm state 101 WORKING --reason "修正が必要"      # DONE から差し戻し
```

- 指定できる変更先は `WORKING` / `BLOCKED` / `DONE` です。`REVIEW` は `wtm finish` で変更します。
- BLOCKED への変更、DONE からの差し戻し、PLANNED から DONE への変更には理由が必要です。DONE から BLOCKED へは直接変更できません。
- DONE への変更は次のように検査します。Gitea と Git には書き込みません（Issue もクローズしません）。

| 検査項目 | 結果 |
|---|---|
| AI が動作中または状態不明、コンフリクト | 中断 |
| 未コミット変更 | 中断（`--allow-dirty` で続行） |
| Checkpoint なし | 中断（`--skip-checkpoint` で続行） |
| PR が未マージまたはなし、基準ブランチにも push 先にもないコミット、Issue の未完了チェックリスト | 確認（`--yes` で省略） |

- PLANNED から DONE に変更する場合は、着手していないものとして Checkpoint、PR、チェックリストの検査を省きます。
- DONE の worktree ではエージェントを起動できません。再開するには WORKING に戻してください。BLOCKED の場合は起動すると WORKING に戻ります。
- `--dry-run` は状態と最終確認日を変更しません（Gitea から取得した Issue のキャッシュは更新します）。

### 9. AI セッションの履歴

```bash
wtm history 101                     # セッションの一覧（--agent / --json 可）
wtm history 101 --session <ID>      # 会話を表示（ID は先頭一致可）
wtm history 101 --session <ID> --format json
wtm history 101 --session <ID> --tools        # ツールの入出力も含める
wtm history 101 --session <ID> --export s.md  # ファイルに出力（既存ファイルは上書きしない）
wtm history 101 --session <ID> --from-backup  # バックアップを読む
wtm history 101 --backup            # バックアップを作成・更新
```

- **検出:** 各セッションファイルの作業ディレクトリで worktree と対応付けます。`wtm` を通さずに起動した Claude Code のセッションも見つかります。
- **バックアップ:** `wtm` から起動したエージェントの終了時に自動で作成します。保存先は `<管理ディレクトリ>/sessions/issue-<番号>/claude/` です。
- **削除後の閲覧:** worktree や元のセッションファイルが消えても、バックアップから閲覧できます。同じパスに worktree を作り直すと、以前のセッションの対応も復元され、`wtm resume` の候補になります。
- **安全性:** 元のセッションファイルは変更しません。会話を Gitea に投稿しません。バックアップには機密情報が含まれる可能性があるので、管理ディレクトリの扱いに注意してください。
- **表示の範囲:** 表示と出力は発話の本文が対象です。思考内容は含めません。ツールの入出力は `--tools` を付けたときだけ含めます。
- **対応範囲:** Claude Code の保存形式は非公開のため、変更されると読めなくなる可能性があります。未対応の形式は閲覧時にエラーになりますが、バックアップは形式に関係なくコピーします。Codex のセッションは検出しません。

### 10. worktree の整理

```bash
wtm cleanup                # 削除候補・要確認・放置の一覧（何も削除しない）
wtm cleanup --dry-run      # 同じ
wtm cleanup 101 --dry-run  # 1 件の検査結果（--json 可）
wtm cleanup 101            # 検査 → 確認 → 削除 → ARCHIVED
```

**候補:** PR がマージ済み、または作業状態が DONE の worktree です。それ以外は `wtm state 101 DONE` で完了にしてから整理します。一括削除はしません。

**次のいずれかに該当すると削除しません（上書きするオプションはありません）:**

- 未コミットの変更、未追跡ファイル、コンフリクトがある
- worktree がロックされている
- AI が動作中、または停止を確認できない（`wtm` の記録、Claude Code のプロセス記録、5 分以内に更新されたセッションで判定）
- Checkpoint がない、または `.wtm/checkpoint.*` が取り込まれていない
- セッションをバックアップできない
- マージも push もされていないコミットがある
- Gitea に接続できず、Issue と PR の状態を確認できない
- カレントディレクトリが対象の worktree の中にある（別の場所から実行してください）

**無視ファイルに注意:** `git worktree remove` は `.gitignore` の対象（`.env`、`node_modules/` など）を確認なしで削除します。`wtm cleanup` は該当するファイルを一覧表示し、確認を求めます。`--yes` のときは `--delete-ignored` も必要です。これらはアーカイブに保存しません。

**残るもの:** Gitea の Issue と PR、ブランチ（削除するコマンドだけ表示します）、Checkpoint、セッションの記録とバックアップ、Claude Code の元のセッションファイル。
さらに次の内容を `<管理ディレクトリ>/archive/issue-<番号>/<日時>/` に保存します。

- `summary.json`: Issue と PR の情報、HEAD、コミット一覧、状態の履歴、Checkpoint、セッションの一覧、復元コマンド
- `files/`: `TASK.md` と `.wtm/` のコピー（会話の内容は含みません）

**整理の後は:** `wtm history 101` で会話を閲覧できます。作業を再開するには `wtm start --issue 101` で作り直します（ブランチを再利用し、以前のセッションも再開の候補になります）。

**検出の限界:** Codex と、起動時の作業ディレクトリが別の場所にある Claude Code は検出できません。Codex を使った worktree では、停止しているかどうかを確認するよう求めます。

### 11. 既存 worktree の取り込み

```bash
wtm adopt C:/work/myproject/worktrees/existing --issue 101
```

ファイル・ブランチ・履歴は変更せず、DB に登録して初回 Checkpoint を保存します。

### 12. 診断

```bash
wtm doctor          # Python / git / gh・tea / GitHub・Gitea 接続 / claude / codex / DB / worktree の整合性
wtm doctor --fix    # 中断した作成処理の予約、停止済みエージェント記録のみ修復
```

## 設定 (config.toml)

```toml
[repository]
path = "C:/work/myproject/main"
remote = "origin"
base_branch = "develop"
worktree_root = "C:/work/myproject/worktrees"

[forge]
provider = "github"   # "github"（gh）| "gitea"（tea）
enabled = true
cli = "gh"
login = ""            # gitea: tea のログイン名
host = ""             # github: GitHub Enterprise Server のホスト名（空なら github.com）
repo = ""             # "owner/name"。空ならリモート URL から求める

[agents]
default = "claude"

[agents.commands]     # エージェントの起動コマンド（引数配列）
claude = ["claude"]
codex = ["codex"]

[management]
stale_days = 7
require_issue = true
auto_checkpoint = false   # AI の終了時に Checkpoint を自動で記録する
diverged_commits = 50     # wtm review: 基準からのコミット差の警告しきい値
large_change_files = 50   # wtm review: 変更ファイル数の警告しきい値

[cleanup]
require_confirmation = true    # false: 確認を省略（--yes と同じ。無視ファイルには --delete-ignored が必要）
require_clean_worktree = true  # false: wtm の事前検査のみ省略（--force は使わないので Git が拒否する）
require_session_backup = true  # false: バックアップに失敗しても削除する（履歴を失う可能性あり）

[task]
file_name = "TASK.md"
exclude = true        # info/exclude に登録する

[naming]              # {issue}=Issue 番号（必須）, {slug}=識別子
branch = "wt-{issue}-{slug}"
worktree = "wt-{issue}-{slug}"   # パス区切り文字は不可
```

認証情報は書かないでください（認証は gh / tea の設定を使います）。
旧形式の `[gitea]` セクションもそのまま読み込めます（`provider = "gitea"` として扱います）。

## 開発

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -e . pytest
.venv/Scripts/python -m pytest
```

テストは一時ディレクトリに作成した実 Git リポジトリ（日本語・空白を含むパス）を使用し、Gitea と AI CLI はモックします。

## ライセンス

[LICENSE](LICENSE) を参照してください。
