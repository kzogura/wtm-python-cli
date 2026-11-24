from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from wt import service
from wt.errors import GiteaError, IssueFetchError
from wt.gitea import Issue, PullRequest
from wt.gitops import Git


def git(cwd: Path, *args: str) -> str:
    cp = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8", check=True)
    return cp.stdout


@pytest.fixture(autouse=True)
def isolated_git(tmp_path_factory, monkeypatch):
    """ユーザーのグローバル Git 設定や WT_CONFIG の影響を受けないようにする。"""
    home = tmp_path_factory.mktemp("home")
    gitconfig = home / ".gitconfig"
    gitconfig.write_text(
        "[user]\n\tname = wt-test\n\temail = wt-test@example.invalid\n"
        "[init]\n\tdefaultBranch = main\n[core]\n\tautocrlf = false\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gitconfig))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.delenv("WT_CONFIG", raising=False)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home / ".claude"))


class FakeGitea:
    """GiteaClient のテスト用実装。実環境の Gitea には一切アクセスしない。"""

    def __init__(self, issues: dict[int, Issue] | None = None) -> None:
        self.issues = issues or {}
        self.calls: list[int] = []
        self.pulls: list[PullRequest] = []
        self.comments: list[tuple[int, str]] = []
        self.created_pulls: list[dict] = []
        self.fail_pulls = False

    def get_issue(self, number: int) -> Issue:
        self.calls.append(number)
        if number not in self.issues:
            raise IssueFetchError(f"Issue #{number} を取得できません: not found")
        return self.issues[number]

    def create_issue(self, title: str, body: str) -> Issue:
        number = max([0, *self.issues]) + 1
        issue = Issue(number, title, body, "open", f"https://gitea.invalid/o/r/issues/{number}")
        self.issues[number] = issue
        return issue

    def add_comment(self, number: int, body: str) -> None:
        if number not in self.issues:
            raise GiteaError(f"Issue #{number} がありません")
        self.comments.append((number, body))

    def find_pulls(self, branch: str) -> list[PullRequest]:
        if self.fail_pulls:
            raise IssueFetchError("connection refused")
        return sorted((p for p in self.pulls if p.head == branch), key=lambda p: p.number, reverse=True)

    def create_pull(self, head: str, base: str, title: str, body: str, draft: bool = False) -> PullRequest:
        number = 1000 + len(self.pulls)
        pr = PullRequest(number, title, "open", False, f"https://gitea.invalid/o/r/pulls/{number}", head, base, draft)
        self.pulls.append(pr)
        self.created_pulls.append({"head": head, "base": base, "title": title, "body": body, "draft": draft})
        return pr


FakeIssues = FakeGitea


@pytest.fixture
def workspace(tmp_path) -> Path:
    # Windows の日本語・空白を含むパスを常に使う
    ws = tmp_path / "ワーク スペース"
    ws.mkdir()
    return ws


@pytest.fixture
def repo(workspace) -> Path:
    """origin (bare) を持ち、main に 1 コミットあるリポジトリ。"""
    origin = workspace / "origin.git"
    git(workspace, "init", "--bare", str(origin))
    main = workspace / "main"
    main.mkdir()
    git(main, "init")
    (main / "README.md").write_text("hello\n", encoding="utf-8")
    git(main, "add", "README.md")
    git(main, "commit", "-m", "init")
    git(main, "remote", "add", "origin", str(origin))
    git(main, "push", "-u", "origin", "main")
    return main


@dataclass
class Env:
    app: service.App
    issues: FakeIssues
    repo: Path
    workspace: Path


def make_issues(*numbers: int, state: str = "open") -> FakeIssues:
    return FakeIssues({n: Issue(n, f"Fix bug number {n}", f"本文 {n}\n\n- [ ] 完了条件", state,
                                f"https://gitea.invalid/o/r/issues/{n}") for n in numbers})


@pytest.fixture
def env(repo, workspace) -> Env:
    g = Git()
    plan = service.plan_init(repo, g, provider="gitea")
    cfg = service.execute_init(plan, g, base_branch="main", forge_enabled=True, forge_login="test")
    issues = make_issues(*range(1, 21), 101, 102, 103)
    app = service.open_app(None, repo, g, gitea_factory=lambda _c: issues)
    assert app.config.config_path == cfg.config_path
    return Env(app, issues, repo, workspace)


def start(env: Env, number: int, **kw) -> service.StartResult:
    issue = service.resolve_issue(env.app, number, offline=False, title=None)
    plan, _ = service.plan_start(env.app, issue, **kw)
    return service.execute_start(env.app, plan, agent=None)
