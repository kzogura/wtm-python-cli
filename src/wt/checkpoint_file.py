"""AI が生成した Checkpoint ファイル（Markdown / JSON）の読み込み。"""

from __future__ import annotations

import json
import re
from pathlib import Path

from .errors import WtError

MAX_FILE_BYTES = 1024 * 1024
FIELDS = ("completed", "remaining", "next_action", "blockers")
ALIASES = {
    "completed": "completed", "done": "completed", "完了事項": "completed", "完了": "completed",
    "remaining": "remaining", "todo": "remaining", "未完了事項": "remaining", "未完了": "remaining",
    "残作業": "remaining",
    "next action": "next_action", "next_action": "next_action", "next": "next_action",
    "次のアクション": "next_action",
    "blockers": "blockers", "blocker": "blockers", "ブロッカー": "blockers",
}
_HEADING = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")


def _alias(name: str) -> str | None:
    return ALIASES.get(name.strip().rstrip(":：").strip().lower())


def _parse_markdown(text: str, path: Path) -> tuple[dict[str, str], list[str]]:
    buckets: dict[str, list[str]] = {}
    warnings: list[str] = []
    level: int | None = None  # 最初に認識した見出しのレベル。それより深い見出しは本文として扱う
    current: str | None = None
    fence: str | None = None
    for line in text.splitlines():
        stripped = line.lstrip()
        marker = stripped[:3]
        if marker in ("```", "~~~"):
            if fence is None:
                fence = marker
            elif marker == fence:
                fence = None
        m = _HEADING.match(line) if fence is None else None
        if m and (level is None or len(m.group(1)) <= level):
            field = _alias(m.group(2))
            if field is not None:
                if field in buckets:
                    raise WtError(f"Checkpoint ファイルの見出しが重複しています: {m.group(2)} ({path})")
                level = len(m.group(1)) if level is None else level
                current = field
                buckets[field] = []
                continue
            if level is not None:  # 同レベル以上の未知の見出しでセクションを閉じる
                warnings.append(f"未対応の見出しを無視しました: {m.group(2)}")
                current = None
            continue
        if current is not None:
            buckets[current].append(line)
    if not buckets:
        raise WtError(f"Checkpoint ファイルに認識できる見出しがありません: {path}",
                      hint="## Completed / ## Remaining / ## Next Action / ## Blockers を使用してください。")
    return {k: "\n".join(v).strip() for k, v in buckets.items()}, warnings


def _parse_json(text: str, path: Path) -> tuple[dict[str, str], list[str]]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise WtError(f"Checkpoint ファイルの JSON が不正です: {path}: {e}") from e
    if not isinstance(data, dict):
        raise WtError(f"Checkpoint ファイルの JSON はオブジェクトにしてください: {path}")
    values: dict[str, str] = {}
    warnings: list[str] = []
    for key, value in data.items():
        field = _alias(key)
        if field is None:
            warnings.append(f"未対応の項目を無視しました: {key}")
            continue
        if isinstance(value, list) and all(isinstance(x, str) for x in value):
            value = "\n".join(f"- {x}" for x in value)
        if value is None:
            value = ""
        if not isinstance(value, str):
            raise WtError(f"Checkpoint ファイルの {key} は文字列または文字列の配列にしてください: {path}")
        values[field] = value.strip()
    return values, warnings


def load_checkpoint_file(path: Path) -> tuple[dict[str, str], list[str]]:
    """(completed/remaining/next_action/blockers の辞書, 警告) を返す。"""
    try:
        size = path.stat().st_size
        if size > MAX_FILE_BYTES:
            raise WtError(f"Checkpoint ファイルが大きすぎます（上限 1 MiB）: {path}")
        text = path.read_bytes().decode("utf-8-sig")
    except OSError as e:
        raise WtError(f"Checkpoint ファイルを読み込めません: {path}: {e}") from e
    except UnicodeDecodeError as e:
        raise WtError(f"Checkpoint ファイルは UTF-8 で保存してください: {path}") from e
    parser = _parse_json if path.suffix.lower() == ".json" else _parse_markdown
    parsed, warnings = parser(text, path)
    if not any(parsed.values()):
        raise WtError(f"Checkpoint ファイルに内容がありません: {path}")
    return {f: parsed.get(f, "") for f in FIELDS}, warnings
