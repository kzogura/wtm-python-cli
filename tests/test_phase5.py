"""Phase 5: wtm cleanup（§10 の 10〜13 を含む）。実際の Git リポジトリを使い、Gitea と AI はモック。"""

from __future__ import annotations

import json
import os
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from .conftest import Env, git, start
from wt import cleanup, cli, service, sessions
from wt.claude_store import claude_config_dir, registered_processes
from wt.errors import CommandFailedError, ConflictError, NotManagedError, SafetyError
from wt.gitea import PullRequest, pull_from_api

runner = CliRunner()


def add_commit(path: Path, name: str = "work.txt") -> str:
    (path / name).write_text("work\n", encoding="utf-8")
    git(path, "add", name)
    git(path, "commit", "-m", f"add {name}")
    return git(path, "rev-parse", "HEAD").strip()


def push(path: Path) -> None:
    branch = git(path, "symbolic-ref", "--short", "HEAD").strip()
    git(path, "push", "origin", f"{branch}:{branch}")


def merge_on_remote(env: Env, path: Path) -> str:
    """Gitea でのマージの代わりに、origin/main を worktree の HEAD まで進める。"""
    head = git(path, "rev-parse", "HEAD").strip()
    git(path, "push", "origin", "HEAD:refs/heads/main")
    git(env.repo, "fetch", "origin")
    return head


def add_pr(env: Env, branch: str, *, merged: bool = True, state: str = "closed", head_sha: str = "") -> PullRequest:
    pr = PullRequest(50 + len(env.issues.pulls), "PR", state, merged, "https://gitea.invalid/pr", branch, "main",
                     head_sha=head_sha)
    env.issues.pulls.append(pr)
    return pr


def checkpoint(env: Env, number: int) -> None:
    row = service.get_managed(env.app, number)
    service.create_checkpoint(env.app, row["id"], completed="done", remaining="", next_action="", blockers="")


def merged_worktree(env: Env, number: int = 1) -> tuple[service.StartResult, Path]:
    """PR がマージされ、全ての検査を通過する worktree。"""
    res = start(env, number)
    path = res.plan.path
    add_commit(path, f"work-{number}.txt")
    push(path)
    head = merge_on_remote(env, path)
    add_pr(env, res.plan.branch, head_sha=head)
    env.issues.issues[number] = replace(env.issues.issues[number], state="closed")
    checkpoint(env, number)
    return res, path


def claude_session(cwd: Path, session_id: str = "11111111-1111-4111-8111-111111111111", age: float = 3600) -> Path:
    project = claude_config_dir() / "projects" / "probe"
    project.mkdir(parents=True, exist_ok=True)
    file = project / f"{session_id}.jsonl"
    file.write_text(
        json.dumps({"type": "user", "cwd": str(cwd), "sessionId": session_id, "version": "2.1.318",
                    "timestamp": "2026-11-01T00:00:00Z", "message": {"role": "user", "content": "こんにちは"}},
                   ensure_ascii=False) + "\n", encoding="utf-8")
    old = time.time() - age
    os.utime(file, (old, old))
    return file


def statuses(plan: cleanup.CleanupPlan, name: str) -> list[str]:
    return [c.status for c in plan.checks if c.name == name]


def block_messages(plan: cleanup.CleanupPlan) -> str:
    return "\n".join(c.message for c in plan.blockers)


def row_state(env: Env, number: int) -> str:
    with env.app.db.read() as c:
        return c.execute("SELECT state FROM worktrees WHERE issue_number=? ORDER BY id DESC", (number,)).fetchone()["state"]


# ---------------------------------------------------------------------------
# 正常系
# ---------------------------------------------------------------------------
def test_cleanup_removes_merged_worktree_and_keeps_everything_else(env, monkeypatch):
    res, path = merged_worktree(env)
    source = claude_session(path)
    (path / "TASK.md").write_text("# メモ\n", encoding="utf-8")
    plan = cleanup.plan_cleanup(env.app, 1)
    assert plan.candidate and not plan.blockers
    assert plan.mode == "remove"

    result = cleanup.execute_cleanup(env.app, plan)

    assert result.removed and not path.exists()
    assert row_state(env, 1) == "ARCHIVED"
    assert not any(line.startswith(f"worktree {path.as_posix()}")
                   for line in git(env.repo, "worktree", "list", "--porcelain").splitlines())
    # ブランチ、Checkpoint、セッションの元ファイルは残る
    assert res.plan.branch in git(env.repo, "branch", "--list", res.plan.branch)
    assert source.is_file()
    with env.app.db.read() as c:
        assert c.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0] == 1
        session = c.execute("SELECT backup_path FROM agent_sessions").fetchone()
    assert Path(session["backup_path"]).is_file()
    # アーカイブ
    summary = json.loads((result.archive / "summary.json").read_text(encoding="utf-8"))
    assert summary["worktree_removed"] is True and summary["archived_at"]
    assert summary["worktree"]["branch"] == res.plan.branch
    assert summary["pr"]["status"] == "merged"
    assert summary["state_history"][-1]["to_state"] == "ARCHIVED"
    assert not any("prune" in step for step in summary["restore_steps"])
    assert any("worktree add" in step for step in summary["restore_steps"])
    assert (result.archive / "files" / "TASK.md").read_text(encoding="utf-8") == "# メモ\n"
    assert "こんにちは" not in (result.archive / "summary.json").read_text(encoding="utf-8")


def test_history_is_available_after_cleanup_even_without_source(env):
    """§10-13: worktree 削除後の履歴閲覧。"""
    _, path = merged_worktree(env)
    source = claude_session(path)
    cleanup.execute_cleanup(env.app, cleanup.plan_cleanup(env.app, 1))

    data = sessions.collect_history(env.app, 1)
    assert data["worktrees"][0]["state"] == "ARCHIVED"
    assert [s["origin"] for s in data["sessions"]] == ["live"]

    source.unlink()  # このテスト用に作ったファイル
    data = sessions.collect_history(env.app, 1)
    assert [s["origin"] for s in data["sessions"]] == ["backup"]
    opened = sessions.open_session(env.app, 1, "11111111")
    assert opened.origin == "backup" and not opened.warnings
    assert "こんにちは" in opened.path.read_text(encoding="utf-8")


def test_issue_can_be_restarted_after_cleanup_and_sessions_follow(env):
    res, path = merged_worktree(env)
    claude_session(path)
    cleanup.execute_cleanup(env.app, cleanup.plan_cleanup(env.app, 1))

    with pytest.raises(NotManagedError) as e:
        service.get_managed(env.app, 1)
    assert "整理済み" in e.value.message and "wtm history 1" in e.value.hint

    env.issues.issues[1] = replace(env.issues.issues[1], state="open")
    restarted = start(env, 1)
    assert restarted.plan.path == path and restarted.plan.branch == res.plan.branch
    assert not restarted.plan.new_branch
    sessions.sync_sessions(env.app, 1)
    with env.app.db.read() as c:
        rows = c.execute("SELECT s.session_id FROM agent_sessions s JOIN worktrees w ON w.id = s.worktree_id"
                         " WHERE w.state<>'ARCHIVED'").fetchall()
    assert [r["session_id"] for r in rows] == ["11111111-1111-4111-8111-111111111111"]


def test_squash_merged_pr_is_recognized_by_head_sha(env):
    res = start(env, 1)
    path = res.plan.path
    head = add_commit(path)
    push(path)
    # squash マージ: origin/main には別のコミットが入り、リモートブランチも削除される
    (env.repo / "squashed.txt").write_text("x\n", encoding="utf-8")
    git(env.repo, "add", "squashed.txt")
    git(env.repo, "commit", "-m", "squash")
    git(env.repo, "push", "origin", "main")
    git(env.repo, "push", "origin", "--delete", res.plan.branch)
    git(env.repo, "fetch", "--prune", "origin")
    add_pr(env, res.plan.branch, head_sha=head)
    env.issues.issues[1] = replace(env.issues.issues[1], state="closed")
    checkpoint(env, 1)
    plan = cleanup.plan_cleanup(env.app, 1)
    assert statuses(plan, cleanup.CHECK_COMMITS) == [cleanup.OK]
    assert not plan.blockers


def test_commit_after_merge_is_refused(env):
    _, path = merged_worktree(env)
    add_commit(path, "after-merge.txt")
    checkpoint(env, 1)
    plan = cleanup.plan_cleanup(env.app, 1)
    assert plan.candidate
    assert "マージも push もされていない" in block_messages(plan)
    with pytest.raises(SafetyError):
        cleanup.execute_cleanup(env.app, plan)
    assert path.is_dir()


def test_pushed_but_unmerged_commit_needs_confirmation(env):
    res = start(env, 1)
    add_commit(res.plan.path)
    push(res.plan.path)
    checkpoint(env, 1)
    with env.app.db.transaction() as c:
        env.app.db.set_state(c, res.worktree_id, "DONE", "test")
    add_pr(env, res.plan.branch, merged=False, state="open")
    plan = cleanup.plan_cleanup(env.app, 1)
    assert plan.candidate and not plan.blockers
    assert {c.name for c in plan.confirmations} >= {cleanup.CHECK_COMMITS, cleanup.CHECK_PR}


def test_done_with_unsaved_commits_is_refused(env):
    res = start(env, 1)
    add_commit(res.plan.path)
    checkpoint(env, 1)
    with env.app.db.transaction() as c:
        env.app.db.set_state(c, res.worktree_id, "DONE", "test")
    plan = cleanup.plan_cleanup(env.app, 1)
    assert plan.candidate
    assert statuses(plan, cleanup.CHECK_COMMITS) == [cleanup.BLOCKED]


# ---------------------------------------------------------------------------
# 削除の拒否
# ---------------------------------------------------------------------------
def test_uncommitted_change_is_refused(env):
    """§10-10。"""
    _, path = merged_worktree(env)
    (path / "README.md").write_text("changed\n", encoding="utf-8")
    plan = cleanup.plan_cleanup(env.app, 1)
    assert statuses(plan, cleanup.CHECK_CHANGES) == [cleanup.BLOCKED]
    with pytest.raises(SafetyError):
        cleanup.execute_cleanup(env.app, plan)
    assert (path / "README.md").read_text(encoding="utf-8") == "changed\n"
    assert row_state(env, 1) != "ARCHIVED"


def test_untracked_file_is_refused(env):
    _, path = merged_worktree(env)
    (path / "new.txt").write_text("x", encoding="utf-8")
    plan = cleanup.plan_cleanup(env.app, 1)
    assert statuses(plan, cleanup.CHECK_UNTRACKED) == [cleanup.BLOCKED]
    assert "new.txt" in block_messages(plan)


def test_git_refuses_even_if_clean_check_is_disabled(env):
    _, path = merged_worktree(env)
    env.app.config.cleanup.require_clean_worktree = False
    (path / "new.txt").write_text("x", encoding="utf-8")
    plan = cleanup.plan_cleanup(env.app, 1)
    assert statuses(plan, cleanup.CHECK_UNTRACKED) == [cleanup.INFO]
    assert not plan.blockers
    with pytest.raises(service.WtError) as e:
        cleanup.execute_cleanup(env.app, plan)
    assert "削除できませんでした" in e.value.message
    assert (path / "new.txt").is_file()
    assert row_state(env, 1) != "ARCHIVED"


def test_locked_worktree_is_refused(env):
    """§10-11。"""
    _, path = merged_worktree(env)
    git(env.repo, "worktree", "lock", str(path), "--reason", "使用中")
    plan = cleanup.plan_cleanup(env.app, 1)
    assert statuses(plan, cleanup.CHECK_LOCK) == [cleanup.BLOCKED]
    assert "使用中" in block_messages(plan)
    assert any("git worktree unlock" in h for h in plan.hints)
    with pytest.raises(SafetyError):
        cleanup.execute_cleanup(env.app, plan)
    assert path.is_dir()


def test_session_backup_failure_refuses_removal(env, monkeypatch):
    """§10-12。"""
    _, path = merged_worktree(env)
    claude_session(path)
    plan = cleanup.plan_cleanup(env.app, 1)
    assert not plan.blockers

    def fail(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(sessions, "_copy_verified", fail)
    with pytest.raises(SafetyError) as e:
        cleanup.execute_cleanup(env.app, plan)
    assert "disk full" in e.value.message
    assert path.is_dir() and row_state(env, 1) != "ARCHIVED"


def test_session_backup_failure_can_be_downgraded_by_config(env, monkeypatch):
    _, path = merged_worktree(env)
    claude_session(path)
    env.app.config.cleanup.require_session_backup = False
    plan = cleanup.plan_cleanup(env.app, 1)

    def fail(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(sessions, "_copy_verified", fail)
    result = cleanup.execute_cleanup(env.app, plan)
    assert result.removed
    assert any("disk full" in n for n in result.notes)


def test_missing_checkpoint_is_refused(env):
    res = start(env, 1)
    add_pr(env, res.plan.branch, head_sha=git(res.plan.path, "rev-parse", "HEAD").strip())
    plan = cleanup.plan_cleanup(env.app, 1)
    assert statuses(plan, cleanup.CHECK_CHECKPOINT) == [cleanup.BLOCKED]


def test_unimported_checkpoint_file_is_refused(env):
    _, path = merged_worktree(env)
    wtm_dir = path / ".wtm"
    wtm_dir.mkdir()
    file = wtm_dir / "checkpoint.md"
    file.write_text("## Completed\nx\n", encoding="utf-8")
    future = time.time() + 5
    os.utime(file, (future, future))
    (env.repo / ".git" / "info").mkdir(exist_ok=True)
    with open(env.repo / ".git" / "info" / "exclude", "a", encoding="utf-8") as f:
        f.write("/.wtm/\n")
    plan = cleanup.plan_cleanup(env.app, 1)
    assert "取り込まれていません" in block_messages(plan)
    assert any("--from-file" in h for h in plan.hints)


def test_running_agent_is_refused(env):
    res, path = merged_worktree(env)
    with env.app.db.transaction() as c:
        c.execute("INSERT INTO agent_runs(worktree_id, agent, pid, status, started_at) VALUES (?,?,?,?,?)",
                  (res.worktree_id, "claude", os.getpid(), "running", service.now_iso()))
    plan = cleanup.plan_cleanup(env.app, 1)
    assert statuses(plan, cleanup.CHECK_AGENT) == [cleanup.BLOCKED]


def test_agent_with_unknown_pid_is_refused(env):
    """停止を断定できない記録は保留する。"""
    res, _ = merged_worktree(env)
    with env.app.db.transaction() as c:
        c.execute("INSERT INTO agent_runs(worktree_id, agent, pid, status, started_at) VALUES (?,?,?,?,?)",
                  (res.worktree_id, "claude", None, "unknown", service.now_iso()))
    assert statuses(cleanup.plan_cleanup(env.app, 1), cleanup.CHECK_AGENT) == [cleanup.BLOCKED]


def registry_file(content: str, pid: int = 4242) -> Path:
    directory = claude_config_dir() / "sessions"
    directory.mkdir(parents=True, exist_ok=True)
    file = directory / f"{pid}.json"
    file.write_text(content, encoding="utf-8")
    return file


def test_claude_started_outside_wtm_is_detected(env):
    _, path = merged_worktree(env)
    registry_file(json.dumps({"pid": os.getpid(), "cwd": str(path / "sub"), "sessionId": "x"}))
    plan = cleanup.plan_cleanup(env.app, 1)
    assert "動作中の可能性" in block_messages(plan)


def test_registry_of_stopped_process_or_other_directory_is_ignored(env, monkeypatch):
    _, path = merged_worktree(env)
    registry_file(json.dumps({"pid": os.getpid(), "cwd": str(env.repo)}), pid=1)  # 別の場所で動作中
    registry_file(json.dumps({"pid": 999999, "cwd": str(path)}), pid=2)
    registry_file("{壊れた記録", pid=3)
    registry_file(json.dumps({"pid": "x", "cwd": str(path)}), pid=4)
    monkeypatch.setattr(cleanup, "pid_alive", lambda pid: pid != 999999)
    assert len(registered_processes()) == 2
    assert not cleanup.plan_cleanup(env.app, 1).blockers


def test_unknown_liveness_of_registered_process_is_refused(env, monkeypatch):
    _, path = merged_worktree(env)
    registry_file(json.dumps({"pid": 1234, "cwd": str(path)}))
    monkeypatch.setattr(cleanup, "pid_alive", lambda _pid: None)
    assert "動作中の可能性" in block_messages(cleanup.plan_cleanup(env.app, 1))


def test_recently_updated_session_is_refused(env):
    _, path = merged_worktree(env)
    claude_session(path, age=0)
    plan = cleanup.plan_cleanup(env.app, 1)
    assert "分以内に更新" in block_messages(plan)


def test_gitea_failure_holds_removal(env):
    res, _ = merged_worktree(env)
    with env.app.db.transaction() as c:
        env.app.db.set_state(c, res.worktree_id, "DONE", "test")
    env.issues.fail_pulls = True
    plan = cleanup.plan_cleanup(env.app, 1)
    assert statuses(plan, cleanup.CHECK_PR) == [cleanup.BLOCKED]


def test_issue_fetch_failure_holds_removal(env):
    _, _path = merged_worktree(env)
    del env.issues.issues[1]
    assert statuses(cleanup.plan_cleanup(env.app, 1), cleanup.CHECK_ISSUE) == [cleanup.BLOCKED]


def offline_merged_worktree(env: Env, number: int) -> None:
    """--offline --title で登録した Issue の worktree（Gitea に同じ番号があるとは限らない）。"""
    issue = service.resolve_issue(env.app, number, offline=True, title="手動")
    plan, _ = service.plan_start(env.app, issue)
    res = service.execute_start(env.app, plan, agent=None)
    add_pr(env, res.plan.branch, head_sha=git(res.plan.path, "rev-parse", "HEAD").strip())
    checkpoint(env, number)


def test_manual_issue_unknown_to_gitea_is_reported_as_unconfirmed(env):
    offline_merged_worktree(env, 500)
    plan = cleanup.plan_cleanup(env.app, 500)
    assert statuses(plan, cleanup.CHECK_ISSUE) == [cleanup.INFO]
    assert plan.candidate and not plan.blockers


def test_manual_issue_known_to_gitea_uses_its_real_state(env):
    env.issues.issues[1] = replace(env.issues.issues[1], state="closed")
    offline_merged_worktree(env, 1)
    plan = cleanup.plan_cleanup(env.app, 1)
    assert plan.issue_state == "closed"
    assert statuses(plan, cleanup.CHECK_ISSUE) == [cleanup.OK]


def test_not_a_candidate_without_merge_or_done(env):
    res = start(env, 1)
    add_pr(env, res.plan.branch, merged=False, state="open")
    checkpoint(env, 1)
    plan = cleanup.plan_cleanup(env.app, 1)
    assert not plan.candidate
    assert statuses(plan, cleanup.CHECK_CANDIDATE) == [cleanup.BLOCKED]
    assert any("wtm state 1 DONE" in h for h in plan.hints)
    with pytest.raises(SafetyError):
        cleanup.execute_cleanup(env.app, plan)
    assert res.plan.path.is_dir()


def test_cwd_inside_worktree_is_refused(env, monkeypatch):
    _, path = merged_worktree(env)
    monkeypatch.chdir(path)
    assert "カレントディレクトリ" in block_messages(cleanup.plan_cleanup(env.app, 1))


def test_main_worktree_is_never_removed(env):
    res = start(env, 1)
    with env.app.db.transaction() as c:  # DB が壊れて main worktree を指している場合
        c.execute("UPDATE worktrees SET path=?, path_key=? WHERE id=?",
                  (str(env.repo), service.path_key(env.repo), res.worktree_id))
    plan = cleanup.plan_cleanup(env.app, 1)
    assert "main worktree" in block_messages(plan)
    with pytest.raises(SafetyError):
        cleanup.execute_cleanup(env.app, plan)
    assert env.repo.is_dir()


def test_management_dir_inside_worktree_is_refused(env):
    _, path = merged_worktree(env)
    env.app.config.config_path = path / "config.toml"
    assert "管理ディレクトリ" in block_messages(cleanup.plan_cleanup(env.app, 1))


def test_directory_unknown_to_git_is_not_touched(env):
    res, path = merged_worktree(env)
    git(env.repo, "worktree", "remove", str(path))
    path.mkdir()
    (path / "keep.txt").write_text("x", encoding="utf-8")
    plan = cleanup.plan_cleanup(env.app, 1)
    assert "Git の worktree ではない" in block_messages(plan)
    assert (path / "keep.txt").is_file()


# ---------------------------------------------------------------------------
# 無視ファイル（git worktree remove は確認なしで削除する）
# ---------------------------------------------------------------------------
def with_ignored(env: Env) -> Path:
    _, path = merged_worktree(env)
    (env.repo / ".git" / "info" / "exclude").write_text("*.env\nbuild/\n/TASK.md\n/.wtm/\n", encoding="utf-8")
    (path / "secret.env").write_text("TOKEN=x\n", encoding="utf-8")
    (path / "build").mkdir()
    (path / "build" / "a.bin").write_text("x", encoding="utf-8")
    return path


def test_ignored_files_require_explicit_permission(env):
    path = with_ignored(env)
    plan = cleanup.plan_cleanup(env.app, 1)
    assert plan.ignored == ["build/", "secret.env"]
    assert statuses(plan, cleanup.CHECK_IGNORED) == [cleanup.CONFIRM]
    with pytest.raises(SafetyError) as e:
        cleanup.execute_cleanup(env.app, plan)
    assert "--delete-ignored" in e.value.hint
    assert (path / "secret.env").is_file()

    result = cleanup.execute_cleanup(env.app, plan, allow_ignored=True)
    assert result.removed
    summary = json.loads((result.archive / "summary.json").read_text(encoding="utf-8"))
    assert summary["deleted_ignored_files"] == ["build/", "secret.env"]
    assert not (result.archive / "files" / "secret.env").exists()  # 秘密情報を複製しない


def test_new_ignored_file_after_planning_aborts(env):
    path = with_ignored(env)
    plan = cleanup.plan_cleanup(env.app, 1)
    (path / "later.env").write_text("x", encoding="utf-8")
    with pytest.raises(SafetyError) as e:
        cleanup.execute_cleanup(env.app, plan, allow_ignored=True)
    assert "later.env" in e.value.message
    assert path.is_dir()


# ---------------------------------------------------------------------------
# 検査と実行の間の変更・途中失敗
# ---------------------------------------------------------------------------
def test_change_after_planning_aborts(env):
    _, path = merged_worktree(env)
    plan = cleanup.plan_cleanup(env.app, 1)
    (path / "late.txt").write_text("x", encoding="utf-8")
    with pytest.raises(SafetyError):
        cleanup.execute_cleanup(env.app, plan)
    assert (path / "late.txt").is_file()


def test_commit_after_planning_aborts(env):
    _, path = merged_worktree(env)
    plan = cleanup.plan_cleanup(env.app, 1)
    add_commit(path, "late.txt")
    with pytest.raises(SafetyError) as e:
        cleanup.execute_cleanup(env.app, plan)
    assert "HEAD" in e.value.message


def test_state_change_after_planning_aborts(env):
    res, path = merged_worktree(env)
    plan = cleanup.plan_cleanup(env.app, 1)
    with env.app.db.transaction() as c:
        env.app.db.set_state(c, res.worktree_id, "WORKING", "別プロセス")
    with pytest.raises(ConflictError):
        cleanup.execute_cleanup(env.app, plan)
    assert path.is_dir()


def test_agent_started_after_planning_aborts(env):
    res, path = merged_worktree(env)
    plan = cleanup.plan_cleanup(env.app, 1)
    with env.app.db.transaction() as c:
        c.execute("INSERT INTO agent_runs(worktree_id, agent, pid, status, started_at) VALUES (?,?,?,?,?)",
                  (res.worktree_id, "claude", os.getpid(), "running", service.now_iso()))
    with pytest.raises(SafetyError):
        cleanup.execute_cleanup(env.app, plan)
    assert path.is_dir()


def test_failed_removal_keeps_state_and_records_archive(env, monkeypatch):
    _, path = merged_worktree(env)
    plan = cleanup.plan_cleanup(env.app, 1)

    def fail(*_a, **_k):
        raise CommandFailedError(["git", "worktree", "remove"], 1, "", "fatal: 失敗")

    monkeypatch.setattr(env.app.git, "worktree_remove", fail)
    with pytest.raises(service.WtError) as e:
        cleanup.execute_cleanup(env.app, plan)
    assert "失敗" in e.value.message
    assert path.is_dir() and row_state(env, 1) != "ARCHIVED"
    archive = cleanup.latest_archive(env.app, 1)
    assert json.loads((archive / "summary.json").read_text(encoding="utf-8"))["worktree_removed"] is False

    monkeypatch.undo()  # 再実行で完了できる
    result = cleanup.execute_cleanup(env.app, cleanup.plan_cleanup(env.app, 1))
    assert result.removed and row_state(env, 1) == "ARCHIVED"


def test_archive_failure_refuses_removal(env, monkeypatch):
    _, path = merged_worktree(env)
    plan = cleanup.plan_cleanup(env.app, 1)

    def fail(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(cleanup.shutil, "copy2", fail)
    with pytest.raises(SafetyError) as e:
        cleanup.execute_cleanup(env.app, plan)
    assert "disk full" in e.value.message
    assert path.is_dir() and row_state(env, 1) != "ARCHIVED"


def test_partial_removal_with_empty_directory_counts_as_removed(env, monkeypatch):
    """Windows ではプロセスが使用中のディレクトリだけ残して exit 255 になる（実機で確認した挙動）。"""
    _, path = merged_worktree(env)
    plan = cleanup.plan_cleanup(env.app, 1)
    real = env.app.git.worktree_remove

    def partial(repo, target):
        real(repo, target)
        target.mkdir()
        raise CommandFailedError(["git", "worktree", "remove"], 255, "", "error: failed to delete: Permission denied")

    monkeypatch.setattr(env.app.git, "worktree_remove", partial)
    result = cleanup.execute_cleanup(env.app, plan)
    assert result.removed and row_state(env, 1) == "ARCHIVED"
    assert not path.exists()
    assert any("削除に失敗" in n for n in result.notes)


def test_interrupted_after_removal_can_be_completed(env):
    """削除の後に中断した場合: 次の実行で記録だけを更新できる。"""
    _, path = merged_worktree(env)
    git(env.repo, "worktree", "remove", str(path))
    plan = cleanup.plan_cleanup(env.app, 1)
    assert plan.mode == "missing" and plan.candidate and not plan.blockers
    assert not any(c.name == cleanup.CHECK_COMMITS for c in plan.checks)
    result = cleanup.execute_cleanup(env.app, plan)
    assert not result.removed and row_state(env, 1) == "ARCHIVED"
    assert json.loads((result.archive / "summary.json").read_text(encoding="utf-8"))["saved_files"] == []


def test_prunable_entry_is_archived_without_git_operation(env, monkeypatch):
    import shutil

    res = start(env, 1)
    shutil.rmtree(res.plan.path)
    plan = cleanup.plan_cleanup(env.app, 1)
    assert plan.mode == "missing" and not plan.blockers
    assert any("worktree prune" in h for h in plan.hints)
    monkeypatch.setattr(env.app.git, "worktree_remove", lambda *_a: pytest.fail("呼ばれないはず"))
    cleanup.execute_cleanup(env.app, plan)
    assert row_state(env, 1) == "ARCHIVED"


def test_locked_entry_with_missing_directory_is_refused(env):
    """リムーバブルドライブ上などの可能性があるため、ロックを尊重する。"""
    import shutil

    res = start(env, 1)
    git(env.repo, "worktree", "lock", str(res.plan.path))
    shutil.rmtree(res.plan.path)
    plan = cleanup.plan_cleanup(env.app, 1)
    assert statuses(plan, cleanup.CHECK_LOCK) == [cleanup.BLOCKED]


# ---------------------------------------------------------------------------
# 一覧・放置検出
# ---------------------------------------------------------------------------
def categories(env: Env, **kw) -> dict[int, str]:
    return {e["issue"]: e["category"] for e in cleanup.collect_cleanup(env.app, **kw)["entries"]}


def test_overview_classifies_worktrees_without_changing_anything(env):
    _, merged_path = merged_worktree(env, 1)
    dirty_res, dirty_path = merged_worktree(env, 2)
    (dirty_path / "x.txt").write_text("x", encoding="utf-8")
    start(env, 3)  # 作業中
    stale = start(env, 4)
    with env.app.db.transaction() as c:
        c.execute("UPDATE worktrees SET last_checked_at=? WHERE id=?",
                  ((datetime.now(UTC) - timedelta(days=30)).isoformat(), stale.worktree_id))
    closed = start(env, 5)
    add_pr(env, closed.plan.branch, merged=False, state="closed")
    closed_issue = start(env, 6)
    env.issues.issues[6] = replace(env.issues.issues[6], state="closed")
    before = git(env.repo, "worktree", "list", "--porcelain")

    data = cleanup.collect_cleanup(env.app)

    assert {e["issue"]: e["category"] for e in data["entries"]} == {
        1: "ready", 2: "blocked", 3: "active", 4: "stale", 5: "review", 6: "review"}
    assert git(env.repo, "worktree", "list", "--porcelain") == before
    assert merged_path.is_dir() and closed_issue.plan.path.is_dir()
    assert data["schema_version"] == 1
    assert next(e for e in data["entries"] if e["issue"] == 4)["days_since_checked"] == 30


def test_overview_does_not_update_last_checked(env):
    res = start(env, 1)
    old = (datetime.now(UTC) - timedelta(days=30)).isoformat()
    with env.app.db.transaction() as c:
        c.execute("UPDATE worktrees SET last_checked_at=? WHERE id=?", (old, res.worktree_id))
    cleanup.collect_cleanup(env.app)
    assert service.get_managed(env.app, 1)["last_checked_at"] == old


def test_overview_with_gitea_down_asks_only_once(env):
    start(env, 1)
    start(env, 2)
    env.issues.fail_pulls = True
    data = cleanup.collect_cleanup(env.app)
    assert len(data["warnings"]) == 1
    assert {e["category"] for e in data["entries"]} == {"review"}
    assert not any(e["candidate"] for e in data["entries"])


def test_overview_lists_unmanaged_worktrees(env):
    git(env.repo, "worktree", "add", "-b", "other", str(env.workspace / "other"))
    assert [u["branch"] for u in cleanup.collect_cleanup(env.app)["unmanaged"]] == ["other"]


# ---------------------------------------------------------------------------
# 大きなリポジトリ（中断された git worktree add）
# ---------------------------------------------------------------------------
def test_long_running_git_operations_have_no_timeout(env, monkeypatch):
    calls = {}
    real = env.app.git.run

    def spy(args, cwd, **kw):
        calls[args[0] if args[0] != "worktree" else f"worktree {args[1]}"] = kw.get("timeout", 120)
        return real(args, cwd, **kw)

    monkeypatch.setattr(env.app.git, "run", spy)
    res = start(env, 1)
    env.app.git.worktree_remove(env.repo, res.plan.path)
    assert calls["worktree add"] is None and calls["worktree remove"] is None and calls["fetch"] is None


def interrupted_add(env: Env, monkeypatch, *, dirty: bool) -> Path:
    """git worktree add が中断された状態（ロック理由 initializing）を再現して start を失敗させる。"""
    real = env.app.git.worktree_add

    def interrupted(repo, path, **kw):
        real(repo, path, **kw)
        git(repo, "worktree", "lock", str(path), "--reason", "initializing")
        if dirty:
            (path / "partial.bin").write_text("x", encoding="utf-8")
        raise CommandFailedError(["git", "worktree", "add"], -1, "", "タイムアウトしました")

    monkeypatch.setattr(env.app.git, "worktree_add", interrupted)
    issue = service.resolve_issue(env.app, 1, offline=False, title=None)
    plan, _ = service.plan_start(env.app, issue)
    with pytest.raises(CommandFailedError):
        service.execute_start(env.app, plan, agent=None)
    return plan.path


def test_rollback_clears_initializing_lock(env, monkeypatch):
    path = interrupted_add(env, monkeypatch, dirty=False)
    assert not path.exists()
    assert "wt-1-" not in git(env.repo, "branch", "--list", "wt-1-*")
    with env.app.db.read() as c:
        assert c.execute("SELECT COUNT(*) FROM worktrees").fetchone()[0] == 0


def test_rollback_never_forces_and_prints_manual_steps(env, monkeypatch, capsys):
    path = interrupted_add(env, monkeypatch, dirty=True)
    assert (path / "partial.bin").is_file()  # --force を使わない
    err = capsys.readouterr().err
    assert "worktree remove --force" in err and "branch -D" in err
    assert "wt-1-" in git(env.repo, "branch", "--list", "wt-1-*")


# ---------------------------------------------------------------------------
# API の解釈
# ---------------------------------------------------------------------------
def test_pull_from_api_reads_head_sha():
    pr = pull_from_api({"number": 5, "state": "closed", "merged": True, "html_url": "u",
                        "head": {"ref": "b", "sha": "abc"}, "base": {"ref": "main"}})
    assert pr.head_sha == "abc" and pr.status == "merged"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
@pytest.fixture
def cli_env(env, monkeypatch):
    monkeypatch.setattr(cli, "_app", lambda: env.app)
    monkeypatch.chdir(env.repo)
    monkeypatch.setenv("COLUMNS", "200")
    return env


def invoke(*args: str, input: str | None = None):
    return runner.invoke(cli.app, ["cleanup", *args], input=input)


def test_cli_without_issue_never_deletes(cli_env):
    _, path = merged_worktree(cli_env)
    result = invoke()
    assert result.exit_code == 0 and "削除可能" in result.output
    assert path.is_dir()
    result = invoke("--dry-run")
    assert result.exit_code == 0 and path.is_dir()


def test_cli_has_no_force_and_no_bulk_delete(cli_env):
    assert "--force" not in runner.invoke(cli.app, ["cleanup", "--help"]).output
    assert invoke("--force").exit_code != 0
    result = invoke("--yes")
    assert result.exit_code == 1


def test_cli_dry_run_changes_nothing(cli_env):
    _, path = merged_worktree(cli_env)
    result = invoke("1", "--dry-run")
    assert result.exit_code == 0, result.output
    assert "dry-run" in result.output
    assert path.is_dir() and row_state(cli_env, 1) != "ARCHIVED"
    assert cleanup.latest_archive(cli_env.app, 1) is None


def test_cli_dry_run_reports_blockers_with_exit_code(cli_env):
    _, path = merged_worktree(cli_env)
    (path / "x.txt").write_text("x", encoding="utf-8")
    result = invoke("1", "--dry-run")
    assert result.exit_code == 1 and "NG" in result.output


def test_cli_requires_confirmation(cli_env):
    _, path = merged_worktree(cli_env)
    result = invoke("1", input="n\n")
    assert result.exit_code == 1
    assert path.is_dir() and row_state(cli_env, 1) != "ARCHIVED"
    result = invoke("1", input="y\n")
    assert result.exit_code == 0, result.output
    assert not path.exists() and row_state(cli_env, 1) == "ARCHIVED"
    assert "branch -d" in result.output


def test_cli_no_input_does_not_delete(cli_env):
    _, path = merged_worktree(cli_env)
    assert invoke("1", input="").exit_code != 0
    assert path.is_dir()


def test_cli_yes_skips_prompt_but_not_checks(cli_env):
    _, path = merged_worktree(cli_env)
    (path / "x.txt").write_text("x", encoding="utf-8")
    assert invoke("1", "--yes").exit_code == 1
    assert path.is_dir()
    (path / "x.txt").unlink()
    assert invoke("1", "--yes").exit_code == 0
    assert not path.exists()


def test_cli_confirmation_config_can_be_disabled(cli_env):
    _, path = merged_worktree(cli_env)
    cli_env.app.config.cleanup.require_confirmation = False
    assert invoke("1").exit_code == 0
    assert not path.exists()


def test_cli_ignored_files_with_yes_need_extra_flag(cli_env):
    path = with_ignored(cli_env)
    result = invoke("1", "--yes")
    assert result.exit_code == 1 and "--delete-ignored" in result.output
    assert (path / "secret.env").is_file()
    assert invoke("1", "--yes", "--delete-ignored").exit_code == 0
    assert not path.exists()


def test_cli_interactive_confirmation_covers_ignored_files(cli_env):
    path = with_ignored(cli_env)
    result = invoke("1", input="y\n")
    assert result.exit_code == 0, result.output
    assert "secret.env" in result.output and not path.exists()


def test_cli_json_output(cli_env):
    merged_worktree(cli_env)
    data = json.loads(invoke("1", "--dry-run", "--json").stdout)
    assert data["category"] == "ready" and data["checks"]
    data = json.loads(invoke("--json").stdout)
    assert data["entries"][0]["issue"] == 1
    assert invoke("1", "--json").exit_code == 1


def test_cli_list_archived(cli_env):
    _, path = merged_worktree(cli_env)
    assert "ありません" in runner.invoke(cli.app, ["list", "--archived"]).output
    invoke("1", "--yes")
    result = runner.invoke(cli.app, ["list", "--archived"])
    assert result.exit_code == 0 and "#1" in result.output
    data = json.loads(runner.invoke(cli.app, ["list", "--archived", "--json"]).stdout)
    assert data["archived"][0]["issue"] == 1
    assert Path(data["archived"][0]["archive"], "summary.json").is_file()
    active = json.loads(runner.invoke(cli.app, ["list", "--json", "--offline"]).stdout)
    assert active["worktrees"] == []
    assert not path.exists()


def test_cli_status_after_cleanup_explains_what_to_do(cli_env):
    merged_worktree(cli_env)
    invoke("1", "--yes")
    result = runner.invoke(cli.app, ["status", "1"])
    assert result.exit_code == 1 and "整理済み" in result.output


def test_state_command_points_to_cleanup(cli_env):
    start(cli_env, 1)
    result = runner.invoke(cli.app, ["state", "1", "ARCHIVED"])
    assert result.exit_code == 1 and "wtm cleanup" in result.output
