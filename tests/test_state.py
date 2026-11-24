"""wtm state: 作業状態の手動変更。"""

from __future__ import annotations

import json
import os
import sys

import pytest
from typer.testing import CliRunner

from wt import cli, lifecycle, review, service
from wt.errors import ConflictError, SafetyError, WtError
from wt.gitea import Issue, PullRequest
from wt.handoff import handoff_path

from .conftest import git, start

runner = CliRunner()
STATES = ("PLANNED", "WORKING", "BLOCKED", "REVIEW", "DONE")


def wid(env, issue: int = 1) -> int:
    return service.get_managed(env.app, issue)["id"]


def state(env, issue: int = 1) -> str:
    return service.get_managed(env.app, issue)["state"]


def force_state(env, value: str, issue: int = 1, reason: str = "test") -> None:
    with env.app.db.transaction() as c:
        env.app.db.set_state(c, wid(env, issue), value, reason)


def history(env) -> list[dict]:
    return lifecycle.state_info(env.app, 1)["history"]


def checkpoint(env) -> None:
    service.create_checkpoint(env.app, wid(env), completed="c", remaining="", next_action="", blockers="")


def done_ready(env) -> None:
    """DONE の検査をすべて通る状態にする。"""
    res = start(env, 1)
    env.issues.issues[1] = Issue(1, "t", "- [x] 完了", "closed", "")
    env.issues.pulls.append(PullRequest(5, "t", "closed", True, "u", res.plan.branch, "main", False))
    checkpoint(env)


def commit(path, name: str = "a.txt") -> None:
    (path / name).write_text("x", encoding="utf-8")
    git(path, "add", "--", name)
    git(path, "commit", "-m", name)


@pytest.fixture
def cli_env(env, monkeypatch):
    monkeypatch.setattr(cli, "_app", lambda: env.app)
    return env


# --- 遷移表 ------------------------------------------------------------------
@pytest.mark.parametrize("from_state", STATES)
@pytest.mark.parametrize("to_state", ("WORKING", "BLOCKED", "DONE"))
def test_transition_table(env, from_state, to_state):
    done_ready(env)
    force_state(env, from_state)
    if from_state == "DONE" and to_state == "BLOCKED":
        with pytest.raises(ConflictError, match="変更できません"):
            lifecycle.plan_state_change(env.app, 1, to_state)
        assert state(env) == "DONE"
        return
    plan = lifecycle.plan_state_change(env.app, 1, to_state)
    assert plan.changed == (from_state != to_state)
    assert plan.reason_required == (
        plan.changed and (to_state == "BLOCKED" or (from_state, to_state) in {("DONE", "WORKING"), ("PLANNED", "DONE")}))
    before = len(history(env))
    lifecycle.execute_state_change(env.app, plan, "理由")
    assert state(env) == to_state
    assert len(history(env)) == before + (1 if plan.changed else 0)


@pytest.mark.parametrize("target", ["REVIEW", "ARCHIVED", "PLANNED", "UNKNOWN"])
def test_unavailable_targets(env, target):
    start(env, 1)
    with pytest.raises(WtError):
        lifecycle.plan_state_change(env.app, 1, target)


def test_target_is_case_insensitive(env):
    start(env, 1)
    assert lifecycle.plan_state_change(env.app, 1, "blocked").to_state == "BLOCKED"


def test_reason_is_required_and_recorded(env):
    start(env, 1)
    plan = lifecycle.plan_state_change(env.app, 1, "BLOCKED")
    for empty in (None, "   "):
        with pytest.raises(WtError, match="理由が必要"):
            lifecycle.execute_state_change(env.app, plan, empty)
    assert state(env) == "PLANNED"
    lifecycle.execute_state_change(env.app, plan, " 承認待ち ")
    assert (history(env)[-1]["to_state"], history(env)[-1]["reason"]) == ("BLOCKED", "承認待ち")


def test_concurrent_state_change_is_detected(env):
    start(env, 1)
    plan = lifecycle.plan_state_change(env.app, 1, "BLOCKED")
    force_state(env, "WORKING")
    with pytest.raises(ConflictError, match="検査の間"):
        lifecycle.execute_state_change(env.app, plan, "理由")
    assert state(env) == "WORKING"


def test_creating_reservation_is_rejected(env):
    start(env, 1)
    with env.app.db.transaction() as c:
        c.execute("UPDATE worktrees SET provision='creating'")
    with pytest.raises(ConflictError, match="作成処理中"):
        lifecycle.plan_state_change(env.app, 1, "BLOCKED")


# --- DONE の検査 --------------------------------------------------------------
def test_done_passes_without_confirmation_when_all_clear(env):
    done_ready(env)
    force_state(env, "WORKING")
    plan = lifecycle.plan_state_change(env.app, 1, "DONE")
    assert (plan.blockers, plan.confirmations) == ([], [])


def test_done_blocked_by_running_agent_without_override(env):
    done_ready(env)
    force_state(env, "WORKING")
    with env.app.db.transaction() as c:
        c.execute("INSERT INTO agent_runs(worktree_id, agent, pid, status, started_at) VALUES (?,?,?,?,?)",
                  (wid(env), "claude", os.getpid(), "running", "2026-01-01T00:00:00+00:00"))
    plan = lifecycle.plan_state_change(env.app, 1, "DONE", allow_dirty=True, skip_checkpoint=True)
    assert any("動作中" in b for b in plan.blockers)
    with pytest.raises(SafetyError):
        lifecycle.execute_state_change(env.app, plan)
    assert state(env) == "WORKING"


def test_done_blocked_by_uncommitted_changes_with_override(env):
    done_ready(env)
    force_state(env, "WORKING")
    path = service.get_managed(env.app, 1)["path"]
    with open(f"{path}/wip.txt", "w", encoding="utf-8") as f:
        f.write("x")
    assert any("未コミット" in b for b in lifecycle.plan_state_change(env.app, 1, "DONE").blockers)
    plan = lifecycle.plan_state_change(env.app, 1, "DONE", allow_dirty=True)
    assert plan.blockers == [] and any("未コミット" in n for n in plan.notices)


def test_done_conflict_cannot_be_overridden(env, monkeypatch):
    done_ready(env)
    force_state(env, "WORKING")
    real = service.collect_status

    def with_conflict(*args, **kwargs):
        status = real(*args, **kwargs)
        status["git"]["conflicted"] = ["a.txt"]
        return status

    monkeypatch.setattr(lifecycle, "collect_status", with_conflict)
    plan = lifecycle.plan_state_change(env.app, 1, "DONE", allow_dirty=True)
    assert any("コンフリクト" in b for b in plan.blockers)


def test_done_blocked_without_checkpoint_with_override(env):
    res = start(env, 1)
    env.issues.pulls.append(PullRequest(5, "t", "closed", True, "u", res.plan.branch, "main", False))
    force_state(env, "WORKING")
    assert any("Checkpoint" in b for b in lifecycle.plan_state_change(env.app, 1, "DONE").blockers)
    plan = lifecycle.plan_state_change(env.app, 1, "DONE", skip_checkpoint=True)
    assert not any("Checkpoint" in b for b in plan.blockers)


def test_done_confirms_pr_commits_and_checklist(env):
    res = start(env, 1)
    checkpoint(env)
    force_state(env, "WORKING")
    commit(res.plan.path)
    text = " ".join(lifecycle.plan_state_change(env.app, 1, "DONE").confirmations)
    assert "PR がありません" in text and "コミットが 1 件" in text and "未完了が 1 件" in text
    env.issues.pulls.append(PullRequest(5, "t", "open", False, "u", res.plan.branch, "main", False))
    assert "未マージ" in " ".join(lifecycle.plan_state_change(env.app, 1, "DONE").confirmations)


def test_done_pushed_commits_need_no_commit_confirmation(env):
    res = start(env, 1)
    checkpoint(env)
    force_state(env, "WORKING")
    commit(res.plan.path)
    git(res.plan.path, "push", "origin", res.plan.branch)
    assert "コミットが" not in " ".join(lifecycle.plan_state_change(env.app, 1, "DONE").confirmations)


def test_done_from_planned_skips_work_checks(env):
    start(env, 1)
    plan = lifecycle.plan_state_change(env.app, 1, "DONE")
    assert plan.blockers == [] and plan.confirmations == [] and plan.reason_required


def test_done_with_missing_worktree_skips_git_checks(env):
    done_ready(env)
    force_state(env, "WORKING")
    git(env.repo, "worktree", "remove", service.get_managed(env.app, 1)["path"])
    plan = lifecycle.plan_state_change(env.app, 1, "DONE")
    assert plan.blockers == [] and any("Git の検査を省略" in n for n in plan.notices)


def test_done_offline_does_not_call_gitea(env):
    start(env, 1)
    checkpoint(env)
    force_state(env, "WORKING")
    env.issues.calls.clear()
    plan = lifecycle.plan_state_change(env.app, 1, "DONE", offline=True)
    assert env.issues.calls == [] and any("確認していません" in n for n in plan.notices)
    assert not any("PR" in m for m in plan.confirmations)


def test_state_change_writes_nothing_to_gitea_or_git(env):
    res = start(env, 1)
    checkpoint(env)
    force_state(env, "WORKING")
    head = git(res.plan.path, "rev-parse", "HEAD")
    lifecycle.execute_state_change(env.app, lifecycle.plan_state_change(env.app, 1, "DONE"))
    assert env.issues.comments == [] and env.issues.created_pulls == []
    assert env.issues.issues[1].state == "open"
    assert git(res.plan.path, "rev-parse", "HEAD") == head and res.plan.path.is_dir()


# --- 他のコマンドへの影響 ------------------------------------------------------
def test_blocked_reason_appears_in_status_handoff_and_review(env):
    path = start(env, 1).plan.path
    lifecycle.execute_state_change(env.app, lifecycle.plan_state_change(env.app, 1, "BLOCKED"), "承認待ち")
    assert service.collect_status(env.app, 1)["blocked_reason"] == "承認待ち"
    service.create_handoff(env.app, 1)
    assert "停止理由: 承認待ち" in handoff_path(path).read_text(encoding="utf-8")
    assert any("承認待ち" in f["message"] for f in review.collect_review(env.app, 1)["findings"])
    assert any("承認待ち" in w for w in service.plan_resume(env.app, 1, agent="claude").warnings)

    lifecycle.execute_state_change(env.app, lifecycle.plan_state_change(env.app, 1, "WORKING"), "解決")
    assert service.collect_status(env.app, 1)["blocked_reason"] is None
    service.create_handoff(env.app, 1)
    assert "停止理由" not in handoff_path(path).read_text(encoding="utf-8")


def test_blocked_returns_to_working_on_launch(env):
    start(env, 1)
    force_state(env, "BLOCKED")
    env.app.config.agents.commands["codex"] = [sys.executable, "-c", "pass"]
    service.launch_agent(env.app, 1, "codex")
    assert state(env) == "WORKING"


def test_done_rejects_agent_launch_until_reopened(env):
    start(env, 1)
    force_state(env, "DONE")
    env.app.config.agents.commands["codex"] = [sys.executable, "-c", "pass"]
    with pytest.raises(ConflictError, match="DONE"):
        service.launch_agent(env.app, 1, "codex")
    with pytest.raises(ConflictError, match="DONE"):
        service.plan_resume(env.app, 1, agent="codex")
    with env.app.db.read() as c:
        assert c.execute("SELECT COUNT(*) FROM agent_runs").fetchone()[0] == 0
    lifecycle.execute_state_change(env.app, lifecycle.plan_state_change(env.app, 1, "WORKING"), "差し戻し")
    assert service.launch_agent(env.app, 1, "codex").rc == 0


def test_review_recommends_finish_for_done(env):
    start(env, 1)
    force_state(env, "DONE")
    report = review.collect_review(env.app, 1)
    assert "state_done" in {f["rule"] for f in report["findings"]}
    assert report["recommendation"]["action"] == "finish"


# --- CLI ---------------------------------------------------------------------
def test_cli_show_history(cli_env):
    start(cli_env, 1)
    force_state(cli_env, "BLOCKED", reason="承認待ち")
    r = runner.invoke(cli.app, ["state", "1"])
    assert r.exit_code == 0 and "承認待ち" in r.output
    r = runner.invoke(cli.app, ["state", "1", "--json"])
    data = json.loads(r.output)
    assert data["state"] == "BLOCKED" and data["history"][-1]["reason"] == "承認待ち"
    assert runner.invoke(cli.app, ["state", "1", "--yes"]).exit_code != 0
    assert runner.invoke(cli.app, ["state", "1", "WORKING", "--json"]).exit_code != 0


def test_cli_blocked_with_reason_option_and_prompt(cli_env):
    start(cli_env, 1)
    r = runner.invoke(cli.app, ["state", "1", "BLOCKED", "--reason", "待ち"])
    assert r.exit_code == 0, r.output
    assert state(cli_env) == "BLOCKED"
    r = runner.invoke(cli.app, ["state", "1", "WORKING"])
    assert r.exit_code == 0 and state(cli_env) == "WORKING"
    r = runner.invoke(cli.app, ["state", "1", "BLOCKED"], input="対話の理由\n")
    assert r.exit_code == 0 and history(cli_env)[-1]["reason"] == "対話の理由"


def test_cli_yes_without_required_reason_fails(cli_env):
    start(cli_env, 1)
    r = runner.invoke(cli.app, ["state", "1", "BLOCKED", "--yes"])
    assert r.exit_code != 0 and state(cli_env) == "PLANNED"


def test_cli_done_confirmation(cli_env):
    start(cli_env, 1)
    checkpoint(cli_env)
    force_state(cli_env, "WORKING")
    r = runner.invoke(cli.app, ["state", "1", "DONE"], input="n\n")
    assert r.exit_code == 1 and state(cli_env) == "WORKING"
    r = runner.invoke(cli.app, ["state", "1", "DONE"], input="y\n")
    assert r.exit_code == 0 and state(cli_env) == "DONE"


def test_cli_done_yes_skips_confirmation_but_not_blockers(cli_env):
    start(cli_env, 1)
    force_state(cli_env, "WORKING")
    r = runner.invoke(cli.app, ["state", "1", "DONE", "--yes"])  # Checkpoint なし
    assert r.exit_code == 1 and state(cli_env) == "WORKING"
    r = runner.invoke(cli.app, ["state", "1", "DONE", "--yes", "--skip-checkpoint"])
    assert r.exit_code == 0 and state(cli_env) == "DONE"


def test_cli_dry_run_changes_nothing(cli_env):
    start(cli_env, 1)
    checkpoint(cli_env)
    force_state(cli_env, "WORKING")
    before = (history(cli_env), service.get_managed(cli_env.app, 1)["last_checked_at"])
    for args in (["DONE"], ["BLOCKED"]):
        r = runner.invoke(cli.app, ["state", "1", *args, "--dry-run"])
        assert r.exit_code == 0, r.output
    assert state(cli_env) == "WORKING"
    assert (history(cli_env), service.get_managed(cli_env.app, 1)["last_checked_at"]) == before


def test_cli_same_state_is_noop(cli_env):
    start(cli_env, 1)
    before = history(cli_env)
    r = runner.invoke(cli.app, ["state", "1", "PLANNED"])
    assert r.exit_code != 0  # PLANNED は指定不可
    force_state(cli_env, "BLOCKED")
    before = history(cli_env)
    r = runner.invoke(cli.app, ["state", "1", "BLOCKED"])
    assert r.exit_code == 0 and "既に" in r.output and history(cli_env) == before
