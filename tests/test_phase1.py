"""Phase 1: init / start / list / status / adopt / doctor と安全性のテスト。

テスト番号は DevPrompt.md §10 の項目番号に対応する。
"""

from __future__ import annotations

import sqlite3
import sys
import threading
from pathlib import Path

import pytest

from wt import service
from wt.db import Database
from wt.errors import ConflictError, IssueFetchError, SafetyError, WtError
from wt.gitea import TeaClient
from wt.gitops import Git

from .conftest import git, make_issues, start


def rows(env, sql="SELECT * FROM worktrees"):
    with env.app.db.read() as c:
        return c.execute(sql).fetchall()


# --- 1. 空リポジトリでの初期化 ---------------------------------------------
def test_init_on_empty_repository(workspace):
    main = workspace / "main"
    main.mkdir()
    git(main, "init")
    g = Git()
    plan = service.plan_init(main, g)
    assert plan.default_base_branch == "main"
    assert any("リモート" in w for w in plan.warnings)
    cfg = service.execute_init(plan, g, base_branch="main", forge_enabled=False)
    assert cfg.db_path.exists()
    assert cfg.management_dir == (workspace / "management").resolve()
    assert cfg.repository.worktree_root == (workspace / "worktrees").resolve()
    assert g.config_get(main, "wt.config") == cfg.config_path.as_posix()

    app = service.open_app(None, main, g)
    issue = service.resolve_issue(app, 1, offline=True, title="first")
    with pytest.raises(WtError, match="基準ブランチ"):
        service.plan_start(app, issue)
    assert rows_count(app) == 0


def rows_count(app):
    with app.db.read() as c:
        return c.execute("SELECT COUNT(*) FROM worktrees").fetchone()[0]


def test_init_twice_does_not_overwrite(env):
    g = Git()
    before = env.app.config.config_path.read_text(encoding="utf-8")
    with pytest.raises(ConflictError):
        service.execute_init(service.plan_init(env.repo, g), g, base_branch="x", forge_enabled=False)
    assert env.app.config.config_path.read_text(encoding="utf-8") == before


def test_init_non_standard_layout(tmp_path):
    repo = tmp_path / "myproj"
    repo.mkdir()
    git(repo, "init")
    plan = service.plan_init(repo, Git())
    assert plan.management_dir == (tmp_path / "myproj.wt" / "management").resolve()


# --- 2. worktree の新規作成 / 15. 日本語・空白パス -----------------------------
def test_start_creates_worktree(env):
    res = start(env, 101)
    path = res.plan.path
    assert path.is_dir() and " " in str(path) and "ワーク" in str(path)
    assert res.plan.branch == "wt-101-fix-bug-number-101"
    assert path.name == "wt-101-fix-bug-number-101"
    assert git(path, "rev-parse", "--abbrev-ref", "HEAD").strip() == res.plan.branch
    task = path / "TASK.md"
    text = task.read_text(encoding="utf-8")
    assert "#101" in text and "Fix bug number 101" in text and "## Definition of Done" in text
    # TASK.md は Git の追跡対象外（.gitignore は変更しない）
    assert git(path, "status", "--porcelain") == ""
    assert not (path / ".gitignore").exists()
    row = rows(env)[0]
    assert (row["issue_number"], row["state"], row["provision"]) == (101, "PLANNED", "ready")
    assert Path(row["path"]).is_absolute()
    hist = rows(env, "SELECT * FROM state_history")
    assert [(h["from_state"], h["to_state"]) for h in hist] == [(None, "PLANNED")]


def test_start_reuses_existing_branch_without_reset(env):
    git(env.repo, "branch", "wt-102-old-name")
    sha = git(env.repo, "rev-parse", "wt-102-old-name").strip()
    res = start(env, 102)
    assert res.plan.branch == "wt-102-old-name" and not res.plan.new_branch
    assert res.plan.path.name == "wt-102-old-name"  # slug を既存ブランチから引き継ぐ
    assert git(res.plan.path, "rev-parse", "HEAD").strip() == sha


def test_start_refuses_branch_used_by_other_worktree(env):
    other = env.workspace / "other wt"
    git(env.repo, "worktree", "add", "-b", "wt-103-x", str(other))
    issue = service.resolve_issue(env.app, 103, offline=False, title=None)
    with pytest.raises(ConflictError, match="使用中"):
        service.plan_start(env.app, issue)


def test_start_refuses_closed_issue(env):
    env.issues.issues.update(make_issues(7, state="closed").issues)
    issue = service.resolve_issue(env.app, 7, offline=False, title=None)
    with pytest.raises(ConflictError, match="クローズ"):
        service.plan_start(env.app, issue)
    plan, _ = service.plan_start(env.app, issue, allow_closed=True)
    assert plan.branch.startswith("wt-7-")


def test_start_does_not_overwrite_existing_task_file(env):
    # 既存ブランチに TASK.md が（未追跡で）存在するケースは worktree 側にあり得ないので、
    # 追跡済み TASK.md がある場合を検証する
    (env.repo / "TASK.md").write_text("project task\n", encoding="utf-8")
    git(env.repo, "add", "TASK.md")
    git(env.repo, "commit", "-m", "task")
    git(env.repo, "push")
    res = start(env, 5)
    assert res.task_file is None
    assert any("追跡済み" in w for w in res.warnings)
    assert (res.plan.path / "TASK.md").read_text(encoding="utf-8") == "project task\n"


def test_start_dry_run_plan_has_no_side_effects(env):
    issue = service.resolve_issue(env.app, 3, offline=False, title=None)
    plan, _ = service.plan_start(env.app, issue, fetch=False)
    assert not plan.path.exists()
    assert rows_count(env.app) == 0


# --- 3. 複数 worktree の並列作成 ---------------------------------------------
def test_parallel_start_different_issues(env):
    errors: list[BaseException] = []

    def worker(n: int) -> None:
        try:
            start(env, n)
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(1, 6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert sorted(r["issue_number"] for r in rows(env)) == [1, 2, 3, 4, 5]
    assert len(Git().worktree_list(env.repo)) == 6


# --- 4. 同一 Issue の重複登録防止 --------------------------------------------
def test_duplicate_issue_rejected(env):
    start(env, 1)
    issue = service.resolve_issue(env.app, 1, offline=False, title=None)
    with pytest.raises(ConflictError, match="既に"):
        service.plan_start(env.app, issue)


def test_duplicate_issue_concurrent(env):
    issue = service.resolve_issue(env.app, 9, offline=False, title=None)
    plan, _ = service.plan_start(env.app, issue)
    results: list[object] = []

    def worker() -> None:
        try:
            results.append(service.execute_start(env.app, plan, agent=None))
        except ConflictError as e:
            results.append(e)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    ok = [r for r in results if isinstance(r, service.StartResult)]
    assert len(ok) == 1, results
    assert len(rows(env)) == 1
    assert len(Git().worktree_list(env.repo)) == 2


# --- 5. 既存 worktree の取り込み ---------------------------------------------
def test_adopt_existing_worktree(env):
    path = env.workspace / "既存 wt"
    git(env.repo, "worktree", "add", "-b", "topic/x", str(path))
    (path / "work.txt").write_text("wip", encoding="utf-8")
    head_before = git(path, "rev-parse", "HEAD")
    issue = service.resolve_issue(env.app, 10, offline=False, title=None)
    res = service.adopt(env.app, path, issue)
    assert res.branch == "topic/x"
    assert git(path, "rev-parse", "HEAD") == head_before
    assert (path / "work.txt").read_text(encoding="utf-8") == "wip"
    row = rows(env)[0]
    assert (row["origin"], row["state"]) == ("adopt", "WORKING")
    cp = rows(env, "SELECT * FROM checkpoints")[0]
    assert "work.txt" in cp["uncommitted_json"]
    assert Path(cp["file_path"]).exists()
    with pytest.raises(ConflictError):
        service.adopt(env.app, path, service.resolve_issue(env.app, 11, offline=False, title=None))


def test_adopt_rejects_foreign_repository(env, workspace):
    other = workspace / "other-repo"
    other.mkdir()
    git(other, "init")
    (other / "a").write_text("a", encoding="utf-8")
    git(other, "add", "a")
    git(other, "commit", "-m", "a")
    issue = service.resolve_issue(env.app, 12, offline=False, title=None)
    with pytest.raises(SafetyError):
        service.adopt(env.app, other, issue)
    with pytest.raises(SafetyError, match="main worktree"):
        service.adopt(env.app, env.repo, issue)


# --- 6. Git 差分・未追跡ファイル検出 -----------------------------------------
def test_list_detects_changes_and_untracked(env):
    a = start(env, 1).plan.path
    b = start(env, 2).plan.path
    (a / "README.md").write_text("changed\n", encoding="utf-8")
    (b / "新しい ファイル.txt").write_text("x", encoding="utf-8")
    data = service.collect_list(env.app)
    by = {w["issue"]: w for w in data["worktrees"]}
    assert (by[1]["dirty"], by[1]["untracked"]) == (True, False)
    assert (by[2]["dirty"], by[2]["untracked"]) == (False, True)
    assert by[1]["last_commit_at"] and by[1]["title"] == "Fix bug number 1"
    assert data["schema_version"] == 1 and data["unmanaged"] == []
    st = service.collect_status(env.app, 2)
    assert st["git"]["untracked"] == ["新しい ファイル.txt"]
    assert st["git"]["ahead"] == 0 and st["git"]["behind"] == 0


def test_list_marks_stale_and_unmanaged(env):
    from datetime import UTC, datetime, timedelta

    start(env, 1)
    git(env.repo, "worktree", "add", "-b", "loose", str(env.workspace / "loose"))
    data = service.collect_list(env.app, now=datetime.now(UTC) + timedelta(days=8))
    assert data["worktrees"][0]["stale"] is True
    assert [u["branch"] for u in data["unmanaged"]] == ["loose"]


def test_list_warns_when_worktree_removed_externally(env):
    path = start(env, 1).plan.path
    (path / "TASK.md").unlink()
    git(env.repo, "worktree", "remove", str(path))
    w = service.collect_list(env.app)["worktrees"][0]
    assert w["exists"] is False and w["warnings"]
    assert rows(env)[0]["state"] == "PLANNED"  # 勝手に上書きしない


# --- 7. Issue 取得失敗 / 8. tea 未インストール ----------------------------------
def test_issue_fetch_failure_creates_nothing(env):
    with pytest.raises(IssueFetchError):
        service.resolve_issue(env.app, 999, offline=False, title=None)
    assert rows_count(env.app) == 0


def test_tea_not_installed(tmp_path):
    client = TeaClient("tea-does-not-exist-xyz", tmp_path)
    with pytest.raises(IssueFetchError, match="見つかりません"):
        client.get_issue(1)


def test_offline_requires_title(env):
    with pytest.raises(IssueFetchError, match="タイトル"):
        service.resolve_issue(env.app, 50, offline=True, title=None)
    iss = service.resolve_issue(env.app, 50, offline=True, title="Offline task")
    assert iss.source == "manual" and env.issues.calls == []


def test_status_falls_back_to_cache_when_gitea_fails(env):
    start(env, 1)
    del env.issues.issues[1]
    st = service.collect_status(env.app, 1)
    assert st["issue"]["title"] == "Fix bug number 1"
    assert any("キャッシュ" in w for w in st["warnings"])


def test_doctor_without_tea(env, monkeypatch):
    cfg_text = env.app.config.config_path.read_text(encoding="utf-8")
    env.app.config.config_path.write_text(cfg_text.replace('cli = "tea"', 'cli = "tea-missing-xyz"'),
                                          encoding="utf-8")
    checks = {c.name: c for c in service.doctor(None, env.repo)}
    assert checks["tea"].status == "warn"
    assert checks["Gitea 接続"].status == "skip"
    assert checks["管理 DB"].status == "ok"
    assert checks["Git リポジトリ"].status == "ok"


# --- 9. AI CLI 未インストール ----------------------------------------------
def test_agent_cli_not_installed(env):
    start(env, 1)
    env.app.config.agents.commands["claude"] = ["claude-not-installed-xyz"]
    with pytest.raises(WtError, match="見つかりません"):
        service.launch_agent(env.app, 1, "claude")
    assert rows(env, "SELECT * FROM agent_runs") == []
    assert rows(env)[0]["state"] == "PLANNED"


def test_agent_launch_runs_in_worktree(env):
    path = start(env, 1).plan.path
    script = "import pathlib,sys; pathlib.Path('launched.txt').write_text('ok'); sys.exit(3)"
    env.app.config.agents.commands["codex"] = [sys.executable, "-c", script]
    rc = service.launch_agent(env.app, 1, "codex").rc
    assert rc == 3
    assert (path / "launched.txt").read_text() == "ok"
    run = rows(env, "SELECT * FROM agent_runs")[0]
    assert (run["status"], run["exit_code"]) == ("exited", 3) and run["pid"]
    row = rows(env)[0]
    assert (row["state"], row["agent"]) == ("WORKING", "codex")


def test_agent_duplicate_launch_detected(env):
    start(env, 1)
    wid = rows(env)[0]["id"]
    import os

    with env.app.db.transaction() as c:  # 自プロセスを「実行中のエージェント」として登録
        c.execute("INSERT INTO agent_runs(worktree_id, agent, pid, status, started_at) VALUES (?,?,?,?,?)",
                  (wid, "claude", os.getpid(), "running", "2026-01-01T00:00:00+00:00"))
    env.app.config.agents.commands["codex"] = [sys.executable, "-c", "pass"]
    with pytest.raises(ConflictError, match="動作中"):
        service.launch_agent(env.app, 1, "codex")


# --- 14. SQLite 同時更新 ---------------------------------------------------
def test_sqlite_concurrent_updates(env):
    start(env, 1)
    wid = rows(env)[0]["id"]
    db = Database(env.app.config.db_path)
    errors: list[BaseException] = []

    def worker(i: int) -> None:
        try:
            for j in range(20):
                with db.transaction() as c:
                    c.execute("INSERT INTO state_history(worktree_id, from_state, to_state, reason, changed_at)"
                              " VALUES (?,?,?,?,?)", (wid, "PLANNED", "PLANNED", f"{i}-{j}", "t"))
                db.touch_checked(wid)
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert len(rows(env, "SELECT * FROM state_history WHERE reason LIKE '%-%'")) == 160


def test_db_partial_unique_index(env):
    with env.app.db.transaction() as c:
        c.execute("INSERT INTO worktrees(issue_number,name,path,path_key,state,origin,created_at,updated_at,"
                  "last_checked_at) VALUES (1,'a','p','k1','ARCHIVED','start','t','t','t')")
        c.execute("INSERT INTO worktrees(issue_number,name,path,path_key,state,origin,created_at,updated_at,"
                  "last_checked_at) VALUES (1,'a','p','k1','PLANNED','start','t','t','t')")
    with pytest.raises(sqlite3.IntegrityError):
        with env.app.db.transaction() as c:
            c.execute("INSERT INTO worktrees(issue_number,name,path,path_key,state,origin,created_at,updated_at,"
                      "last_checked_at) VALUES (1,'b','q','k2','WORKING','start','t','t','t')")


# --- 16. Git コマンド途中失敗時の復旧 ----------------------------------------
def test_recovery_when_worktree_add_fails(env, monkeypatch):
    def boom(*a, **k):
        raise WtError("simulated git failure")

    monkeypatch.setattr(env.app.git, "worktree_add", boom)
    with pytest.raises(WtError, match="simulated"):
        start(env, 1)
    assert rows_count(env.app) == 0
    monkeypatch.undo()
    assert start(env, 1).plan.path.is_dir()


def test_recovery_after_worktree_created(env, monkeypatch):
    def boom(path, issue):
        path.write_text("partial", encoding="utf-8")
        raise OSError("disk full (simulated)")

    monkeypatch.setattr(service, "write_task_file", boom)
    with pytest.raises(OSError):
        start(env, 1)
    assert rows_count(env.app) == 0
    assert len(Git().worktree_list(env.repo)) == 1
    assert not Git().branch_exists(env.repo, "wt-1-fix-bug-number-1")
    assert not (env.app.config.repository.worktree_root / "wt-1-fix-bug-number-1").exists()
    monkeypatch.undo()
    assert start(env, 1).task_file is not None


def test_doctor_fixes_interrupted_reservation(env):
    with env.app.db.transaction() as c:
        c.execute("INSERT INTO worktrees(issue_number,name,path,path_key,state,provision,origin,created_at,"
                  "updated_at,last_checked_at) VALUES (4,'issue-4','x','x','PLANNED','creating','start',"
                  "'2020-01-01T00:00:00+00:00','t','t')")
    checks = {c.name: c for c in service.doctor(None, env.repo)}
    assert "中断した作成処理" in checks["worktree メタデータ"].message
    service.doctor(None, env.repo, fix=True)
    assert rows_count(env.app) == 0
    start(env, 4)


def test_repo_slug_derived_from_remote(env):
    git(env.repo, "remote", "set-url", "origin", "ssh://git@gitea.local:2222/owner1/proj.git")
    assert service.gitea_repo_slug(env.app.config) == "owner1/proj"
    client = service.default_gitea_client(env.app.config)
    assert client._context_args() == ["--login", "test", "--repo", "owner1/proj"]
    env.app.config.forge.repo = "explicit/name"
    assert service.gitea_repo_slug(env.app.config) == "explicit/name"


def test_start_with_slug_names_branch_and_folder(env):
    issue = service.resolve_issue(env.app, 1, offline=False, title=None)
    plan, _ = service.plan_start(env.app, issue, slug="trial")
    assert plan.branch == "wt-1-trial"
    assert plan.path == (env.app.config.repository.worktree_root / "wt-1-trial").resolve()


def test_existing_branch_glob_does_not_match_other_issue(env):
    git(env.repo, "branch", "wt-10-other")
    issue = service.resolve_issue(env.app, 1, offline=False, title=None)
    plan, _ = service.plan_start(env.app, issue)
    assert plan.branch == "wt-1-fix-bug-number-1" and plan.new_branch


def test_custom_naming_templates(env):
    env.app.config.naming.branch = "feature/{issue}-{slug}"
    env.app.config.naming.worktree = "issue-{issue}"
    res = start(env, 3, slug="abc")
    assert res.plan.branch == "feature/3-abc"
    assert res.plan.path.name == "issue-3"
