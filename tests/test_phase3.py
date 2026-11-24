"""Phase 3: AI エージェント連携（引き継ぎ・Checkpoint 取り込み・resume・review）。

AI CLI は引数と cwd を記録するだけの Python スクリプトに差し替える。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from wt import cli, review, service
from wt.checkpoint_file import load_checkpoint_file
from wt.errors import ConfigError, ConflictError, UnsupportedOperationError, WtError
from wt.gitea import Issue, PullRequest
from wt.handoff import MARKER, handoff_path

from .conftest import git, start

runner = CliRunner()


class FakeAgent:
    """起動時の引数と cwd を JSON Lines に記録して、指定の終了コードで終わるコマンド。"""

    def __init__(self, env, name: str = "claude", rc: int = 0, writes: dict[str, str] | None = None) -> None:
        self.log = env.workspace / f"{name}-log-{uuid.uuid4().hex}.jsonl"
        script = env.workspace / f"{name}-{uuid.uuid4().hex}.py"
        script.write_text(
            "import json,os,pathlib,sys\n"
            f"log = pathlib.Path({str(self.log)!r})\n"
            "with log.open('a', encoding='utf-8') as f:\n"
            "    f.write(json.dumps({'argv': sys.argv[1:], 'cwd': os.getcwd()}) + '\\n')\n"
            f"for name, text in {writes or {}!r}.items():\n"
            "    p = pathlib.Path(name); p.parent.mkdir(exist_ok=True); p.write_text(text, encoding='utf-8')\n"
            f"sys.exit({rc})\n",
            encoding="utf-8",
        )
        env.app.config.agents.commands[name] = [sys.executable, str(script)]

    @property
    def calls(self) -> list[dict]:
        if not self.log.exists():
            return []
        return [json.loads(x) for x in self.log.read_text(encoding="utf-8").splitlines()]


def wid(env, issue: int = 1) -> int:
    return service.get_managed(env.app, issue)["id"]


def sessions(env, issue: int = 1) -> list[str]:
    with env.app.db.read() as c:
        return [s["session_id"] for s in env.app.db.list_sessions(c, wid(env, issue))]


def checkpoint_count(env) -> int:
    with env.app.db.read() as c:
        return c.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0]


def commit(path: Path, name: str, text: str = "x") -> None:
    (path / name).parent.mkdir(parents=True, exist_ok=True)
    (path / name).write_text(text, encoding="utf-8")
    git(path, "add", "--", name)
    git(path, "commit", "-m", f"add {name}")


@pytest.fixture
def cli_env(env, monkeypatch):
    monkeypatch.setattr(cli, "_app", lambda: env.app)
    return env


# --- 起動と引き継ぎ -----------------------------------------------------------
def test_claude_launch_assigns_session_and_passes_handoff(env):
    path = start(env, 1).plan.path
    agent = FakeAgent(env)
    res = service.launch_agent(env.app, 1, "claude")
    (call,) = agent.calls
    assert Path(call["cwd"]).resolve() == path.resolve()
    assert call["argv"][0] == "--session-id" and str(uuid.UUID(call["argv"][1])) == res.session_id
    assert call["argv"][2:] == ["--append-system-prompt-file", str(handoff_path(path))]
    assert sessions(env) == [res.session_id]
    with env.app.db.read() as c:
        run = c.execute("SELECT * FROM agent_runs").fetchone()
    assert (run["session_id"], run["status"]) == (res.session_id, "exited")
    assert res.instructions_passed and not res.resumed


def test_codex_launch_passes_no_options_but_writes_handoff(env):
    path = start(env, 1).plan.path
    agent = FakeAgent(env, "codex")
    res = service.launch_agent(env.app, 1, "codex")
    assert agent.calls[0]["argv"] == []
    assert not res.instructions_passed and res.session_id is None
    assert handoff_path(path).is_file() and sessions(env) == []


def test_handoff_contents_and_git_exclusion(env):
    res = start(env, 1)
    path = res.plan.path
    env.issues.issues[1] = Issue(1, "ログイン修正", "本文 {issue} はそのまま\n- [ ] 完了条件A", "open", "https://x/1")
    service.create_checkpoint(env.app, wid(env), completed="API", remaining="テスト", next_action="UI",
                              blockers="承認待ち", session_id="secret-session-id")
    target, text, _ = service.create_handoff(env.app, 1)
    assert target == handoff_path(path) and target.read_text(encoding="utf-8") == text
    assert text.startswith(MARKER)
    assert "Issue #1 専用" in text and "本文 {issue} はそのまま" in text and "ログイン修正" in text
    for value in ("API", "テスト", "UI", "承認待ち"):
        assert value in text
    assert "secret-session-id" not in text  # セッション ID は渡さない
    assert ".wtm/checkpoint.md" in text and "TASK.md" in text
    assert env.app.git.status(path).clean  # .wtm/ も TASK.md も未追跡として現れない


def test_handoff_does_not_touch_claude_md_or_foreign_file(env):
    path = start(env, 1).plan.path
    (path / "CLAUDE.md").write_text("existing", encoding="utf-8")
    service.create_handoff(env.app, 1)
    assert (path / "CLAUDE.md").read_text(encoding="utf-8") == "existing"
    service.create_handoff(env.app, 1)  # 自分が生成したものは再生成できる

    handoff_path(path).write_text("ユーザーのメモ", encoding="utf-8")
    with pytest.raises(ConflictError, match="上書きしません"):
        service.create_handoff(env.app, 1)
    assert handoff_path(path).read_text(encoding="utf-8") == "ユーザーのメモ"


def test_handoff_failure_marks_run_failed_and_does_not_launch(env):
    path = start(env, 1).plan.path
    agent = FakeAgent(env)
    (path / ".wtm").write_text("file, not directory", encoding="utf-8")
    with pytest.raises(ConflictError):
        service.launch_agent(env.app, 1, "claude")
    assert agent.calls == []
    with env.app.db.read() as c:
        assert c.execute("SELECT status FROM agent_runs").fetchone()["status"] == "failed"


def test_custom_instructions_template(env):
    start(env, 1)
    (env.app.config.management_dir / "agent-instructions.md").write_text(
        "独自ルール #{issue} {task_file} {checkpoint_file} {unknown}", encoding="utf-8")
    _, text, _ = service.create_handoff(env.app, 1)
    assert "独自ルール #1 TASK.md .wtm/checkpoint.md {unknown}" in text
    assert "wtm 共通指示" not in text


def test_cli_handoff_stdout(cli_env):
    start(cli_env, 1)
    r = runner.invoke(cli.app, ["handoff", "1", "--offline", "--stdout"])
    assert r.exit_code == 0 and MARKER in r.output


# --- Checkpoint ファイル ------------------------------------------------------
def test_parse_markdown_checkpoint(tmp_path):
    f = tmp_path / "c.md"
    f.write_text(
        "\ufeff# タイトル\n\n## Completed\n- a\n\n### 詳細\nb\n\n```\n## Remaining\n```\n\n"
        "## 未完了事項\nc\n\n## メモ\n無視される\n\n## Next Action:\nd\n",
        encoding="utf-8",
    )
    values, warnings = load_checkpoint_file(f)
    assert values["completed"] == "- a\n\n### 詳細\nb\n\n```\n## Remaining\n```"
    assert values["remaining"] == "c" and values["next_action"] == "d" and values["blockers"] == ""
    assert warnings == ["未対応の見出しを無視しました: メモ"]


def test_parse_json_checkpoint(tmp_path):
    f = tmp_path / "c.json"
    f.write_text(json.dumps({"completed": ["a", "b"], "next_action": "c", "blockers": None, "extra": 1}), encoding="utf-8")
    values, warnings = load_checkpoint_file(f)
    assert values == {"completed": "- a\n- b", "remaining": "", "next_action": "c", "blockers": ""}
    assert warnings == ["未対応の項目を無視しました: extra"]


@pytest.mark.parametrize(("name", "content", "message"), [
    ("a.md", "本文だけ", "見出し"),
    ("a.md", "## Completed\n\n## Remaining\n", "内容がありません"),
    ("a.md", "## Completed\nx\n## 完了事項\ny\n", "重複"),
    ("a.json", "{", "JSON が不正"),
    ("a.json", "[1]", "オブジェクト"),
    ("a.json", '{"completed": 1}', "文字列"),
])
def test_invalid_checkpoint_files(tmp_path, name, content, message):
    f = tmp_path / name
    f.write_text(content, encoding="utf-8")
    with pytest.raises(WtError, match=message):
        load_checkpoint_file(f)


def test_checkpoint_file_size_encoding_and_missing(tmp_path):
    big = tmp_path / "big.md"
    big.write_bytes(b"x" * (1024 * 1024 + 1))
    with pytest.raises(WtError, match="大きすぎ"):
        load_checkpoint_file(big)
    sjis = tmp_path / "sjis.md"
    sjis.write_bytes("## 完了事項\nあ".encode("cp932"))
    with pytest.raises(WtError, match="UTF-8"):
        load_checkpoint_file(sjis)
    with pytest.raises(WtError, match="読み込めません"):
        load_checkpoint_file(tmp_path / "missing.md")


def test_cli_checkpoint_from_file(cli_env, tmp_path):
    start(cli_env, 1)
    f = tmp_path / "cp.md"
    f.write_text("## Completed\n実装\n## Blockers\n待ち\n", encoding="utf-8")
    r = runner.invoke(cli.app, ["checkpoint", "1", "--from-file", str(f)])
    assert r.exit_code == 0, r.output
    with cli_env.app.db.read() as c:
        cp = cli_env.app.db.latest_checkpoint(c, wid(cli_env))
    assert (cp["completed"], cp["blockers"], cp["remaining"]) == ("実装", "待ち", "")
    assert str(f) in json.loads(Path(cp["file_path"]).read_text(encoding="utf-8"))["note"]
    assert f.exists()  # 元のファイルは残す

    r = runner.invoke(cli.app, ["checkpoint", "1", "--from-file", str(f), "--completed", "x"])
    assert r.exit_code != 0 and checkpoint_count(cli_env) == 1


def test_checkpoint_records_latest_session_but_comment_omits_it(env):
    start(env, 1)
    FakeAgent(env)
    res = service.launch_agent(env.app, 1, "claude")
    cp_id = service.create_checkpoint(env.app, wid(env), completed="c", remaining="", next_action="", blockers="")
    cp = service.get_checkpoint(env.app, cp_id)
    assert cp["session_id"] == res.session_id
    assert res.session_id not in service.format_checkpoint_comment(cp, "b")


# --- auto_checkpoint ---------------------------------------------------------
def test_new_checkpoint_file_is_only_suggested_by_default(env):
    start(env, 1)
    FakeAgent(env, writes={".wtm/checkpoint.md": "## Completed\nx\n"})
    res = service.launch_agent(env.app, 1, "claude")
    assert checkpoint_count(env) == 0 and res.checkpoint_id is None
    assert any("--from-file" in n for n in res.notes)


def test_auto_checkpoint_imports_new_file(env):
    start(env, 1)
    env.app.config.management.auto_checkpoint = True
    FakeAgent(env, writes={".wtm/checkpoint.md": "## Remaining\n残り\n"})
    res = service.launch_agent(env.app, 1, "claude")
    assert service.get_checkpoint(env.app, res.checkpoint_id)["remaining"] == "残り"


def test_auto_checkpoint_ignores_old_file_and_records_metadata(env):
    path = start(env, 1).plan.path
    env.app.config.management.auto_checkpoint = True
    old = path / ".wtm" / "checkpoint.md"
    old.parent.mkdir()
    old.write_text("## Remaining\n古い内容\n", encoding="utf-8")
    os.utime(old, (1_000_000, 1_000_000))
    FakeAgent(env)
    res = service.launch_agent(env.app, 1, "claude")
    assert service.get_checkpoint(env.app, res.checkpoint_id)["remaining"] == ""


def test_auto_checkpoint_broken_file_keeps_result(env):
    start(env, 1)
    env.app.config.management.auto_checkpoint = True
    FakeAgent(env, rc=0, writes={".wtm/checkpoint.md": "見出しなし"})
    res = service.launch_agent(env.app, 1, "claude")
    assert res.rc == 0 and res.checkpoint_id is None and any("自動で記録できません" in n for n in res.notes)


# --- resume ------------------------------------------------------------------
def test_resume_uses_latest_session_of_agent(env):
    start(env, 1)
    FakeAgent(env)
    first = service.launch_agent(env.app, 1, "claude").session_id
    second = service.launch_agent(env.app, 1, "claude").session_id
    plan = service.plan_resume(env.app, 1)
    assert plan.agent == "claude" and plan.session_id == second != first

    agent = FakeAgent(env)
    res = service.launch_agent(env.app, 1, plan.agent, plan.session_id, status=plan.status)
    assert res.resumed and res.session_id == second
    assert agent.calls[0]["argv"][:2] == ["--resume", second]
    assert "--session-id" not in agent.calls[0]["argv"]
    assert "--append-system-prompt-file" in agent.calls[0]["argv"]

    assert service.plan_resume(env.app, 1, new_session=True).session_id is None


def test_resume_session_options_are_validated(env):
    start(env, 1)
    with pytest.raises(WtError, match="同時に"):
        service.plan_resume(env.app, 1, session="abc", new_session=True)
    with pytest.raises(WtError, match="形式が不正"):
        service.plan_resume(env.app, 1, session="--dangerous")
    with pytest.raises(UnsupportedOperationError):
        service.plan_resume(env.app, 1, agent="codex", session="abc")
    with pytest.raises(ConfigError, match="設定されていません"):
        service.plan_resume(env.app, 1, agent="unknown-agent")


def test_resume_new_session_for_agent_without_resume(env):
    start(env, 1)
    FakeAgent(env)
    service.launch_agent(env.app, 1, "claude")
    assert service.plan_resume(env.app, 1, agent="codex").session_id is None  # 他エージェントのセッションは使わない


def test_resume_warns_about_explicit_session_of_other_worktree(env):
    start(env, 1)
    start(env, 2)
    FakeAgent(env)
    other = service.launch_agent(env.app, 2, "claude").session_id
    plan = service.plan_resume(env.app, 1, session=other)
    assert any("Issue #2" in w for w in plan.warnings)


def test_failed_resume_is_detected_and_not_remembered(env):
    start(env, 1)
    FakeAgent(env)
    good = service.launch_agent(env.app, 1, "claude").session_id
    FakeAgent(env, rc=1)
    res = service.launch_agent(env.app, 1, "claude", "unknown-session")
    assert res.resume_failed and res.rc == 1
    assert sessions(env) == [good]
    assert service.plan_resume(env.app, 1).session_id == good


def test_long_session_with_error_exit_is_not_resume_failure(env, monkeypatch):
    start(env, 1)
    monkeypatch.setattr(service, "RESUME_FAILURE_WINDOW_SECONDS", 0.0)
    FakeAgent(env, rc=1)
    assert not service.launch_agent(env.app, 1, "claude", "some-session").resume_failed
    assert "some-session" in sessions(env)


def test_new_session_error_exit_is_not_resume_failure(env):
    start(env, 1)
    FakeAgent(env, rc=1)
    assert not service.launch_agent(env.app, 1, "claude").resume_failed


def test_cli_resume_falls_back_to_new_session(cli_env):
    start(cli_env, 1)
    FakeAgent(cli_env)
    service.launch_agent(cli_env.app, 1, "claude")
    failing = FakeAgent(cli_env, rc=1)
    r = runner.invoke(cli.app, ["resume", "1", "--offline", "--agent", "claude"], input="y\n")
    assert r.exit_code == 0, r.output
    first, second = failing.calls
    assert first["argv"][0] == "--resume" and second["argv"][0] == "--session-id"
    assert "--append-system-prompt-file" in second["argv"]


def test_cli_resume_fallback_can_be_declined(cli_env):
    start(cli_env, 1)
    FakeAgent(cli_env)
    service.launch_agent(cli_env.app, 1, "claude")
    failing = FakeAgent(cli_env, rc=1)
    r = runner.invoke(cli.app, ["resume", "1", "--offline"], input="n\n")
    assert r.exit_code == 1 and len(failing.calls) == 1


def test_cli_resume_dry_run_does_not_launch(cli_env):
    start(cli_env, 1)
    agent = FakeAgent(cli_env)
    r = runner.invoke(cli.app, ["resume", "1", "--offline", "--dry-run", "--agent", "claude"])
    assert r.exit_code == 0, r.output
    assert agent.calls == []


def test_resume_from_review_returns_to_working(env):
    start(env, 1)
    with env.app.db.transaction() as c:
        env.app.db.set_state(c, wid(env), "REVIEW", "test")
    plan = service.plan_resume(env.app, 1, agent="claude")
    assert any("REVIEW" in w for w in plan.warnings)
    FakeAgent(env)
    service.launch_agent(env.app, 1, "claude", status=plan.status)
    assert service.get_managed(env.app, 1)["state"] == "WORKING"


def test_resume_warns_closed_issue_and_merged_pr(env):
    res = start(env, 1)
    env.issues.issues[1] = Issue(1, "t", "", "closed", "")
    env.issues.pulls.append(PullRequest(5, "t", "closed", True, "u", res.plan.branch, "main", False))
    text = " ".join(service.plan_resume(env.app, 1, agent="claude").warnings)
    assert "クローズ済み" in text and "マージ済み" in text


@pytest.mark.parametrize("keep_registration", [False, True])
def test_resume_missing_worktree_shows_working_restore_steps(env, keep_registration):
    path = start(env, 1).plan.path
    agent = FakeAgent(env)
    if keep_registration:  # ディレクトリだけ消えて Git の登録が残っている場合
        shutil.rmtree(path)
    else:
        git(env.repo, "worktree", "remove", str(path))
    with pytest.raises(WtError, match="存在しません") as e:
        service.plan_resume(env.app, 1, agent="claude")
    assert not path.exists() and agent.calls == []  # 勝手に再作成しない
    assert "worktree add" in e.value.hint and ("prune" in e.value.hint) == keep_registration
    for step in service.restore_steps(env.app, service.get_managed(env.app, 1)):
        subprocess.run(step, shell=True, check=True, capture_output=True)
    assert path.is_dir()
    assert service.plan_resume(env.app, 1, agent="claude").path == path


def test_restore_steps_when_branch_is_gone(env):
    res = start(env, 1)
    git(env.repo, "worktree", "remove", str(res.plan.path))
    git(env.repo, "branch", "-D", res.plan.branch)
    (step,) = service.restore_steps(env.app, service.get_managed(env.app, 1))
    assert "見つかりません" in step


def test_status_warning_for_missing_worktree_is_accurate(env):
    path = start(env, 1).plan.path
    git(env.repo, "worktree", "remove", str(path))
    text = " ".join(service.collect_status(env.app, 1)["warnings"])
    assert "worktree add" in text and "wtm start" not in text


# --- review ------------------------------------------------------------------
def rules(report: dict) -> set[str]:
    return {f["rule"] for f in report["findings"]}


def test_parse_checklist_ignores_code_blocks():
    body = "- [x] a\n* [ ] b\n```\n- [ ] code\n```\n  + [X] c\n- 通常の項目\n"
    assert review.parse_checklist(body) == (["a", "c"], ["b"])


def test_review_basic_report(env):
    path = start(env, 1).plan.path
    commit(path, "src/a.txt")
    (path / "wip.txt").write_text("x", encoding="utf-8")
    report = review.collect_review(env.app, 1)
    assert report["schema_version"] == 1
    assert {"definition_of_done_incomplete", "checkpoint_missing", "uncommitted_changes"} <= rules(report)
    assert report["definition_of_done"]["todo"] == ["完了条件"]
    assert [c["subject"] for c in report["git"]["commits"]] == ["add src/a.txt"]
    assert report["git"]["changed_files"] == ["src/a.txt", "wip.txt"]
    assert report["git"]["uncommitted"] == ["wip.txt"]
    assert report["recommendation"]["action"] == "continue"
    json.dumps(report)  # JSON 化できること


def test_review_does_not_change_git_or_gitea(env):
    path = start(env, 1).plan.path
    head = git(path, "rev-parse", "HEAD")
    review.collect_review(env.app, 1)
    assert git(path, "rev-parse", "HEAD") == head and env.app.git.status(path).clean
    assert env.issues.comments == [] and env.issues.created_pulls == []


def test_review_stale_checkpoint_and_blockers(env):
    start(env, 1)
    service.create_checkpoint(env.app, wid(env), completed="", remaining="r", next_action="", blockers="承認待ち")
    fresh = review.collect_review(env.app, 1)
    assert "checkpoint_stale" not in rules(fresh) and "blockers_recorded" in rules(fresh)
    later = datetime.now(UTC) + timedelta(days=7, minutes=1)
    assert "checkpoint_stale" in rules(review.collect_review(env.app, 1, now=later))


def test_review_file_overlap_with_other_worktree(env):
    a = start(env, 1).plan.path
    b = start(env, 2).plan.path
    c = start(env, 3).plan.path
    commit(a, "shared.txt")
    (b / "shared.txt").write_text("uncommitted", encoding="utf-8")  # 未コミットの変更も対象
    commit(c, "other.txt")
    report = review.collect_review(env.app, 1)
    assert report["overlaps"] == [{"issue": 2, "path": str(b), "files": ["shared.txt"]}]
    assert "file_overlap" in rules(report) and report["recommendation"]["action"] == "split"


def test_review_large_change_and_divergence(env):
    path = start(env, 1).plan.path
    commit(path, "a.txt")
    commit(path, "b.txt")
    env.app.config.management.large_change_files = 2
    env.app.config.management.diverged_commits = 2
    report = review.collect_review(env.app, 1)
    assert {"large_change", "branch_diverged"} <= rules(report)
    assert report["recommendation"]["action"] == "split"


@pytest.mark.parametrize(("merged", "state", "rule"), [(True, "closed", "pr_merged"), (False, "closed", "pr_closed"),
                                                       (False, "open", "pr_exists")])
def test_review_pr_state(env, merged, state, rule):
    res = start(env, 1)
    env.issues.pulls.append(PullRequest(5, "t", state, merged, "u", res.plan.branch, "main", False))
    report = review.collect_review(env.app, 1)
    assert rule in rules(report)
    assert (report["recommendation"]["action"] == "finish") == merged


def test_review_recommends_finish_when_definition_of_done_complete(env):
    path = start(env, 1).plan.path
    env.issues.issues[1] = Issue(1, "t", "- [x] 完了", "open", "")
    assert review.collect_review(env.app, 1)["recommendation"]["action"] == "continue"  # コミットが無い
    commit(path, "a.txt")
    assert review.collect_review(env.app, 1)["recommendation"]["action"] == "finish"


def test_review_without_checklist_cannot_judge(env):
    start(env, 1)
    env.issues.issues[1] = Issue(1, "t", "説明だけ", "open", "")
    report = review.collect_review(env.app, 1)
    assert "definition_of_done_unknown" in rules(report)
    assert report["recommendation"]["action"] == "continue"


def test_review_offline_uses_cache_only(env):
    start(env, 1)
    env.issues.calls.clear()
    report = review.collect_review(env.app, 1, offline=True)
    assert env.issues.calls == [] and not report["pr_checked"]
    assert "未取得" in review.render_markdown(report)


def test_review_missing_worktree(env):
    path = start(env, 1).plan.path
    git(env.repo, "worktree", "remove", str(path))
    report = review.collect_review(env.app, 1)
    assert "worktree_missing" in rules(report) and not report["git"]["exists"]
    review.render_markdown(report)


def test_cli_review_formats_and_output(cli_env, tmp_path):
    start(cli_env, 1)
    r = runner.invoke(cli.app, ["review", "1", "--format", "json"])
    assert r.exit_code == 0 and json.loads(r.output)["issue_number"] == 1
    r = runner.invoke(cli.app, ["review", "1", "--format", "md"])
    assert r.exit_code == 0 and r.output.startswith("# wtm review: Issue #1")
    r = runner.invoke(cli.app, ["review", "1"])
    assert r.exit_code == 0 and "wtm review" in r.output
    r = runner.invoke(cli.app, ["review", "1", "--format", "xml"])
    assert r.exit_code != 0

    out = tmp_path / "レビュー 結果.md"
    r = runner.invoke(cli.app, ["review", "1", "--format", "md", "-o", str(out)])
    assert r.exit_code == 0 and out.read_text(encoding="utf-8").startswith("# wtm review")
    before = out.read_text(encoding="utf-8")
    r = runner.invoke(cli.app, ["review", "1", "--format", "json", "-o", str(out)])
    assert r.exit_code != 0 and out.read_text(encoding="utf-8") == before  # 上書きしない
