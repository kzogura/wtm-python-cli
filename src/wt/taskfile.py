"""TASK.md の生成と、Git 追跡対象外にするための info/exclude 登録。

リポジトリ共有の .gitignore は変更せず、ローカル専用の `<git-common-dir>/info/exclude`
に `/TASK.md` を追記する（全 worktree 共通。追跡済みの TASK.md には影響しない）。
"""

from __future__ import annotations

from pathlib import Path

from .forge import Issue

EXCLUDE_MARKER = "# added by wtm (worktree-local file)"

TEMPLATE = """# 作業管理

## Issue
#{number}{url_suffix}

## Goal
{title}

{body}

## Scope
（作業対象を記入）

## Out of Scope
（対象外を記入。Issue の目的を超える作業は新しい Issue に分割すること）

## Definition of Done
（完了条件を記入）

## Current Status
PLANNED

## Completed

## Remaining

## Next Action

## Notes
"""


def render_task(issue: Issue) -> str:
    body = issue.body.strip() or "（Issue 本文なし）"
    return TEMPLATE.format(
        number=issue.number,
        url_suffix=f" ({issue.url})" if issue.url else "",
        title=issue.title,
        body=body,
    )


def write_task_file(path: Path, issue: Issue) -> bool:
    """新規作成のみ。既に存在する場合は書かずに False を返す。"""
    try:
        with open(path, "x", encoding="utf-8", newline="\n") as f:
            f.write(render_task(issue))
    except FileExistsError:
        return False
    return True


def ensure_excluded(common_dir: Path, file_name: str) -> bool:
    """info/exclude に `/<file_name>` を追記する。追記したら True。"""
    exclude = common_dir / "info" / "exclude"
    pattern = f"/{file_name}"
    existing = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
    if pattern in (line.strip() for line in existing.splitlines()):
        return False
    exclude.parent.mkdir(parents=True, exist_ok=True)
    with open(exclude, "a", encoding="utf-8", newline="\n") as f:
        if existing and not existing.endswith("\n"):
            f.write("\n")
        f.write(f"{EXCLUDE_MARKER}\n{pattern}\n")
    return True
