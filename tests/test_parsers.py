from pathlib import Path

from wt.config import Config, RepositoryConfig, load_config
from wt.gitea import issue_from_api
from wt.gitops import parse_status_porcelain_z, parse_worktree_porcelain_z
from wt.service import make_slug

import pytest

from wt.errors import IssueFetchError


def test_parse_worktree_porcelain_z(tmp_path):
    a, b = tmp_path / "main", tmp_path / "wt dir" / "issue-1"
    out = (
        f"worktree {a.as_posix()}\0HEAD abc\0branch refs/heads/main\0\0"
        f"worktree {b.as_posix()}\0HEAD def\0branch refs/heads/feature/1-x\0locked because\0\0"
        f"worktree {(tmp_path / 'gone').as_posix()}\0HEAD 123\0detached\0prunable gitdir file points to non-existent location\0\0"
    )
    wts = parse_worktree_porcelain_z(out)
    assert [w.branch for w in wts] == ["main", "feature/1-x", None]
    assert wts[1].locked and wts[1].locked_reason == "because"
    assert wts[1].path == b.resolve()
    assert wts[2].detached and wts[2].prunable


def test_parse_status_porcelain_z():
    out = "M  staged.txt\0 M mod.txt\0?? new file.txt\0R  new.txt\0old.txt\0UU conflict.txt\0"
    st = parse_status_porcelain_z(out)
    assert st.staged == ["staged.txt", "new.txt"]
    assert st.modified == ["mod.txt"]
    assert st.untracked == ["new file.txt"]
    assert st.conflicted == ["conflict.txt"]
    assert st.dirty and st.has_untracked and not st.clean


def test_make_slug():
    assert make_slug("Fix login bug!") == "fix-login-bug"
    assert make_slug("ログイン不具合") == "task"
    assert len(make_slug("a" * 100)) == 30


def test_issue_from_api():
    iss = issue_from_api({"number": 5, "title": "T", "body": None, "state": "open", "html_url": "u"}, 5)
    assert (iss.number, iss.title, iss.body, iss.is_open) == (5, "T", "", True)
    with pytest.raises(IssueFetchError):
        issue_from_api({"message": "issue does not exist"}, 5)
    with pytest.raises(IssueFetchError):
        issue_from_api({"number": 5, "title": "PR", "pull_request": {"merged": False}}, 5)


def test_config_roundtrip(tmp_path):
    p = tmp_path / "日本語 dir" / "config.toml"
    cfg = Config(config_path=p, repository=RepositoryConfig(path=tmp_path / "main", base_branch="develop",
                                                             worktree_root=tmp_path / "worktrees"))
    p.parent.mkdir()
    p.write_text(cfg.to_toml(), encoding="utf-8")
    loaded = load_config(p)
    assert loaded.repository.base_branch == "develop"
    assert loaded.repository.worktree_root == (tmp_path / "worktrees").resolve()
    assert loaded.agent_command("claude") == ["claude"]
    assert Path(loaded.db_path).name == "wt.db"


@pytest.mark.parametrize("url,expected", [
    ("ssh://git@gitea.local:2222/kzogura/wt-test.git", "kzogura/wt-test"),
    ("git@gitea.local:kzogura/wt-test.git", "kzogura/wt-test"),
    ("http://gitea.local:3000/kzogura/wt-test.git", "kzogura/wt-test"),
    ("https://example.com/gitea/org/repo", "org/repo"),
    ("C:/work/origin.git", None),
    ("/srv/git/origin.git", None),
    ("", None),
])
def test_repo_slug_from_url(url, expected):
    from wt.gitea import repo_slug_from_url

    assert repo_slug_from_url(url) == expected


def test_unresolved_owner_gives_hint():
    with pytest.raises(IssueFetchError) as ei:
        issue_from_api({"message": "GetUserByName", "errors": ["user redirect does not exist [name: issues]"]}, 1)
    assert "[forge].repo" in (ei.value.hint or "")


def test_slug_from_name():
    from wt.service import slug_from_name

    assert slug_from_name("wt-{issue}-{slug}", 1, "wt-1-trial") == "trial"
    assert slug_from_name("feature/{issue}-{slug}", 12, "feature/12-a-b") == "a-b"
    assert slug_from_name("wt-{issue}-{slug}", 1, "wt-10-x") is None
    assert slug_from_name("wt-{issue}", 1, "wt-1") is None


@pytest.mark.parametrize("branch,worktree,ok", [
    ("wt-{issue}-{slug}", "wt-{issue}-{slug}", True),
    ("feature/{issue}-{slug}", "issue-{issue}", True),
    ("wt-{slug}", "wt-{issue}", False),          # {issue} 必須
    ("wt-{issue}-{user}", "wt-{issue}", False),  # 未対応プレースホルダ
    ("wt-{issue}", "a/{issue}", False),          # パス区切り
])
def test_validate_naming(branch, worktree, ok):
    from wt.config import ConfigError, NamingConfig, validate_naming

    if ok:
        validate_naming(NamingConfig(branch, worktree))
    else:
        with pytest.raises(ConfigError):
            validate_naming(NamingConfig(branch, worktree))
