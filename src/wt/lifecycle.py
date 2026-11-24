"""wtm state: 作業状態の手動変更（WORKING / BLOCKED / DONE）。

REVIEW は wtm finish、ARCHIVED は wtm cleanup が担当する。
Gitea と Git には書き込まない（Issue のクローズや PR の操作は行わない）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import ConflictError, SafetyError, WtError
from .review import parse_checklist
from .service import App, collect_status, get_managed, possibly_running_agents

ALLOWED: dict[str, set[str]] = {
    "PLANNED": {"WORKING", "BLOCKED", "DONE"},
    "WORKING": {"BLOCKED", "DONE"},
    "BLOCKED": {"WORKING", "DONE"},
    "REVIEW": {"WORKING", "BLOCKED", "DONE"},
    "DONE": {"WORKING"},
}
_OTHER_COMMANDS = {
    "REVIEW": "REVIEW への変更は wtm finish を使用してください。",
    "ARCHIVED": "ARCHIVED への変更は wtm cleanup を使用してください。",
    "PLANNED": "PLANNED には戻せません。",
}


def reason_required(from_state: str, to_state: str) -> bool:
    return to_state == "BLOCKED" or (from_state, to_state) in {("DONE", "WORKING"), ("PLANNED", "DONE")}


@dataclass
class StatePlan:
    issue_number: int
    worktree_id: int
    from_state: str
    to_state: str
    blockers: list[str] = field(default_factory=list)  # 1 つでもあれば変更しない
    confirmations: list[str] = field(default_factory=list)  # 続行に確認が必要
    notices: list[str] = field(default_factory=list)  # 表示のみ

    @property
    def changed(self) -> bool:
        return self.from_state != self.to_state

    @property
    def reason_required(self) -> bool:
        return self.changed and reason_required(self.from_state, self.to_state)


def _check_done(app: App, plan: StatePlan, row: Any, status: dict[str, Any], *, allow_dirty: bool,
                skip_checkpoint: bool, offline: bool) -> None:
    git = status["git"]
    started = plan.from_state != "PLANNED"  # 着手前に終える場合は作業成果の確認を省く
    if git.get("exists"):
        if git["conflicted"]:
            plan.blockers.append(f"コンフリクト中のファイルが {len(git['conflicted'])} 件あります。")
        dirty = sorted(set(git["staged"] + git["modified"] + git["untracked"]))
        if dirty:
            message = f"未コミットの変更があります（{len(dirty)} 件）: " + ", ".join(dirty[:10])
            (plan.notices if allow_dirty else plan.blockers).append(message)
        refs = [git["base"]]
        remote_ref = f"refs/remotes/{app.config.repository.remote}/{git.get('branch') or row['branch']}"
        if app.git.ref_exists(app.repo, remote_ref):
            refs.append(remote_ref)
        unsaved = app.git.count_commits_not_in(Path(row["path"]), refs)
        if unsaved:
            plan.confirmations.append(f"基準ブランチにも push 先にもないコミットが {unsaved} 件あります。")
    else:
        plan.notices.append("worktree が存在しないため Git の検査を省略しました。")
    if not started:
        return

    if status["last_checkpoint"] is None:
        message = "Checkpoint がありません（wtm checkpoint で記録するか --skip-checkpoint を指定）。"
        (plan.notices if skip_checkpoint else plan.blockers).append(message)
    if offline or not app.config.forge.enabled:
        plan.notices.append("PR と Issue の最新状態は確認していません。")
    else:
        pr = status["pr"]
        if pr is None:
            plan.confirmations.append("PR がありません（取得に失敗した場合を含む）。")
        elif pr["status"] != "merged":
            plan.confirmations.append(f"PR #{pr['number']} は未マージです（{pr['status']}）。")
    issue = status["issue"]
    if issue:
        _, todo = parse_checklist(issue["body"])
        if todo:
            plan.confirmations.append(f"Issue のチェックリストに未完了が {len(todo)} 件あります。")
        if issue["state"] == "open":
            plan.notices.append("Issue はオープンのままです。wtm は Issue をクローズしません。")


def plan_state_change(app: App, issue_number: int, target: str, *, allow_dirty: bool = False,
                      skip_checkpoint: bool = False, offline: bool = False) -> StatePlan:
    to_state = target.upper()
    if to_state in _OTHER_COMMANDS:
        raise WtError(f"{to_state} は指定できません。", hint=_OTHER_COMMANDS[to_state])
    if to_state not in ("WORKING", "BLOCKED", "DONE"):
        raise WtError(f"未対応の状態です: {target}", hint="WORKING / BLOCKED / DONE のいずれかを指定してください。")
    row = get_managed(app, issue_number)
    if row["provision"] == "creating":
        raise ConflictError(f"Issue #{issue_number} は作成処理中です。")
    from_state = row["state"]
    plan = StatePlan(issue_number, row["id"], from_state, to_state)
    if not plan.changed:
        return plan
    if to_state not in ALLOWED.get(from_state, set()):
        raise ConflictError(f"{from_state} から {to_state} には変更できません。",
                            hint="先に WORKING に戻してください。" if from_state == "DONE" else None)

    running = possibly_running_agents(app, row["id"])
    if to_state == "DONE":
        plan.blockers += [f"{r['agent']} が動作中の可能性があります (pid={r['pid']})。終了してから実行してください。"
                          for r in running]
        status = collect_status(app, issue_number, refresh_issue=not offline, touch=False)
        plan.notices += status["warnings"]
        _check_done(app, plan, row, status, allow_dirty=allow_dirty, skip_checkpoint=skip_checkpoint, offline=offline)
    elif to_state == "BLOCKED":
        plan.notices += [f"{r['agent']} が動作中の可能性があります (pid={r['pid']})。" for r in running]
    return plan


def execute_state_change(app: App, plan: StatePlan, reason: str | None = None) -> None:
    reason = (reason or "").strip()
    if plan.blockers:
        raise SafetyError("状態を変更できません:\n- " + "\n- ".join(plan.blockers))
    if plan.reason_required and not reason:
        raise WtError(f"{plan.from_state} から {plan.to_state} への変更には理由が必要です。", hint="--reason を指定してください。")
    if not plan.changed:
        return
    if plan.to_state == "DONE" and (running := possibly_running_agents(app, plan.worktree_id)):
        raise SafetyError(f"{running[0]['agent']} が動作中の可能性があるため中断しました。")
    with app.db.transaction() as c:
        current = c.execute("SELECT state FROM worktrees WHERE id=?", (plan.worktree_id,)).fetchone()
        if current is None or current["state"] != plan.from_state:
            found = current["state"] if current else "なし"
            raise ConflictError(f"検査の間に作業状態が変更されました（{plan.from_state} → {found}）。再実行してください。")
        app.db.set_state(c, plan.worktree_id, plan.to_state, reason or "wtm state")
    app.db.touch_checked(plan.worktree_id)


def state_info(app: App, issue_number: int) -> dict[str, Any]:
    row = get_managed(app, issue_number)
    with app.db.read() as c:
        history = [dict(h) for h in c.execute(
            "SELECT from_state, to_state, reason, changed_at FROM state_history WHERE worktree_id=? ORDER BY id",
            (row["id"],)).fetchall()]
    return {"issue": issue_number, "state": row["state"], "history": history}
