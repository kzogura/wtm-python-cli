"""SQLite 管理 DB。

- 接続は操作ごとに短命に開き、更新は BEGIN IMMEDIATE で直列化する。
- WAL + busy_timeout で複数 wtm プロセスからの同時アクセスに対応する。
- 同一 Issue の重複作成は部分一意インデックスで DB レベルでも防止する。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .errors import WtError

SCHEMA_VERSION = 1

STATES = ("PLANNED", "WORKING", "BLOCKED", "REVIEW", "DONE", "ARCHIVED")

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Issue のローカルキャッシュ（正本は GitHub / Gitea）
CREATE TABLE IF NOT EXISTS issues (
    number     INTEGER PRIMARY KEY,
    title      TEXT NOT NULL,
    body       TEXT NOT NULL DEFAULT '',
    state      TEXT NOT NULL DEFAULT 'unknown',
    url        TEXT NOT NULL DEFAULT '',
    source     TEXT NOT NULL,             -- 'github' | 'gitea' | 'manual'
    fetched_at TEXT NOT NULL
);

-- Issue : worktree は 1 : 多 を許容（v1 は部分一意インデックスで 1 : 1 に制限）
CREATE TABLE IF NOT EXISTS worktrees (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_number    INTEGER NOT NULL,
    name            TEXT NOT NULL,
    path            TEXT NOT NULL,
    path_key        TEXT NOT NULL,
    branch          TEXT,
    base_ref        TEXT,
    state           TEXT NOT NULL CHECK (state IN ({",".join(repr(s) for s in STATES)})),
    provision       TEXT NOT NULL DEFAULT 'ready' CHECK (provision IN ('creating', 'ready')),
    origin          TEXT NOT NULL CHECK (origin IN ('start', 'adopt')),
    agent           TEXT,
    task_file       TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    last_checked_at TEXT NOT NULL,
    archived_at     TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_worktrees_active_issue
    ON worktrees(issue_number) WHERE state <> 'ARCHIVED';
CREATE UNIQUE INDEX IF NOT EXISTS ux_worktrees_active_path
    ON worktrees(path_key) WHERE state <> 'ARCHIVED';

CREATE TABLE IF NOT EXISTS state_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    worktree_id INTEGER NOT NULL REFERENCES worktrees(id) ON DELETE CASCADE,
    from_state  TEXT,
    to_state    TEXT NOT NULL,
    reason      TEXT NOT NULL DEFAULT '',
    changed_at  TEXT NOT NULL
);

-- AI プロセスの起動状態（作業ライフサイクルとは独立）
CREATE TABLE IF NOT EXISTS agent_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    worktree_id INTEGER NOT NULL REFERENCES worktrees(id) ON DELETE CASCADE,
    agent       TEXT NOT NULL,
    pid         INTEGER,
    session_id  TEXT,
    status      TEXT NOT NULL CHECK (status IN ('running', 'exited', 'failed', 'unknown')),
    exit_code   INTEGER,
    started_at  TEXT NOT NULL,
    ended_at    TEXT
);

-- AI セッションと worktree の対応（Phase 4 で本格利用）
CREATE TABLE IF NOT EXISTS agent_sessions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    worktree_id   INTEGER NOT NULL REFERENCES worktrees(id) ON DELETE CASCADE,
    agent         TEXT NOT NULL,
    session_id    TEXT NOT NULL,
    title         TEXT,
    created_at    TEXT,
    updated_at    TEXT,
    source_path   TEXT,
    backup_path   TEXT,
    backed_up_at  TEXT,
    UNIQUE (worktree_id, agent, session_id)
);

CREATE TABLE IF NOT EXISTS checkpoints (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    worktree_id      INTEGER NOT NULL REFERENCES worktrees(id) ON DELETE CASCADE,
    issue_number     INTEGER NOT NULL,
    created_at       TEXT NOT NULL,
    worktree_path    TEXT NOT NULL,
    head             TEXT,
    diffstat         TEXT NOT NULL DEFAULT '',
    uncommitted_json TEXT NOT NULL DEFAULT '[]',
    completed        TEXT NOT NULL DEFAULT '',
    remaining        TEXT NOT NULL DEFAULT '',
    next_action      TEXT NOT NULL DEFAULT '',
    blockers         TEXT NOT NULL DEFAULT '',
    session_id       TEXT,
    file_path        TEXT,
    published_at     TEXT
);
"""


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


class Database:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 30000")
        return conn

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """書き込みトランザクション。BEGIN IMMEDIATE で書き込みロックを先に取得する。"""
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
        finally:
            conn.close()

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = self.connect()
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            # executescript は保留中トランザクションを暗黙 COMMIT するため、スクリプト内で完結させる
            conn.executescript(
                "BEGIN IMMEDIATE;" + SCHEMA
                + f"INSERT INTO meta(key, value) VALUES ('schema_version', '{SCHEMA_VERSION}') "
                "ON CONFLICT(key) DO NOTHING; COMMIT;"
            )
        finally:
            conn.close()

    def check_schema(self) -> None:
        if not self.path.exists():
            raise WtError(f"管理 DB がありません: {self.path}", hint="wtm init を実行してください。")
        with self.read() as c:
            try:
                row = c.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            except sqlite3.DatabaseError as e:
                raise WtError(f"管理 DB を読み込めません: {self.path}: {e}") from e
        if row is None or int(row["value"]) != SCHEMA_VERSION:
            raise WtError(f"未対応の DB スキーマです: {row['value'] if row else 'なし'} (期待値 {SCHEMA_VERSION})")

    # --- worktrees --------------------------------------------------------
    def get_active_by_issue(self, c: sqlite3.Connection, issue: int) -> sqlite3.Row | None:
        return c.execute(
            "SELECT * FROM worktrees WHERE issue_number=? AND state<>'ARCHIVED'", (issue,)
        ).fetchone()

    def get_active_by_path(self, c: sqlite3.Connection, key: str) -> sqlite3.Row | None:
        return c.execute(
            "SELECT * FROM worktrees WHERE path_key=? AND state<>'ARCHIVED'", (key,)
        ).fetchone()

    def list_worktrees(self, c: sqlite3.Connection, include_archived: bool = False) -> list[sqlite3.Row]:
        sql = "SELECT * FROM worktrees"
        if not include_archived:
            sql += " WHERE state<>'ARCHIVED'"
        return c.execute(sql + " ORDER BY issue_number, id").fetchall()

    def set_state(self, c: sqlite3.Connection, worktree_id: int, to_state: str, reason: str = "") -> None:
        if to_state not in STATES:
            raise ValueError(to_state)
        row = c.execute("SELECT state FROM worktrees WHERE id=?", (worktree_id,)).fetchone()
        from_state = row["state"] if row else None
        ts = now_iso()
        c.execute(
            "UPDATE worktrees SET state=?, updated_at=?, archived_at=CASE WHEN ?='ARCHIVED' THEN ? ELSE archived_at END "
            "WHERE id=?",
            (to_state, ts, to_state, ts, worktree_id),
        )
        c.execute(
            "INSERT INTO state_history(worktree_id, from_state, to_state, reason, changed_at) VALUES (?,?,?,?,?)",
            (worktree_id, from_state, to_state, reason, ts),
        )

    def touch_checked(self, worktree_id: int) -> None:
        with self.transaction() as c:
            c.execute("UPDATE worktrees SET last_checked_at=? WHERE id=?", (now_iso(), worktree_id))

    # --- issues -----------------------------------------------------------
    def upsert_issue(self, c: sqlite3.Connection, number: int, title: str, body: str,
                     state: str, url: str, source: str) -> None:
        c.execute(
            "INSERT INTO issues(number, title, body, state, url, source, fetched_at) VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(number) DO UPDATE SET title=excluded.title, body=excluded.body, state=excluded.state, "
            "url=excluded.url, source=excluded.source, fetched_at=excluded.fetched_at",
            (number, title, body, state, url, source, now_iso()),
        )

    def get_issue(self, c: sqlite3.Connection, number: int) -> sqlite3.Row | None:
        return c.execute("SELECT * FROM issues WHERE number=?", (number,)).fetchone()

    # --- checkpoints ------------------------------------------------------
    def latest_checkpoint(self, c: sqlite3.Connection, worktree_id: int) -> sqlite3.Row | None:
        return c.execute(
            "SELECT * FROM checkpoints WHERE worktree_id=? ORDER BY created_at DESC, id DESC LIMIT 1",
            (worktree_id,),
        ).fetchone()

    def list_checkpoints(self, c: sqlite3.Connection, issue_number: int) -> list[sqlite3.Row]:
        # 整理済み（ARCHIVED）や作り直し前の worktree の Checkpoint も含める
        return c.execute(
            "SELECT * FROM checkpoints WHERE issue_number=? ORDER BY created_at DESC, id DESC", (issue_number,)
        ).fetchall()

    def insert_checkpoint(self, c: sqlite3.Connection, data: dict[str, Any]) -> int:
        data = dict(data)
        data["uncommitted_json"] = json.dumps(data.pop("uncommitted", []), ensure_ascii=False)
        cols = ",".join(data)
        cur = c.execute(f"INSERT INTO checkpoints({cols}) VALUES ({','.join('?' * len(data))})",
                        tuple(data.values()))
        return int(cur.lastrowid)

    # --- agents -----------------------------------------------------------
    def running_agents(self, c: sqlite3.Connection, worktree_id: int) -> list[sqlite3.Row]:
        return c.execute(
            "SELECT * FROM agent_runs WHERE worktree_id=? AND status IN ('running','unknown')",
            (worktree_id,),
        ).fetchall()

    def list_sessions(self, c: sqlite3.Connection, worktree_id: int) -> list[sqlite3.Row]:
        return c.execute(
            "SELECT * FROM agent_sessions WHERE worktree_id=? ORDER BY updated_at DESC, id DESC", (worktree_id,)
        ).fetchall()

    def upsert_session(self, c: sqlite3.Connection, worktree_id: int, agent: str, session_id: str) -> None:
        ts = now_iso()
        c.execute(
            "INSERT INTO agent_sessions(worktree_id, agent, session_id, created_at, updated_at) VALUES (?,?,?,?,?) "
            "ON CONFLICT(worktree_id, agent, session_id) DO UPDATE SET updated_at=excluded.updated_at",
            (worktree_id, agent, session_id, ts, ts),
        )
