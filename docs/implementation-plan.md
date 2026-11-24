# wtm 実装計画と進捗

最終更新: 2026-11-05

## 検証環境

| ツール | バージョン | 備考 |
|---|---|---|
| Python | 3.14.0 | 要件は 3.12 以上 |
| git | 2.50.1.windows.1 | |
| tea | 0.16.0 | Gitea 1.26.1（gitea.local）で読み取り操作を確認 |
| Claude Code | 2.1.311〜2.1.318 | `--resume <id>`, `--session-id` を `--help` で確認。`--append-system-prompt-file` は `claude -p` で動作確認（`--help` に個別の説明なし） |
| Codex CLI | 未導入 | オプション未検証 |
| uv | 未導入 | venv + pip で開発・テスト |

## Phase 1: Git worktree 管理 — 完了

完了条件: init / list / status / start（Issue 番号指定）/ adopt / doctor が実 Git リポジトリで動作し、pytest が通ること。

- [x] `wtm init`（Git 共通ディレクトリ検出、tea・ログイン確認、基準ブランチ選択、DB・設定作成、`git config wt.config`）
- [x] `wtm start --issue`（`tea api` による Issue 取得、`--offline --title`、fetch、既存ブランチ再利用、TASK.md、info/exclude、ロールバック）
- [x] `wtm start` からのエージェント起動（cwd=worktree、agent_runs 記録、重複起動検知）
- [x] `wtm list`（`--stale`, `--json`, `--wide`、管理外 worktree・不整合の警告）
- [x] `wtm status`（Issue キャッシュフォールバック、Git 状態、ahead/behind、最終確認日の更新）
- [x] `wtm adopt`（検証・初回 Checkpoint 作成）
- [x] `wtm doctor`（`--fix` は中断予約と停止済み agent_runs の更新のみ）
- [x] SQLite スキーマ v1、WAL、BEGIN IMMEDIATE、部分一意インデックス
- [x] テスト（§10 の 1〜9, 14〜16 に相当）

## Phase 2: Gitea 連携 — 実装完了（実 Gitea への書き込みは未検証）

完了条件: Issue 作成・PR 取得/作成・Checkpoint のコメント投稿・finish がモック上のテストで動作し、読み取り操作が実 Gitea で動作すること。

- [x] PR 取得（`tea api /repos/{owner}/{repo}/pulls?state=all` をページング、head ブランチで照合）
- [x] `wtm list` / `wtm status` の PR 表示（`pr_state` + `pr` オブジェクト。`wtm list --offline` で省略。取得失敗時は警告 1 件で以降の問い合わせを省略）
- [x] `wtm start --new`（タイトル・本文を対話/オプションで入力、作成前に確認。`tea issues create` は番号を返さないため `tea api -X POST -d @-` を使用）
- [x] `wtm checkpoint N [--publish]`（手入力・メタデータ取得、`tea comments add` で投稿。投稿前に本文を表示して確認。AI セッションの内容・ID は投稿しない）
- [x] `wtm finish N`（作業状態・AI 実行中・未コミット・Checkpoint の検査、fetch、既存 PR 検索、`--push`/`--create-pr` または対話確認、`tea pulls create --description-file -`、REVIEW 遷移）
- [x] Git 操作のプロセス間ロック（`<git-common-dir>/wt.lock`。並列 `wtm start` で Git の worktree 管理ファイル読み取り競合が起きるため）
- [x] 実 Gitea での読み取り確認（Issue 取得・PR 検索: 2026-10-30, Gitea 1.26.1）
- [x] 実 Gitea での書き込み確認（2026-10-30, ユーザー許可のうえ kzogura/wt-test で実施）
  - `start --new` → Issue #4 作成、`checkpoint --publish` → コメント 1 件、`finish --push --create-pr` → push と PR #5（`wt-4-p2check → main`, 本文 `Closes #4`）、状態 REVIEW
- [ ] （改善候補）Checkpoint コメントの日時を UTC ではなくローカル時刻で表示

## Phase 3: AI エージェント連携 — 実装完了（対話セッションでの実機確認は未実施）

完了条件: 引き継ぎ情報の生成と受け渡し、Checkpoint ファイルの取り込み、review、resume がモックのエージェントを使ったテストで動作し、Claude CLI の使用オプションが実機で動作すること。

- [x] 共通指示テンプレートと `.wtm/handoff.md`（既存 CLAUDE.md / AGENTS.md は変更しない。`agent-instructions.md` で差し替え可）
- [x] Claude Code 起動（`--session-id` で ID を割り当てて記録、`--append-system-prompt-file` で指示を渡す）
- [x] Codex 起動（オプションなし。指示は利用者が伝える）
- [x] `wtm handoff`
- [x] `wtm checkpoint --from-file`（Markdown / JSON）、`auto_checkpoint`
- [x] `wtm review`（ルールベース。text / Markdown / JSON 出力）
- [x] `wtm resume`（worktree 存在確認、削除済みなら復元方法の提示のみ、再開失敗時は確認のうえ新規セッションで継続）
- [x] `wtm status` の復元案内を修正（`wtm start` は登録済みで失敗するため、`git worktree add` を案内）
- [x] Claude CLI のオプション確認（2026-11-05, Claude Code 2.1.311, `claude -p` で実施）
  - `--session-id` + `--append-system-prompt-file`、`--resume` + `--append-system-prompt-file` が動作
  - 不明なセッション ID の `--resume` は exit 1、使用済み ID の `--session-id` は exit 1
- [x] 実 Claude CLI との通し確認（2026-11-05, 一時リポジトリ、`[agents.commands]` に `claude -p` を設定）
  - `wtm resume` で新規起動 → handoff の内容で Issue 番号に回答、2 回目は同じセッションを再開、`git status` はクリーン
- [ ] 対話セッションでの実機確認（`wtm start` / `wtm resume` から Claude Code を起動。特に、再開失敗時に対話モードでも非 0 で終了するか）
- [x] `wtm state`（BLOCKED / DONE への手動遷移と差し戻し。仕様に明示が無いため利用者と合意して追加）
- [ ] Codex CLI 導入後のオプション確認と Capability 更新

## Phase 4: 履歴管理 — 実装完了

完了条件: Claude セッションの検出・バックアップ・閲覧がテスト用の保存先で動作し、worktree と元ファイルの削除後もバックアップから閲覧できること。実際の Claude CLI のセッションでも動作すること。

- [x] `ClaudeSessionStore` 互換性レイヤー（読み取り専用の解析、バージョン判定、破損行の許容、未対応形式は明示エラー）
  - パスの変換規則は使わず、各ファイルの `cwd` で対応付ける
- [x] セッション ID と worktree の対応記録、`wtm` を通さず起動したセッションの検出、worktree 再作成時の対応復元
- [x] バックアップ（`management/sessions/` へコピーして検証、元ファイルは変更しない。エージェント終了時に自動実行）
- [x] `wtm history`（一覧、`--agent`、`--json`、Markdown/JSON エクスポート、バックアップからの閲覧）
- [x] `wtm adopt` での既存セッションの検索、`wtm doctor` の保存先チェック、`AgentAdapter` の一覧・取得・出力
- [x] 実 Claude CLI との通し確認（2026-11-05, 2.1.318, 一時リポジトリ、`claude -p`）
  - エージェント終了時の自動バックアップ、元ファイルが変わらないこと、worktree と元ファイルの削除後にバックアップから閲覧できることを確認
- [ ] 対話セッションで作られた履歴の表示確認（`claude -p` で作ったセッションでのみ確認済み）
- [ ] Codex のセッション取得（Codex CLI 導入後に確認）

## Phase 5: 安全な後片付け — 実装完了

完了条件: 必須チェックのいずれかに該当すると削除を拒否し、削除後も履歴を閲覧できることが、実 Git リポジトリのテストで確認できること。PR の判定が実 Gitea の応答で動作すること。

- [x] `wtm cleanup [N] [--dry-run] [--yes] [--delete-ignored] [--json]`（未コミット・未追跡・ロック・AI 実行中・Checkpoint・セッションバックアップ・Issue/PR 状態の全チェック、`git worktree remove` のみ、`--force` なし）
  - `.wtm/` と TASK.md はアーカイブに保存する。`.wtm/checkpoint.*` が未取り込みなら拒否して取り込みを促す
  - 無視対象のその他のファイル（`.env` など）は一覧を表示して確認を求める（仕様に明示は無いが、Git が確認なしで削除するため追加）
  - 削除候補は PR マージ済みまたは DONE。DONE でも安全チェックは省略しない。マージも push もされていないコミットがあれば止める
- [x] 放置 worktree 検出、PR マージ済みとの照合（`wtm cleanup` の一覧。`wtm list --stale` も利用可）
- [x] wtm の外で起動した Claude Code の検出（プロセス記録と、セッションの更新時刻）
- [x] ARCHIVED 遷移とアーカイブ、`wtm list --archived`
- [x] テスト §10 の 10〜13（`tests/test_phase5.py`）
- [x] 実機確認（2026-11-05）: 実 Gitea の PR #5 を読み取る `--dry-run`（一時クローン）、`wtm.exe` による削除までの通し（`--no-gitea` のローカルリポジトリ）
- [ ] 最終コードでの全テストの実行（2026-11-05 はメモリ不足で実行が停止され、未完了）
- [ ] 実 Gitea に接続した環境での削除の確認（`--dry-run` まで確認済み）
- [ ] Codex の実行中検出（Codex CLI 導入後に確認。現在は使用記録がある worktree で確認を求めるのみ）

## 実装上の制約・未確認事項

- `tea api` のレスポンスは Gitea REST API の Issue オブジェクト（`number,title,body,state,html_url,pull_request`）を前提にしている。2026-10-30 に実 Gitea（kzogura/wt-test）で Issue 取得を確認済み。
- tea の `{owner}/{repo}` 置換はログインの SSH ホストとリモート URL が一致しないと空になる（`GetUserByName` エラー）。wtm は `[gitea].repo`、未設定ならリモート URL から求めた owner/name を常に `--repo` で渡す。
- Claude Code のセッション形式は 2.1.309〜2.1.318 の実ファイルで確認した。非公開形式のため、更新で読めなくなる可能性がある（`claude_store.py` のみ修正すればよい構成）。
- `info/exclude` は全 worktree 共通のため、main worktree の `TASK.md` も無視対象になる。
- wtm 外で起動した Claude Code は `sessions/<pid>.json`（非公開形式。2.1.318 で確認）とセッションの更新時刻で検出する。起動時の作業ディレクトリが worktree の外にある場合と、Codex は検出できない。
- `wtm start` は既定でエージェントを前面起動し、終了まで待機する。
- PR 本文は `Closes #<Issue>` で始まるため、PR マージ時に Gitea が Issue を自動クローズする。
- `wtm finish` の push は `git push -u <remote> <branch>`（--force なし）。SSH パスフレーズ入力が必要な環境では ssh-agent を使うこと（出力をキャプチャするため対話入力できない）。
- `tea pulls create` の出力形式は未確認のため、作成後に PR 検索で番号を特定している。
