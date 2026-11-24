"""GitHub（gh）対応と [forge] 設定のテスト。gh は run() のモックで置き換え、実環境には触れない。"""

from __future__ import annotations

import json

import pytest

from wt import github, service
from wt.config import ForgeConfig, load_config
from wt.errors import ConfigError, ForgeError, IssueFetchError
from wt.forge import host_from_url, pull_from_api
from wt.github import GhClient
from wt.gitea import TeaClient
from wt.gitops import Git
from wt.proc import CmdResult


class FakeRun:
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.calls: list[dict] = []

    def __call__(self, args, cwd=None, *, check=False, timeout=None, env=None, input=None):
        self.calls.append({"args": args, "input": input})
        out = self.outputs.pop(0) if self.outputs else ""
        return CmdResult(list(args), 0, out, "")


@pytest.fixture
def gh(tmp_path, monkeypatch):
    def make(*outputs, host=""):
        fake = FakeRun(outputs)
        monkeypatch.setattr(github, "run", fake)
        return GhClient("gh", tmp_path, host=host, repo_slug="o/r"), fake
    return make


# --- GhClient --------------------------------------------------------------
def test_gh_get_issue(gh):
    client, fake = gh(json.dumps({"number": 5, "title": "T", "body": None, "state": "open",
                                  "html_url": "https://github.com/o/r/issues/5"}))
    issue = client.get_issue(5)
    assert (issue.number, issue.title, issue.body, issue.source) == (5, "T", "", "github")
    assert fake.calls[0]["args"] == ["gh", "api", "repos/o/r/issues/5"]


def test_gh_get_issue_rejects_pull_request(gh):
    client, _ = gh(json.dumps({"number": 5, "title": "T", "pull_request": {"url": "x"}}))
    with pytest.raises(IssueFetchError, match="Pull Request"):
        client.get_issue(5)


def test_gh_enterprise_host(gh):
    client, fake = gh(json.dumps({"number": 1, "title": "T"}), host="ghe.example.com")
    client.get_issue(1)
    assert fake.calls[0]["args"] == ["gh", "api", "--hostname", "ghe.example.com", "repos/o/r/issues/1"]


def test_gh_create_issue_posts_json_on_stdin(gh):
    client, fake = gh(json.dumps({"number": 42, "title": "新機能", "state": "open", "html_url": "u"}))
    assert client.create_issue("新機能", "本文\n改行").number == 42
    call = fake.calls[0]
    assert call["args"] == ["gh", "api", "-X", "POST", "--input", "-", "repos/o/r/issues"]
    assert json.loads(call["input"]) == {"title": "新機能", "body": "本文\n改行"}


def test_gh_create_issue_error(gh):
    client, _ = gh(json.dumps({"message": "Validation Failed"}))
    with pytest.raises(ForgeError, match="Validation Failed"):
        client.create_issue("t", "")


def test_gh_add_comment(gh):
    client, fake = gh(json.dumps({"id": 1}))
    client.add_comment(7, "## 進捗")
    assert fake.calls[0]["args"] == ["gh", "api", "-X", "POST", "--input", "-", "repos/o/r/issues/7/comments"]
    assert json.loads(fake.calls[0]["input"]) == {"body": "## 進捗"}


def test_gh_find_pulls_uses_head_filter_and_merged_at(gh):
    data = [{"number": 3, "title": "t", "state": "closed", "merged_at": "2026-01-01T00:00:00Z",
             "html_url": "", "head": {"ref": "wt-1-x", "sha": "abc"}, "base": {"ref": "main"}},
            {"number": 9, "title": "t", "state": "open", "merged_at": None, "draft": True,
             "html_url": "", "head": {"ref": "wt-1-x"}, "base": {"ref": "main"}}]
    client, fake = gh(json.dumps(data))
    pulls = client.find_pulls("wt-1-x")
    assert [(p.number, p.status) for p in pulls] == [(9, "draft"), (3, "merged")]
    endpoint = fake.calls[0]["args"][-1]
    assert endpoint.startswith("repos/o/r/pulls?") and "state=all" in endpoint and "head=o%3Awt-1-x" in endpoint


def test_gh_create_pull_returns_response(gh):
    client, fake = gh(json.dumps({"number": 11, "title": "T", "state": "open", "html_url": "u",
                                  "head": {"ref": "wt-1-x"}, "base": {"ref": "main"}, "draft": True}))
    pr = client.create_pull("wt-1-x", "main", "T", "Closes #1", draft=True)
    assert (pr.number, pr.status) == (11, "draft")
    assert json.loads(fake.calls[0]["input"]) == {"head": "wt-1-x", "base": "main", "title": "T",
                                                  "body": "Closes #1", "draft": True}


def test_gh_auth_hosts(gh):
    client, _ = gh(json.dumps({"hosts": {"github.com": [
        {"state": "success", "active": False, "login": "b"},
        {"state": "success", "active": True, "login": "a"},
        {"state": "error", "active": False, "login": "c"}]}}))
    assert client.auth_hosts() == {"github.com": ["a", "b"]}


def test_gh_requires_repo_slug(tmp_path):
    with pytest.raises(IssueFetchError, match="owner/name"):
        GhClient("gh", tmp_path).get_issue(1)


def test_pull_from_api_gitea_merged_flag_still_works():
    assert pull_from_api({"number": 1, "state": "closed", "merged": True}).status == "merged"


@pytest.mark.parametrize("url, host", [
    ("https://github.com/o/r.git", "github.com"),
    ("git@github.com:o/r.git", "github.com"),
    ("ssh://git@gitea.local:2222/o/r.git", "gitea.local"),
    ("https://user@ghe.example.com/o/r", "ghe.example.com"),
    ("C:/repos/r", None),
])
def test_host_from_url(url, host):
    assert host_from_url(url) == host


# --- [forge] 設定 ------------------------------------------------------------
def test_forge_config_defaults_to_github():
    f = ForgeConfig()
    assert (f.provider, f.cli, f.label) == ("github", "gh", "GitHub")
    assert ForgeConfig(provider="gitea").cli == "tea"


def test_forge_config_rejects_unknown_provider():
    with pytest.raises(ConfigError, match="provider"):
        ForgeConfig(provider="bitbucket")


def test_legacy_gitea_section_is_read_as_gitea(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('[repository]\npath = "C:/x/main"\n\n[gitea]\nenabled = true\ncli = "tea"\n'
                 'login = "me"\nrepo = "o/r"\n', encoding="utf-8")
    cfg = load_config(p)
    assert (cfg.forge.provider, cfg.forge.login, cfg.forge.repo) == ("gitea", "me", "o/r")
    assert "[forge]" in cfg.to_toml() and 'provider = "gitea"' in cfg.to_toml()


def test_make_forge_client_by_provider(env):
    assert isinstance(service.make_forge_client(env.app.config), TeaClient)
    env.app.config.forge = ForgeConfig(provider="github", host="ghe.example.com")
    client = service.make_forge_client(env.app.config)
    assert isinstance(client, GhClient) and client.host == "ghe.example.com"


# --- init --------------------------------------------------------------------
def test_init_defaults_to_github(repo):
    g = Git()
    plan = service.plan_init(repo, g)
    assert (plan.forge_provider, plan.forge_cli) == ("github", "gh")
    cfg = service.execute_init(plan, g, base_branch="main", forge_enabled=False)
    assert load_config(cfg.config_path).forge.provider == "github"


def test_init_github_warns_on_non_github_remote(repo):
    from tests.conftest import git

    git(repo, "remote", "set-url", "origin", "ssh://git@gitea.local:2222/o/r.git")
    plan = service.plan_init(repo, Git())
    assert any("--forge gitea" in w for w in plan.warnings)


def test_init_github_accepts_explicit_enterprise_host(repo):
    plan = service.plan_init(repo, Git(), forge_host="ghe.example.com")
    assert plan.forge_host == "ghe.example.com"
    assert not any("github.com ではありません" in w for w in plan.warnings)
