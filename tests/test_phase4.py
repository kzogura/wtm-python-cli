"""Phase 4: Claude セッションの検出・バックアップ・閲覧。

CLAUDE_CONFIG_DIR は conftest で一時ディレクトリに差し替えられ、実際の履歴には触れない。
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import uuid
from pathlib import Path

import pytest
from typer.testing import CliRunner

from wt import cli, service, sessions
from wt.agents import CodexAdapter, ClaudeAdapter
from wt.claude_store import (
    ClaudeSessionStore,
    UnsupportedSessionFormatError,
    claude_projects_dir,
    read_messages,
    read_summary,
)
from wt.errors import NotManagedError, UnsupportedOperationError, WtError

from .conftest import git, start

runner = CliRunner()


def user(text, ts="2026-11-01T10:00:00.000Z", **kw):
    return {"type": "user", "uuid": uuid.uuid4().hex, "timestamp": ts, "version": "2.1.318",
            "message": {"role": "user", "content": text}, **kw}


def assistant(blocks, ts="2026-11-01T10:01:00.000Z", **kw):
    return {"type": "assistant", "uuid": uuid.uuid4().hex, "timestamp": ts, "version": "2.1.318",
            "message": {"role": "assistant", "content": blocks}, **kw}


def write_session(cwd, session_id=None, records=None, project="any-directory-name"):
    """保存先にセッションを置く。ディレクトリ名の規則には依存しない。"""
    session_id = session_id or str(uuid.uuid4())
    directory = claude_projects_dir() / project
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{session_id}.jsonl"
    records = records if records is not None else [user("最初の指示"), assistant([{"type": "text", "text": "応答"}])]
    lines = [{"type": "mode", "mode": "x", "sessionId": session_id}]
    for r in records:
        lines.append({**r, "cwd": str(cwd), "sessionId": session_id} if isinstance(r, dict) else r)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        for line in lines:
            f.write((line if isinstance(line, str) else json.dumps(line, ensure_ascii=False)) + "\n")
    return session_id, path


def history(env, issue=1):
    return sessions.collect_history(env.app, issue)["sessions"]


def by_id(env, session_id, issue=1):
    return next(s for s in history(env, issue) if s["session_id"] == session_id)


def fingerprint(path: Path):
    return path.read_bytes(), path.stat().st_mtime_ns


def stored(env, session_id):
    with env.app.db.read() as c:
        return c.execute("SELECT * FROM agent_sessions WHERE session_id=?", (session_id,)).fetchall()


def archive(env, issue=1):
    wid = service.get_managed(env.app, issue)["id"]
    with env.app.db.transaction() as c:
        env.app.db.set_state(c, wid, "ARCHIVED", "test")


@pytest.fixture
def cli_env(env, monkeypatch):
    monkeypatch.setattr(cli, "_app", lambda: env.app)
    return env


# --- 互換性レイヤー ------------------------------------------------------------
def test_summary_reads_metadata(tmp_path):
    p = claude_projects_dir() / "p"
    p.mkdir(parents=True)
    f = p / "abc.jsonl"
    f.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in [
        user("新しい", ts="2026-11-02T00:00:00.000Z", cwd="C:/w"),
        user("古い", ts="2026-11-01T00:00:00.000Z"),
        {"type": "ai-title", "aiTitle": "仮"},
        {"type": "ai-title", "aiTitle": "最終タイトル\n2行目"},
    ]) + "\n", encoding="utf-8")
    s = read_summary(f)
    assert (s.session_id, s.cwd, s.title, s.version) == ("abc", "C:/w", "最終タイトル", "2.1.318")
    assert s.created_at == "2026-11-01T00:00:00+00:00" and s.updated_at == "2026-11-02T00:00:00+00:00"
    assert s.message_count == 2 and s.format == "ok"


def test_title_falls_back_to_first_user_message(env):
    _, path = write_session("C:/w", records=[user("x" * 100 + "\n続き")])
    assert read_summary(path).title == "x" * 60


def test_broken_lines_are_skipped(env):
    _, path = write_session("C:/w", records=[user("指示"), "{壊れた行", "[1,2]", assistant([{"type": "text", "text": "応答"}])])
    with open(path, "ab") as f:
        f.write(b"\xff\xfe invalid\n")
    summary, messages = read_messages(path)
    assert summary.skipped_lines == 3 and summary.format == "ok"
    assert [m.text for m in messages] == ["指示", "応答"]


def test_conversation_excludes_meta_sidechain_duplicates_and_thinking(env):
    dup = user("一度だけ")
    _, path = write_session("C:/w", records=[
        user("メタ", isMeta=True), user("サブ", isSidechain=True), dup, dup,
        assistant([{"type": "thinking", "thinking": "秘密の思考"}, {"type": "text", "text": "本文"}]),
        assistant([{"type": "tool_use", "name": "Read", "input": {"file_path": "a"}}]),
        user([{"type": "tool_result", "content": "結果"}]),
        user([{"type": "text", "text": "図"}, {"type": "image", "source": {}}]),
    ])
    _, plain = read_messages(path)
    assert [(m.text, m.images) for m in plain] == [("一度だけ", 0), ("本文", 0), ("図", 1)]
    _, with_tools = read_messages(path, include_tools=True)
    assert len(with_tools) == 5
    assert with_tools[2].tools == [{"name": "Read", "input": {"file_path": "a"}}]
    assert with_tools[3].tool_results == ["結果"]
    assert "秘密の思考" not in repr(with_tools)


@pytest.mark.parametrize(("records", "detail"), [
    (["{壊れた行", "[1]"], None),  # 下で mode 行を除くケース
    ([{"type": "user", "message": "想定外の形", "timestamp": "2026-11-01T00:00:00Z"}], "会話の形式"),
    ([user("x", version="3.0.0")], "未対応のバージョン"),
])
def test_unsupported_format_is_explicit_error(env, records, detail):
    _, path = write_session("C:/w", records=records)
    if detail is None:
        path.write_text("{壊れた行\n[1]\n", encoding="utf-8")
    assert read_summary(path).format == "unsupported"
    with pytest.raises(UnsupportedSessionFormatError):
        read_messages(path)


def test_store_scan_uses_cwd_not_directory_name(env, workspace):
    target = workspace / "作業 ツリー"
    target.mkdir()
    a, _ = write_session(target, project="totally-unrelated")
    b, _ = write_session(target / "sub", project="other")
    c, _ = write_session(workspace / "別", project="another")
    (claude_projects_dir() / "another" / "not a session.jsonl").write_text("{}", encoding="utf-8")
    store = ClaudeSessionStore.default()
    assert {h.session_id for h in store.for_project(target)} == {a, b}
    assert store.find(c) is not None and store.find("none") is None


def test_store_handles_missing_directory():
    store = ClaudeSessionStore.default()
    assert not store.available() and store.scan() == []


# --- 対応表の同期 --------------------------------------------------------------
def test_detects_session_started_outside_wtm(env):
    path = start(env, 1).plan.path
    sid, source = write_session(path)
    write_session(env.workspace / "unrelated")
    entries = history(env)
    assert [e["session_id"] for e in entries] == [sid]
    assert entries[0]["title"] == "最初の指示" and entries[0]["message_count"] == 2
    assert entries[0]["origin"] == "live" and entries[0]["backup_status"] == "none"
    assert entries[0]["source_path"] == str(source)


def test_recorded_session_is_found_even_if_cwd_differs(env):
    start(env, 1)
    with env.app.db.transaction() as c:
        env.app.db.upsert_session(c, service.get_managed(env.app, 1)["id"], "claude", "recorded-id")
    write_session(env.workspace / "moved", session_id="recorded-id")
    assert by_id(env, "recorded-id")["origin"] == "live"


def test_session_after_archive_does_not_belong_to_archived_work(env):
    path = start(env, 1).plan.path
    old, _ = write_session(path, records=[user("前", ts="2000-01-01T00:00:00.000Z")])
    archive(env)
    new, _ = write_session(path, records=[user("後", ts="2999-01-01T00:00:00.000Z")])
    assert [e["session_id"] for e in history(env)] == [old]
    assert stored(env, new) == []


def test_recreated_worktree_restores_session_mapping(env):
    res = start(env, 1)
    path = res.plan.path
    sid, _ = write_session(path, records=[user("過去の指示", ts="2000-01-01T00:00:00.000Z")])
    sessions.backup_issue(env.app, 1)
    git(env.repo, "worktree", "remove", str(path))
    archive(env)

    assert start(env, 1).plan.path == path  # 同じパスに再作成（既存ブランチを再利用）
    plan = service.plan_resume(env.app, 1, agent="claude")
    assert plan.session_id == sid
    assert len(stored(env, sid)) == 2 and all(s["backup_path"] for s in stored(env, sid))
    assert [e["session_id"] for e in history(env)] == [sid]


def test_resume_uses_session_started_outside_wtm(env):
    path = start(env, 1).plan.path
    sid, _ = write_session(path)
    assert service.plan_resume(env.app, 1, agent="claude").session_id == sid


def test_adopt_finds_existing_sessions(env):
    target = env.workspace / "既存 worktree"
    git(env.repo, "worktree", "add", "-b", "existing", str(target))
    sid, _ = write_session(target)
    from wt.gitea import Issue

    res = service.adopt(env.app, target, Issue(1, "t", "", "open", ""))
    assert [e["session_id"] for e in history(env)] == [sid]
    assert service.get_checkpoint(env.app, res.checkpoint_id)["session_id"] == sid


# --- バックアップ -------------------------------------------------------------
def test_backup_copies_verified_without_touching_source(env):
    sid, source = write_session(start(env, 1).plan.path)
    before = fingerprint(source)
    (result,), _ = sessions.backup_issue(env.app, 1)
    assert result.status == "copied"
    assert result.backup_path == env.app.config.sessions_dir / "issue-1" / "claude" / f"{sid}.jsonl"
    assert result.backup_path.read_bytes() == source.read_bytes()
    assert fingerprint(source) == before
    meta = json.loads(result.backup_path.with_name(f"{sid}.meta.json").read_text(encoding="utf-8"))
    assert meta["sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert meta["issue"] == 1
    entry = by_id(env, sid)
    assert entry["backup_status"] == "current" and entry["backed_up_at"]


def test_backup_is_skipped_when_unchanged_and_tracks_growth(env):
    sid, source = write_session(start(env, 1).plan.path)
    sessions.backup_issue(env.app, 1)
    (again,), _ = sessions.backup_issue(env.app, 1)
    assert again.status == "unchanged"
    with open(source, "a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(user("追記")) + "\n")
    assert by_id(env, sid)["backup_status"] == "outdated"
    (updated,), _ = sessions.backup_issue(env.app, 1)
    assert updated.status == "copied" and updated.preserved is None
    assert updated.backup_path.read_bytes() == source.read_bytes()


def test_backup_preserves_old_copy_when_source_is_rewritten(env):
    _, source = write_session(start(env, 1).plan.path)
    (first,), _ = sessions.backup_issue(env.app, 1)
    old = first.backup_path.read_bytes()
    source.write_text(json.dumps(user("書き換え")) + "\n", encoding="utf-8")
    (second,), _ = sessions.backup_issue(env.app, 1)
    assert second.preserved is not None and second.preserved.read_bytes() == old
    assert second.backup_path.read_bytes() == source.read_bytes()


def test_backup_includes_session_directory(env):
    sid, source = write_session(start(env, 1).plan.path)
    extra = source.with_suffix("") / "subagents" / "a.jsonl"
    extra.parent.mkdir(parents=True)
    extra.write_text("{}", encoding="utf-8")
    (result,), _ = sessions.backup_issue(env.app, 1)
    assert (result.backup_path.parent / sid / "subagents" / "a.jsonl").read_text(encoding="utf-8") == "{}"


def test_backup_works_for_unsupported_format(env):
    _, source = write_session(start(env, 1).plan.path, records=[user("x", version="9.0.0")])
    (result,), _ = sessions.backup_issue(env.app, 1)
    assert result.status == "copied" and result.backup_path.read_bytes() == source.read_bytes()


def test_backup_failure_is_reported_and_not_recorded(env, monkeypatch):
    sid, _ = write_session(start(env, 1).plan.path)

    def fail(*_args):
        raise OSError("disk full")

    monkeypatch.setattr(sessions, "_copy_verified", fail)
    (result,), _ = sessions.backup_issue(env.app, 1)
    assert result.status == "failed" and "disk full" in result.message
    assert by_id(env, sid)["backup_status"] == "none"
    assert stored(env, sid)[0]["backup_path"] is None


def test_verification_failure_leaves_no_partial_backup(env, monkeypatch):
    sid, _ = write_session(start(env, 1).plan.path)
    monkeypatch.setattr(sessions, "_sha256", lambda *_a: "mismatch")
    (result,), _ = sessions.backup_issue(env.app, 1)
    assert result.status == "failed" and "検証" in result.message
    directory = env.app.config.sessions_dir / "issue-1" / "claude"
    assert list(directory.iterdir()) == []


def test_session_without_file_needs_no_backup(env):
    start(env, 1)
    with env.app.db.transaction() as c:
        env.app.db.upsert_session(c, service.get_managed(env.app, 1)["id"], "claude", "never-used")
    (result,), _ = sessions.backup_issue(env.app, 1)
    assert result.status == "no_source"
    assert by_id(env, "never-used")["origin"] == "missing"


def _session_writing_agent(env, rc=0):
    """--session-id を受け取って保存先にセッションを書く Claude の代役。"""
    script = env.workspace / f"fake-claude-{uuid.uuid4().hex}.py"
    script.write_text(
        "import json,os,pathlib,sys\n"
        "sid = sys.argv[sys.argv.index('--session-id') + 1]\n"
        f"d = pathlib.Path({str(claude_projects_dir())!r}) / 'p'\n"
        "d.mkdir(parents=True, exist_ok=True)\n"
        "rec = {'type': 'user', 'cwd': os.getcwd(), 'sessionId': sid, 'version': '2.1.318',\n"
        "       'timestamp': '2026-11-01T00:00:00Z', 'message': {'content': '指示'}}\n"
        "(d / f'{sid}.jsonl').write_text(json.dumps(rec) + '\\n', encoding='utf-8')\n"
        f"sys.exit({rc})\n", encoding="utf-8")
    env.app.config.agents.commands["claude"] = [sys.executable, str(script)]


def test_agent_exit_backs_up_session_automatically(env):
    start(env, 1)
    _session_writing_agent(env)
    res = service.launch_agent(env.app, 1, "claude")
    entry = by_id(env, res.session_id)
    assert entry["backup_status"] == "current" and entry["message_count"] == 1
    assert res.notes == []


def test_agent_exit_backup_failure_only_adds_note(env, monkeypatch):
    start(env, 1)
    _session_writing_agent(env)

    def fail(*_args):
        raise OSError("disk full")

    monkeypatch.setattr(sessions, "_copy_verified", fail)
    res = service.launch_agent(env.app, 1, "claude")
    assert res.rc == 0 and any("disk full" in n for n in res.notes)


# --- worktree 削除後の閲覧（テスト §10-13） ---------------------------------------
def test_history_after_worktree_removed_and_source_deleted(cli_env):
    env = cli_env
    path = start(env, 1).plan.path
    sid, source = write_session(path, records=[user("削除前の指示")])
    sessions.backup_issue(env.app, 1)
    git(env.repo, "worktree", "remove", str(path))
    archive(env)
    source.unlink()

    entry = by_id(env, sid)
    assert (entry["origin"], entry["backup_status"], entry["message_count"]) == ("backup", "current", 1)
    r = runner.invoke(cli.app, ["history", "1", "--session", sid])
    assert r.exit_code == 0, r.output
    assert "削除前の指示" in r.output and "参照元: backup" in r.output


def test_view_with_source_gone_and_no_backup_fails_clearly(env):
    sid, source = write_session(start(env, 1).plan.path)
    history(env)
    source.unlink()
    with pytest.raises(WtError, match="バックアップにもありません"):
        sessions.open_session(env.app, 1, sid)


def test_corrupted_backup_shows_warning(env):
    sid, _ = write_session(start(env, 1).plan.path)
    (result,), _ = sessions.backup_issue(env.app, 1)
    with open(result.backup_path, "a", encoding="utf-8") as f:
        f.write("\n")
    opened = sessions.open_session(env.app, 1, sid, from_backup=True)
    assert opened.origin == "backup" and any("破損" in w for w in opened.warnings)


def test_from_backup_requires_backup(env):
    sid, _ = write_session(start(env, 1).plan.path)
    with pytest.raises(WtError, match="バックアップはありません"):
        sessions.open_session(env.app, 1, sid, from_backup=True)


def test_sessions_of_other_issue_are_not_accessible(env):
    start(env, 1)
    other = start(env, 2).plan.path
    sid, _ = write_session(other)
    with pytest.raises(NotManagedError):
        sessions.open_session(env.app, 1, sid)


def test_unknown_issue(env):
    with pytest.raises(NotManagedError):
        sessions.collect_history(env.app, 999)


# --- CLI ---------------------------------------------------------------------
def test_cli_list_table_and_json(cli_env):
    sid, _ = write_session(start(cli_env, 1).plan.path)
    r = runner.invoke(cli.app, ["history", "1"])
    assert r.exit_code == 0 and sid in r.output  # 80 桁でも ID を折り返さない
    data = json.loads(runner.invoke(cli.app, ["history", "1", "--json"]).output)
    assert data["schema_version"] == 1 and data["sessions"][0]["session_id"] == sid
    assert data["worktrees"][0]["exists"] is True


def test_cli_agent_without_detection_shows_notice(cli_env):
    write_session(start(cli_env, 1).plan.path)
    r = runner.invoke(cli.app, ["history", "1", "--agent", "codex"])
    assert r.exit_code == 0 and "対応していません" in r.output and "セッションはありません" in r.output


def test_cli_store_missing_warns_but_works(cli_env):
    start(cli_env, 1)
    r = runner.invoke(cli.app, ["history", "1"])
    assert r.exit_code == 0 and "見つかりません" in r.output


def test_cli_view_formats_and_prefix(cli_env):
    sid, _ = write_session(start(cli_env, 1).plan.path, records=[
        user("指示A"), assistant([{"type": "text", "text": "応答B"},
                                  {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}])])
    r = runner.invoke(cli.app, ["history", "1", "--session", sid[:8]])
    assert r.exit_code == 0 and "指示A" in r.output and "応答B" in r.output and "Bash" not in r.output
    r = runner.invoke(cli.app, ["history", "1", "--session", sid, "--tools"])
    assert "Bash" in r.output and "ls" in r.output
    data = json.loads(runner.invoke(cli.app, ["history", "1", "-s", sid, "--format", "json"]).output)
    assert data["session_id"] == sid and data["messages"][0]["text"] == "指示A" and "tools" not in data["messages"][1]
    assert runner.invoke(cli.app, ["history", "1", "-s", sid, "--format", "xml"]).exit_code != 0


def test_cli_ambiguous_prefix(cli_env):
    path = start(cli_env, 1).plan.path
    write_session(path, session_id="same-prefix-1")
    write_session(path, session_id="same-prefix-2")
    r = runner.invoke(cli.app, ["history", "1", "--session", "same-prefix"])
    assert r.exit_code != 0 and "複数" in r.output


def test_cli_export_does_not_overwrite(cli_env, tmp_path):
    sid, _ = write_session(start(cli_env, 1).plan.path)
    out = tmp_path / "出力 先.md"
    r = runner.invoke(cli.app, ["history", "1", "-s", sid, "--export", str(out)])
    assert r.exit_code == 0 and "最初の指示" in out.read_text(encoding="utf-8") and "機密" in r.output
    before = out.read_text(encoding="utf-8")
    r = runner.invoke(cli.app, ["history", "1", "-s", sid, "--export", str(out), "--format", "json"])
    assert r.exit_code != 0 and out.read_text(encoding="utf-8") == before


def test_cli_unsupported_session_is_error_in_view_only(cli_env):
    sid, _ = write_session(start(cli_env, 1).plan.path, records=[user("x", version="3.0.0")])
    r = runner.invoke(cli.app, ["history", "1"])
    assert r.exit_code == 0 and "未対応" in r.output
    r = runner.invoke(cli.app, ["history", "1", "-s", sid])
    assert r.exit_code != 0 and "対応していません" in r.output


def test_cli_backup_and_failure_exit_code(cli_env, monkeypatch):
    write_session(start(cli_env, 1).plan.path)
    r = runner.invoke(cli.app, ["history", "1", "--backup"])
    assert r.exit_code == 0 and "バックアップしました" in r.output
    r = runner.invoke(cli.app, ["history", "1", "--backup"])
    assert r.exit_code == 0 and "変更なし" in r.output

    write_session(service.get_managed(cli_env.app, 1)["path"])
    monkeypatch.setattr(sessions, "_copy_verified", lambda *_a: (_ for _ in ()).throw(OSError("disk full")))
    r = runner.invoke(cli.app, ["history", "1", "--backup"])
    assert r.exit_code == 1 and "disk full" in r.output


@pytest.mark.parametrize("args", [["--backup", "--session", "x"], ["--export", "a.md"], ["--from-backup"], ["--tools"],
                                  ["--session", "x", "--json"]])
def test_cli_invalid_option_combinations(cli_env, args):
    start(cli_env, 1)
    assert runner.invoke(cli.app, ["history", "1", *args]).exit_code != 0


def test_history_never_posts_to_gitea(cli_env):
    sid, _ = write_session(start(cli_env, 1).plan.path)
    runner.invoke(cli.app, ["history", "1", "--backup"])
    runner.invoke(cli.app, ["history", "1", "-s", sid])
    assert cli_env.issues.comments == [] and cli_env.issues.created_pulls == []


# --- アダプタ・doctor ----------------------------------------------------------
def test_claude_adapter_session_operations(env, tmp_path):
    path = start(env, 1).plan.path
    sid, _ = write_session(path)
    adapter = ClaudeAdapter(["claude"])
    assert [s["session_id"] for s in adapter.list_sessions(path)] == [sid]
    assert adapter.get_session(sid)["title"] == "最初の指示"
    out = adapter.export_session(sid, tmp_path / "s.md")
    assert "最初の指示" in out.read_text(encoding="utf-8")
    with pytest.raises(FileExistsError):
        adapter.export_session(sid, out)
    with pytest.raises(NotManagedError):
        adapter.get_session("missing")


def test_codex_adapter_reports_unsupported(tmp_path):
    adapter = CodexAdapter(["codex"])
    with pytest.raises(UnsupportedOperationError):
        adapter.list_sessions(tmp_path)
    with pytest.raises(UnsupportedOperationError):
        adapter.export_session("x", tmp_path / "x.md")


def test_doctor_reports_session_store(env):
    write_session("C:/w")
    checks = {c.name: c for c in service.doctor(None, env.repo, env.app.git)}
    assert checks["Claude セッション保存先"].status == "ok" and "1 件" in checks["Claude セッション保存先"].message


@pytest.mark.skipif(os.name != "posix", reason="POSIX のパーミッションのみ")
def test_backup_is_private_on_posix(env):
    write_session(start(env, 1).plan.path)
    (result,), _ = sessions.backup_issue(env.app, 1)
    assert result.backup_path.stat().st_mode & 0o077 == 0
    assert result.backup_path.parent.stat().st_mode & 0o077 == 0
