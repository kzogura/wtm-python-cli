"""Claude Code のセッション保存先を読み取るための互換性レイヤー（読み取り専用）。

Claude Code の保存形式は非公開で変更され得る。公式 CLI に一覧・出力の機能が無いため
（2.1.318 の `claude --help` で確認）、内部形式への依存はこのモジュールに閉じ込める。

確認した形式（2.1.309〜2.1.318）:
- `<設定ディレクトリ>/projects/<変換後のパス>/<セッション ID>.jsonl`
- 各行が JSON オブジェクトで `type` を持つ。会話は `user` / `assistant` の `message.content`。
- `cwd`・`timestamp`・`version` は該当する行にのみ存在し、`timestamp` は順不同。

ディレクトリ名の変換規則は公開仕様ではないため使わず、各ファイルの `cwd` で対応付ける。
破損行は読み飛ばし、形式を認識できない場合は UnsupportedSessionFormatError にする。
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC
from pathlib import Path
from typing import Any

from .db import parse_iso
from .errors import WtError
from .paths import is_within

SUPPORTED_MAJOR_VERSION = 2
HEAD_LINES = 200
TITLE_LENGTH = 60
SESSION_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")


class UnsupportedSessionFormatError(WtError):
    """未対応のセッション形式・バージョン。"""


def claude_config_dir() -> Path:
    base = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(base) if base else Path.home() / ".claude"


def claude_projects_dir() -> Path:
    return claude_config_dir() / "projects"


@dataclass(frozen=True)
class RegisteredProcess:
    pid: int
    cwd: str
    session_id: str | None = None


def registered_processes(config_dir: Path | None = None) -> list[RegisteredProcess]:
    """Claude Code が `sessions/<pid>.json` に残す起動中プロセスの記録を読む（2.1.318 で確認）。

    wtm を通さず起動した Claude の検出に使う。非公開形式のため、読めない記録は無視する。
    停止後もファイルが残ることがあるので、呼び出し側で PID の生存を確認すること。
    """
    directory = (config_dir or claude_config_dir()) / "sessions"
    found: list[RegisteredProcess] = []
    try:
        files = sorted(directory.glob("*.json"))
    except OSError:
        return found
    for file in files:
        try:
            data = json.loads(file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        pid, cwd, session_id = data.get("pid"), data.get("cwd"), data.get("sessionId")
        if isinstance(pid, int) and isinstance(cwd, str) and cwd:
            found.append(RegisteredProcess(pid, cwd, session_id if isinstance(session_id, str) else None))
    return found


@dataclass
class SessionHead:
    session_id: str
    path: Path
    cwd: str | None


@dataclass
class SessionSummary:
    session_id: str
    path: Path
    cwd: str | None = None
    version: str | None = None
    title: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    message_count: int = 0
    skipped_lines: int = 0
    format: str = "ok"  # ok | unsupported
    format_detail: str = ""


@dataclass
class Message:
    role: str  # user | assistant
    timestamp: str | None
    text: str
    images: int = 0
    tools: list[dict[str, Any]] = field(default_factory=list)
    tool_results: list[Any] = field(default_factory=list)


def _iter_lines(path: Path) -> Iterator[dict[str, Any] | None]:
    """各行のオブジェクトを返す。解釈できない行は None。"""
    try:
        with open(path, "rb") as f:
            for raw in f:
                if not raw.strip():
                    continue
                try:
                    record = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, ValueError):
                    yield None
                    continue
                yield record if isinstance(record, dict) else None
    except OSError as e:
        raise WtError(f"セッションファイルを読み込めません: {path}: {e}") from e


def _parse_message(record: dict[str, Any]) -> Message | None:
    """会話の 1 レコードを解釈する。形式が想定外なら None。"""
    message = record.get("message")
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    result = Message(record["type"], record.get("timestamp"), "")
    if isinstance(content, str):
        result.text = content
        return result
    if not isinstance(content, list):
        return None
    texts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text" and isinstance(block.get("text"), str):
            texts.append(block["text"])
        elif kind == "image":
            result.images += 1
        elif kind == "tool_use":
            result.tools.append({"name": block.get("name"), "input": block.get("input")})
        elif kind == "tool_result":
            result.tool_results.append(block.get("content"))
        # thinking などは閲覧・出力の対象にしない
    result.text = "\n".join(texts)
    return result


def _conversation(path: Path) -> Iterator[tuple[dict[str, Any] | None, Message | None]]:
    """(レコード, 会話メッセージ) を返す。サブエージェントとメタ情報は会話に含めない。"""
    seen: set[str] = set()
    for record in _iter_lines(path):
        message = None
        if record is not None and record.get("type") in ("user", "assistant"):
            uuid = record.get("uuid")
            duplicated = isinstance(uuid, str) and uuid in seen
            if isinstance(uuid, str):
                seen.add(uuid)
            if not duplicated and not record.get("isSidechain") and not record.get("isMeta"):
                message = _parse_message(record)
                if message is None:
                    record = {**record, "_malformed": True}
        yield record, message


def read_summary(path: Path) -> SessionSummary:
    """ファイル全体からメタ情報を求める。形式の問題は例外にせず format に記録する。"""
    summary = SessionSummary(path.stem, path)
    typed = conversation_records = malformed = 0
    first_user: str | None = None
    ai_title: str | None = None
    first_ts = last_ts = None
    versions: set[str] = set()
    line_count = 0
    for record, message in _conversation(path):
        line_count += 1
        if record is None:
            summary.skipped_lines += 1
            continue
        if isinstance(record.get("type"), str):
            typed += 1
        if isinstance(record.get("cwd"), str) and summary.cwd is None:
            summary.cwd = record["cwd"]
        if isinstance(record.get("version"), str):
            versions.add(record["version"])
            summary.version = record["version"]
        ts = parse_iso(record.get("timestamp")) if isinstance(record.get("timestamp"), str) else None
        if ts is not None:
            first_ts = ts if first_ts is None or ts < first_ts else first_ts
            last_ts = ts if last_ts is None or ts > last_ts else last_ts
        if record.get("type") == "ai-title" and isinstance(record.get("aiTitle"), str):
            ai_title = record["aiTitle"]
        if record.get("_malformed"):
            malformed += 1
        if message is not None:
            conversation_records += 1
            if message.text.strip():
                summary.message_count += 1
                if first_user is None and message.role == "user":
                    first_user = message.text
    title = (ai_title or first_user or "").strip()
    summary.title = title.splitlines()[0][:TITLE_LENGTH] if title else None
    if first_ts:
        summary.created_at = first_ts.astimezone(UTC).isoformat(timespec="seconds")
    if last_ts:
        summary.updated_at = last_ts.astimezone(UTC).isoformat(timespec="seconds")

    unsupported_versions = sorted(v for v in versions if not _supported_version(v))
    if line_count and typed == 0:
        summary.format, summary.format_detail = "unsupported", "認識できる行がありません"
    elif malformed and conversation_records == 0:
        summary.format, summary.format_detail = "unsupported", "会話の形式を解釈できません"
    elif unsupported_versions:
        summary.format = "unsupported"
        summary.format_detail = f"未対応のバージョンです: {', '.join(unsupported_versions)}"
    return summary


def _supported_version(version: str) -> bool:
    major = version.split(".", 1)[0]
    return major.isdigit() and int(major) == SUPPORTED_MAJOR_VERSION


def read_messages(path: Path, *, include_tools: bool = False) -> tuple[SessionSummary, list[Message]]:
    """会話を読み込む。未対応の形式は例外にする。"""
    summary = read_summary(path)
    if summary.format != "ok":
        raise UnsupportedSessionFormatError(
            f"このセッションの形式には対応していません: {path}（{summary.format_detail}）",
            hint="`wtm` の対応バージョンを確認してください。バックアップされたファイル自体は変更されません。")
    messages = []
    for _, message in _conversation(path):
        if message is None:
            continue
        has_tools = include_tools and (message.tools or message.tool_results)
        if message.text.strip() or message.images or has_tools:
            messages.append(message)
    return summary, messages


class ClaudeSessionStore:
    def __init__(self, projects_dir: Path) -> None:
        self.projects_dir = Path(projects_dir)

    @classmethod
    def default(cls) -> ClaudeSessionStore:
        return cls(claude_projects_dir())

    def available(self) -> bool:
        return self.projects_dir.is_dir()

    def scan(self) -> list[SessionHead]:
        """全プロジェクトのセッションを、各ファイルの先頭だけ読んで列挙する。"""
        heads: list[SessionHead] = []
        try:
            project_dirs = [d for d in self.projects_dir.iterdir() if d.is_dir() and not d.is_symlink()]
        except OSError:
            return heads
        for directory in project_dirs:
            try:
                files = list(directory.glob("*.jsonl"))
            except OSError:
                continue
            for path in files:
                if path.is_symlink() or not SESSION_ID_PATTERN.fullmatch(path.stem):
                    continue
                heads.append(SessionHead(path.stem, path, self._read_cwd(path)))
        return heads

    @staticmethod
    def _read_cwd(path: Path) -> str | None:
        try:
            for index, record in enumerate(_iter_lines(path)):
                if index >= HEAD_LINES:
                    break
                if record is not None and isinstance(record.get("cwd"), str):
                    return record["cwd"]
        except WtError:
            pass
        return None

    def find(self, session_id: str) -> Path | None:
        return next((h.path for h in self.scan() if h.session_id == session_id), None)

    def for_project(self, project_path: Path) -> list[SessionHead]:
        return [h for h in self.scan() if h.cwd and is_within(Path(h.cwd), project_path)]


# ---------------------------------------------------------------------------
# 出力
# ---------------------------------------------------------------------------
ROLE_LABELS = {"user": "ユーザー", "assistant": "Claude"}


def render_markdown(summary: SessionSummary, messages: list[Message], *, origin: str,
                    include_tools: bool = False) -> str:
    lines = [f"# Claude Code セッション {summary.session_id}", ""]
    for label, value in (("タイトル", summary.title), ("開始", summary.created_at), ("最終更新", summary.updated_at),
                         ("作業ディレクトリ", summary.cwd), ("Claude Code", summary.version), ("参照元", origin)):
        if value:
            lines.append(f"- {label}: {value}")
    if summary.skipped_lines:
        lines.append(f"- 読み飛ばした破損行: {summary.skipped_lines}")
    lines.append("")
    for m in messages:
        lines += [f"## {ROLE_LABELS.get(m.role, m.role)} ({m.timestamp or '日時不明'})", ""]
        if m.text.strip():
            lines += [m.text.strip(), ""]
        if m.images:
            lines += [f"（画像 {m.images} 件）", ""]
        if include_tools:
            for tool in m.tools:
                body = json.dumps(tool["input"], ensure_ascii=False, indent=2, default=str)
                lines += [f"ツール呼び出し: {tool['name']}", "", "````json", body, "````", ""]
            for result in m.tool_results:
                body = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, indent=2, default=str)
                lines += ["ツール結果:", "", "````", str(body), "````", ""]
    return "\n".join(lines)


def to_export_dict(summary: SessionSummary, messages: list[Message], *, origin: str,
                   include_tools: bool = False) -> dict[str, Any]:
    def message_dict(m: Message) -> dict[str, Any]:
        data: dict[str, Any] = {"role": m.role, "timestamp": m.timestamp, "text": m.text, "images": m.images}
        if include_tools:
            data["tools"] = m.tools
            data["tool_results"] = m.tool_results
        return data

    return {
        "schema_version": 1,
        "agent": "claude",
        "session_id": summary.session_id,
        "title": summary.title,
        "created_at": summary.created_at,
        "updated_at": summary.updated_at,
        "cwd": summary.cwd,
        "version": summary.version,
        "origin": origin,
        "skipped_lines": summary.skipped_lines,
        "messages": [message_dict(m) for m in messages],
    }
