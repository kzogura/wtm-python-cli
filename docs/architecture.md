# wtm アーキテクチャ

## 1. 要件確認で洗い出した問題点・依存関係

| # | 論点 | 判断 |
|---|---|---|
| 1 | `tea issues <n>` の詳細表示は `--output json` が効くか不明（Gitea ログイン未設定の環境では検証不可） | Issue 取得は `tea api /repos/{owner}/{repo}/issues/<n>` で Gitea REST API の JSON を取得する。`tea api` は tea 0.16.0 の `--help` で存在とオプション（`--login`, `--repo`）を確認済み |
| 2 | 設定ファイルの探索方法が未定義 | `--config` → 環境変数 `WT_CONFIG` → 対象リポジトリのローカル Git 設定 `wt.config`（`wtm init` が `git config --local` で書き込む）。linked worktree の中からでも共通の `.git/config` を参照できる |
| 3 | 標準構成（`workspace/main`）以外のリポジトリの管理ディレクトリの置き場 | リポジトリ名が `main` なら `workspace/{worktrees,management}`、それ以外は `<repo>.wt/{worktrees,management}` を既定にし、`--worktree-root` / `--management-dir` で上書き可能 |
| 4 | TASK.md を `.gitignore` を変えずに追跡対象外にする方法 | `<git-common-dir>/info/exclude` に `/TASK.md` を追記する（ローカル専用・非共有）。全 worktree 共通に効くが、追跡済みファイルには影響しない。追跡済み TASK.md がある場合は生成しない。`[task].exclude = false` で無効化可能 |
| 5 | 同一 Issue の並列作成の排他 | SQLite の部分一意インデックス（`state <> 'ARCHIVED'` の行で `issue_number` 一意）＋ `BEGIN IMMEDIATE`。Git 操作前に「予約行（provision=creating）」を作る |
| 6 | Git 操作途中失敗の整合性回復 | 失敗時に実際の状態（worktree list・ブランチ有無）を調べ、自分が作ったもの（worktree・新規ブランチ・TASK.md・予約行）だけを戻す。プロセス強制終了で残った予約は `wtm doctor --fix` で削除（10 分以上経過かつ Git 上に worktree が無い場合のみ） |
| 7 | AI プロセス検出の OS 差異と PID 再利用 | Windows は `tasklist`、POSIX は `os.kill(pid, 0)`。生存しているように見える PID は「動作中の可能性あり」、判定不能は「状態不明」として扱い、どちらも処理を保留する |
| 8 | Claude Code セッションストレージは非公開仕様 | Phase 4 で互換性レイヤー（`ClaudeSessionStore`）として分離。Phase 1 では保存先（`$CLAUDE_CONFIG_DIR` or `~/.claude` の `projects/`）の参照可否のみ doctor で確認 |
| 9 | Codex CLI が開発環境に無い | オプションを推測しない。Codex アダプタは「起動のみ」を Capability として宣言 |
| 10 | Phase 1 の `start` は Issue 番号指定だが Issue 取得は Phase 2 | Phase 1 で `tea api` による取得を実装（正本は Gitea）。tea が使えない場合のために `--offline --title` を用意 |
| 11 | `uv` が開発環境に未導入 | `pyproject.toml` は uv / pip 両対応。開発検証は venv + pip で実施 |
| 12 | `--new`（Issue 対話作成）は Issue 作成 = 実環境変更 | Phase 2 で実装。テストではモックのみ使用 |
| 13 | ブランチ名・worktree 名の形式（仕様書では `feature/<n>-<slug>` / `issue-<n>`） | 利用者の要望により既定を `wt-<n>-<slug>`（ブランチ・フォルダ共通）に変更。`[naming].branch` / `[naming].worktree` のテンプレートで仕様書の形式にも戻せる。`{issue}` は必須（既存ブランチ検索に使用） |
| 14 | コマンド名（仕様書では `wt`）は Windows Terminal の `wt.exe` と衝突する | 利用者の要望により正式名を `wtm` に変更し、`wt` スクリプトは廃止。`WT_CONFIG`、`wt.config`、`wt.db`、`wt.lock`、`<repo>.wt/`、既定のブランチ名 `wt-<n>-<slug>`、Python パッケージ名 `wt`、配布名 `wt-cli` は既存環境との互換性のため維持 |
| 15 | 共通指示を CLAUDE.md を変更せずに渡す方法 | worktree 内の `.wtm/handoff.md` に生成し、Claude Code には `--append-system-prompt-file` で渡す。`.wtm/` は `info/exclude` で追跡対象外。Codex はオプション未確認のため利用者が読むよう伝える |
| 16 | Claude のセッション ID の取得方法 | 内部ファイルを解析せず、新規起動時に `--session-id <uuid>` で wtm が割り当てて DB に記録する。wtm を通さず起動したセッションは Phase 4 で検出する |
| 17 | セッション再開の失敗をどう検知するか | 端末を継承するため stderr を読めない。`--resume` で起動して 15 秒以内に非 0 で終了した場合を失敗とみなす。`claude -p --resume <不明な ID>` が exit 1 になることは確認済み。対話モードでの同じ動作は未確認 |
| 18 | `.wtm/checkpoint.md` の取り込みの既定動作 | `auto_checkpoint = false` では取り込みコマンドを案内するだけ。`true` ならそのセッション中に更新されたファイルだけを取り込み、古いファイルは取り込まない |
| 19 | BLOCKED / DONE への遷移手段が仕様に無い | 利用者と合意のうえ `wtm state` に 1 本化。変更先は WORKING / BLOCKED / DONE。DONE はエージェント起動を拒否し、差し戻しの理由を履歴に残す。PR なしの DONE は確認のうえ許可し、未マージのコミットは Phase 5 の cleanup で止める |
| 20 | Claude のセッション一覧・出力を取得する公式 CLI が無い（2.1.318 の `--help` で確認） | 内部 JSONL の読み取りを `claude_store.py` に閉じ込める。ディレクトリ名の変換規則は非公開なので使わず、各ファイルの `cwd` で対応付ける |
| 21 | バックアップの更新方法 | バイト単位でコピーし、読み直して SHA-256 で検証してから置き換える。元ファイルが追記以外で変わっていたら旧バックアップを `.bak` として残す。現在の内容かどうかはサイズと更新時刻で判定する |
| 22 | 未対応形式の扱い | 主要バージョンが 2 以外、または形式を解釈できないファイルは閲覧時にエラーにする。一覧では「未対応」と表示し、バックアップは形式に依存せず実行する |
| 23 | `git worktree remove` は無視対象のファイル（`.env` など）を確認なしで削除する（git 2.50.1 で確認）。仕様の「未追跡ファイルがない」の検査では検出できない | `git ls-files --others --ignored --exclude-standard --directory` で検出し、確認を求める。`--yes` では `--delete-ignored` を別途必須にする。`TASK.md` と `.wtm/` はアーカイブに保存する。その他は秘密情報の複製を避けるため保存しない |
| 24 | Windows ではプロセスの cwd が中にある worktree の削除が exit 255 で失敗するが、中身と Git の登録は消えて空のディレクトリだけが残る（実機で確認） | 失敗後に Git の登録とディレクトリを確認する。登録が消えて空なら削除できたものとして扱い、残りの処理を完了する。それ以外は状態を変えずエラーにする。自プロセスの cwd が中にある場合は事前に拒否する |
| 25 | squash マージではブランチのコミットが基準ブランチに含まれない | Gitea の PR の `head.sha`（2026-11-05, Gitea 1.26.1 で確認）までを到達済みとして比較する。マージ後に追加したコミットは検出する |
| 26 | wtm の外で起動した AI の検出 | Claude Code は `<設定ディレクトリ>/sessions/<pid>.json` の `pid` / `cwd`（2.1.318 で確認。非公開形式）と、セッションファイルの更新時刻（5 分以内）で判定する。PID の生存を確認できなければ動作中として扱う。Codex は検出できないので、使用記録がある worktree では確認を求める |
| 27 | `[cleanup]` の設定と「`--force` なし」の関係 | `require_clean_worktree = false` でも Git が拒否するため、削除される範囲は変わらない（wtm の事前検査を省略するだけ）。`require_session_backup = false` だけが保護を弱める |
| 28 | 削除を中断できない順序の問題 | セッションのバックアップ → ロック取得 → 再検査 → アーカイブ → `git worktree remove` → ARCHIVED の順。削除後に中断しても、次の実行で「worktree なし」として記録だけ完了できる。スキーマは変更しない |
| 29 | head ブランチを削除すると PR を見つけられない | Gitea は削除後の `head.ref` を `refs/pull/<n>/head` に変え、ブランチ名は `head.label` にだけ残す（2026-11-05, Gitea 1.26.1 で確認）。この場合は `label` で照合する。同じブランチ名を再利用しても、`wtm finish` は HEAD と `head.sha` が異なるマージ済み PR を既存 PR として扱わない |

## 2. 全体構成

```text
cli.py  (Typer + Rich: 入出力・対話・表示のみ)
  │
service.py  (ユースケース: init / start / adopt / list / status / doctor / launch / checkpoint / finish / resume)
review.py   (wtm review のルールとレポート。service を利用)
lifecycle.py (wtm state の遷移表と検査。service と review を利用)
cleanup.py  (wtm cleanup の検査・実行・アーカイブ。service と sessions を利用)
  │
  ├─ sessions.py         セッションと worktree の対応、バックアップ、閲覧
  │    └─ claude_store.py Claude Code の保存形式の互換性レイヤー（読み取り専用）
  ├─ handoff.py          引き継ぎ情報（共通指示 + Issue + Checkpoint）の生成
  ├─ checkpoint_file.py  AI が生成した Checkpoint ファイルの解析
  ├─ config.py    TOML 設定・探索
  ├─ db.py        SQLite（スキーマ・トランザクション）
  ├─ gitops.py    Git CLI ラッパー（porcelain -z パーサ）
  ├─ gitea.py     IssueProvider / TeaClient（tea CLI）
  ├─ agents.py    AgentAdapter + Capability（Claude / Codex）
  ├─ taskfile.py  TASK.md 生成・info/exclude
  ├─ paths.py     パス正規化・比較
  └─ proc.py      subprocess 実行（shell=False）・PID 生存確認
```

- 外部依存（Git 以外）は `App` に注入する。テストは `IssueProvider` を Fake に差し替え、エージェントは設定の `[agents.commands]` で `python -c ...` に差し替える。
- Git は常に一時ディレクトリの実リポジトリでテストする。

### 正本と責務

| 対象 | 正本 | wtm での扱い |
|---|---|---|
| 目的・終了条件 | Gitea Issue | `issues` テーブルはキャッシュ。`status` で都度更新し、失敗時はキャッシュ表示＋警告 |
| worktree の実状態 | `git worktree list --porcelain -z` | DB とずれたら警告のみ。自動で上書きしない |
| ローカル作業状態・割当 | SQLite | `worktrees`, `state_history`, `agent_runs` |
| AI 会話履歴 | 各エージェントのストレージ | `agent_sessions` は対応表とバックアップ先のみ（Phase 4） |
| 経過・引き継ぎ | Checkpoint（JSON ファイル＋DB） | `management/checkpoints/issue-N/*.json` |

## 3. データモデル（schema_version = 1）

```text
issues(number PK, title, body, state, url, source['gitea'|'manual'], fetched_at)

worktrees(id PK, issue_number, name, path, path_key, branch, base_ref,
          state[PLANNED|WORKING|BLOCKED|REVIEW|DONE|ARCHIVED],
          provision['creating'|'ready'], origin['start'|'adopt'], agent, task_file,
          created_at, updated_at, last_checked_at, archived_at)
  UNIQUE(issue_number) WHERE state <> 'ARCHIVED'   -- v1 の 1 Issue = 1 worktree
  UNIQUE(path_key)     WHERE state <> 'ARCHIVED'

state_history(id, worktree_id → worktrees, from_state, to_state, reason, changed_at)

agent_runs(id, worktree_id, agent, pid, session_id,
           status['running'|'exited'|'failed'|'unknown'], exit_code, started_at, ended_at)
  -- AI プロセス状態。作業ライフサイクル(state)とは独立

agent_sessions(id, worktree_id, agent, session_id, title, created_at, updated_at,
               source_path, backup_path, backed_up_at, UNIQUE(worktree_id, agent, session_id))

checkpoints(id, worktree_id, issue_number, created_at, worktree_path, head, diffstat,
            uncommitted_json, completed, remaining, next_action, blockers,
            session_id, file_path, published_at)

meta(key PK, value)   -- schema_version
```

- Issue と worktree は 1 対多を表現できる（`worktrees.issue_number` は非一意列）。v1 の 1 対 1 は部分一意インデックスで強制し、将来インデックスを外すだけで拡張できる。
- パスは正規化済み絶対パスを `path` に、大文字小文字差を吸収したキーを `path_key` に保存する。
- 時刻はすべて UTC の ISO 8601。

## 4. コマンドと入出力

| コマンド | Phase | 主な入力 | 出力 |
|---|---|---|---|
| `wtm init` | 1 | `--base-branch --worktree-root --management-dir --remote --gitea-login --no-gitea --yes --dry-run` | 設定パネル。config.toml / wt.db / git config `wt.config` を作成 |
| `wtm start` | 1 | `--issue N [--agent A] [--no-launch] [--offline --title T] [--slug S] [--base B] [--allow-closed] [--dry-run]` | 作成計画パネル、worktree・TASK.md。既定でエージェント起動 |
| `wtm list` | 1, 2, 5 | `[--stale] [--json] [--wide] [--offline]`、`--archived [--json]` | Rich 表 / JSON（`schema_version: 1`）。Phase 2 で PR 列 |
| `wtm status N` | 1 | `[--offline] [--json]` | Issue・作業状態・Git・セッション・Checkpoint・次アクション |
| `wtm adopt PATH` | 1 | `--issue N [--offline --title T] [--dry-run]` | 取り込み結果・初回 Checkpoint |
| `wtm doctor` | 1 | `[--fix] [--json]` | 診断表。error があれば exit 1 |
| `wtm start --new` | 2 | `[--title T] [--body B \| --body-file F] [--yes]` ＋ start のオプション | 確認後に Issue 作成 → worktree 作成 |
| `wtm finish N` | 2 | `[--push] [--create-pr] [--no-pr] [--title T] [--draft] [--allow-dirty] [--skip-checkpoint] [--yes] [--dry-run]` | 検査結果パネル、push・PR 作成（指定/確認時のみ）、REVIEW へ遷移 |
| `wtm checkpoint N` | 2 | `[--completed --remaining --next --blockers] [--publish] [--yes]` | Checkpoint（JSON + DB）、`--publish` で Issue コメント |
| `wtm checkpoint N --from-file F` | 3 | AI が生成した Checkpoint ファイル（Markdown / JSON） | 取り込み（元ファイルは変更しない） |
| `wtm checkpoints N` | 3 | `[--show ID] [--limit N] [--json]` | Checkpoint 履歴の一覧（新しい順）・詳細表示。整理済みの作業も参照可 |
| `wtm review N` | 3 | `[--format text\|md\|json] [--output F] [--offline]` | ルールベースレポート（`schema_version: 1`） |
| `wtm resume N` | 3 | `[--agent A] [--session ID] [--new-session] [--offline] [--yes] [--dry-run]` | エージェント起動。再開に失敗したら確認のうえ新規セッション |
| `wtm handoff N` | 3 | `[--offline] [--stdout]` | `.wtm/handoff.md` の生成 |
| `wtm state N [STATE]` | 3 | `[--reason R] [--yes] [--dry-run] [--allow-dirty] [--skip-checkpoint] [--offline] [--json]` | 状態と履歴の表示、または WORKING / BLOCKED / DONE への変更 |
| `wtm history N` | 4 | `[--agent A] [--json] [--backup]`、`--session ID [--format md\|json] [--tools] [--export F] [--from-backup]` | セッション一覧・閲覧・出力・バックアップ（`schema_version: 1`） |
| `wtm cleanup [N]` | 5 | `[--dry-run] [--yes] [--delete-ignored] [--json]` | N なし: 候補の一覧のみ。N あり: 検査結果、確認後に削除して ARCHIVED へ（`--force` なし。`schema_version: 1`） |

### `wtm list --json` のスキーマ（v1）

```json
{
  "schema_version": 1,
  "generated_at": "ISO8601",
  "repository": "絶対パス",
  "stale_days": 7,
  "worktrees": [{
    "issue": 101, "title": "...", "issue_state": "open|closed|unknown|null",
    "path": "...", "branch": "...", "agent": "claude|null",
    "state": "PLANNED", "provision": "ready|creating",
    "exists": true, "locked": false,
    "dirty": false, "untracked": false,
    "last_commit_at": "ISO8601|null", "last_checkpoint_at": "ISO8601|null",
    "pr_state": "open|draft|merged|closed|null",
    "pr": {"number": 8, "title": "...", "status": "open", "url": "...", "head": "...", "base": "..."},
    "created_at": "...", "last_checked_at": "...", "days_since_checked": 0, "stale": false,
    "warnings": []
  }],
  "unmanaged": [{"path": "...", "branch": "...", "locked": false, "prunable": false}],
  "warnings": []
}
```

フィールドの追加は後方互換とし、削除・意味変更時は `schema_version` を上げる。

## 5. 安全性の方針

- subprocess は `shell=False`・引数配列。Git には `GIT_TERMINAL_PROMPT=0` を渡し認証プロンプトでハングしないようにする。
- 既存ファイルは `open(..., "x")` で作成し、上書きしない（config.toml、TASK.md、Checkpoint）。
- worktree パスは保存先ディレクトリ配下であることを検証。adopt は「worktree ルートであること」「Git 共通ディレクトリが一致すること」「`git worktree list` に存在すること」「main worktree でないこと」を検証。
- 既存ブランチは再利用のみでリセットしない。他 worktree で使用中なら拒否。
- worktree 削除は `git worktree remove`（`--force` なし）のみ。start 失敗時のロールバックと `wtm cleanup` で使用する。
- 認証トークンは扱わない（tea の設定に委ねる）。
- worktree メタデータに触れる Git 操作（worktree list/add/remove、fetch、push、branch -d）は `<git-common-dir>/wt.lock` で直列化する（スレッド間・プロセス間、同一スレッドは再入可）。Git は作成途中の `.git/worktrees/<name>/` を別プロセスが読むと失敗するため。

## 6. Gitea 連携（Phase 2）

| 操作 | tea コマンド | 備考 |
|---|---|---|
| Issue 取得 | `tea api /repos/{owner}/{repo}/issues/N` | |
| Issue 作成 | `tea api -X POST -d @- /repos/{owner}/{repo}/issues` | `tea issues create` は作成番号を機械可読に返さないため |
| PR 検索 | `tea api /repos/{owner}/{repo}/pulls?state=all&page=N&limit=50` | head ブランチ名で照合。open を優先、無ければマージ済み |
| PR 作成 | `tea pulls create --head --base --title --description-file - [--draft]` | 仕様どおり tea pulls create を使用。作成後に PR 検索で番号を特定 |
| コメント投稿 | `tea comments add --description BODY N` | |

- すべて `--login`（設定時）と `--repo owner/name`（`[gitea].repo` またはリモート URL から算出）を付ける。
- 書き込み操作（Issue 作成・コメント投稿・push・PR 作成）は、CLI で内容を表示して確認するか、`--yes` / `--push` / `--create-pr` の明示指定がある場合のみ実行する。`--dry-run` では書き込まない。
- Checkpoint のコメントには完了/未完了/次アクション/ブロッカー、HEAD、未コミットファイル名、差分統計のみを含め、AI セッションの内容や ID は含めない。
- `wtm finish` の中断条件: 作業状態が DONE/ARCHIVED、AI が動作中または状態不明、未コミット変更（`--allow-dirty` で警告に格下げ。コンフリクトは不可）、Checkpoint なし（`--skip-checkpoint` で警告に格下げ）。

## 7. AI エージェント連携（Phase 3）

| 操作 | Claude Code | Codex |
|---|---|---|
| 新規起動 | `claude --session-id <uuid> --append-system-prompt-file <handoff>` | `codex`（オプションなし） |
| 再開 | `claude --resume <id> --append-system-prompt-file <handoff>` | 非対応 |
| 指示の受け渡し | 自動 | 利用者が `.wtm/handoff.md` を読むよう伝える |

- `.wtm/handoff.md` は起動のたびに置き換える。先頭に wtm の目印が無いファイルは上書きせず、起動を中止する。
- Issue 本文と Checkpoint は参考情報として明示し、ユーザーの指示を優先するよう共通指示に記す。
- セッションの対応は `agent_sessions` と `agent_runs.session_id` に記録する。新規セッションは起動直後に、再開したセッションは正常に再開できた場合だけ記録し、再開に失敗したセッションを次回の候補にしない。
- `wtm review` は読み取りのみ（最終確認日と Issue キャッシュの更新を除く）。fetch は行わないため、基準との差はローカルの参照に基づく。

## 8. セッション履歴（Phase 4）

```text
<管理ディレクトリ>/sessions/issue-<N>/claude/
├── <セッション ID>.jsonl                元ファイルのコピー
├── <セッション ID>.meta.json            SHA-256、サイズ、元のパス、更新時刻
├── <セッション ID>/                      付随ファイル（存在する場合）
└── <セッション ID>.<日時>.jsonl.bak      追記以外で変わる前のコピー
```

- 同期のタイミング: `wtm history`、`wtm resume`、`wtm adopt`、エージェントの終了後。全プロジェクトのファイルの先頭だけを読み、対象 Issue の worktree に該当するものだけ全体を読む。
- 対応付け: 記録済みの ID、または `cwd` が worktree 内にあるセッション。ARCHIVED の worktree については、整理後に始まったセッションを対象外にする。
- 同じパスに worktree を作り直すと、以前のセッションを新しい worktree にも対応付け、バックアップの情報も引き継ぐ。
- 閲覧できるのはその Issue に対応付いたセッションだけ。`--session` で他の Issue のファイルは開けない。
- サブエージェントの会話（`isSidechain`）、メタ情報（`isMeta`）、思考内容は表示と出力の対象外。バックアップには含まれる。
- Phase 5 の `cleanup` は、`collect_history` の `backup_status` で保全の有無を確認する。

## 9. worktree の整理（Phase 5）

```text
<管理ディレクトリ>/archive/issue-<N>/<日時>/
├── summary.json   Issue / PR / HEAD / コミット / 状態の履歴 / Checkpoint / セッション / 復元コマンド
└── files/         TASK.md と .wtm/ のコピー（リンクは対象外）
```

- 分類: `ready`（削除可能）、`blocked`（候補だが要対応）、`review`（Issue や PR が閉じている、または確認できない）、`stale`（放置）、`active`（作業中）。
- 検査の結果: `blocked` は削除しない。`confirm` は確認の画面で強調する。`info` は表示のみ。
- 確認できない項目（Gitea の接続失敗、PID の生存不明、コミット数の取得失敗）は `blocked` にする。
- `git fetch` と `summary.json` の作成は実行時だけ行う。`--dry-run` と一覧は Git と worktree を変更しない（セッションの対応表と Issue のキャッシュは更新する）。一覧は最終確認日を更新しない。
- 再検査: 削除の直前にリポジトリのロックを取り、作業状態、HEAD、変更、無視ファイル、AI の動作を確認し直す。検査の後に変わっていれば中断する。
- `worktree が存在しない` 場合: Git の操作はせず記録だけを ARCHIVED にする。Git の登録が残っていれば `git worktree prune` を案内する。ロックされていれば拒否する（リムーバブルドライブの可能性があるため）。Git が認識しないディレクトリに中身があれば、触らずに拒否する。
- ARCHIVED の行は削除しない。同じ Issue を `wtm start` で作り直すと新しい行を作り、以前のセッションも対応付く。
