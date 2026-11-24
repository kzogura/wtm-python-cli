"""Phase 2: Gitea 連携（Issue 作成・PR 取得/作成・Checkpoint 投稿・finish）のテスト。

Gitea は FakeGitea、tea CLI は run() のモックで置き換え、実環境には書き込まない。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from wt import cli, gitea, service
from wt.errors import GiteaError, SafetyError, WtError
from wt.gitea import PullRequest, TeaClient, pull_from_api
from wt.locks import repo_lock
from wt.proc import CmdResult

from .conftest import git, start

runner = CliRunner()


def pr(number, head, state="open", merged=False, base="main", draft=False):
    return PullRequest(number, f"PR {number}", state, merged, f"https://gitea.invalid/pulls/{number}", head, base, draft)


def commit_in(path: Path, name: str = "feature.txt") -> None:
    (path / name).write_text("x\n", encoding="utf-8")
    git(path, "add", name)
    git(path, "commit", "-m", f"add {name}")


# --- TeaClient: 引数配列（--help で確認したオプションのみ使用）-------------------
class FakeRun:
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.calls: list[dict] = []

    def __call__(self, args, cwd=None, *, check=False, timeout=None, env=None, input=None):
        self.calls.append({"args": args, "cwd": cwd, "input": input})
        out = self.outputs.pop(0) if self.outputs else ""
        return CmdResult(list(args), 0, out, "")


@pytest.fixture
def tea(tmp_path, monkeypatch):
    def make(*outputs):
        fake = FakeRun(outputs)
        monkeypatch.setattr(gitea, "run", fake)
        return TeaClient("tea", tmp_path, login="me", repo_slug="o/r"), fake
    return make


def test_tea_create_issue_uses_api_post_with_stdin(tea):
    client, fake = tea(json.dumps({"number": 42, "title": "新機能", "body": "本文", "state": "open",
                                   "html_url": "u"}))
    issue = client.create_issue("新機能", "本文\n改行")
    assert issue.number == 42
    call = fake.calls[0]
    assert call["args"] == ["tea", "api", "--login", "me", "--repo", "o/r", "-X", "POST", "-d", "@-",
                            "/repos/{owner}/{repo}/issues"]
    assert json.loads(call["input"]) == {"title": "新機能", "body": "本文\n改行"}


def test_tea_create_issue_error_response(tea):
    client, _ = tea(json.dumps({"message": "token is required"}))
    with pytest.raises(GiteaError, match="token is required"):
        client.create_issue("t", "")


def test_tea_add_comment(tea):
    client, fake = tea("")
    client.add_comment(7, "## 進捗")
    assert fake.calls[0]["args"] == ["tea", "comments", "add", "--login", "me", "--repo", "o/r",
                                     "--description", "## 進捗", "7"]


def test_tea_find_pulls_pages_and_filters(tea):
    page1 = [{"number": n, "title": "t", "state": "closed", "merged": n == 3, "html_url": "",
              "head": {"ref": "wt-1-x" if n in (3, 40) else "other"}, "base": {"ref": "main"}}
             for n in range(1, gitea.PULLS_PAGE_LIMIT + 1)]
    page2 = [{"number": 60, "title": "t", "state": "open", "merged": False, "html_url": "",
              "head": {"ref": "wt-1-x"}, "base": {"ref": "main"}}]
    client, fake = tea(json.dumps(page1), json.dumps(page2))
    pulls = client.find_pulls("wt-1-x")
    assert [p.number for p in pulls] == [60, 40, 3]
    assert [p.status for p in pulls] == ["open", "closed", "merged"]
    assert "page=2" in fake.calls[1]["args"][-1] and "state=all" in fake.calls[1]["args"][-1]


def test_tea_create_pull_args_and_lookup(tea):
    created = [{"number": 9, "title": "T", "state": "open", "merged": False, "html_url": "u",
                "head": {"ref": "wt-1-x"}, "base": {"ref": "main"}}]
    client, fake = tea("created", json.dumps(created))
    result = client.create_pull("wt-1-x", "main", "T", "Closes #1", draft=True)
    assert result.number == 9
    assert fake.calls[0]["args"] == ["tea", "pulls", "create", "--login", "me", "--repo", "o/r",
                                     "--head", "wt-1-x", "--base", "main", "--title", "T",
                                     "--description-file", "-", "--draft"]
    assert fake.calls[0]["input"] == "Closes #1"


def test_pull_from_api_status():
    assert pull_from_api({"number": 1, "state": "open", "draft": True, "head": {"ref": "b"}}).status == "draft"
    assert pull_from_api({"number": 1, "state": "closed", "merged": True}).status == "merged"
    assert pull_from_api({"message": "x"}) is None


def test_tea_find_pulls_matches_merged_pr_after_branch_deleted(tea):
    # Gitea 1.26.1 の実際の応答形式: ブランチ削除後の ref は refs/pull/<n>/head
    deleted = [{"number": 38, "title": "t", "state": "closed", "merged": True, "html_url": "",
                "head": {"label": "wt-37-ai", "ref": "refs/pull/38/head", "sha": "abc"},
                "base": {"label": "main", "ref": "main"}},
               {"number": 35, "title": "t", "state": "closed", "merged": True, "html_url": "",
                "head": {"label": "wt-34-other", "ref": "refs/pull/35/head", "sha": "def"},
                "base": {"label": "main", "ref": "main"}}]
    client, _ = tea(json.dumps(deleted))
    pulls = client.find_pulls("wt-37-ai")
    assert [p.number for p in pulls] == [38]
    assert pulls[0].head == "wt-37-ai" and pulls[0].head_sha == "abc" and pulls[0].status == "merged"


# --- PR 状態の表示 -----------------------------------------------------------
def test_list_and_status_show_pr(env):
    res = start(env, 1)
    env.issues.pulls = [pr(5, res.plan.branch, state="closed"), pr(8, res.plan.branch)]
    data = service.collect_list(env.app)
    w = data["worktrees"][0]
    assert w["pr_state"] == "open" and w["pr"]["number"] == 8
    st = service.collect_status(env.app, 1)
    assert st["pr"]["number"] == 8
    offline = service.collect_list(env.app, offline=True)
    assert offline["worktrees"][0]["pr"] is None


def test_list_pr_failure_warns_once(env):
    start(env, 1)
    start(env, 2)
    env.issues.fail_pulls = True
    data = service.collect_list(env.app)
    assert len(data["warnings"]) == 1 and "PR" in data["warnings"][0]
    assert all(w["pr"] is None for w in data["worktrees"])


# --- wt start --new -------------------------------------------------------
def test_create_issue_then_start(env):
    issue = service.create_issue(env.app, "  New feature  ", "目的: ...")
    assert issue.title == "New feature"
    plan, _ = service.plan_start(env.app, issue, slug="feat")
    assert plan.branch == f"wt-{issue.number}-feat"
    with env.app.db.read() as c:
        assert env.app.db.get_issue(c, issue.number)["title"] == "New feature"


def test_create_issue_requires_gitea(env):
    env.app.config.forge.enabled = False
    with pytest.raises(WtError, match="無効"):
        service.create_issue(env.app, "t", "")


def test_cli_start_new(env, monkeypatch):
    monkeypatch.setattr(cli, "_app", lambda: env.app)
    before = set(env.issues.issues)
    r = runner.invoke(cli.app, ["start", "--new", "--title", "CLI issue", "--body", "b", "--dry-run"])
    assert r.exit_code == 0, r.output
    assert set(env.issues.issues) == before  # dry-run では作成しない
    r = runner.invoke(cli.app, ["start", "--new", "--title", "CLI issue", "--body", "b", "--slug", "cli"],
                      input="n\n")
    assert r.exit_code == 1 and set(env.issues.issues) == before  # 確認で拒否
    r = runner.invoke(cli.app, ["start", "--new", "--title", "CLI issue", "--body", "b", "--slug", "cli",
                                "--yes", "--no-launch"])
    assert r.exit_code == 0, r.output
    (number,) = set(env.issues.issues) - before
    assert (env.app.config.repository.worktree_root / f"wt-{number}-cli").is_dir()


# --- Checkpoint の Issue コメント投稿 ------------------------------------------
def test_publish_checkpoint(env):
    res = start(env, 1)
    (res.plan.path / "wip.txt").write_text("x", encoding="utf-8")
    wid = service.get_managed(env.app, 1)["id"]
    cp_id = service.create_checkpoint(env.app, wid, completed="API 実装", remaining="テスト",
                                      next_action="レビュー依頼", blockers="", session_id="secret-session")
    body = service.publish_checkpoint(env.app, cp_id)
    assert env.issues.comments == [(1, body)]
    assert "API 実装" in body and "`wip.txt`" in body and res.plan.branch in body
    assert "secret-session" not in body  # セッション情報は投稿しない
    assert service.get_checkpoint(env.app, cp_id)["published_at"]


def test_cli_checkpoint_publish_requires_confirmation(env, monkeypatch):
    monkeypatch.setattr(cli, "_app", lambda: env.app)
    start(env, 1)
    r = runner.invoke(cli.app, ["checkpoint", "1", "--completed", "a", "--publish"], input="n\n")
    assert r.exit_code == 1 and env.issues.comments == []
    r = runner.invoke(cli.app, ["checkpoint", "1", "--completed", "a", "--publish", "--yes"])
    assert r.exit_code == 0, r.output
    assert len(env.issues.comments) == 1


# --- wt finish -------------------------------------------------------------
def checkpointed(env, number):
    res = start(env, number)
    wid = service.get_managed(env.app, number)["id"]
    service.create_checkpoint(env.app, wid, completed="done", remaining="", next_action="", blockers="")
    return res.plan.path


def test_finish_blocks_on_dirty_and_missing_checkpoint(env):
    path = start(env, 1).plan.path
    (path / "README.md").write_text("changed", encoding="utf-8")
    plan = service.plan_finish(env.app, 1)
    assert any("未コミット" in b for b in plan.blockers)
    assert any("Checkpoint" in b for b in plan.blockers)
    with pytest.raises(SafetyError):
        service.execute_finish(env.app, plan, push=False, create_pr=False)
    assert service.get_managed(env.app, 1)["state"] == "PLANNED"
    plan = service.plan_finish(env.app, 1, allow_dirty=True, skip_checkpoint=True)
    assert plan.blockers == [] and len(plan.warnings) >= 2


def test_finish_blocks_when_agent_running(env):
    checkpointed(env, 1)
    wid = service.get_managed(env.app, 1)["id"]
    with env.app.db.transaction() as c:
        c.execute("INSERT INTO agent_runs(worktree_id, agent, pid, status, started_at) VALUES (?,?,?,?,?)",
                  (wid, "claude", os.getpid(), "running", "2026-01-01T00:00:00+00:00"))
    plan = service.plan_finish(env.app, 1)
    assert any("動作中" in b for b in plan.blockers)


def test_finish_push_and_create_pr(env):
    path = checkpointed(env, 1)
    commit_in(path)
    plan = service.plan_finish(env.app, 1)
    assert plan.blockers == [] and plan.ahead_of_base == 1 and plan.needs_push and plan.can_create_pr
    with pytest.raises(SafetyError, match="push"):
        service.execute_finish(env.app, plan, push=False, create_pr=True)
    res = service.execute_finish(env.app, plan, push=True, create_pr=True)
    assert res.pushed and res.pr_created
    branch = plan.branch
    assert git(env.repo, "ls-remote", "origin", branch).strip()  # 実際に bare origin へ push された
    created = env.issues.created_pulls[0]
    assert (created["head"], created["base"], created["title"]) == (branch, "main", "Fix bug number 1")
    assert created["body"].startswith("Closes #1") and "done" in created["body"]
    row = service.get_managed(env.app, 1)
    assert row["state"] == "REVIEW"
    with env.app.db.read() as c:
        last = c.execute("SELECT * FROM state_history WHERE worktree_id=? ORDER BY id DESC", (row["id"],)).fetchone()
    assert last["to_state"] == "REVIEW" and f"PR #{res.pr.number}" in last["reason"]

    # 2 回目: 既存 PR を検出して作成しない
    plan2 = service.plan_finish(env.app, 1)
    assert plan2.pr is not None and plan2.pr.number == res.pr.number and not plan2.needs_push
    res2 = service.execute_finish(env.app, plan2, push=True, create_pr=True)
    assert not res2.pr_created and not res2.pushed and len(env.issues.created_pulls) == 1


def test_finish_ignores_merged_pr_of_reused_branch_name(env):
    path = checkpointed(env, 1)
    commit_in(path)
    old = pr(5, git(path, "branch", "--show-current").strip(), state="closed", merged=True)
    old.head_sha = "0" * 40  # 以前の作業の head。現在の HEAD とは異なる
    env.issues.pulls.append(old)
    assert service.plan_finish(env.app, 1).pr is None


def test_finish_keeps_merged_pr_of_current_head(env):
    path = checkpointed(env, 1)
    commit_in(path)
    merged = pr(5, git(path, "branch", "--show-current").strip(), state="closed", merged=True)
    merged.head_sha = git(path, "rev-parse", "HEAD").strip()
    env.issues.pulls.append(merged)
    assert service.plan_finish(env.app, 1).pr.number == 5


def test_finish_without_commits_cannot_create_pr(env):
    checkpointed(env, 1)
    plan = service.plan_finish(env.app, 1)
    assert plan.ahead_of_base == 0 and not plan.can_create_pr
    with pytest.raises(WtError, match="コミットが無い"):
        service.execute_finish(env.app, plan, push=True, create_pr=True)
    res = service.execute_finish(env.app, plan, push=False, create_pr=False)
    assert res.state == "REVIEW" and env.issues.created_pulls == []


def test_finish_refuses_done(env):
    checkpointed(env, 1)
    wid = service.get_managed(env.app, 1)["id"]
    with env.app.db.transaction() as c:
        env.app.db.set_state(c, wid, "DONE", "test")
    assert any("DONE" in b for b in service.plan_finish(env.app, 1).blockers)


def test_cli_finish(env, monkeypatch):
    monkeypatch.setattr(cli, "_app", lambda: env.app)
    path = checkpointed(env, 1)
    commit_in(path)
    r = runner.invoke(cli.app, ["finish", "1", "--dry-run"])
    assert r.exit_code == 0, r.output
    assert service.get_managed(env.app, 1)["state"] == "PLANNED"
    # 確認で PR 作成を断ると REVIEW 遷移のみ
    r = runner.invoke(cli.app, ["finish", "1"], input="n\n")
    assert r.exit_code == 0, r.output
    assert service.get_managed(env.app, 1)["state"] == "REVIEW" and env.issues.created_pulls == []
    r = runner.invoke(cli.app, ["finish", "1", "--create-pr", "--yes"])
    assert r.exit_code == 1 and "--push" in r.output
    r = runner.invoke(cli.app, ["finish", "1", "--push", "--create-pr", "--yes"])
    assert r.exit_code == 0, r.output
    assert len(env.issues.created_pulls) == 1


# --- プロセス間ロック -------------------------------------------------------
def test_repo_lock_excludes_other_process(tmp_path):
    lock_path = tmp_path / "wt.lock"
    script = (
        "import sys, time; sys.path.insert(0, sys.argv[2]);"
        "from pathlib import Path; from wt.locks import repo_lock;"
        "lock = repo_lock(Path(sys.argv[1]));"
        "lock.__enter__(); print('locked', flush=True); time.sleep(2); lock.__exit__(None, None, None)"
    )
    src = str(Path(__file__).resolve().parents[1] / "src")
    proc = subprocess.Popen([sys.executable, "-c", script, str(lock_path), src], stdout=subprocess.PIPE, text=True)
    assert proc.stdout.readline().strip() == "locked"
    t0 = time.monotonic()
    with repo_lock(lock_path):
        waited = time.monotonic() - t0
    proc.wait(timeout=10)
    assert waited > 1.0
    with repo_lock(lock_path), repo_lock(lock_path):  # 同一スレッド内は再入可能
        pass
