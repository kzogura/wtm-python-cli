"""wtm review: 作業の継続・分割・終了を判断するためのルールベースのレポート。

AI による分析は行わない。Markdown / JSON で出力して AI に渡すことはできる。
Git・Gitea への書き込みは行わない。
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .errors import WtError
from .gitops import find_worktree
from .service import App, base_ref_for, collect_status, days_since, get_managed

REVIEW_SCHEMA_VERSION = 1
MAX_COMMITS = 20
_CHECKLIST = re.compile(r"^\s*[-*+]\s+\[( |x|X)\]\s+(.+?)\s*$")
ACTION_LABELS = {"continue": "継続", "split": "分割を検討", "finish": "終了を検討"}


def parse_checklist(body: str) -> tuple[list[str], list[str]]:
    """Issue 本文のチェックリストを (完了, 未完了) に分ける。コードブロック内は無視する。"""
    done: list[str] = []
    todo: list[str] = []
    fence: str | None = None
    for line in (body or "").splitlines():
        marker = line.lstrip()[:3]
        if marker in ("```", "~~~"):
            if fence is None:
                fence = marker
            elif marker == fence:
                fence = None
            continue
        if fence is None and (m := _CHECKLIST.match(line)):
            (todo if m.group(1) == " " else done).append(m.group(2))
    return done, todo


def _changed_files(app: App, path: Path, base: str) -> set[str] | None:
    """基準との分岐点からの変更と未コミット・未追跡ファイル。取得できなければ None。"""
    committed = app.git.changed_files(path, base)
    if committed is None:
        return None
    return set(committed) | set(app.git.status(path).all_files())


def _find_overlaps(app: App, row: Any, own_files: set[str]) -> tuple[list[dict[str, Any]], list[str]]:
    overlaps: list[dict[str, Any]] = []
    warnings: list[str] = []
    worktrees = app.git.worktree_list(app.repo)
    with app.db.read() as c:
        others = [r for r in app.db.list_worktrees(c) if r["id"] != row["id"] and r["provision"] == "ready"]
    for other in others:
        path = Path(other["path"])
        if find_worktree(worktrees, path) is None or not path.is_dir():
            continue
        try:
            files = _changed_files(app, path, base_ref_for(app, other))
        except WtError as e:
            warnings.append(f"Issue #{other['issue_number']} の変更ファイルを取得できません: {e.message}")
            continue
        if files is None:
            warnings.append(f"Issue #{other['issue_number']} の変更ファイルを取得できません。")
            continue
        if shared := sorted(own_files & files):
            overlaps.append({"issue": other["issue_number"], "path": other["path"], "files": shared})
    return overlaps, warnings


def collect_review(app: App, issue_number: int, *, offline: bool = False, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    row = get_managed(app, issue_number)
    status = collect_status(app, issue_number, refresh_issue=not offline)
    warnings = list(status["warnings"])
    findings: list[dict[str, str]] = []

    def add(rule: str, severity: str, message: str) -> None:
        findings.append({"rule": rule, "severity": severity, "message": message})

    issue = status["issue"]
    git = status["git"]
    pr = status["pr"]
    cp = status["last_checkpoint"]
    mgmt = app.config.management
    done, todo = parse_checklist(issue["body"] if issue else "")

    # Issue と終了条件
    if todo:
        add("definition_of_done_incomplete", "warn", f"Issue の終了条件が未完了です（{len(done)}/{len(done) + len(todo)} 件完了）。")
    elif not done:
        add("definition_of_done_unknown", "info", "Issue 本文にチェックリストが無いため、終了条件を判定できません。")
    if issue and issue["state"] == "closed":
        add("issue_closed", "info", "Issue はクローズ済みです。")

    # Checkpoint
    cp_days = days_since(cp["created_at"], now) if cp else None
    if cp is None:
        add("checkpoint_missing", "warn", "Checkpoint がありません。")
    elif cp_days is not None and cp_days >= mgmt.stale_days:
        add("checkpoint_stale", "warn", f"最終 Checkpoint から {cp_days} 日経過しています（基準 {mgmt.stale_days} 日）。")
    if cp and (cp["blockers"] or "").strip():
        add("blockers_recorded", "warn", "最新の Checkpoint にブロッカーが記録されています。")
    if row["state"] == "BLOCKED":
        reason = f"（理由: {status['blocked_reason']}）" if status["blocked_reason"] else ""
        add("state_blocked", "warn", f"作業状態が BLOCKED です{reason}。")
    if row["state"] == "DONE":
        add("state_done", "info", "作業状態が DONE です。")
    for r in status["agent_runs"]:
        add("agent_may_be_running", "warn", f"{r['agent']} が動作中の可能性があります (pid={r['pid']})。")

    # PR
    if pr:
        rule = {"merged": "pr_merged", "closed": "pr_closed"}.get(pr["status"], "pr_exists")
        severity = "warn" if pr["status"] == "closed" else "info"
        add(rule, severity, f"PR #{pr['number']} は {pr['status']} です。")

    # Git
    commits: list[dict[str, str]] = []
    changed: list[str] = []
    overlaps: list[dict[str, Any]] = []
    uncommitted: list[str] = []
    if not git.get("exists"):
        add("worktree_missing", "error", "worktree が存在しないため Git の状態を確認できません。")
    else:
        path = Path(row["path"])
        base = git["base"]
        uncommitted = sorted(set(git["staged"] + git["modified"] + git["untracked"] + git["conflicted"]))
        commits = app.git.commits(path, f"{base}..HEAD", MAX_COMMITS)
        files = _changed_files(app, path, base)
        if files is None:
            warnings.append(f"基準 {base} との差分を取得できません。")
            files = set(uncommitted)
        changed = sorted(files)
        if git["conflicted"]:
            add("conflicted", "error", f"コンフリクト中のファイルが {len(git['conflicted'])} 件あります。")
        if uncommitted:
            add("uncommitted_changes", "warn", f"未コミットのファイルが {len(uncommitted)} 件あります。")
        ahead, behind = git.get("ahead") or 0, git.get("behind") or 0
        if max(ahead, behind) >= mgmt.diverged_commits:
            add("branch_diverged", "warn", f"基準 {base} から大きく乖離しています（+{ahead} / -{behind}）。")
        if len(changed) >= mgmt.large_change_files:
            add("large_change", "warn", f"変更ファイルが {len(changed)} 件あります（基準 {mgmt.large_change_files} 件）。")
        overlaps, overlap_warnings = _find_overlaps(app, row, files)
        warnings += overlap_warnings
        for o in overlaps:
            add("file_overlap", "warn", f"Issue #{o['issue']} と {len(o['files'])} 件のファイルが重複しています。")

    recommendation = _recommend(findings, done, todo, bool(commits))
    return {
        "schema_version": REVIEW_SCHEMA_VERSION,
        "generated_at": now.isoformat(timespec="seconds"),
        "issue": issue,
        "issue_number": issue_number,
        "state": row["state"],
        "worktree": row["path"],
        "branch": git.get("branch") or row["branch"],
        "definition_of_done": {"done": done, "todo": todo},
        "git": {
            "exists": bool(git.get("exists")),
            "base": git.get("base"),
            "ahead": git.get("ahead"),
            "behind": git.get("behind"),
            "commits": commits,
            "changed_files": changed,
            "uncommitted": uncommitted,
        },
        "pr": pr,
        "pr_checked": not offline,
        "last_checkpoint": {k: cp[k] for k in ("created_at", "head", "completed", "remaining", "next_action", "blockers")}
        if cp else None,
        "overlaps": overlaps,
        "findings": findings,
        "recommendation": recommendation,
        "warnings": warnings,
    }


def _recommend(findings: list[dict[str, str]], done: list[str], todo: list[str], has_commits: bool) -> dict[str, Any]:
    rules = {f["rule"] for f in findings}
    if "pr_merged" in rules:
        action, reasons = "finish", ["PR がマージ済みです。worktree の整理候補です。"]
    elif "state_done" in rules:
        action, reasons = "finish", ["作業状態が DONE です。worktree の整理候補です。"]
    elif "issue_closed" in rules:
        action, reasons = "finish", ["Issue がクローズ済みです。残作業が無いか確認してください。"]
    elif done and not todo and has_commits:
        action, reasons = "finish", ["Issue の終了条件がすべて完了しています。"]
        if "uncommitted_changes" in rules:
            reasons.append("終了の前に未コミットの変更を確認してください。")
    elif rules & {"large_change", "file_overlap"}:
        action, reasons = "split", []
        if "large_change" in rules:
            reasons.append("変更範囲が大きくなっています。Issue の目的を超えていないか確認してください。")
        if "file_overlap" in rules:
            reasons.append("他の worktree と変更ファイルが重複しています。マージ時に競合する可能性があります。")
    else:
        action, reasons = "continue", []
        if todo:
            reasons.append(f"未完了の終了条件が {len(todo)} 件あります。")
        if "blockers_recorded" in rules or "state_blocked" in rules:
            reasons.append("先にブロッカーを解消してください。")
        if not reasons:
            reasons.append("終了を示す条件はまだ満たされていません。")
    return {"action": action, "label": ACTION_LABELS[action], "reasons": reasons}


def _quote(text: str) -> list[str]:
    return [f"> {line}".rstrip() for line in text.strip().splitlines()]


def render_markdown(report: dict[str, Any]) -> str:
    n = report["issue_number"]
    issue = report["issue"]
    rec = report["recommendation"]
    git = report["git"]
    title = issue["title"] if issue else ""
    lines = [f"# wtm review: Issue #{n} {title}".rstrip(), "", f"生成日時: {report['generated_at']}", ""]

    lines += [f"## 判断材料: {rec['label']}", ""]
    lines += [f"- {r}" for r in rec["reasons"]]
    lines += ["", "規則による機械的な判定です。最終判断は人が行ってください。", ""]

    lines += ["## 検出事項", ""]
    if report["findings"]:
        lines += [f"- **{f['severity']}** `{f['rule']}`: {f['message']}" for f in report["findings"]]
    else:
        lines.append("- なし")
    for o in report["overlaps"]:
        lines += [f"  - Issue #{o['issue']} との重複: " + ", ".join(f"`{x}`" for x in o["files"][:20])]
    for w in report["warnings"]:
        lines.append(f"- **warning**: {w}")
    lines.append("")

    lines += ["## Issue", ""]
    if issue:
        lines += [f"- 状態: {issue['state']}"]
        if issue["url"]:
            lines += [f"- URL: {issue['url']}"]
        lines += [""] + (_quote(issue["body"]) or ["（本文なし）"]) + [""]
    else:
        lines += ["（Issue 情報なし）", ""]
    dod = report["definition_of_done"]
    if dod["done"] or dod["todo"]:
        lines += ["### 終了条件", ""]
        lines += [f"- [x] {x}" for x in dod["done"]]
        lines += [f"- [ ] {x}" for x in dod["todo"]]
        lines.append("")

    lines += ["## 作業状態", "", f"- 作業状態: {report['state']}", f"- worktree: `{report['worktree']}`",
              f"- ブランチ: `{report['branch'] or '-'}`"]
    pr = report["pr"]
    if pr:
        lines.append(f"- PR: #{pr['number']} {pr['status']} {pr['url']}")
    else:
        lines.append("- PR: なし" if report["pr_checked"] else "- PR: 未取得（--offline）")
    if git["exists"]:
        lines.append(f"- 基準 `{git['base']}` との差: +{git['ahead']} / -{git['behind']}")
    lines.append("")

    if git["exists"]:
        lines += [f"## コミット（新しい順、最大 {MAX_COMMITS} 件）", ""]
        lines += [f"- `{c['sha']}` {c['date']} {c['subject']}" for c in git["commits"]] or ["- なし"]
        lines += ["", "## 変更ファイル", ""]
        lines += [f"- `{f}`" for f in git["changed_files"]] or ["- なし"]
        if git["uncommitted"]:
            lines += ["", "## 未コミットのファイル", ""]
            lines += [f"- `{f}`" for f in git["uncommitted"]]
        lines.append("")

    cp = report["last_checkpoint"]
    lines += ["## 最新の Checkpoint", ""]
    if cp:
        lines += [f"日時: {cp['created_at']}", ""]
        for label, key in (("完了事項", "completed"), ("未完了事項", "remaining"),
                           ("次のアクション", "next_action"), ("ブロッカー", "blockers")):
            lines += [f"### {label}", "", (cp[key] or "").strip() or "（なし）", ""]
    else:
        lines += ["（なし）", ""]
    return "\n".join(lines)
