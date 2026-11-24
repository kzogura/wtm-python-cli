# Python CLI「wt」開発指示書

## 1. プロジェクト概要

Git worktreeを利用し、Claude Code、Codexなどのコーディングエージェントを複数並列実行するための、個人向けCLIツール `wt` をPythonで開発する。

単なるGit worktreeのラッパーではなく、**Gitea Issueを中心としたAI開発タスク管理ツール**とする。

### 解決したい課題

1. worktree作成時には作業目的が明確だが、数日後に目的が曖昧になる。
2. 複数のAIエージェントを並列実行すると、各作業の進捗・終了条件が分からなくなる。
3. AIが本来の目的を超えて作業を拡大してしまう。
4. Claude Codeのセッション履歴がworktreeごとに管理されるため、worktree削除後の履歴参照が不便になる。
5. 作業終了後もworktreeが残り続け、整理が難しくなる。
6. worktree間で作業対象が重複し、マージ時に競合する。

## 2. 技術要件

- Python 3.12以上
- Windows 11 PowerShell、macOS、Linux対応
- CLIフレームワーク：Typer
- ターミナル表示：Rich
- 設定ファイル：TOML
- ローカル管理DB：SQLite
- パッケージ管理：uvを第一候補とする
- テスト：pytest
- Git操作：Git CLIをsubprocess経由で呼び出す
- Gitea操作：原則 `tea` CLIを利用する
- Claude Code：Claude CLIを利用する
- Codex：Codex CLIを利用する
- v1ではWeb UIを作らない
- 外部LLM APIキーを必須としない
- 主要機能はオフラインでも可能な範囲で利用できるようにする

subprocessは `shell=False` を原則とし、引数を配列で渡す。

Gitやteaのコマンド実行では、標準出力、標準エラー、終了コードを取得し、適切なエラーハンドリングを実装する。

## 3. 設計原則

### 3.1 管理の正本

以下の責務を分離する。

| 対象 | 正本 |
|---|---|
| 作業目的・終了条件 | Gitea Issue |
| ソースコードと変更履歴 | Git |
| worktreeの実際の状態 | Git worktree |
| ローカル作業状態・エージェント割当 | SQLite |
| AI会話履歴 | 各AIエージェントのセッションストレージ |
| 作業経過・引き継ぎ | ローカルCheckpointおよびGitea Issueコメント |

情報の二重管理を最小限にする。

Git、Gitea、ローカルDBの状態が食い違った場合、すぐに上書きせず、状態を調査して警告する。

### 3.2 worktreeの管理単位

原則：

- 1 Issue = 1作業目的
- 1 Issue = 1管理対象worktree
- 1 worktree = 1作業ブランチ
- 1 worktreeを同時に編集するAIエージェントは1つ
- 1 worktree内で複数のAIセッションを順次使用することは認める

将来の拡張を考え、Issueとworktreeの対応は内部データモデルでは1対多を許容できる設計にする。ただしv1では通常1対1として扱う。

### 3.3 作業のライフサイクル

以下の状態を管理する。

- PLANNED：作業準備済み
- WORKING：作業中
- BLOCKED：作業停止・依存待ち
- REVIEW：成果確認中
- DONE：作業完了
- ARCHIVED：worktree整理済み

状態変更履歴も保持する。

AIプロセスの起動状態と、作業ライフサイクルの状態は別々に管理する。

## 4. ディレクトリ構成

以下を標準とする。

```text
workspace/
├── main/
│   └── Git repository
│
├── worktrees/
│   ├── issue-101/
│   ├── issue-102/
│   └── issue-103/
│
└── management/
    ├── config.toml
    ├── wt.db
    ├── checkpoints/
    ├── sessions/
    └── logs/
```

既存のディレクトリ構成も利用できるようにする。

worktreeのパスは絶対パスとしてDBに記録する。外部ディレクトリへの誤った操作を防ぐため、パスの正規化とGitの管理情報による検証を行う。

### config.toml

```toml
[repository]
path = "C:/work/myproject/main"
remote = "origin"
base_branch = "develop"
worktree_root = "C:/work/myproject/worktrees"

[gitea]
enabled = true
cli = "tea"
login = "local-gitea"

[agents]
default = "claude"

[management]
stale_days = 7
require_issue = true
auto_checkpoint = false

[cleanup]
require_confirmation = true
require_clean_worktree = true
require_session_backup = true
```

設定ファイルのパスはOSに依存しない形式で扱う。

認証トークンやパスワードを設定ファイルやログに保存しない。

## 5. コマンド仕様

### 5.1 wt init

管理対象リポジトリを登録する。

```bash
wt init
```

処理：

1. Gitリポジトリを検出する。
2. Gitの共通ディレクトリを検出する。
3. Giteaの接続先を確認する。
4. `tea`の利用可否を確認する。
5. 基準ブランチを選択する。
6. worktree保存先を設定する。
7. 管理用DBと設定ファイルを作成する。

初期化時に既存のworktreeを削除・変更しない。

### 5.2 wt start

Gitea Issueに対応するworktreeを作成する。

```bash
wt start --issue 101 --agent claude
wt start --issue 102 --agent codex
wt start --issue 103 --no-launch
```

処理：

1. `tea`でIssue情報を取得する。
2. Issueの状態を確認する。
3. ローカルDBに登録済みか確認する。
4. Git fetchを行う。
5. 指定された基準ブランチからworktreeを作成する。
6. `TASK.md`を生成する。
7. DBに作業情報を登録する。
8. 必要ならClaude CodeまたはCodexを起動する。

ブランチ名の標準形式：

`feature/<issue番号>-<短い識別子>`

worktree名：

`issue-<issue番号>`

ブランチ名が既に存在する場合や他のworktreeで使用中の場合は、状況を説明して安全に処理する。既存ブランチを強制的にリセットしない。

Issue未作成の場合、`wt start --new`で対話的にIssueを作成できるようにする。

### 5.3 wt list

管理対象worktreeを一覧表示する。

```bash
wt list
wt list --stale
wt list --json
```

表示する情報：

- Issue番号
- Issueタイトル
- worktreeパス
- ブランチ
- 担当AI
- 作業状態
- 最終コミット日時
- 最終Checkpoint日時
- 未コミット変更の有無
- PR状態
- 最終確認日数

Gitのworktree一覧取得には以下を使用する。

```bash
git worktree list --porcelain -z
```

Richで読みやすい一覧を表示する。

7日以上確認されていないworktreeを強調表示する。

JSON出力は他ツールで利用しやすい安定したスキーマにする。

### 5.4 wt status

対象Issueの状態を表示する。

```bash
wt status 101
```

表示内容：

- Issueの目的と終了条件
- ローカルの作業状態
- Gitの変更状況
- PRの状態
- 関連AIセッション一覧
- 最終Checkpoint
- 次のアクション
- 作業開始日と最終確認日

### 5.5 wt checkpoint

現在の作業状態を記録する。

```bash
wt checkpoint 101
wt checkpoint 101 --publish
```

Checkpointには以下を保存する。

- 日時
- Issue番号
- worktreeパス
- HEADコミット
- Git差分の統計情報
- 未コミットファイル一覧
- 完了事項
- 未完了事項
- 次のアクション
- ブロッカー
- AIセッションID（取得可能な場合）

CheckpointはGitコミットとは独立して保存する。

ファイルの変更やGitコミットを自動実行しない。

`--publish`指定時はGitea Issueに進捗をコメントする。

AIによる自動要約は将来の拡張とし、初期版では手入力・既存メタデータの取得・AIが生成したCheckpointファイルの取り込みを実装する。

### 5.6 wt review

作業目的と現在の状態を確認する。

```bash
wt review 101
```

処理：

1. Gitea Issueから目的と終了条件を取得する。
2. Gitのコミット、差分、ファイル一覧を取得する。
3. 最新Checkpointを取得する。
4. 未完了項目を表示する。
5. 作業の継続・分割・終了を判断するためのレポートを生成する。

初期版はルールベースで実装する。

例えば：

- Issueの終了条件が未完了
- 7日以上Checkpointなし
- 未コミット変更あり
- PR作成済み
- PRマージ済み
- ブランチが基準ブランチから大きく乖離
- 他のworktreeと変更対象ファイルが重複

AIに内容を分析させる場合のため、レビュー情報をMarkdownまたはJSONで出力できるようにする。

### 5.7 wt history

AIエージェントのセッション履歴を管理する。

```bash
wt history 101
wt history 101 --agent claude
wt history 101 --json
```

特に重要な機能である。

Claude Codeはworktreeごとのパスに紐づいてローカルセッション履歴を保存する場合がある。

worktree削除後も、過去の指示内容を振り返れるようにする。

要件：

1. Claude Codeのローカルセッション保存先を検出する。
2. 対象worktreeに関連するセッションを一覧化する。
3. セッションID、作成日時、最終更新日時、タイトルなど取得可能な情報を記録する。
4. セッション履歴のバックアップ機能を持たせる。
5. 元のworktreeを削除しても、バックアップから履歴を参照できるようにする。
6. セッションのエクスポート形式としてMarkdownとJSONを検討する。
7. 未対応のClaude Codeバージョンや保存形式は明示的にエラーにする。

**重要：Claude Codeの内部ストレージ形式は非公開・変更される可能性がある。**

公式のCLI機能を優先すること。

内部JSONLなどを読み取る場合は、互換性レイヤーを設け、バージョン差異や破損データに対応する。

セッションファイルを直接変更しない。

セッション情報には機密情報が含まれる可能性があるため、Giteaに会話全文を自動投稿しない。

初期版ではClaude Codeを優先し、Codexは取得可能な範囲で対応する。

### 5.8 wt resume

作業を再開する。

```bash
wt resume 101
wt resume 101 --agent claude
wt resume 101 --session <session-id>
```

処理：

1. 対象worktreeが存在するか確認する。
2. Gitea Issueの状態を取得する。
3. 最新Checkpointを取得する。
4. 関連セッションを確認する。
5. 指定したAIエージェントを適切なworktreeで起動する。

可能であればClaude Codeの既存セッションを再開する。

セッション再開に失敗した場合は、IssueとCheckpointを使用して新規セッションで作業を継続できるようにする。

worktreeが既に削除されている場合は、勝手に再作成せず、復元方法を提示する。

### 5.9 wt finish

作業をレビュー・終了状態へ移行する。

```bash
wt finish 101
```

処理：

1. 作業状態を確認する。
2. 未コミット変更を検査する。
3. Checkpointがあるか確認する。
4. 既存PRを検索する。
5. 必要ならPR作成を支援する。
6. 作業状態をREVIEWに更新する。

PRの作成には`tea pulls create`を利用する。

GitのpushやPR作成は明示的な確認またはオプションによって実行する。

AIが実行中の場合は終了処理を中断する。

### 5.10 wt cleanup

不要になったworktreeを整理する。

```bash
wt cleanup --dry-run
wt cleanup 101
```

削除前の必須チェック：

- 未コミット変更がない
- 未追跡ファイルがない
- Git worktreeがロックされていない
- 対象worktreeでAIプロセスが動作していない
- Checkpointが保存されている
- 必要なセッション履歴がバックアップ済み
- Issue、PRの状態を確認済み

原則としてPRマージ済みのworktreeを削除候補にする。

削除は以下を使用する。

```bash
git worktree remove <path>
```

`--force`はv1では実装しない。

削除後も、Gitea Issue、Checkpoint、セッションメタデータ、バックアップは残す。

Gitブランチの削除はworktree削除とは独立した操作にする。

### 5.11 wt adopt

既存のworktreeを管理対象に取り込む。

```bash
wt adopt C:/work/myproject/worktrees/existing --issue 101
```

処理：

1. 既存worktreeを検証する。
2. Gitの共通ディレクトリが管理対象リポジトリと一致するか確認する。
3. Issue番号との対応を登録する。
4. 既存のClaudeセッションを検索する。
5. 初回Checkpointを作成する。

既存ファイル、ブランチ、Git履歴を変更しない。

### 5.12 wt doctor

実行環境を診断する。

```bash
wt doctor
```

確認対象：

- Pythonバージョン
- Gitコマンド
- teaコマンド
- Gitea接続
- Claude CLI
- Codex CLI
- Gitリポジトリ
- worktreeメタデータ
- 管理DB整合性
- セッション履歴の参照可否

不足している機能があっても、利用可能な機能は動作させる。

## 6. AIエージェント連携

AgentAdapterという抽象インターフェースを設ける。

想定する主な操作：

```python
class AgentAdapter:
    def launch(self, cwd, session_id=None): ...
    def list_sessions(self, project_path): ...
    def get_session(self, session_id): ...
    def export_session(self, session_id, dest): ...
    def resume(self, cwd, session_id): ...
```

Claude Code、CodexをそれぞれAdapterとして実装する。

ただし、共通インターフェースのすべてを各エージェントが実現できるとは限らない。

サポート状況をCapabilityとして宣言し、対応していない操作は適切なメッセージを返す。

AIプロセスの起動時は必ずworktreeルートをカレントディレクトリにする。

エージェントに実行させるコマンドは設定で制御可能にする。

## 7. TASK.mdとAIへの引き継ぎ

worktree開始時に以下のテンプレートを生成する。

```markdown
# 作業管理

## Gitea Issue
#101

## Goal
Issueの目的

## Scope
作業対象

## Out of Scope
対象外

## Definition of Done
完了条件

## Current Status
現在の状態

## Completed
完了事項

## Remaining
残作業

## Next Action
次に実施すること

## Notes
判断履歴
```

ローカル専用のTASK.mdは自動コミットしない。

worktreeごとの専用管理ファイルをGitの追跡対象外にする場合、リポジトリ共有の`.gitignore`を書き換えず、worktree固有の管理ファイルとして扱える方式を検討する。

既存の同名ファイルを上書きしない。

Claude Codeに読み込ませるための共通指示テンプレートも用意する。

ただし、既存の`CLAUDE.md`や`AGENTS.md`を勝手に変更しない。

## 8. セッション履歴の保全

以下は必須要件とする。

- セッション履歴の保存先をworktree内に限定しない。
- 管理DBにworktreeとAIセッションIDの対応を記録する。
- worktree削除前にセッション履歴の保全を確認する。
- 元のセッションが参照できなくても、バックアップを閲覧できるようにする。
- worktree再作成時、過去のセッションとの対応を復元できるようにする。
- Claude Codeの内部仕様に強く依存する処理を他機能から分離する。

セッションの「閲覧」と「再開」は別機能とする。

バックアップからの閲覧を保証対象とし、Claude Code本体での再開は対応バージョンで可能な場合のみ提供する。

## 9. 並列実行・安全性

複数の`wt`プロセスが同時に操作されることを前提にする。

必須事項：

- SQLiteトランザクションの適切な利用
- 作業単位の排他制御
- 同じIssueのworktree重複作成防止
- 同じworktreeへのAI重複起動の検知
- 外部Git操作による状態変更の検知
- Git操作失敗時の整合性回復
- 全ての破壊的操作にdry-runを実装
- 既存ファイルの無断上書き禁止

AIプロセス検出はOS差異を考慮する。

プロセス識別が確実でない場合は「停止済み」と断定せず、状態不明として削除を保留する。

## 10. テスト

pytestによる自動テストを実装する。

最低限、以下をカバーする。

1. 空リポジトリでの初期化
2. worktreeの新規作成
3. 複数worktreeの並列作成
4. 同一Issueの重複登録防止
5. 既存worktreeの取り込み
6. Git差分・未追跡ファイル検出
7. Issue取得失敗
8. tea未インストール
9. AI CLI未インストール
10. 未コミット状態での削除拒否
11. ロックされたworktreeの削除拒否
12. セッションバックアップ失敗時の削除拒否
13. worktree削除後の履歴閲覧
14. SQLite同時更新
15. Windowsの日本語・空白を含むパス
16. Gitコマンド途中失敗時の復旧

Git操作は一時ディレクトリに作成した実際のテスト用リポジトリを利用する。

GiteaやAI CLIはモック可能な構造にする。

実環境のリポジトリやIssueを自動テストで変更しない。

## 11. 実装フェーズ

### Phase 1：Git worktree管理

実装：

- init
- list
- status
- start（Issue番号指定）
- adopt
- doctor
- SQLite管理

この段階で動作するCLIとして完成させる。

### Phase 2：Gitea連携

実装：

- teaによるIssue取得
- Issue新規作成
- PR取得・作成
- CheckpointのIssueコメント投稿
- finish

### Phase 3：AIエージェント連携

実装：

- Claude Code起動
- Codex起動
- worktreeごとの作業引き継ぎ
- checkpoint
- review
- resume

### Phase 4：履歴管理

実装：

- Claudeセッション検出
- セッションID管理
- セッションバックアップ
- history
- worktree削除後の履歴参照

### Phase 5：安全な後片付け

実装：

- cleanup
- 放置worktree検出
- PR状態と削除候補の照合
- 作業履歴アーカイブ

各Phaseごとにテストを実施する。

## 12. 開発進行上のルール

- 実装前にリポジトリと利用可能なCLIのバージョンを確認する。
- teaやClaude CLIのオプションを推測で実装しない。
- `--help`や公式ドキュメントで仕様を確認する。
- 不明な仕様は実装上の制約として記録する。
- 動作しないダミー実装で完了扱いにしない。
- README、設定例、テストを整備する。
- 各Phaseの完了条件を明確にする。
- 作業途中で追加アイデアが出ても、元のタスクを無制限に拡大しない。
- 未完了タスクは`docs/implementation-plan.md`に永続化する。
- 各Phase終了時に進捗と残作業を更新する。
- ユーザーの明示的な許可なく実環境のGitea Issueを作成・変更しない。
- ユーザーの明示的な許可なく実環境のworktreeを削除しない。

## 13. 最初に実施する作業

最初に以下を行うこと。

1. 要件を確認し、実装上の問題点や依存関係を洗い出す。
2. 全体アーキテクチャを設計する。
3. データモデルを定義する。
4. コマンド一覧と入出力を確定する。
5. `docs/architecture.md`を作成する。
6. `docs/implementation-plan.md`を作成する。
7. Phase 1の実装を開始する。
8. pytestで動作を検証する。
9. READMEに実際の使用方法を記載する。

Phase 1完了時には、実装済み機能・テスト結果・未完了事項を報告する。

**最終目標：Gitea Issueを起点としてworktreeを生成し、複数のAIを並列実行し、途中の目的・進捗・セッション履歴を失わず、作業完了時に安全に整理できるCLIを実現する。**
