"""AI セッションと worktree の対応管理、バックアップ、閲覧（Phase 4）。

- 元のセッションファイルは変更しない。
- バックアップは `<管理ディレクトリ>/sessions/issue-<N>/claude/<セッション ID>.jsonl` にバイト単位でコピーする。
- 会話内容は Gitea に投稿しない。
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .claude_store import SESSION_ID_PATTERN, ClaudeSessionStore, SessionSummary, read_summary
from .db import now_iso, parse_iso
from .errors import NotManagedError, WtError
from .paths import is_within

if TYPE_CHECKING:
    import sqlite3

    from .service import App

AGENT = "claude"
HISTORY_SCHEMA_VERSION = 1
_CHUNK = 1024 * 1024


def issue_rows(app: App, issue_number: int) -> list[sqlite3.Row]:
    """Issue の worktree を新しい順に返す（ARCHIVED を含む。削除後も履歴を参照するため）。"""
    with app.db.read() as c:
        rows = c.execute("SELECT * FROM worktrees WHERE issue_number=? ORDER BY id DESC", (issue_number,)).fetchall()
    if not rows:
        raise NotManagedError(f"Issue #{issue_number} の worktree の記録はありません。")
    return rows


def _match_row(rows: list[sqlite3.Row], cwd: str, created_at: str | None) -> sqlite3.Row | None:
    started = parse_iso(created_at)
    for row in rows:
        if not is_within(Path(cwd), Path(row["path"])):
            continue
        archived = parse_iso(row["archived_at"])
        if archived and started and started > archived:  # 整理後に同じパスで始まったものは別の作業
            continue
        return row
    return None


def sync_sessions(app: App, issue_number: int, store: ClaudeSessionStore | None = None) -> list[str]:
    """保存先を調べて DB の対応表を更新する。警告の一覧を返す。

    wtm を通さず起動したセッションも cwd で検出する。worktree を同じパスに作り直した場合は、
    以前の作業のセッションも現在の worktree に対応付ける。
    """
    store = store or ClaudeSessionStore.default()
    if not store.available():
        return []
    rows = issue_rows(app, issue_number)
    row_ids = {r["id"] for r in rows}
    heads = {h.session_id: h for h in store.scan()}
    warnings: list[str] = []
    with app.db.read() as c:
        recorded = [s for s in c.execute("SELECT * FROM agent_sessions WHERE agent=?", (AGENT,)).fetchall()
                    if s["worktree_id"] in row_ids]
    targets: dict[str, set[int]] = {}
    for s in recorded:
        if s["session_id"] in heads:
            targets.setdefault(s["session_id"], set()).add(s["worktree_id"])
    summaries: dict[str, SessionSummary] = {}
    for head in heads.values():
        if head.session_id not in targets and not (
                head.cwd and any(is_within(Path(head.cwd), Path(r["path"])) for r in rows)):
            continue
        try:
            summaries[head.session_id] = read_summary(head.path)
        except WtError as e:
            warnings.append(e.message)
            continue
        if head.cwd:
            row = _match_row(rows, head.cwd, summaries[head.session_id].created_at)
            if row is not None:
                targets.setdefault(head.session_id, set()).add(row["id"])
    with app.db.transaction() as c:
        for session_id, summary in summaries.items():
            for worktree_id in targets.get(session_id, ()):
                app.db.upsert_session(c, worktree_id, AGENT, session_id)
                c.execute(
                    "UPDATE agent_sessions SET title=COALESCE(?, title), created_at=COALESCE(?, created_at),"
                    " updated_at=COALESCE(?, updated_at), source_path=?,"
                    " backup_path=COALESCE(backup_path, (SELECT backup_path FROM agent_sessions o"
                    "   WHERE o.session_id=? AND o.agent=? AND o.backup_path IS NOT NULL LIMIT 1)),"
                    " backed_up_at=COALESCE(backed_up_at, (SELECT backed_up_at FROM agent_sessions o"
                    "   WHERE o.session_id=? AND o.agent=? AND o.backed_up_at IS NOT NULL LIMIT 1))"
                    " WHERE worktree_id=? AND agent=? AND session_id=?",
                    (summary.title, summary.created_at, summary.updated_at, str(summary.path),
                     session_id, AGENT, session_id, AGENT, worktree_id, AGENT, session_id))
    return warnings


def _sessions_of_issue(app: App, issue_number: int) -> list[sqlite3.Row]:
    """同じセッションが複数の worktree 記録にあれば新しい worktree のものだけ返す。"""
    rows = issue_rows(app, issue_number)
    order = {r["id"]: i for i, r in enumerate(rows)}
    marks = ",".join("?" * len(rows))
    with app.db.read() as c:
        all_sessions = c.execute(f"SELECT * FROM agent_sessions WHERE worktree_id IN ({marks})",
                                 [r["id"] for r in rows]).fetchall()
    best: dict[tuple[str, str], sqlite3.Row] = {}
    for s in sorted(all_sessions, key=lambda x: order[x["worktree_id"]], reverse=True):
        best[(s["agent"], s["session_id"])] = s
    return sorted(best.values(), key=lambda s: (s["updated_at"] or "", s["id"]), reverse=True)


# ---------------------------------------------------------------------------
# バックアップ
# ---------------------------------------------------------------------------
def _meta_path(backup: Path) -> Path:
    return backup.with_name(backup.stem + ".meta.json")


def _read_meta(backup: Path) -> dict[str, Any] | None:
    try:
        meta = json.loads(_meta_path(backup).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return meta if isinstance(meta, dict) else None


def _sha256(path: Path, limit: int | None = None) -> str:
    digest = hashlib.sha256()
    remaining = limit
    with open(path, "rb") as f:
        while remaining is None or remaining > 0:
            chunk = f.read(_CHUNK if remaining is None else min(_CHUNK, remaining))
            if not chunk:
                break
            digest.update(chunk)
            if remaining is not None:
                remaining -= len(chunk)
    return digest.hexdigest()


def private_dir(path: Path) -> None:
    """セッションには機密情報が含まれ得るため、POSIX では所有者のみに制限する。"""
    path.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        os.chmod(path, 0o700)


def _private_temp(directory: Path, prefix: str) -> tuple[int, Path]:
    fd, name = tempfile.mkstemp(dir=directory, prefix=prefix, suffix=".tmp")  # 所有者のみ読み書き可で作成される
    return fd, Path(name)


def _copy_verified(source: Path, directory: Path) -> tuple[Path, str, int]:
    """一時ファイルへコピーして読み直しで検証する。(一時ファイル, sha256, サイズ) を返す。"""
    fd, tmp = _private_temp(directory, ".backup-")
    digest = hashlib.sha256()
    size = 0
    try:
        with os.fdopen(fd, "wb") as out, open(source, "rb") as src:
            while chunk := src.read(_CHUNK):
                out.write(chunk)
                digest.update(chunk)
                size += len(chunk)
            out.flush()
            os.fsync(out.fileno())
        sha = digest.hexdigest()
        if _sha256(tmp) != sha:
            raise WtError(f"バックアップの検証に失敗しました: {source}")
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return tmp, sha, size


def _copy_session_dir(source_dir: Path, backup_dir: Path) -> int:
    """セッションの付随ファイル（サブエージェントの記録など）を追加・更新する。リンクは対象外。"""
    copied = 0
    for src in source_dir.rglob("*"):
        if src.is_symlink() or not src.is_file():
            continue
        dest = backup_dir / src.relative_to(source_dir)
        stat = src.stat()
        if dest.is_file() and dest.stat().st_size == stat.st_size and dest.stat().st_mtime_ns == stat.st_mtime_ns:
            continue
        private_dir(dest.parent)
        tmp, _, _ = _copy_verified(src, dest.parent)
        os.utime(tmp, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        os.replace(tmp, dest)
        copied += 1
    return copied


def backup_is_current(source: Path, backup: Path) -> bool:
    meta = _read_meta(backup)
    if meta is None or not backup.is_file():
        return False
    try:
        stat = source.stat()
        return (stat.st_size == meta.get("source_size") and stat.st_mtime_ns == meta.get("source_mtime_ns")
                and backup.stat().st_size == meta.get("size"))
    except OSError:
        return False


@dataclass
class BackupResult:
    session_id: str
    status: str  # copied | unchanged | failed | no_source
    backup_path: Path | None = None
    message: str = ""
    preserved: Path | None = None  # 元ファイルが追記以外で変わったため残した旧バックアップ


def backup_session(app: App, issue_number: int, session: sqlite3.Row) -> BackupResult:
    """1 セッションをバックアップして DB を更新する。元ファイルは読むだけ。"""
    session_id = session["session_id"]
    if not SESSION_ID_PATTERN.fullmatch(session_id):
        return BackupResult(session_id, "failed", message="セッション ID の形式が不正です。")
    source = Path(session["source_path"]) if session["source_path"] else None
    if source is None or not source.is_file():
        return BackupResult(session_id, "no_source")
    directory = app.config.sessions_dir / f"issue-{issue_number}" / AGENT
    backup = directory / f"{session_id}.jsonl"
    try:
        private_dir(directory)
        source_dir = source.with_suffix("")
        if source_dir.is_dir():
            _copy_session_dir(source_dir, directory / session_id)
        if backup_is_current(source, backup):
            _record_backup(app, session_id, backup, keep_time=True)
            return BackupResult(session_id, "unchanged", backup)
        stat = source.stat()
        tmp, sha, size = _copy_verified(source, directory)
        preserved = None
        try:
            if backup.is_file():  # 追記でない変更なら、以前の内容を失わないよう残す
                old_size = backup.stat().st_size
                if old_size > size or _sha256(backup) != _sha256(tmp, old_size):
                    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
                    preserved = directory / f"{session_id}.{stamp}.jsonl.bak"
                    os.replace(backup, preserved)
            os.replace(tmp, backup)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        _write_meta(backup, {"schema_version": 1, "agent": AGENT, "session_id": session_id, "issue": issue_number,
                             "source_path": str(source), "source_size": stat.st_size,
                             "source_mtime_ns": stat.st_mtime_ns, "size": size, "sha256": sha,
                             "backed_up_at": now_iso()})
    except OSError as e:
        return BackupResult(session_id, "failed", message=f"コピーに失敗しました: {e}")
    except WtError as e:
        return BackupResult(session_id, "failed", message=e.message)
    _record_backup(app, session_id, backup)
    return BackupResult(session_id, "copied", backup, preserved=preserved)


def _write_meta(backup: Path, meta: dict[str, Any]) -> None:
    fd, tmp = _private_temp(backup.parent, ".meta-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
        os.replace(tmp, _meta_path(backup))
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _record_backup(app: App, session_id: str, backup: Path, keep_time: bool = False) -> None:
    with app.db.transaction() as c:
        if keep_time:
            c.execute("UPDATE agent_sessions SET backup_path=?, backed_up_at=COALESCE(backed_up_at, ?)"
                      " WHERE session_id=? AND agent=?", (str(backup), now_iso(), session_id, AGENT))
        else:
            c.execute("UPDATE agent_sessions SET backup_path=?, backed_up_at=? WHERE session_id=? AND agent=?",
                      (str(backup), now_iso(), session_id, AGENT))


def backup_issue(app: App, issue_number: int) -> tuple[list[BackupResult], list[str]]:
    warnings = sync_sessions(app, issue_number)
    results = [backup_session(app, issue_number, s) for s in _sessions_of_issue(app, issue_number)
               if s["agent"] == AGENT]
    return results, warnings


def backup_after_agent_exit(app: App, issue_number: int) -> list[str]:
    """AI の終了後に履歴を保全する。失敗しても AI の実行結果には影響させず、メッセージだけ返す。"""
    try:
        results, warnings = backup_issue(app, issue_number)
    except (WtError, OSError) as e:
        return [f"セッションをバックアップできませんでした: {e}"]
    return warnings + [f"セッション {r.session_id} をバックアップできませんでした: {r.message}"
                       for r in results if r.status == "failed"]


# ---------------------------------------------------------------------------
# 一覧・閲覧
# ---------------------------------------------------------------------------
def _session_entry(s: sqlite3.Row) -> dict[str, Any]:
    source = Path(s["source_path"]) if s["source_path"] else None
    backup = Path(s["backup_path"]) if s["backup_path"] else None
    live = bool(source and source.is_file())
    has_backup = bool(backup and backup.is_file())
    entry: dict[str, Any] = {
        "agent": s["agent"], "session_id": s["session_id"], "title": s["title"],
        "created_at": s["created_at"], "updated_at": s["updated_at"],
        "origin": "live" if live else "backup" if has_backup else "missing",
        "source_path": s["source_path"], "backup_path": s["backup_path"], "backed_up_at": s["backed_up_at"],
        "message_count": None, "format": None, "format_detail": "",
    }
    if not has_backup:
        entry["backup_status"] = "none"
    elif live:
        entry["backup_status"] = "current" if backup_is_current(source, backup) else "outdated"
    else:
        entry["backup_status"] = "current"
    target = source if live else backup if has_backup else None
    if target is not None and s["agent"] == AGENT:
        try:
            summary = read_summary(target)
            entry.update(message_count=summary.message_count, format=summary.format,
                         format_detail=summary.format_detail, skipped_lines=summary.skipped_lines)
            entry["title"] = entry["title"] or summary.title
        except WtError as e:
            entry.update(format="unsupported", format_detail=e.message)
    return entry


def collect_history(app: App, issue_number: int, *, agent: str | None = None) -> dict[str, Any]:
    warnings: list[str] = []
    store = ClaudeSessionStore.default()
    if agent in (None, AGENT):
        if store.available():
            warnings += sync_sessions(app, issue_number, store)
        else:
            warnings.append(f"Claude のセッション保存先が見つかりません: {store.projects_dir}（記録とバックアップのみ表示）")
    sessions = [_session_entry(s) for s in _sessions_of_issue(app, issue_number)
                if agent is None or s["agent"] == agent]
    return {
        "schema_version": HISTORY_SCHEMA_VERSION,
        "generated_at": now_iso(),
        "issue": issue_number,
        "worktrees": [{"path": r["path"], "state": r["state"], "exists": Path(r["path"]).is_dir()}
                      for r in issue_rows(app, issue_number)],
        "sessions": sessions,
        "warnings": warnings,
    }


@dataclass
class OpenedSession:
    session_id: str
    path: Path
    origin: str  # live | backup
    warnings: list[str] = field(default_factory=list)


def open_session(app: App, issue_number: int, session_ref: str, *, from_backup: bool = False) -> OpenedSession:
    """ID（先頭一致可）で Issue に紐づくセッションを開く。Issue 外のファイルには触れない。"""
    warnings = sync_sessions(app, issue_number)
    sessions = [s for s in _sessions_of_issue(app, issue_number) if s["agent"] == AGENT]
    matched = [s for s in sessions if s["session_id"] == session_ref] or [
        s for s in sessions if s["session_id"].startswith(session_ref)]
    if not matched:
        raise NotManagedError(f"Issue #{issue_number} にセッション {session_ref} はありません。",
                              hint=f"wtm history {issue_number} で一覧を確認してください。")
    if len(matched) > 1:
        raise WtError(f"セッション {session_ref} に複数が一致します: " + ", ".join(s["session_id"] for s in matched))
    s = matched[0]
    source = Path(s["source_path"]) if s["source_path"] else None
    backup = Path(s["backup_path"]) if s["backup_path"] else None
    if not from_backup and source and source.is_file():
        return OpenedSession(s["session_id"], source, "live", warnings)
    if backup and backup.is_file():
        meta = _read_meta(backup)
        if meta is None or meta.get("sha256") != _sha256(backup):
            warnings.append("バックアップがチェックサムと一致しません。破損している可能性があります。")
        return OpenedSession(s["session_id"], backup, "backup", warnings)
    if from_backup:
        raise WtError(f"セッション {s['session_id']} のバックアップはありません。",
                      hint=f"wtm history {issue_number} --backup で作成できます。")
    raise WtError(f"セッション {s['session_id']} の内容は元の保存先にもバックアップにもありません。")
