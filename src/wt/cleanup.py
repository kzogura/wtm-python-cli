"""wtm cleanup: worktree の安全な整理と作業履歴のアーカイブ（Phase 5）。

- 削除は `git worktree remove <path>` のみ（--force は使わない）。
- 確認できない項目は「問題なし」と扱わず、削除を保留する。
- 削除後も Issue、Checkpoint、セッションの記録とバックアップ、ブランチは残す。
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import handoff as handoff_mod
from . import sessions as sessions_mod
from .claude_store import registered_processes
from .db import now_iso, parse_iso
from .errors import CommandFailedError, ConflictError, SafetyError, WtError
from .forge import PullRequest
from .gitops import WorktreeInfo, find_worktree
from .paths import is_within, normalize, path_key, same_path
from .proc import pid_alive
from .service import (
    App,
    base_ref_for,
    days_since,
    get_managed,
    lookup_pr,
    possibly_running_agents,
    restore_steps,
)

SCHEMA_VERSION = 1
# この時間内にセッションの記録が更新されていれば、AI が動作中の可能性があるとみなす
ACTIVE_SESSION_SECONDS = 300
MAX_ARCHIVED_COMMITS = 200
MAX_SHOWN_FILES = 10

OK, BLOCKED, CONFIRM, INFO = "ok", "blocked", "confirm", "info"

CHECK_WORKTREE = "worktree"
CHECK_CANDIDATE = "削除候補"
CHECK_PR = "PR"
CHECK_ISSUE = "Issue"
CHECK_CHANGES = "未コミット変更"
CHECK_UNTRACKED = "未追跡ファイル"
CHECK_IGNORED = "無視ファイル"
CHECK_LOCK = "ロック"
CHECK_AGENT = "AI プロセス"
CHECK_CHECKPOINT = "Checkpoint"
CHECK_SESSIONS = "セッション履歴"
CHECK_COMMITS = "コミット"


@dataclass
class CleanupCheck:
    name: str
    status: str  # ok | blocked | confirm | info
    message: str


@dataclass
class CleanupPlan:
    issue_number: int
    worktree_id: int
    title: str | None
    path: Path
    branch: str | None
    state: str
    mode: str  # remove: git worktree remove を実行 / missing: 既に存在しないので記録だけ更新
    head: str | None = None
    candidate: bool = False
    candidate_reason: str = ""
    stale: bool = False
    days_since_checked: int | None = None
    issue_state: str | None = None
    pr: PullRequest | None = None
    ignored: list[str] = field(default_factory=list)
    checks: list[CleanupCheck] = field(default_factory=list)
    hints: list[str] = field(default_factory=list)

    def add(self, name: str, status: str, message: str) -> None:
        self.checks.append(CleanupCheck(name, status, message))

    @property
    def blockers(self) -> list[CleanupCheck]:
        return [c for c in self.checks if c.status == BLOCKED]

    @property
    def confirmations(self) -> list[CleanupCheck]:
        return [c for c in self.checks if c.status == CONFIRM]

    @property
    def category(self) -> str:
        """ready: 削除可能 / blocked: 候補だが要対応 / review: 要確認 / stale: 放置 / active: 作業中。"""
        if self.candidate:
            return "blocked" if self.blockers else "ready"
        if self.issue_state == "closed" or (self.pr is not None and self.pr.status == "closed") or any(
                c.name in (CHECK_PR, CHECK_ISSUE) and c.status == BLOCKED for c in self.checks):
            return "review"
        return "stale" if self.stale else "active"

    def to_dict(self) -> dict[str, Any]:
        return {
            "issue": self.issue_number, "title": self.title, "path": str(self.path), "branch": self.branch,
            "state": self.state, "mode": self.mode, "head": self.head, "candidate": self.candidate,
            "candidate_reason": self.candidate_reason, "category": self.category, "stale": self.stale,
            "days_since_checked": self.days_since_checked, "issue_state": self.issue_state,
            "pr": self.pr.to_dict() if self.pr else None, "ignored_files": self.ignored,
            "checks": [c.__dict__ for c in self.checks], "hints": self.hints,
        }


# ---------------------------------------------------------------------------
# 検査
# ---------------------------------------------------------------------------
class _RemoteView:
    """Issue と PR の最新状態。接続できなければ、以降の問い合わせを省略する。"""

    def __init__(self, app: App) -> None:
        self.app = app
        self.error: str | None = None

    def lookup(self, row: sqlite3.Row, branch: str | None) -> tuple[str | None, PullRequest | None, str | None]:
        """(Issue の状態, PR, 取得できなかった理由)。"""
        app, number = self.app, row["issue_number"]
        with app.db.read() as c:
            cached = app.db.get_issue(c, number)
        cached_state = cached["state"] if cached else None
        if not app.config.forge.enabled:
            return cached_state, None, None
        if self.error:
            return cached_state, None, self.error
        pr, warning = lookup_pr(app, branch)
        if warning:
            self.error = warning
            return cached_state, None, warning
        try:
            issue = app.forge.get_issue(number)
        except WtError as e:
            if cached is not None and cached["source"] == "manual":  # 手動登録の番号は Issue トラッカーに無い場合がある
                return cached_state, pr, None
            self.error = f"Issue を取得できません: {e.message.splitlines()[0]}"
            return cached_state, pr, self.error
        with app.db.transaction() as c:
            app.db.upsert_issue(c, issue.number, issue.title, issue.body, issue.state, issue.url, issue.source)
        return issue.state, pr, None


def _is_empty_dir(path: Path) -> bool:
    try:
        return path.is_dir() and not any(path.iterdir())
    except OSError:
        return False


def _shown(files: list[str]) -> str:
    text = ", ".join(files[:MAX_SHOWN_FILES])
    return text + (f" ほか {len(files) - MAX_SHOWN_FILES} 件" if len(files) > MAX_SHOWN_FILES else "")


def _path_checks(app: App, plan: CleanupPlan, worktrees: list[WorktreeInfo]) -> None:
    """削除してはいけない場所を指していないことを確認する。"""
    path = plan.path
    if worktrees and same_path(worktrees[0].path, path):
        plan.add(CHECK_WORKTREE, BLOCKED, "main worktree は削除できません。")
    if is_within(app.config.management_dir, path):
        plan.add(CHECK_WORKTREE, BLOCKED, f"管理ディレクトリがこの worktree の中にあります: {app.config.management_dir}")
    if is_within(app.repo, path) and not same_path(app.repo, path):
        plan.add(CHECK_WORKTREE, BLOCKED, f"管理対象リポジトリがこの worktree の中にあります: {app.repo}")
    if is_within(Path.cwd(), path):
        plan.add(CHECK_WORKTREE, BLOCKED, "カレントディレクトリがこの worktree の中にあります。"
                                          "別のディレクトリ（main worktree など）で実行してください。")
    if plan.mode == "remove" and not is_within(path, app.config.repository.worktree_root):
        plan.add(CHECK_WORKTREE, CONFIRM, f"worktree 保存先の外にあります: {path}")


def _git_checks(app: App, plan: CleanupPlan, info: WorktreeInfo | None) -> None:
    """ローカルの Git の状態。実行直前にも再度確認する項目。"""
    cfg = app.config.cleanup
    if info is not None and info.locked:
        reason = f"（理由: {info.locked_reason}）" if info.locked_reason else ""
        plan.add(CHECK_LOCK, BLOCKED, f"worktree がロックされています{reason}。")
        plan.hints.append(f'ロックを解除するには: git worktree unlock "{plan.path}"')
    else:
        plan.add(CHECK_LOCK, OK, "ロックされていません。")
    if plan.mode != "remove":
        return
    try:
        st = app.git.status(plan.path)
    except WtError as e:
        plan.add(CHECK_CHANGES, BLOCKED, f"Git の状態を取得できません: {e.message.splitlines()[0]}")
        return
    if st.conflicted:
        plan.add(CHECK_CHANGES, BLOCKED, f"コンフリクト中のファイルがあります: {_shown(st.conflicted)}")
    changed = sorted(set(st.staged + st.modified))
    # Git 自身も未コミット変更と未追跡ファイルを拒否するため、設定で無効にしても削除はされない
    dirty_status = BLOCKED if cfg.require_clean_worktree else INFO
    if changed:
        plan.add(CHECK_CHANGES, dirty_status, f"未コミットの変更があります: {_shown(changed)}")
    elif not st.conflicted:
        plan.add(CHECK_CHANGES, OK, "ありません。")
    if st.untracked:
        plan.add(CHECK_UNTRACKED, dirty_status, f"未追跡ファイルがあります: {_shown(st.untracked)}")
    else:
        plan.add(CHECK_UNTRACKED, OK, "ありません。")

    ignored = app.git.ignored_files(plan.path)
    if ignored is None:
        plan.add(CHECK_IGNORED, BLOCKED, "無視対象のファイルを確認できません。")
        return
    # TASK.md と .wtm/ はアーカイブに保存する
    keep = {app.config.task.file_name, f"{handoff_mod.WTM_DIR}/"}
    plan.ignored = [f for f in ignored if f not in keep]
    if plan.ignored:
        plan.add(CHECK_IGNORED, CONFIRM, "Git の無視対象のファイルも一緒に削除されます（保存しません）: "
                                         + _shown(plan.ignored))
    else:
        plan.add(CHECK_IGNORED, OK, "TASK.md と .wtm/ のほかにありません（これらはアーカイブに保存します）。")


def _agent_checks(app: App, plan: CleanupPlan, row: sqlite3.Row, session_sources: list[str]) -> None:
    """AI の動作状況。確実に停止と判断できないものは保留する。"""
    problems = [f"{r['agent']} が動作中の可能性があります (pid={r['pid']})。"
                for r in possibly_running_agents(app, row["id"])]
    for proc in registered_processes():
        if is_within(proc.cwd, plan.path) and pid_alive(proc.pid) is not False:
            problems.append(f"Claude Code がこの worktree で動作中の可能性があります (pid={proc.pid})。")
    now = time.time()
    for source in session_sources:
        try:
            age = now - Path(source).stat().st_mtime
        except OSError:
            continue
        if age < ACTIVE_SESSION_SECONDS:
            problems.append(f"セッションの記録が {ACTIVE_SESSION_SECONDS // 60} 分以内に更新されています。"
                            "AI を終了している場合は、しばらく待ってから再実行してください。")
            break
    for message in problems:
        plan.add(CHECK_AGENT, BLOCKED, message)
    if not problems:
        plan.add(CHECK_AGENT, OK, "動作中の記録はありません。")


def _other_agents(app: App, row: sqlite3.Row) -> list[str]:
    with app.db.read() as c:
        rows = c.execute(
            "SELECT agent FROM agent_runs WHERE worktree_id=? UNION SELECT agent FROM agent_sessions WHERE worktree_id=?",
            (row["id"], row["id"])).fetchall()
    return sorted({r["agent"] for r in rows if r["agent"] != sessions_mod.AGENT})


def _session_checks(app: App, plan: CleanupPlan, row: sqlite3.Row) -> list[str]:
    """セッションの保全状況を確認し、元ファイルのパスを返す。バックアップ自体は実行時に作る。"""
    history = sessions_mod.collect_history(app, plan.issue_number)
    for warning in history["warnings"]:
        plan.add(CHECK_SESSIONS, INFO, warning)
    entries = [s for s in history["sessions"] if s["agent"] == sessions_mod.AGENT]
    pending = [s for s in entries if s["origin"] == "live" and s["backup_status"] != "current"]
    empty = [s for s in entries if s["origin"] == "missing"]
    if pending:
        plan.add(CHECK_SESSIONS, INFO, f"{len(pending)} 件は削除の直前にバックアップします。")
    elif entries and len(entries) > len(empty):
        plan.add(CHECK_SESSIONS, OK, f"{len(entries) - len(empty)} 件はバックアップ済みです。")
    if empty:
        plan.add(CHECK_SESSIONS, INFO, f"{len(empty)} 件は内容がどこにもないため保全できません（起動だけで終わった場合など）。")
    if not entries:
        plan.add(CHECK_SESSIONS, OK, "Claude のセッションはありません。")
    for agent in _other_agents(app, row):
        plan.add(CHECK_SESSIONS, CONFIRM, f"{agent} の履歴は wtm では保全できず、動作中かどうかも検出できません。"
                                          "停止していることと履歴の保存先を確認してください。")
    return [s["source_path"] for s in entries if s["source_path"]]


def _checkpoint_checks(app: App, plan: CleanupPlan, row: sqlite3.Row) -> None:
    with app.db.read() as c:
        cp = app.db.latest_checkpoint(c, row["id"])
    if plan.mode != "remove":
        plan.add(CHECK_CHECKPOINT, OK if cp else INFO, "保存済みです。" if cp else "ありません（worktree が無いため作成できません）。")
        return
    number = plan.issue_number
    since = 0.0
    if cp is not None:
        created = parse_iso(cp["created_at"])
        since = created.timestamp() + 1 if created else 0.0  # created_at は秒単位に切り捨てられている
    unimported = handoff_mod.find_new_checkpoint_file(plan.path, since)
    if unimported is not None:
        plan.add(CHECK_CHECKPOINT, BLOCKED, f"AI が書いた Checkpoint が取り込まれていません: {unimported}")
        plan.hints.append(f'取り込むには: wtm checkpoint {number} --from-file "{unimported}"')
    elif cp is None:
        plan.add(CHECK_CHECKPOINT, BLOCKED, "Checkpoint がありません。")
        plan.hints.append(f"記録するには: wtm checkpoint {number}")
    elif plan.head and cp["head"] != plan.head:
        plan.add(CHECK_CHECKPOINT, INFO, f"保存済みですが、最後の Checkpoint（{cp['created_at']}）以降にコミットがあります。")
    else:
        plan.add(CHECK_CHECKPOINT, OK, f"保存済みです（{cp['created_at']}）。")


def _commit_checks(app: App, plan: CleanupPlan, row: sqlite3.Row) -> None:
    """HEAD のコミットがマージ済み、または push 済みであることを確認する。"""
    merged_refs = [base_ref_for(app, row)]
    # squash マージではブランチのコミットが基準に含まれないため、マージした PR の head と比較する
    if plan.pr and plan.pr.merged and plan.pr.head_sha and app.git.ref_exists(app.repo, plan.pr.head_sha):
        merged_refs.append(plan.pr.head_sha)
    unmerged = app.git.count_commits_not_in(plan.path, merged_refs)
    saved_refs = list(merged_refs)
    remote_ref = f"refs/remotes/{app.config.repository.remote}/{plan.branch}"
    if plan.branch and app.git.ref_exists(app.repo, remote_ref):
        saved_refs.append(remote_ref)
    unsaved = app.git.count_commits_not_in(plan.path, saved_refs)
    if unmerged is None or unsaved is None:
        plan.add(CHECK_COMMITS, BLOCKED, f"コミットの状態を確認できません（基準: {merged_refs[0]}）。")
    elif unsaved:
        plan.add(CHECK_COMMITS, BLOCKED, f"マージも push もされていないコミットが {unsaved} 件あります。")
        plan.hints.append("push して PR を作成するか、不要なら取り消してから再実行してください。")
    elif unmerged:
        plan.add(CHECK_COMMITS, CONFIRM, f"push 済みですが基準ブランチに未マージのコミットが {unmerged} 件あります。")
    else:
        plan.add(CHECK_COMMITS, OK, "すべてマージ済みです。")


def _remote_checks(app: App, plan: CleanupPlan, remote: _RemoteView, row: sqlite3.Row) -> None:
    number = plan.issue_number
    plan.issue_state, plan.pr, error = remote.lookup(row, plan.branch)
    pr, state = plan.pr, row["state"]
    if not app.config.forge.enabled:
        plan.add(CHECK_PR, INFO, f"{app.config.forge.label} 連携が無効のため、PR と Issue の状態は確認していません。")
    elif error:
        plan.add(CHECK_PR if plan.pr is None else CHECK_ISSUE, BLOCKED, f"{error}（確認できるまで削除しません）")
    elif pr is None:
        plan.add(CHECK_PR, CONFIRM if state == "DONE" else INFO, "PR はありません。")
    elif pr.merged:
        plan.add(CHECK_PR, OK, f"#{pr.number} はマージ済みです。")
    else:
        plan.add(CHECK_PR, CONFIRM if state == "DONE" else INFO, f"#{pr.number} は未マージです（{pr.status}）。")
    if app.config.forge.enabled and not error:
        if plan.issue_state == "open":
            plan.add(CHECK_ISSUE, CONFIRM, "オープンのままです。wtm は Issue をクローズしません。")
        elif plan.issue_state == "closed":
            plan.add(CHECK_ISSUE, OK, "クローズ済みです。")
        else:
            plan.add(CHECK_ISSUE, INFO, f"{app.config.forge.label} で確認できませんでした（手動で登録した Issue）。")

    if plan.mode == "missing":
        plan.candidate, plan.candidate_reason = True, "worktree が既に存在しません"
    elif pr is not None and pr.merged:
        plan.candidate, plan.candidate_reason = True, f"PR #{pr.number} がマージ済み"
    elif state == "DONE":
        plan.candidate, plan.candidate_reason = True, "作業状態が DONE"
    if plan.candidate:
        plan.add(CHECK_CANDIDATE, OK, plan.candidate_reason)
        if state in ("PLANNED", "WORKING", "BLOCKED") and plan.mode == "remove":
            plan.add(CHECK_CANDIDATE, CONFIRM, f"作業状態は {state} です。作業が終わっていることを確認してください。")
    else:
        plan.add(CHECK_CANDIDATE, BLOCKED, f"PR がマージされておらず、作業状態も DONE ではありません（{state}）。")
        plan.hints.append(f"PR をマージするか、不要な作業なら wtm state {number} DONE で完了にしてください。")


def plan_cleanup(app: App, issue_number: int, *, fetch: bool = True, remote: _RemoteView | None = None,
                 candidates_only: bool = False, now: datetime | None = None) -> CleanupPlan:
    """削除の可否を検査する。worktree とブランチは変更しない。

    candidates_only のときは、候補でない worktree の詳細な検査を省略する（一覧用）。
    """
    row = get_managed(app, issue_number)
    if row["provision"] == "creating":
        raise ConflictError(f"Issue #{issue_number} は作成処理中です。", hint="中断した場合は wtm doctor --fix を実行してください。")
    path = Path(row["path"])
    worktrees = app.git.worktree_list(app.repo)
    info = find_worktree(worktrees, path)
    is_dir = path.is_dir()
    leftover = is_dir and info is None and not _is_empty_dir(path)
    mode = "remove" if info is not None and is_dir else "missing"
    with app.db.read() as c:
        issue = app.db.get_issue(c, issue_number)
    days = days_since(row["last_checked_at"], now or datetime.now(UTC))
    plan = CleanupPlan(
        issue_number=issue_number, worktree_id=row["id"], title=issue["title"] if issue else None, path=path,
        branch=(info.branch if info else None) or row["branch"], state=row["state"], mode=mode,
        stale=days is not None and days >= app.config.management.stale_days, days_since_checked=days)

    if fetch and app.config.repository.remote in app.git.remotes(app.repo):
        fetched = app.git.fetch(app.repo, app.config.repository.remote)
        if not fetched.ok:
            plan.add(CHECK_COMMITS, INFO, f"git fetch に失敗しました。ローカルの参照で確認します: {fetched.stderr.strip()}")
    if mode == "remove":
        plan.head = app.git.head(path)
        if info is not None and plan.branch != row["branch"] and row["branch"]:
            plan.add(CHECK_WORKTREE, INFO, f"ブランチが記録と異なります（記録: {row['branch']} / 実際: {plan.branch or 'detached'}）。")
        if info is not None and info.detached:
            plan.add(CHECK_WORKTREE, INFO, "detached HEAD です。")
    elif leftover:
        plan.add(CHECK_WORKTREE, BLOCKED, f"Git の worktree ではないディレクトリが残っています: {path}。"
                                          "wtm は削除しません。内容を確認して手動で片付けてください。")
    else:
        plan.add(CHECK_WORKTREE, INFO, "worktree は既に存在しません。記録だけを ARCHIVED にします。")
        if info is not None:
            plan.hints.append(f'Git の登録を消すには: git -C "{app.repo}" worktree prune')
    _path_checks(app, plan, worktrees)

    _remote_checks(app, plan, remote or _RemoteView(app), row)
    if candidates_only and not plan.candidate:
        return plan
    sources = _session_checks(app, plan, row)
    _agent_checks(app, plan, row, sources)
    _git_checks(app, plan, info)
    _checkpoint_checks(app, plan, row)
    if mode == "remove":
        _commit_checks(app, plan, row)
    return plan


def collect_cleanup(app: App, *, now: datetime | None = None) -> dict[str, Any]:
    """全 worktree を削除候補・要確認・放置に分類する（何も削除しない）。"""
    remote = _RemoteView(app)
    with app.db.read() as c:
        rows = [r for r in app.db.list_worktrees(c) if r["provision"] == "ready"]
    plans = [plan_cleanup(app, r["issue_number"], fetch=False, remote=remote, candidates_only=True, now=now)
             for r in rows]
    managed = {r["path_key"] for r in rows}
    worktrees = app.git.worktree_list(app.repo)
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": now_iso(),
        "repository": str(app.repo),
        "stale_days": app.config.management.stale_days,
        "entries": [p.to_dict() for p in plans],
        "unmanaged": [{"path": str(w.path), "branch": w.branch, "locked": w.locked}
                      for w in worktrees[1:] if path_key(w.path) not in managed],
        "warnings": [remote.error] if remote.error else [],
    }


# ---------------------------------------------------------------------------
# アーカイブ
# ---------------------------------------------------------------------------
def _copy_worktree_notes(app: App, path: Path, directory: Path) -> list[str]:
    """`git worktree remove` で消える TASK.md と .wtm/ を保存する。リンクは対象外。"""
    copied: list[str] = []
    sources = [path / app.config.task.file_name]
    wtm_dir = path / handoff_mod.WTM_DIR
    if wtm_dir.is_dir() and not wtm_dir.is_symlink():
        sources += sorted(p for p in wtm_dir.rglob("*"))
    for source in sources:
        if source.is_symlink() or not source.is_file():
            continue
        relative = source.relative_to(path)
        target = directory / "files" / relative
        sessions_mod.private_dir(target.parent)
        shutil.copy2(source, target)
        copied.append(relative.as_posix())
    return copied


def _write_json(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, default=str)
    os.replace(tmp, path)


def write_archive(app: App, plan: CleanupPlan, row: sqlite3.Row) -> tuple[Path, dict[str, Any]]:
    """作業の記録を `<管理ディレクトリ>/archive/issue-<N>/<日時>/` に残す。会話の内容は含めない。"""
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    directory = app.config.archive_dir / f"issue-{plan.issue_number}" / stamp
    sessions_mod.private_dir(directory.parent)
    directory.mkdir()
    sessions_mod.private_dir(directory)
    with app.db.read() as c:
        issue = app.db.get_issue(c, plan.issue_number)
        history = c.execute("SELECT from_state, to_state, reason, changed_at FROM state_history"
                            " WHERE worktree_id=? ORDER BY id", (row["id"],)).fetchall()
        checkpoints = c.execute("SELECT id, created_at, head, completed, remaining, next_action, blockers, file_path"
                                " FROM checkpoints WHERE worktree_id=? ORDER BY id", (row["id"],)).fetchall()
        sessions = c.execute("SELECT agent, session_id, created_at, updated_at, source_path, backup_path,"
                             " backed_up_at FROM agent_sessions WHERE worktree_id=? ORDER BY id", (row["id"],)).fetchall()
    commits: list[dict[str, str]] = []
    if plan.mode == "remove" and plan.head:
        commits = app.git.commits(plan.path, f"{base_ref_for(app, row)}..HEAD", MAX_ARCHIVED_COMMITS)
    files = _copy_worktree_notes(app, plan.path, directory) if plan.mode == "remove" else []
    summary: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "archived_at": None,
        "worktree_removed": False,
        "issue": {k: issue[k] for k in ("number", "title", "state", "url")} if issue
        else {"number": plan.issue_number},
        "pr": plan.pr.to_dict() if plan.pr else None,
        "worktree": {"path": row["path"], "branch": plan.branch, "base_ref": row["base_ref"], "head": plan.head,
                     "origin": row["origin"], "agent": row["agent"], "created_at": row["created_at"],
                     "state": plan.state, "mode": plan.mode},
        "reason": plan.candidate_reason,
        "commits": commits,
        "state_history": [dict(h) for h in history],
        "checkpoints": [dict(cp) for cp in checkpoints],
        "sessions": [dict(s) for s in sessions],
        "saved_files": files,
        "deleted_ignored_files": plan.ignored,
        "restore_steps": restore_steps(app, row),
    }
    _write_json(directory / "summary.json", summary)
    return directory, summary


def latest_archive(app: App, issue_number: int) -> Path | None:
    directory = app.config.archive_dir / f"issue-{issue_number}"
    try:
        entries = sorted(p for p in directory.iterdir() if p.is_dir())
    except OSError:
        return None
    return entries[-1] if entries else None


def collect_archived(app: App) -> dict[str, Any]:
    """整理済みの作業の一覧。"""
    with app.db.read() as c:
        rows = c.execute("SELECT w.*, i.title AS title, i.state AS issue_state FROM worktrees w"
                         " LEFT JOIN issues i ON i.number = w.issue_number WHERE w.state='ARCHIVED'"
                         " ORDER BY w.archived_at DESC, w.id DESC").fetchall()
    return {
        "schema_version": SCHEMA_VERSION,
        "archived": [{"issue": r["issue_number"], "title": r["title"], "issue_state": r["issue_state"],
                      "path": r["path"], "branch": r["branch"], "archived_at": r["archived_at"],
                      "archive": str(a) if (a := latest_archive(app, r["issue_number"])) else None}
                     for r in rows],
    }


# ---------------------------------------------------------------------------
# 実行
# ---------------------------------------------------------------------------
@dataclass
class CleanupResult:
    issue_number: int
    removed: bool  # このコマンドで worktree を削除した
    archive: Path
    branch: str | None
    backups: list[sessions_mod.BackupResult] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _backup_sessions(app: App, plan: CleanupPlan) -> tuple[list[sessions_mod.BackupResult], list[str]]:
    results, notes = sessions_mod.backup_issue(app, plan.issue_number)
    failed = [r for r in results if r.status == "failed"]
    if failed:
        lines = [f"セッション {r.session_id} をバックアップできません: {r.message}" for r in failed]
        if app.config.cleanup.require_session_backup:
            raise SafetyError("worktree を削除しませんでした:\n- " + "\n- ".join(lines),
                              hint=f"wtm history {plan.issue_number} --backup で確認してください。")
        notes += lines
    return results, notes


def _recheck(app: App, plan: CleanupPlan) -> sqlite3.Row:
    """削除の直前に状態を確認し直す。検査の後に別の操作が入っていれば中断する。"""
    row = get_managed(app, plan.issue_number)
    if row["id"] != plan.worktree_id or row["state"] != plan.state:
        raise ConflictError(f"検査の間に作業状態が変更されました（{plan.state} → {row['state']}）。再実行してください。")
    path = Path(row["path"])
    info = find_worktree(app.git.worktree_list(app.repo), path)
    current = CleanupPlan(plan.issue_number, plan.worktree_id, plan.title, path, plan.branch, row["state"],
                          "remove" if info is not None and path.is_dir() else "missing")
    if current.mode != plan.mode:
        raise ConflictError("検査の間に worktree の状態が変更されました。再実行してください。")
    with app.db.read() as c:
        sources = [s["source_path"] for s in app.db.list_sessions(c, row["id"]) if s["source_path"]]
    _agent_checks(app, current, row, sources)
    _git_checks(app, current, info)
    if current.mode == "remove" and app.git.head(path) != plan.head:
        current.add(CHECK_COMMITS, BLOCKED, "検査の後に HEAD が変更されました。")
    if new_files := sorted(set(current.ignored) - set(plan.ignored)):
        current.add(CHECK_IGNORED, BLOCKED, f"検査の後に無視対象のファイルが増えました: {_shown(new_files)}")
    if current.blockers:
        raise SafetyError("worktree を削除しませんでした:\n- " + "\n- ".join(c.message for c in current.blockers))
    return row


def _remove_worktree(app: App, plan: CleanupPlan, archive: Path) -> list[str]:
    """`git worktree remove` を実行する。Windows では使用中のディレクトリの削除だけが失敗することがある。"""
    notes: list[str] = []
    path = plan.path
    try:
        app.git.worktree_remove(app.repo, path)
    except CommandFailedError as e:
        # 中身と Git の登録が消え、空のディレクトリだけが残った場合は削除できたものとして扱う
        if find_worktree(app.git.worktree_list(app.repo), path) is not None or (path.exists() and not _is_empty_dir(path)):
            raise WtError(f"worktree を削除できませんでした: {path}\n{(e.stderr or e.stdout).strip()}",
                          hint=f"作業状態は変更していません。記録は {archive} に保存しました。"
                               f" wtm cleanup {plan.issue_number} --dry-run で状態を確認してください。") from e
        notes.append("Git は削除に失敗しましたが、worktree の中身と登録は消えています。")
    if path.exists():
        try:
            path.rmdir()  # 空のディレクトリだけが対象。中身があれば失敗する
        except OSError:
            notes.append(f"空のディレクトリが残っています（他のプロセスが使用中の可能性）: {path}")
    return notes


def execute_cleanup(app: App, plan: CleanupPlan, *, allow_ignored: bool = False) -> CleanupResult:
    """セッションを保全してから worktree を削除し、ARCHIVED に遷移する。ブランチは削除しない。"""
    if plan.blockers:
        raise SafetyError("worktree を削除しませんでした:\n- " + "\n- ".join(c.message for c in plan.blockers))
    if plan.ignored and not allow_ignored:
        raise SafetyError(f"Git の無視対象のファイルが削除されます: {_shown(plan.ignored)}",
                          hint="内容を確認し、削除してよければ --delete-ignored を指定してください。")
    backups, notes = _backup_sessions(app, plan)
    with app.git.lock(app.repo):
        row = _recheck(app, plan)
        try:
            archive, summary = write_archive(app, plan, row)
        except OSError as e:
            raise SafetyError(f"作業の記録を保存できないため worktree を削除しませんでした: {e}") from e
        if plan.mode == "remove":
            notes += _remove_worktree(app, plan, archive)
        reason = "wtm cleanup: " + plan.candidate_reason
        with app.db.transaction() as c:
            app.db.set_state(c, row["id"], "ARCHIVED", reason)
            history = c.execute("SELECT from_state, to_state, reason, changed_at FROM state_history"
                                " WHERE worktree_id=? ORDER BY id", (row["id"],)).fetchall()
        steps = restore_steps(app, row)
    summary.update(archived_at=now_iso(), worktree_removed=plan.mode == "remove", restore_steps=steps,
                   state_history=[dict(h) for h in history])
    try:
        _write_json(archive / "summary.json", summary)
    except OSError as e:  # 削除と状態の更新は完了している
        notes.append(f"作業の記録を更新できませんでした（削除前の内容が残っています）: {e}")
    return CleanupResult(plan.issue_number, plan.mode == "remove", archive, plan.branch, backups, notes)


def branch_delete_hint(app: App, branch: str | None) -> str | None:
    """ブランチの削除は独立した操作なので、コマンドの提示だけを行う。"""
    if not branch or not app.git.branch_exists(app.repo, branch):
        return None
    return f'git -C "{normalize(app.repo)}" branch -d {branch}'
