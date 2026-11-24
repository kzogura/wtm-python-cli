"""wtm の CLI（Typer + Rich）。処理本体は service モジュールにある。"""

from __future__ import annotations

import functools
import json
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.markdown import Markdown
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table

from . import __version__, claude_store, lifecycle, review as review_mod, service, sessions
from . import cleanup as cleanup_mod
from .agents import Capability
from .db import parse_iso
from .errors import ConflictError, SafetyError, WtError
from .forge import DEFAULT_PROVIDER, PROVIDER_LABEL, PROVIDERS
from .gitops import Git

app = typer.Typer(
    name="wtm",
    help="GitHub / Gitea の Issue を起点に Git worktree と AI エージェントを管理する CLI",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()
err_console = Console(stderr=True)

ConfigOpt = Annotated[str | None, typer.Option("--config", help="config.toml のパス（既定: WT_CONFIG → git config wt.config）")]


class State:
    config: str | None = None


state = State()


def _version_cb(value: bool) -> None:
    if value:
        console.print(f"wtm {__version__}")
        raise typer.Exit()


@app.callback()
def _main(
    config: ConfigOpt = None,
    version: Annotated[bool, typer.Option("--version", callback=_version_cb, is_eager=True, help="バージョン表示")] = False,
) -> None:
    state.config = config


def handle_errors(fn):
    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any):
        try:
            return fn(*args, **kwargs)
        except WtError as e:
            err_console.print(f"[bold red]エラー:[/] {escape(e.message)}")
            if e.hint:
                err_console.print(f"[yellow]ヒント:[/] {escape(e.hint)}")
            raise typer.Exit(e.exit_code) from None
    return wrapper


def _app() -> service.App:
    return service.open_app(state.config, Path.cwd())


def _warn(msgs: list[str]) -> None:
    for m in msgs:
        err_console.print(f"[yellow]警告:[/] {escape(m)}")


def _local_date(iso: str | None) -> str:
    dt = parse_iso(iso)
    return dt.astimezone().strftime("%Y-%m-%d %H:%M") if dt else "-"


PR_STYLES = {"open": "green", "draft": "cyan", "merged": "magenta", "closed": "dim"}


def _pr_label(pr: dict | None) -> str:
    if not pr:
        return "-"
    return f"[{PR_STYLES.get(pr['status'], 'white')}]#{pr['number']} {pr['status']}[/]"


def _print_json(data: Any) -> None:
    # Rich の装飾を避けて素の JSON を出す（他ツール連携用）
    typer.echo(_json_text(data))


def _json_text(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2, default=str)


def _run_agent(a: service.App, issue: int, agent: str, session_id: str | None = None,
               status: dict[str, Any] | None = None) -> service.LaunchResult:
    """エージェントを前面起動し、終了後の案内を表示する。"""
    path = service.get_managed(a, issue)["path"]
    action = f"{agent} のセッション {session_id} を再開" if session_id else f"{agent} を起動"
    console.print(f"[cyan]{action}します（cwd={escape(path)}）[/]")
    if not a.adapter(agent).supports(Capability.INSTRUCTIONS_FILE):
        console.print(f"[yellow]{agent} には指示を自動で渡せません。[/]起動後に次のように伝えてください: "
                      f"「{service.handoff_mod.WTM_DIR}/{service.handoff_mod.HANDOFF_FILE} を読んで作業してください」")
    res = service.launch_agent(a, issue, agent, session_id, status=status)
    if not res.resume_failed:
        console.print(f"{agent} が終了しました (exit {res.rc})。`wtm status {issue}` で状態を確認できます。")
        if res.session_id:
            console.print(f"[dim]セッション ID: {escape(res.session_id)}[/]")
    _warn_notes(res.notes)
    return res


def _warn_notes(notes: list[str]) -> None:
    for n in notes:
        console.print(f"[dim]{escape(n)}[/]")


# ---------------------------------------------------------------------------
@app.command()
@handle_errors
def init(
    base_branch: Annotated[str | None, typer.Option("--base-branch", help="基準ブランチ")] = None,
    worktree_root: Annotated[Path | None, typer.Option("--worktree-root", help="worktree 保存先")] = None,
    management_dir: Annotated[Path | None, typer.Option("--management-dir", help="管理ディレクトリ")] = None,
    remote: Annotated[str, typer.Option(help="リモート名")] = "origin",
    forge: Annotated[str, typer.Option(
        "--forge", help="Issue/PR の連携先: github（gh CLI）| gitea（tea CLI）", case_sensitive=False,
    )] = DEFAULT_PROVIDER,
    forge_login: Annotated[str | None, typer.Option("--forge-login", help="gitea: tea のログイン名")] = None,
    forge_host: Annotated[str | None, typer.Option(
        "--forge-host", help="github: GitHub Enterprise Server のホスト名（省略時は github.com）")] = None,
    forge_cli: Annotated[str | None, typer.Option("--forge-cli", help="CLI の実行ファイル（既定: gh / tea）")] = None,
    no_forge: Annotated[bool, typer.Option("--no-forge", help="Issue/PR 連携を無効にする（オフライン運用）")] = False,
    gitea_login: Annotated[str | None, typer.Option("--gitea-login", hidden=True)] = None,
    no_gitea: Annotated[bool, typer.Option("--no-gitea", hidden=True)] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="質問せず既定値を使う")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="作成内容の表示のみ")] = False,
) -> None:
    """管理対象リポジトリを登録する（既存 worktree は変更しない）。"""
    # 旧オプション（--gitea-login / --no-gitea）の互換
    if gitea_login is not None:
        forge, forge_login = "gitea", forge_login or gitea_login
    no_forge = no_forge or no_gitea
    forge = forge.lower()
    if forge not in PROVIDERS:
        raise WtError(f"--forge には {' / '.join(PROVIDERS)} のいずれかを指定してください: {forge}")
    if forge_login and forge != "gitea":
        raise WtError("--forge-login は --forge gitea のときだけ指定できます。",
                      hint="GitHub のアカウントは gh auth switch で切り替えてください。")
    if forge_host and forge != "github":
        raise WtError("--forge-host は --forge github のときだけ指定できます。",
                      hint="Gitea の接続先は tea login で設定し、--forge-login で選びます。")
    git = Git()
    plan = service.plan_init(Path.cwd(), git, provider=forge, forge_cli=forge_cli or "",
                             forge_host=forge_host or "", remote=remote,
                             management_dir=management_dir, worktree_root=worktree_root)
    _warn(plan.warnings)
    if plan.existing_config and plan.existing_config.exists():
        raise WtError(f"既に初期化されています: {plan.existing_config}")

    base = base_branch
    if base is None:
        if yes:
            base = plan.default_base_branch
        else:
            choices = ", ".join(plan.base_branch_candidates) or "なし"
            base = typer.prompt(f"基準ブランチ（候補: {choices}）", default=plan.default_base_branch)

    label = PROVIDER_LABEL[plan.forge_provider]
    forge_enabled = not no_forge and plan.forge_available
    login = forge_login or ""
    accounts = plan.forge_accounts
    if forge_enabled and plan.forge_provider == "gitea":
        if not login and accounts:
            if len(accounts) == 1 or yes:
                login = accounts[0]
            else:
                login = typer.prompt(f"tea ログイン（{', '.join(accounts)}）", default=accounts[0])
        if not accounts:
            _warn(["tea のログインが未設定です。`tea login add` 後に config.toml の [forge].login を設定してください。"])
    if forge_enabled and plan.forge_provider == "github":
        if (plan.forge_host or "github.com") not in accounts:
            _warn([f"gh が {plan.forge_host or 'github.com'} に未ログインです。"
                   f"`gh auth login{' --hostname ' + plan.forge_host if plan.forge_host else ''}` を実行してください。"])

    if not forge_enabled:
        forge_desc = "無効"
    elif plan.forge_provider == "github":
        forge_desc = f"有効 (gh, host={plan.forge_host or 'github.com'})"
    else:
        forge_desc = f"有効 (tea, login={login or '自動'})"
    table = Table(show_header=False, box=None)
    for k, v in [("リポジトリ", plan.repo_path), ("Git 共通ディレクトリ", plan.common_dir),
                 ("基準ブランチ", base), ("リモート", plan.remote),
                 ("worktree 保存先", plan.worktree_root), ("管理ディレクトリ", plan.management_dir),
                 (f"{label} 連携", forge_desc)]:
        table.add_row(f"[bold]{k}[/]", escape(str(v)))
    console.print(Panel(table, title="wtm init" + (" (dry-run)" if dry_run else "")))
    if dry_run:
        return
    cfg = service.execute_init(plan, git, base_branch=base, forge_enabled=forge_enabled, forge_login=login)
    console.print(f"[green]初期化しました:[/] {escape(str(cfg.config_path))}")


# ---------------------------------------------------------------------------
@app.command()
@handle_errors
def start(
    issue: Annotated[int | None, typer.Option("--issue", "-i", help="Issue 番号")] = None,
    new: Annotated[bool, typer.Option("--new", help="GitHub / Gitea に Issue を新規作成してから開始する")] = False,
    body: Annotated[str | None, typer.Option("--body", help="--new で作成する Issue の本文")] = None,
    body_file: Annotated[Path | None, typer.Option("--body-file", help="--new で作成する Issue の本文ファイル")] = None,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Issue 作成の確認を省略する")] = False,
    agent: Annotated[str | None, typer.Option("--agent", "-a", help="起動する AI エージェント (claude|codex)")] = None,
    no_launch: Annotated[bool, typer.Option("--no-launch", help="エージェントを起動しない")] = False,
    offline: Annotated[bool, typer.Option("--offline", help="GitHub / Gitea に問い合わせない（--title 必須）")] = False,
    title: Annotated[str | None, typer.Option("--title", help="Issue タイトル（--new / --offline 時）")] = None,
    slug: Annotated[str | None, typer.Option("--slug", help="ブランチ名の短い識別子")] = None,
    base: Annotated[str | None, typer.Option("--base", help="基準ブランチ（既定: 設定値）")] = None,
    allow_closed: Annotated[bool, typer.Option("--allow-closed", help="クローズ済み Issue でも作成する")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="作成内容の表示のみ")] = False,
) -> None:
    """Issue に対応する worktree を作成する。"""
    a = _app()
    agent_name = agent or a.config.agents.default
    if new:
        if issue is not None or offline:
            raise WtError("--new は --issue / --offline と同時に指定できません。")
        iss = _create_issue_interactively(a, title, body, body_file, yes=yes, dry_run=dry_run)
        if iss is None:
            return
        issue = iss.number
    elif issue is None:
        raise WtError("--issue で Issue 番号を指定するか、--new で Issue を作成してください。")
    else:
        iss = service.resolve_issue(a, issue, offline=offline, title=title)
    plan, warnings = service.plan_start(a, iss, slug=slug, base=base, allow_closed=allow_closed, fetch=not dry_run)
    _warn(warnings)
    table = Table(show_header=False, box=None)
    for k, v in [("Issue", f"#{iss.number} {iss.title} [{iss.state}]"), ("worktree", plan.path),
                 ("ブランチ", plan.branch + (" (新規)" if plan.new_branch else " (既存)")),
                 ("基準", plan.base_ref or "-"), ("エージェント", "起動しない" if no_launch else agent_name)]:
        table.add_row(f"[bold]{k}[/]", escape(str(v)))
    for n in plan.notes:
        table.add_row("[bold]注記[/]", escape(n))
    console.print(Panel(table, title="wtm start" + (" (dry-run)" if dry_run else "")))
    if dry_run:
        return
    console.print("worktree を作成しています（大きなリポジトリでは数分かかります）...")
    res = service.execute_start(a, plan, agent=None if no_launch else agent_name)
    _warn(res.warnings)
    console.print(f"[green]worktree を作成しました:[/] {escape(str(plan.path))}")
    if res.task_file:
        console.print(f"  TASK.md: {escape(str(res.task_file))}")
    if not no_launch:
        _run_agent(a, issue, agent_name)


def _create_issue_interactively(a: service.App, title: str | None, body: str | None, body_file: Path | None,
                                *, yes: bool, dry_run: bool):
    if body is not None and body_file is not None:
        raise WtError("--body と --body-file は同時に指定できません。")
    if title is None:
        title = typer.prompt("Issue タイトル").strip()
    if body_file is not None:
        try:
            body = body_file.read_text(encoding="utf-8")
        except OSError as e:
            raise WtError(f"本文ファイルを読み込めません: {body_file}: {e}") from e
    elif body is None:
        body = typer.prompt("Issue 本文（目的・終了条件。空でも可）", default="", show_default=False)
    repo = service.forge_repo_slug(a.config) or "(不明)"
    label = a.config.forge.label
    console.print(Panel(f"[bold]リポジトリ:[/] {escape(repo)}\n[bold]タイトル:[/] {escape(title)}\n\n"
                        f"{escape(body or '（本文なし）')}",
                        title=f"{label} に作成する Issue" + (" (dry-run)" if dry_run else "")))
    if dry_run:
        return None
    if not yes and not typer.confirm(f"この内容で {label} に Issue を作成しますか?", default=False):
        raise typer.Exit(1)
    iss = service.create_issue(a, title, body or "")
    console.print(f"[green]Issue #{iss.number} を作成しました:[/] {escape(iss.url)}")
    return iss


# ---------------------------------------------------------------------------
@app.command("list")
@handle_errors
def list_cmd(
    stale: Annotated[bool, typer.Option("--stale", help="放置 worktree のみ表示")] = False,
    as_json: Annotated[bool, typer.Option("--json", help="JSON で出力")] = False,
    wide: Annotated[bool, typer.Option("--wide", help="worktree パスも表示")] = False,
    offline: Annotated[bool, typer.Option("--offline", help="GitHub / Gitea に問い合わせない（PR 状態を表示しない）")] = False,
    archived: Annotated[bool, typer.Option("--archived", help="整理済み（ARCHIVED）の作業を表示")] = False,
) -> None:
    """管理対象 worktree を一覧表示する。"""
    a = _app()
    if archived:
        if stale or offline or wide:
            raise WtError("--archived と同時に指定できるオプションは --json だけです。")
        _list_archived(a, as_json)
        return
    data = service.collect_list(a, offline=offline)
    _warn(data["warnings"])
    if stale:
        data["worktrees"] = [w for w in data["worktrees"] if w["stale"]]
    if as_json:
        _print_json(data)
        return

    def changes(w: dict) -> str:
        if w["dirty"] is None:
            return "?"
        marks = ("M" if w["dirty"] else "") + ("U" if w["untracked"] else "")
        return f"[red]{marks}[/]" if marks else "-"

    table = Table(title=f"wtm list ({escape(data['repository'])})")
    cols = ["Issue", "タイトル", "状態", "AI", "ブランチ", "変更", "最終コミット", "最終CP", "PR", "確認"]
    if wide:
        cols.append("パス")
    for col in cols:
        table.add_column(col, overflow="fold")
    for w in data["worktrees"]:
        days = w["days_since_checked"]
        checked = "-" if days is None else f"{days}日前"
        cells = [
            f"#{w['issue']}", escape(w["title"] or ""), w["state"], w["agent"] or "-",
            escape(w["branch"] or "-"), changes(w), _local_date(w["last_commit_at"]),
            _local_date(w["last_checkpoint_at"]), _pr_label(w["pr"]),
            f"[red]{checked}[/]" if w["stale"] else checked,
        ]
        if wide:
            cells.append(escape(w["path"]))
        table.add_row(*cells, style="bold yellow" if w["stale"] else None)
    if data["worktrees"]:
        console.print(table)
        console.print("[dim]変更: M=未コミット変更 U=未追跡ファイル / 黄色=放置（"
                      f"{data['stale_days']}日以上未確認）[/]")
    else:
        console.print("管理対象の worktree はありません。")
    for w in data["worktrees"]:
        for m in w["warnings"]:
            err_console.print(f"[yellow]警告 #{w['issue']}:[/] {escape(m)}")
    if data["unmanaged"] and not stale:
        console.print("[dim]管理外の worktree（wtm adopt で取り込み可能）:[/]")
        for u in data["unmanaged"]:
            console.print(f"  [dim]{escape(u['path'])} ({escape(str(u['branch']))})[/]")


def _list_archived(a: service.App, as_json: bool) -> None:
    data = cleanup_mod.collect_archived(a)
    if as_json:
        _print_json(data)
        return
    if not data["archived"]:
        console.print("整理済みの作業はありません。")
        return
    table = Table(title="整理済みの作業")
    for col in ("Issue", "タイトル", "整理日時", "ブランチ", "記録"):
        table.add_column(col, overflow="fold")
    for e in data["archived"]:
        table.add_row(f"#{e['issue']}", escape(e["title"] or ""), _local_date(e["archived_at"]),
                      escape(e["branch"] or "-"), escape(e["archive"] or "-"))
    console.print(table)
    console.print("[dim]会話の履歴は wtm history <Issue> で参照できます。[/]")


# ---------------------------------------------------------------------------
@app.command()
@handle_errors
def status(
    issue: Annotated[int, typer.Argument(help="Issue 番号")],
    offline: Annotated[bool, typer.Option("--offline", help="GitHub / Gitea に問い合わせない")] = False,
    as_json: Annotated[bool, typer.Option("--json", help="JSON で出力")] = False,
) -> None:
    """対象 Issue の状態を表示する。"""
    a = _app()
    d = service.collect_status(a, issue, refresh_issue=not offline)
    if as_json:
        _print_json(d)
        return
    iss = d["issue"]
    wt_row = d["worktree"]
    g = d["git"]
    head = f"#{issue} {iss['title']}" if iss else f"#{issue}"
    lines = []
    if iss:
        lines += [f"[bold]Issue 状態:[/] {iss['state']}  {escape(iss['url'] or '')}",
                  f"[bold]目的・終了条件:[/]\n{escape(iss['body'] or '（本文なし）')}", ""]
    lines += [
        f"[bold]作業状態:[/] {wt_row['state']}    [bold]担当 AI:[/] {wt_row['agent'] or '-'}",
        *([f"[bold]停止理由:[/] {escape(d['blocked_reason'])}"] if d["blocked_reason"] else []),
        f"[bold]worktree:[/] {escape(wt_row['path'])}",
        f"[bold]作業開始:[/] {wt_row['created_at']}    [bold]最終確認:[/] {wt_row['last_checked_at']}",
    ]
    if g.get("exists"):
        lc = g["last_commit"]
        lines += [
            f"[bold]ブランチ:[/] {escape(g['branch'] or 'detached')}  基準 {escape(g['base'])} から "
            f"+{g['ahead'] if g['ahead'] is not None else '?'} / -{g['behind'] if g['behind'] is not None else '?'}",
            f"[bold]最終コミット:[/] {escape(lc['date'] + ' ' + lc['subject']) if lc else 'なし'}",
            f"[bold]変更:[/] staged {len(g['staged'])} / modified {len(g['modified'])} / "
            f"untracked {len(g['untracked'])} / conflicted {len(g['conflicted'])}",
        ]
        for f in (g["staged"] + g["modified"] + g["untracked"])[:20]:
            lines.append(f"    {escape(f)}")
    pr = d["pr"]
    if pr:
        lines.append(f"[bold]PR:[/] {_pr_label(pr)} {escape(pr['title'])}  {escape(pr['url'])}")
    else:
        lines.append("[bold]PR:[/] なし" if not offline else "[bold]PR:[/] 未取得（--offline）")
    sessions = d["sessions"]
    lines.append(f"[bold]AI セッション:[/] {len(sessions)} 件")
    for s in sessions:
        lines.append(f"    {s['agent']} {s['session_id']} {escape(s['title'] or '')}")
    for r in d["agent_runs"]:
        lines.append(f"[bold]実行中の可能性:[/] {r['agent']} pid={r['pid']} ({r['started_at']})")
    cp = d["last_checkpoint"]
    lines.append(f"[bold]最終 Checkpoint:[/] {cp['created_at'] if cp else 'なし'}")
    lines.append(f"[bold]次のアクション:[/] {escape(d['next_action'] or '未記録')}")
    console.print(Panel("\n".join(lines), title=escape(head)))
    _warn(d["warnings"])


# ---------------------------------------------------------------------------
@app.command()
@handle_errors
def adopt(
    path: Annotated[Path, typer.Argument(help="取り込む既存 worktree のパス")],
    issue: Annotated[int, typer.Option("--issue", "-i", help="対応する Issue 番号")],
    offline: Annotated[bool, typer.Option("--offline", help="GitHub / Gitea に問い合わせない（--title 必須）")] = False,
    title: Annotated[str | None, typer.Option("--title", help="オフライン時の Issue タイトル")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="検証のみ")] = False,
) -> None:
    """既存の worktree を管理対象に取り込む（ファイル・ブランチ・履歴は変更しない）。"""
    a = _app()
    iss = service.resolve_issue(a, issue, offline=offline, title=title)
    res = service.adopt(a, path, iss, dry_run=dry_run)
    if dry_run:
        console.print(f"[green]取り込み可能です:[/] {escape(str(res.path))} ({escape(str(res.branch))}) → Issue #{issue}")
        return
    console.print(f"[green]取り込みました:[/] {escape(str(res.path))} ({escape(str(res.branch))}) → Issue #{issue}")
    console.print(f"  初回 Checkpoint id={res.checkpoint_id}")


# ---------------------------------------------------------------------------
@app.command()
@handle_errors
def checkpoint(
    issue: Annotated[int, typer.Argument(help="Issue 番号")],
    completed: Annotated[str | None, typer.Option("--completed", help="完了事項")] = None,
    remaining: Annotated[str | None, typer.Option("--remaining", help="未完了事項")] = None,
    next_action: Annotated[str | None, typer.Option("--next", help="次のアクション")] = None,
    blockers: Annotated[str | None, typer.Option("--blockers", help="ブロッカー")] = None,
    from_file: Annotated[Path | None, typer.Option("--from-file", help="AI が生成した Checkpoint ファイル（Markdown / JSON）を取り込む")] = None,
    publish:Annotated[bool, typer.Option("--publish", help="Issue に進捗をコメント投稿する")] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="入力・投稿の確認を省略する")] = False,
) -> None:
    """現在の作業状態を Checkpoint として記録する（ファイル変更・コミットは行わない）。"""
    a = _app()
    row = service.get_managed(a, issue)
    values = {"completed": completed, "remaining": remaining, "next_action": next_action, "blockers": blockers}
    if from_file is not None:
        if any(v is not None for v in values.values()):
            raise WtError("--from-file と --completed / --remaining / --next / --blockers は同時に指定できません。")
        cp_id, file_warnings = service.import_checkpoint_file(a, row["id"], from_file)
        _warn(file_warnings)
    else:
        if not yes and all(v is None for v in values.values()):
            for key, label in (("completed", "完了事項"), ("remaining", "未完了事項"),
                               ("next_action", "次のアクション"), ("blockers", "ブロッカー")):
                values[key] = typer.prompt(label + "（空でも可）", default="", show_default=False)
        cp_id = service.create_checkpoint(a, row["id"], **{k: v or "" for k, v in values.items()})
    cp = service.get_checkpoint(a, cp_id)
    console.print(f"[green]Checkpoint を保存しました[/] (id={cp_id}): {escape(cp['file_path'])}")
    if not publish:
        return
    body = service.format_checkpoint_comment(cp, row["branch"])
    console.print(Panel(escape(body), title=f"Issue #{issue} に投稿するコメント"))
    if not yes and not typer.confirm(f"この内容を {a.config.forge.label} の Issue にコメント投稿しますか?", default=False):
        console.print("投稿しませんでした（Checkpoint はローカルに保存済みです）。")
        raise typer.Exit(1)
    service.publish_checkpoint(a, cp_id)
    console.print(f"[green]Issue #{issue} にコメントを投稿しました。[/]")


# ---------------------------------------------------------------------------
@app.command()
@handle_errors
def finish(
    issue: Annotated[int, typer.Argument(help="Issue 番号")],
    push: Annotated[bool, typer.Option("--push", help="ブランチをリモートに push する")] = False,
    create_pr: Annotated[bool, typer.Option("--create-pr", help="PR が無ければ作成する（gh / tea 経由）")] = False,
    no_pr: Annotated[bool, typer.Option("--no-pr", help="push / PR 作成の確認をせず REVIEW への遷移のみ行う")] = False,
    title: Annotated[str | None, typer.Option("--title", help="PR タイトル（既定: Issue タイトル）")] = None,
    draft: Annotated[bool, typer.Option("--draft", help="ドラフト PR として作成する")] = False,
    allow_dirty: Annotated[bool, typer.Option("--allow-dirty", help="未コミット変更があっても続行する")] = False,
    skip_checkpoint: Annotated[bool, typer.Option("--skip-checkpoint", help="Checkpoint が無くても続行する")] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="確認を省略する（--push/--create-pr の指定は必要）")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="確認結果の表示のみ")] = False,
) -> None:
    """作業をレビュー状態 (REVIEW) へ移行する。push / PR 作成は確認またはオプション指定時のみ。"""
    a = _app()
    plan = service.plan_finish(a, issue, allow_dirty=allow_dirty, skip_checkpoint=skip_checkpoint,
                               fetch=not dry_run)
    cp = plan.checkpoint
    if plan.remote_exists:
        push_state = "push 済み" if not plan.unpushed else f"未 push のコミット {plan.unpushed} 件"
    else:
        push_state = "リモートにブランチなし"
    table = Table(show_header=False, box=None)
    for k, v in [("Issue", f"#{issue} {plan.issue_title}"), ("作業状態", plan.state),
                 ("ブランチ", f"{plan.branch} → {plan.base_branch}"),
                 ("基準からのコミット", "?" if plan.ahead_of_base is None else str(plan.ahead_of_base)),
                 ("push 状態", push_state), ("最終 Checkpoint", cp["created_at"] if cp else "なし")]:
        table.add_row(f"[bold]{k}[/]", escape(str(v)))
    table.add_row("[bold]PR[/]", _pr_label(plan.pr.to_dict()) + f" {escape(plan.pr.url)}" if plan.pr else "なし")
    console.print(Panel(table, title="wtm finish" + (" (dry-run)" if dry_run else "")))
    _warn(plan.warnings)
    if plan.blockers:
        for b in plan.blockers:
            err_console.print(f"[red]中断理由:[/] {escape(b)}")
        raise typer.Exit(1)
    if dry_run:
        return

    if not no_pr and plan.can_create_pr and not create_pr and not yes:
        create_pr = typer.confirm(f"PR を作成しますか? ({plan.branch} → {plan.base_branch})", default=False)
    if create_pr and plan.can_create_pr and plan.needs_push and not push:
        if yes:
            raise WtError(f"ブランチ {plan.branch} が push されていません。", hint="--push を指定してください。")
        push = typer.confirm(f"{a.config.repository.remote} に {plan.branch} を push しますか?", default=False)
        if not push:
            raise WtError("push しないため PR を作成できません。中断しました。")
    if not create_pr and not push and plan.pr is None and plan.can_create_pr:
        console.print("[dim]PR は作成しません（後で wtm finish --push --create-pr で作成できます）。[/]")

    res = service.execute_finish(a, plan, push=push, create_pr=create_pr, title=title, draft=draft)
    if res.pushed:
        console.print(f"[green]push しました:[/] {escape(plan.branch)}")
    if res.pr_created and res.pr:
        console.print(f"[green]PR #{res.pr.number} を作成しました:[/] {escape(res.pr.url)}")
    console.print(f"[green]作業状態を {res.state} にしました。[/]")


# ---------------------------------------------------------------------------
@app.command("state")
@handle_errors
def state_cmd(
    issue: Annotated[int, typer.Argument(help="Issue 番号")],
    target: Annotated[str | None, typer.Argument(help="変更先 (WORKING|BLOCKED|DONE)。省略すると現在の状態と履歴を表示")] = None,
    reason: Annotated[str | None, typer.Option("--reason", help="変更理由（BLOCKED への変更などで必須）")] = None,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="確認を省略する")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="検査結果の表示のみ")] = False,
    allow_dirty: Annotated[bool, typer.Option("--allow-dirty", help="DONE: 未コミット変更があっても続行する")] = False,
    skip_checkpoint: Annotated[bool, typer.Option("--skip-checkpoint", help="DONE: Checkpoint が無くても続行する")] = False,
    offline: Annotated[bool, typer.Option("--offline", help="GitHub / Gitea に問い合わせない")] = False,
    as_json: Annotated[bool, typer.Option("--json", help="履歴を JSON で出力（変更先を省略した場合のみ）")] = False,
) -> None:
    """作業状態を変更する（WORKING / BLOCKED / DONE）。Issue トラッカーと Git には書き込まない。"""
    a = _app()
    if target is None:
        if reason is not None or yes or dry_run or allow_dirty or skip_checkpoint or offline:
            raise WtError("変更先を省略した場合に指定できるオプションは --json だけです。")
        info = lifecycle.state_info(a, issue)
        if as_json:
            _print_json(info)
            return
        table = Table(title=f"Issue #{issue}: {info['state']}")
        for col in ("日時", "変更前", "変更後", "理由"):
            table.add_column(col, overflow="fold")
        for h in info["history"]:
            table.add_row(_local_date(h["changed_at"]), h["from_state"] or "-", h["to_state"], escape(h["reason"]))
        console.print(table)
        return
    if as_json:
        raise WtError("--json は変更先を省略した場合だけ指定できます。")

    plan = lifecycle.plan_state_change(a, issue, target, allow_dirty=allow_dirty,
                                       skip_checkpoint=skip_checkpoint, offline=offline)
    if not plan.changed:
        console.print(f"作業状態は既に {plan.to_state} です。")
        return
    console.print(f"[bold]Issue #{issue}:[/] {plan.from_state} → {plan.to_state}" + (" (dry-run)" if dry_run else ""))
    _warn(plan.notices)
    for m in plan.confirmations:
        err_console.print(f"[yellow]確認:[/] {escape(m)}")
    if plan.blockers:
        for b in plan.blockers:
            err_console.print(f"[red]中断理由:[/] {escape(b)}")
        raise typer.Exit(1)
    if dry_run:
        if plan.reason_required and not (reason or "").strip():
            console.print("[dim]実行時には --reason が必要です。[/]")
        return
    if plan.reason_required and not (reason or "").strip() and not yes:
        reason = typer.prompt("変更理由")
    if plan.confirmations and not yes and not typer.confirm("続行しますか?", default=False):
        raise typer.Exit(1)
    lifecycle.execute_state_change(a, plan, reason)
    console.print(f"[green]作業状態を {plan.to_state} にしました。[/]")


# ---------------------------------------------------------------------------
@app.command()
@handle_errors
def resume(
    issue: Annotated[int, typer.Argument(help="Issue 番号")],
    agent: Annotated[str | None, typer.Option("--agent", "-a", help="AI エージェント（既定: 前回の担当）")] = None,
    session: Annotated[str | None, typer.Option("--session", help="再開するセッション ID（既定: 最後に使ったセッション）")] = None,
    new_session: Annotated[bool, typer.Option("--new-session", help="セッションを再開せず新規に起動する")] = False,
    offline: Annotated[bool, typer.Option("--offline", help="GitHub / Gitea に問い合わせない")] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="再開に失敗したら確認せず新規セッションで起動する")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="計画の表示のみ")] = False,
) -> None:
    """作業を再開する。セッションを再開できなければ Issue と Checkpoint を渡して新規に起動する。"""
    a = _app()
    plan = service.plan_resume(a, issue, agent=agent, session=session, new_session=new_session, offline=offline)
    iss = plan.status["issue"]
    cp = plan.status["last_checkpoint"]
    table = Table(show_header=False, box=None)
    for k, v in [("Issue", f"#{issue} {iss['title']} [{iss['state']}]" if iss else f"#{issue}"),
                 ("作業状態", plan.state), ("worktree", plan.path), ("エージェント", plan.agent),
                 ("セッション", f"{plan.session_id} を再開" if plan.session_id else "新規"),
                 ("最終 Checkpoint", cp["created_at"] if cp else "なし"),
                 ("次のアクション", plan.status["next_action"] or "未記録")]:
        table.add_row(f"[bold]{k}[/]", escape(str(v)))
    console.print(Panel(table, title="wtm resume" + (" (dry-run)" if dry_run else "")))
    _warn(plan.warnings)
    if dry_run:
        return
    res = _run_agent(a, issue, plan.agent, plan.session_id, status=plan.status)
    if not res.resume_failed:
        return
    err_console.print(f"[yellow]セッション {escape(str(plan.session_id))} を再開できませんでした (exit {res.rc})。[/]")
    if not yes and not typer.confirm("Issue と Checkpoint を渡して新規セッションで起動しますか?", default=True):
        raise typer.Exit(1)
    _run_agent(a, issue, plan.agent, status=plan.status)


# ---------------------------------------------------------------------------
ORIGIN_LABELS = {"live": "元の保存先", "backup": "バックアップ", "missing": "[dim]内容なし[/]"}
BACKUP_LABELS = {"current": "[green]最新[/]", "outdated": "[yellow]更新あり[/]", "none": "[yellow]なし[/]"}


@app.command()
@handle_errors
def history(
    issue: Annotated[int, typer.Argument(help="Issue 番号")],
    agent: Annotated[str | None, typer.Option("--agent", "-a", help="AI エージェントで絞り込む")] = None,
    as_json: Annotated[bool, typer.Option("--json", help="一覧を JSON で出力")] = False,
    session: Annotated[str | None, typer.Option("--session", "-s", help="表示するセッション ID（先頭一致可）")] = None,
    export: Annotated[Path | None, typer.Option("--export", help="--session の内容をファイルに出力（既存ファイルは上書きしない）")] = None,
    fmt: Annotated[str, typer.Option("--format", "-f", help="--session の出力形式 (md|json)")] = "md",
    tools: Annotated[bool, typer.Option("--tools", help="--session の出力にツールの入出力を含める")] = False,
    from_backup: Annotated[bool, typer.Option("--from-backup", help="--session でバックアップを読む")] = False,
    backup: Annotated[bool, typer.Option("--backup", help="セッションをバックアップする")] = False,
) -> None:
    """AI セッションの履歴を一覧・閲覧・バックアップする（worktree の削除後も参照可）。"""
    a = _app()
    if agent is not None:
        adapter = a.adapter(agent)
        if not adapter.supports(Capability.LIST_SESSIONS):
            _warn([f"{agent} のセッションの検出には対応していません。wtm の記録だけを表示します。"])
    if backup:
        if session or export or as_json or from_backup or tools:
            raise WtError("--backup は他の表示オプションと同時に指定できません。")
        _history_backup(a, issue)
    elif session is not None:
        if as_json:
            raise WtError("--session の JSON 出力には --format json を指定してください。")
        _history_show(a, issue, session, fmt, export, tools, from_backup)
    else:
        if export or from_backup or tools:
            raise WtError("--export / --from-backup / --tools には --session が必要です。")
        _history_list(a, issue, agent, as_json)


def _history_backup(a: service.App, issue: int) -> None:
    results, warnings = sessions.backup_issue(a, issue)
    _warn(warnings)
    if not results:
        console.print("バックアップの対象となるセッションはありません。")
    for r in results:
        if r.status == "copied":
            console.print(f"[green]バックアップしました:[/] {escape(r.session_id)} → {escape(str(r.backup_path))}")
            if r.preserved:
                _warn([f"元のファイルが追記以外で変更されていたため、以前のバックアップを残しました: {r.preserved}"])
        elif r.status == "unchanged":
            console.print(f"変更なし: {escape(r.session_id)}")
        elif r.status == "no_source":
            console.print(f"[dim]元のファイルがないため省略: {escape(r.session_id)}[/]")
        else:
            err_console.print(f"[red]失敗:[/] {escape(r.session_id)}: {escape(r.message)}")
    if any(r.status == "failed" for r in results):
        raise typer.Exit(1)


def _history_show(a: service.App, issue: int, session: str, fmt: str, export: Path | None,
                  tools: bool, from_backup: bool) -> None:
    if fmt not in ("md", "json"):
        raise WtError(f"未対応の出力形式です: {fmt}", hint="md / json のいずれかを指定してください。")
    opened = sessions.open_session(a, issue, session, from_backup=from_backup)
    _warn(opened.warnings)
    summary, messages = claude_store.read_messages(opened.path, include_tools=tools)
    if fmt == "json":
        content = _json_text(claude_store.to_export_dict(summary, messages, origin=opened.origin, include_tools=tools))
    else:
        content = claude_store.render_markdown(summary, messages, origin=opened.origin, include_tools=tools)
    if export is None:
        typer.echo(content)
        return
    try:
        with open(export, "x", encoding="utf-8", newline="\n") as f:
            f.write(content + "\n")
    except FileExistsError:
        raise ConflictError(f"ファイルが既に存在します: {export}", hint="既存ファイルは上書きしません。") from None
    except OSError as e:
        raise WtError(f"ファイルに書き込めません: {export}: {e}") from e
    console.print(f"[green]出力しました:[/] {escape(str(export))}")
    _warn(["会話には機密情報が含まれる可能性があります。共有する前に内容を確認してください。"])


def _history_list(a: service.App, issue: int, agent: str | None, as_json: bool) -> None:
    data = sessions.collect_history(a, issue, agent=agent)
    if as_json:
        _print_json(data)
        return
    _warn(data["warnings"])
    if not data["sessions"]:
        console.print(f"Issue #{issue} のセッションはありません。")
        return
    table = Table(title=f"Issue #{issue} のセッション")
    for col in ("AI", "セッション ID", "タイトル", "開始", "最終更新", "発話", "参照元", "バックアップ"):
        # ID はコピーして使うので折り返さない
        table.add_column(col, overflow="fold", no_wrap=col == "セッション ID")
    for s in data["sessions"]:
        count = "?" if s["message_count"] is None else str(s["message_count"])
        if s["format"] == "unsupported":
            count = "[red]未対応[/]"
        table.add_row(s["agent"], s["session_id"], escape(s["title"] or ""), _local_date(s["created_at"]),
                      _local_date(s["updated_at"]), count, ORIGIN_LABELS[s["origin"]], BACKUP_LABELS[s["backup_status"]])
    console.print(table)
    for s in data["sessions"]:
        if s["format"] == "unsupported":
            err_console.print(f"[yellow]警告:[/] {s['session_id']}: {escape(s['format_detail'])}")
    console.print(f"[dim]閲覧: wtm history {issue} --session <ID> / 保全: wtm history {issue} --backup[/]")


# ---------------------------------------------------------------------------
CHECK_LABELS = {cleanup_mod.OK: "[green]OK[/]", cleanup_mod.BLOCKED: "[red]NG[/]",
                cleanup_mod.CONFIRM: "[yellow]確認[/]", cleanup_mod.INFO: "[dim]情報[/]"}
CATEGORY_LABELS = {"ready": "[green]削除可能[/]", "blocked": "[red]要対応[/]", "review": "[yellow]要確認[/]",
                   "stale": "[yellow]放置[/]", "active": "[dim]作業中[/]"}


def _print_cleanup_plan(plan: cleanup_mod.CleanupPlan, dry_run: bool) -> None:
    title = f"wtm cleanup #{plan.issue_number} {plan.title or ''}" + (" (dry-run)" if dry_run else "")
    action = "git worktree remove" if plan.mode == "remove" else "記録の更新のみ"
    console.print(f"[bold]{escape(title)}[/]")
    console.print(f"  worktree: {escape(str(plan.path))}（{action}）")
    console.print(f"  ブランチ: {escape(plan.branch or '-')}（削除しません）   作業状態: {plan.state} → ARCHIVED")
    table = Table(show_header=True)
    table.add_column("項目", no_wrap=True)
    table.add_column("結果", no_wrap=True)
    table.add_column("詳細", overflow="fold")
    for c in plan.checks:
        table.add_row(c.name, CHECK_LABELS[c.status], escape(c.message))
    console.print(table)
    for hint in plan.hints:
        console.print(f"[yellow]ヒント:[/] {escape(hint)}")


def _cleanup_overview(a: service.App, as_json: bool) -> None:
    data = cleanup_mod.collect_cleanup(a)
    if as_json:
        _print_json(data)
        return
    _warn(data["warnings"])
    entries = data["entries"]
    if not entries:
        console.print("管理対象の worktree はありません。")
    else:
        table = Table(title="wtm cleanup（何も削除していません）")
        for col in ("Issue", "タイトル", "作業状態", "PR", "判定", "確認日", "理由"):
            table.add_column(col, overflow="fold")
        for e in entries:
            problems = [c["message"] for c in e["checks"] if c["status"] == cleanup_mod.BLOCKED]
            reason = "; ".join(problems) if problems and e["candidate"] else e["candidate_reason"]
            days = "-" if e["days_since_checked"] is None else f"{e['days_since_checked']}日前"
            table.add_row(f"#{e['issue']}", escape(e["title"] or ""), e["state"], _pr_label(e["pr"]),
                          CATEGORY_LABELS[e["category"]], f"[red]{days}[/]" if e["stale"] else days, escape(reason))
        console.print(table)
        console.print(f"[dim]放置 = {data['stale_days']}日以上未確認で削除候補ではないもの。"
                      "削除するには wtm cleanup <Issue>（候補の一括削除はしません）。[/]")
    if data["unmanaged"]:
        console.print("[dim]管理外の worktree（wtm は削除しません）:[/]")
        for u in data["unmanaged"]:
            console.print(f"  [dim]{escape(u['path'])} ({escape(str(u['branch']))})[/]")


@app.command()
@handle_errors
def cleanup(
    issue: Annotated[int | None, typer.Argument(help="Issue 番号（省略すると候補の一覧だけを表示）")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="検査結果の表示のみ")] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="確認を省略する（検査は省略しない）")] = False,
    delete_ignored: Annotated[bool, typer.Option("--delete-ignored", help="--yes のとき、Git の無視対象のファイルの削除も認める")] = False,
    as_json: Annotated[bool, typer.Option("--json", help="検査結果を JSON で出力（削除はしない）")] = False,
) -> None:
    """不要になった worktree を安全に整理する（git worktree remove のみ。ブランチは残す）。"""
    a = _app()
    if issue is None:
        if yes or delete_ignored:
            raise WtError("一括削除には対応していません。Issue 番号を指定してください。")
        _cleanup_overview(a, as_json)
        return
    if as_json and not dry_run:
        raise WtError("--json は --dry-run と同時に、または Issue 番号を省略して指定してください。")
    plan = cleanup_mod.plan_cleanup(a, issue, fetch=not dry_run)
    if as_json:
        _print_json(plan.to_dict())
    else:
        _print_cleanup_plan(plan, dry_run)
    if plan.blockers:
        raise typer.Exit(1)
    if dry_run:
        return
    skip_confirmation = yes or not a.config.cleanup.require_confirmation
    if plan.ignored and skip_confirmation and not delete_ignored:
        raise SafetyError("Git の無視対象のファイルが削除されるため中断しました。",
                          hint="内容を確認し、削除してよければ --delete-ignored を指定してください。")
    if not skip_confirmation and not typer.confirm(f"Issue #{issue} の worktree を整理しますか?", default=False):
        console.print("中断しました。何も変更していません。")
        raise typer.Exit(1)
    console.print("整理しています（大きな worktree では数分かかります）...")
    res = cleanup_mod.execute_cleanup(a, plan, allow_ignored=delete_ignored or not skip_confirmation)
    if res.removed:
        console.print(f"[green]worktree を削除しました:[/] {escape(str(plan.path))}")
    console.print(f"[green]作業状態を ARCHIVED にしました。[/] 記録: {escape(str(res.archive))}")
    copied = sum(1 for r in res.backups if r.status == "copied")
    if copied:
        console.print(f"  セッションを {copied} 件バックアップしました。")
    _warn(res.notes)
    console.print(f"[dim]履歴の参照: wtm history {issue} / 作り直し: wtm start --issue {issue}[/]")
    if hint := cleanup_mod.branch_delete_hint(a, res.branch):
        console.print(f"[dim]ブランチ {escape(res.branch or '')} は残しました。不要なら: {escape(hint)}[/]")


# ---------------------------------------------------------------------------
@app.command()
@handle_errors
def handoff(
    issue: Annotated[int, typer.Argument(help="Issue 番号")],
    offline: Annotated[bool, typer.Option("--offline", help="GitHub / Gitea に問い合わせない")] = False,
    stdout: Annotated[bool, typer.Option("--stdout", help="内容を標準出力にも出す")] = False,
) -> None:
    """AI に渡す引き継ぎ情報（共通指示・Issue・最新 Checkpoint）を .wtm/handoff.md に生成する。"""
    target, text, warnings = service.create_handoff(_app(), issue, offline=offline)
    _warn(warnings)
    if stdout:
        typer.echo(text)
    else:
        console.print(f"[green]引き継ぎ情報を生成しました:[/] {escape(str(target))}")


# ---------------------------------------------------------------------------
@app.command()
@handle_errors
def review(
    issue: Annotated[int, typer.Argument(help="Issue 番号")],
    fmt: Annotated[str, typer.Option("--format", "-f", help="出力形式 (text|md|json)")] = "text",
    output: Annotated[Path | None, typer.Option("--output", "-o", help="ファイルに保存する（既存ファイルは上書きしない）")] = None,
    offline: Annotated[bool, typer.Option("--offline", help="GitHub / Gitea に問い合わせない")] = False,
) -> None:
    """作業の継続・分割・終了を判断するためのレポートを出力する（ルールベース）。"""
    if fmt not in ("text", "md", "json"):
        raise WtError(f"未対応の出力形式です: {fmt}", hint="text / md / json のいずれかを指定してください。")
    report = review_mod.collect_review(_app(), issue, offline=offline)
    if output is not None:
        content = _json_text(report) if fmt == "json" else review_mod.render_markdown(report)
        try:
            with open(output, "x", encoding="utf-8", newline="\n") as f:
                f.write(content + "\n")
        except FileExistsError:
            raise ConflictError(f"ファイルが既に存在します: {output}", hint="既存ファイルは上書きしません。") from None
        except OSError as e:
            raise WtError(f"ファイルに書き込めません: {output}: {e}") from e
        console.print(f"[green]レビューを保存しました:[/] {escape(str(output))}")
    elif fmt == "json":
        _print_json(report)
    elif fmt == "md":
        typer.echo(review_mod.render_markdown(report))
    else:
        console.print(Markdown(review_mod.render_markdown(report)))


# ---------------------------------------------------------------------------
@app.command()
@handle_errors
def doctor(
    fix: Annotated[bool, typer.Option("--fix", help="安全に修復できる項目（中断した予約・停止済み記録）のみ修復")] = False,
    as_json: Annotated[bool, typer.Option("--json", help="JSON で出力")] = False,
) -> None:
    """実行環境と管理データを診断する。"""
    checks = service.doctor(state.config, Path.cwd(), fix=fix)
    if as_json:
        _print_json([c.__dict__ for c in checks])
    else:
        icons = {"ok": "[green]OK[/]", "warn": "[yellow]WARN[/]", "error": "[red]ERROR[/]", "skip": "[dim]SKIP[/]"}
        table = Table(title="wtm doctor")
        table.add_column("項目")
        table.add_column("結果")
        table.add_column("詳細", overflow="fold")
        for c in checks:
            table.add_row(c.name, icons[c.status], escape(c.message))
        console.print(table)
    if any(c.status == "error" for c in checks):
        raise typer.Exit(1)


def main() -> None:
    app(prog_name="wtm")


if __name__ == "__main__":
    main()
