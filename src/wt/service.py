"""wtm のユースケース実装。CLI 表示とは分離し、テストから直接呼べるようにする。"""

from __future__ import annotations

import json
import string
import re
import sqlite3
import sys
import time
import unicodedata
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from . import __version__
from . import handoff as handoff_mod
from . import sessions as sessions_mod
from .agents import AgentAdapter, Capability, get_adapter
from .checkpoint_file import load_checkpoint_file
from .claude_store import ClaudeSessionStore, claude_projects_dir
from .config import (
    GIT_CONFIG_KEY,
    Config,
    ForgeConfig,
    RepositoryConfig,
    TaskConfig,
    find_config_path,
    load_config,
    write_config,
)
from .db import Database, now_iso, parse_iso
from .errors import (
    ConflictError,
    IssueFetchError,
    NotManagedError,
    SafetyError,
    UnsupportedOperationError,
    WtError,
)
from .forge import DEFAULT_CLI, ForgeClient, Issue, PullRequest, host_from_url, repo_slug_from_url
from .gitea import TeaClient
from .github import GhClient
from .gitops import Git, WorktreeInfo, find_worktree
from .paths import is_within, normalize, path_key, same_path
from .proc import pid_alive, which
from .taskfile import ensure_excluded, write_task_file

LIST_SCHEMA_VERSION = 1


# ---------------------------------------------------------------------------
# アプリケーションコンテキスト
# ---------------------------------------------------------------------------
@dataclass
class App:
    config: Config
    db: Database
    git: Git
    forge: ForgeClient
    agent_factory: Callable[[str, list[str]], AgentAdapter] = get_adapter

    @property
    def repo(self) -> Path:
        return self.config.repository.path

    def adapter(self, name: str) -> AgentAdapter:
        return self.agent_factory(name, self.config.agent_command(name))

    @property
    def gitea(self) -> ForgeClient:  # 旧名（互換用）
        return self.forge


def forge_repo_slug(cfg: Config, git: Git | None = None) -> str:
    """[forge].repo が空ならリモート URL から owner/name を求める（CLI の推定に頼らない）。"""
    if cfg.forge.repo:
        return cfg.forge.repo
    url = (git or Git()).remote_url(cfg.repository.path, cfg.repository.remote)
    return (repo_slug_from_url(url) if url else None) or ""


def make_forge_client(cfg: Config, git: Git | None = None, cwd: Path | None = None) -> TeaClient | GhClient:
    f = cfg.forge
    path = cwd or cfg.repository.path
    slug = forge_repo_slug(cfg, git)
    if f.provider == "github":
        return GhClient(f.cli, path, f.host, slug)
    return TeaClient(f.cli, path, f.login, slug)


def default_forge_client(cfg: Config) -> ForgeClient:
    return make_forge_client(cfg)


# 旧名（互換用）
gitea_repo_slug = forge_repo_slug
default_gitea_client = default_forge_client


def open_app(explicit_config: str | None, cwd: Path, git: Git | None = None,
             forge_factory: Callable[[Config], ForgeClient] = default_forge_client, *,
             gitea_factory: Callable[[Config], ForgeClient] | None = None) -> App:
    git = git or Git()
    cfg_path = find_config_path(explicit_config, cwd, git.config_get)
    cfg = load_config(cfg_path)
    db = Database(cfg.db_path)
    db.check_schema()
    return App(config=cfg, db=db, git=git, forge=(gitea_factory or forge_factory)(cfg))


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------
@dataclass
class InitPlan:
    repo_path: Path
    common_dir: Path
    remote: str
    remotes: list[str]
    base_branch_candidates: list[str]
    default_base_branch: str
    management_dir: Path
    worktree_root: Path
    existing_config: Path | None
    forge_provider: str
    forge_cli: str
    forge_available: bool
    # gitea: tea のログイン名 / github: gh で認証済みのホスト名
    forge_accounts: list[str]
    remote_host: str = ""
    forge_host: str = ""  # github: 推定した GitHub Enterprise のホスト（github.com なら空）
    warnings: list[str] = field(default_factory=list)


def plan_init(cwd: Path, git: Git, *, provider: str = "github", forge_cli: str = "", forge_host: str = "",
              remote: str = "origin", management_dir: Path | None = None,
              worktree_root: Path | None = None) -> InitPlan:
    top = git.toplevel(cwd)
    if top is None:
        if git.is_bare(cwd):
            raise WtError("bare リポジトリは v1 では未対応です。", hint="通常のクローン（main worktree）で実行してください。")
        raise WtError(f"Git リポジトリが見つかりません: {cwd}")
    common = git.common_dir(top)
    wts = git.worktree_list(top)
    main = wts[0] if wts else None
    if main is None or main.bare:
        raise WtError("main worktree を特定できません（bare リポジトリは未対応）。")
    repo_path = main.path
    warnings: list[str] = []
    if not same_path(repo_path, top):
        warnings.append(f"カレントは linked worktree です。main worktree {repo_path} を管理対象として登録します。")

    remotes = git.remotes(repo_path)
    if remote not in remotes:
        if remotes:
            warnings.append(f"リモート '{remote}' がありません（存在: {', '.join(remotes)}）。")
        else:
            warnings.append("リモートが未設定です。fetch と Issue/PR 連携は利用できません。")

    candidates: list[str] = []
    for b in (git.remote_default_branch(repo_path, remote) if remote in remotes else None,
              git.current_branch(repo_path), "develop", "main", "master"):
        if b and b not in candidates:
            if b in ("develop", "main", "master") and not (
                git.branch_exists(repo_path, b) or git.ref_exists(repo_path, f"{remote}/{b}")
                or b == git.current_branch(repo_path)
            ):
                continue
            candidates.append(b)
    default_base = candidates[0] if candidates else "main"

    # 標準構成 (workspace/main) なら workspace/worktrees, workspace/management
    if repo_path.name == "main":
        ws = repo_path.parent
        default_mgmt, default_root = ws / "management", ws / "worktrees"
    else:
        ws = repo_path.parent / f"{repo_path.name}.wt"
        default_mgmt, default_root = ws / "management", ws / "worktrees"

    existing = git.config_get(repo_path, GIT_CONFIG_KEY)
    fcfg = ForgeConfig(provider=provider, cli=forge_cli, host=forge_host)  # provider の検証と既定 CLI
    cli = fcfg.cli
    remote_url = git.remote_url(repo_path, remote) if remote in remotes else None
    remote_host = (host_from_url(remote_url) if remote_url else None) or ""
    cli_ok = which(cli) is not None
    accounts: list[str] = []
    host = forge_host
    if fcfg.provider == "gitea":
        if cli_ok:
            try:
                accounts = [str(x.get("name", "")) for x in TeaClient(cli, repo_path).logins()]
            except WtError as e:
                warnings.append(f"tea logins の取得に失敗しました: {e.message}")
    elif cli_ok:
        try:
            accounts = [h for h, logins in GhClient(cli, repo_path).auth_hosts().items() if logins]
        except WtError as e:
            warnings.append(f"gh auth status の取得に失敗しました: {e.message}")
    if fcfg.provider == "github" and not host and remote_host and remote_host != "github.com":
        if remote_host in accounts:  # gh で認証済みの GitHub Enterprise Server
            host = remote_host
        else:
            warnings.append(f"リモートのホスト {remote_host} は github.com ではありません。"
                            "Gitea なら --forge gitea、GitHub Enterprise なら --forge-host を指定してください。")
    if not cli_ok:
        warnings.append(f"{cli} が見つかりません。{fcfg.label} 連携なし（--offline）で利用できます。")

    return InitPlan(
        repo_path=repo_path,
        common_dir=common,
        remote=remote,
        remotes=remotes,
        base_branch_candidates=candidates,
        default_base_branch=default_base,
        management_dir=normalize(management_dir) if management_dir else default_mgmt,
        worktree_root=normalize(worktree_root) if worktree_root else default_root,
        existing_config=normalize(existing) if existing else None,
        forge_provider=fcfg.provider,
        forge_cli=cli,
        forge_available=cli_ok,
        forge_accounts=accounts,
        remote_host=remote_host,
        forge_host="" if host == "github.com" else host,
        warnings=warnings,
    )


def execute_init(plan: InitPlan, git: Git, *, base_branch: str, forge_enabled: bool,
                 forge_login: str = "") -> Config:
    """管理 DB と設定ファイルを作成する。既存 worktree・既存ファイルは変更しない。"""
    if plan.existing_config and plan.existing_config.exists():
        raise ConflictError(f"既に初期化されています: {plan.existing_config}",
                            hint="設定を変更する場合は config.toml を直接編集してください。")
    cfg_path = plan.management_dir / "config.toml"
    if cfg_path.exists():
        raise ConflictError(f"設定ファイルが既に存在します: {cfg_path}",
                            hint="既存ファイルは上書きしません。--management-dir で別の場所を指定してください。")
    if is_within(plan.worktree_root, plan.repo_path) or is_within(plan.management_dir, plan.repo_path):
        raise SafetyError("worktree 保存先・管理ディレクトリをリポジトリ内に置くことはできません。")

    remote_url = git.remote_url(plan.repo_path, plan.remote) if plan.remote in plan.remotes else None
    repo_slug = (repo_slug_from_url(remote_url) if remote_url else None) or ""
    cfg = Config(
        config_path=cfg_path,
        repository=RepositoryConfig(path=plan.repo_path, remote=plan.remote, base_branch=base_branch,
                                    worktree_root=plan.worktree_root),
        forge=ForgeConfig(provider=plan.forge_provider, enabled=forge_enabled, cli=plan.forge_cli,
                          login=forge_login, host=plan.forge_host, repo=repo_slug),
        task=TaskConfig(),
    )
    write_config(cfg)
    for d in (cfg.checkpoints_dir, cfg.sessions_dir, cfg.logs_dir, plan.worktree_root):
        d.mkdir(parents=True, exist_ok=True)
    Database(cfg.db_path).initialize()
    git.config_set_local(plan.repo_path, GIT_CONFIG_KEY, cfg_path.as_posix())
    return cfg


# ---------------------------------------------------------------------------
# Issue
# ---------------------------------------------------------------------------
def resolve_issue(app: App, number: int, *, offline: bool, title: str | None) -> Issue:
    if number <= 0:
        raise WtError("Issue 番号は正の整数で指定してください。")
    if offline or not app.config.forge.enabled:
        if not title:
            with app.db.read() as c:
                cached = app.db.get_issue(c, number)
            if cached is None:
                raise IssueFetchError(f"オフラインでは Issue #{number} のタイトルが必要です。",
                                      hint="--title を指定してください。")
            return Issue(number, cached["title"], cached["body"], cached["state"], cached["url"], cached["source"])
        return Issue(number=number, title=title, source="manual")
    return app.forge.get_issue(number)


def render_name(template: str, issue: int, slug: str) -> str:
    return template.format(issue=issue, slug=slug)


def slug_from_name(template: str, issue: int, name: str) -> str | None:
    """テンプレートから生成された名前を逆算して slug を取り出す（一致しなければ None）。"""
    pattern = ""
    for literal, field_name, _, _ in string.Formatter().parse(template):
        pattern += re.escape(literal)
        if field_name == "issue":
            pattern += str(issue)
        elif field_name == "slug":
            pattern += "(?P<slug>.+)"
    m = re.fullmatch(pattern, name)
    return m.group("slug") if m and "slug" in m.groupdict() else None


def make_slug(title: str, max_len: int = 30) -> str:
    s = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode("ascii").lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    s = s[:max_len].rstrip("-")
    return s or "task"


# ---------------------------------------------------------------------------
# start
# ---------------------------------------------------------------------------
@dataclass
class StartPlan:
    issue: Issue
    name: str
    path: Path
    branch: str
    new_branch: bool
    base_ref: str | None
    notes: list[str] = field(default_factory=list)


@dataclass
class StartResult:
    plan: StartPlan
    worktree_id: int
    task_file: Path | None
    warnings: list[str] = field(default_factory=list)


def plan_start(app: App, issue: Issue, *, slug: str | None = None, base: str | None = None,
               allow_closed: bool = False, fetch: bool = True) -> tuple[StartPlan, list[str]]:
    warnings: list[str] = []
    cfg = app.config
    if issue.state == "closed" and not allow_closed:
        raise ConflictError(f"Issue #{issue.number} はクローズ済みです。", hint="意図的な場合は --allow-closed を指定してください。")

    with app.db.read() as c:
        existing = app.db.get_active_by_issue(c, issue.number)
    if existing is not None:
        what = "作成処理中" if existing["provision"] == "creating" else f"登録済み（{existing['state']}）"
        raise ConflictError(f"Issue #{issue.number} は既に{what}です: {existing['path']}",
                            hint="wtm status / wtm resume を使用してください。作成処理が中断した場合は wtm doctor --fix。")

    repo = app.repo
    worktrees = app.git.worktree_list(repo)
    notes: list[str] = []
    naming = cfg.naming

    # ブランチ決定: 指定 slug → テンプレートに一致する既存ブランチ → タイトルから生成
    if slug:
        slug_value = make_slug(slug)
        branch = render_name(naming.branch, issue.number, slug_value)
    else:
        existing_branches = app.git.local_branches(repo, render_name(naming.branch, issue.number, "*"))
        if len(existing_branches) > 1:
            raise ConflictError(
                f"Issue #{issue.number} 用のブランチが複数あります: {', '.join(existing_branches)}",
                hint="--slug で使用するブランチを指定してください。")
        if existing_branches:
            branch = existing_branches[0]
            slug_value = slug_from_name(naming.branch, issue.number, branch) or make_slug(issue.title)
        else:
            slug_value = make_slug(issue.title)
            branch = render_name(naming.branch, issue.number, slug_value)

    name = render_name(naming.worktree, issue.number, slug_value)
    root = cfg.repository.worktree_root
    path = normalize(root / name)
    if not is_within(path, root) or path_key(path) == path_key(root):
        raise SafetyError(f"worktree パスが保存先の外を指しています: {path}")
    if path.exists():
        raise ConflictError(f"worktree パスが既に存在します: {path}",
                            hint="既存 worktree を取り込む場合は wtm adopt を使用してください。")

    r = app.git.run(["check-ref-format", "--branch", branch], repo, check=False)
    if not r.ok:
        raise WtError(f"ブランチ名が不正です: {branch}")

    base_ref: str | None = None
    if app.git.branch_exists(repo, branch):
        for w in worktrees:
            if w.branch == branch:
                raise ConflictError(f"ブランチ {branch} は別の worktree で使用中です: {w.path}",
                                    hint="その worktree を wtm adopt で取り込むか、--slug で別名を指定してください。")
        new_branch = False
        notes.append(f"既存ブランチ {branch} をそのまま使用します（リセットしません）。")
    else:
        new_branch = True
        base_branch = base or cfg.repository.base_branch
        remote = cfg.repository.remote
        remote_ref = f"{remote}/{base_branch}"
        if fetch and remote in app.git.remotes(repo):
            fr = app.git.fetch(repo, remote)
            if not fr.ok:
                warnings.append(f"git fetch {remote} に失敗しました。ローカルの参照を使用します: {fr.stderr.strip()}")
        if app.git.ref_exists(repo, remote_ref):
            base_ref = remote_ref
        elif app.git.ref_exists(repo, base_branch):
            base_ref = base_branch
            if remote in app.git.remotes(repo):
                warnings.append(f"{remote_ref} が無いためローカルブランチ {base_branch} を基準にします。")
        else:
            raise WtError(f"基準ブランチ {base_branch} が見つかりません（コミットが無いリポジトリでは作成できません）。",
                          hint="--base で基準ブランチを指定するか、最初のコミットを作成してください。")
    return StartPlan(issue, name, path, branch, new_branch, base_ref, notes), warnings


def execute_start(app: App, plan: StartPlan, *, agent: str | None) -> StartResult:
    cfg = app.config
    repo = app.repo
    ts = now_iso()
    issue = plan.issue
    warnings: list[str] = []

    # 1) DB に予約行を作成（部分一意インデックスで同一 Issue の並列作成を排他）
    try:
        with app.db.transaction() as c:
            app.db.upsert_issue(c, issue.number, issue.title, issue.body, issue.state, issue.url, issue.source)
            cur = c.execute(
                "INSERT INTO worktrees(issue_number, name, path, path_key, branch, base_ref, state, provision, origin,"
                " agent, created_at, updated_at, last_checked_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (issue.number, plan.name, str(plan.path), path_key(plan.path), plan.branch, plan.base_ref,
                 "PLANNED", "creating", "start", agent, ts, ts, ts),
            )
            wid = int(cur.lastrowid)
    except sqlite3.IntegrityError as e:
        raise ConflictError(f"Issue #{issue.number} または {plan.path} は既に登録されています（並列実行を検知）。") from e

    branch_existed = app.git.branch_exists(repo, plan.branch)
    task_path: Path | None = None
    task_written = False
    task_created_by_us: Path | None = None  # 途中失敗で中途半端に作られた場合も削除対象にする
    try:
        # 2) worktree 作成
        app.git.worktree_add(repo, plan.path, branch=plan.branch, new_branch=plan.new_branch, base=plan.base_ref)
        info = find_worktree(app.git.worktree_list(repo), plan.path)
        if info is None:
            raise WtError(f"作成した worktree が git worktree list に見つかりません: {plan.path}")

        # 3) TASK.md（既存ファイルは上書きしない）
        task_path = plan.path / cfg.task.file_name
        if app.git.is_tracked(plan.path, cfg.task.file_name):
            warnings.append(f"{cfg.task.file_name} はリポジトリで追跡済みのため生成しません。")
            task_path = None
        else:
            if not task_path.exists():
                task_created_by_us = task_path
            task_written = write_task_file(task_path, issue)
            if not task_written:
                warnings.append(f"{task_path} が既に存在するため生成しませんでした。")
            if cfg.task.exclude:
                ensure_excluded(app.git.common_dir(repo), cfg.task.file_name)

        # 4) 確定
        with app.db.transaction() as c:
            c.execute("UPDATE worktrees SET provision='ready', task_file=?, updated_at=? WHERE id=?",
                      (str(task_path) if task_path else None, now_iso(), wid))
            c.execute("INSERT INTO state_history(worktree_id, from_state, to_state, reason, changed_at)"
                      " VALUES (?,?,?,?,?)", (wid, None, "PLANNED", "wtm start", now_iso()))
    except BaseException:
        warnings += _rollback_start(app, wid, plan, branch_existed, task_created_by_us)
        for w in warnings:
            print(f"warning: {w}", file=sys.stderr)
        raise
    return StartResult(plan, wid, task_path if task_written else None, warnings)


def _rollback_start(app: App, wid: int, plan: StartPlan, branch_existed: bool, task_file: Path | None) -> list[str]:
    """start の途中失敗時に、実際の状態を調べて自分が作ったものだけを戻す。"""
    msgs: list[str] = []
    repo = app.repo
    try:
        if task_file is not None and task_file.exists():
            task_file.unlink()
        info = find_worktree(app.git.worktree_list(repo), plan.path)
        removed = info is None
        if info is not None:
            try:
                # 中断された git worktree add が残すロック。この実行で作成したパスなので解除してよい
                if info.locked and info.locked_reason == "initializing":
                    app.git.worktree_unlock(repo, plan.path)
                app.git.worktree_remove(repo, plan.path)
                removed = True
                msgs.append(f"作成途中の worktree を削除しました: {plan.path}")
            except WtError as e:
                # チェックアウトが途中だと変更ありと判定される。--force は使わず、判断を利用者に委ねる
                msgs.append(f"worktree を自動削除できませんでした: {plan.path}: {e.message}\n"
                            "作成途中で中断されたもので不要なら、内容を確認して次を実行してください:\n"
                            f'  git -C "{repo}" worktree remove --force "{plan.path}"\n'
                            f'  git -C "{repo}" branch -D {plan.branch}')
        if removed and plan.new_branch and not branch_existed and app.git.branch_exists(repo, plan.branch):
            if app.git.delete_branch_safe(repo, plan.branch):
                msgs.append(f"作成途中のブランチを削除しました: {plan.branch}")
            else:
                msgs.append(f"ブランチ {plan.branch} を削除できませんでした（手動確認が必要）。")
    finally:
        with app.db.transaction() as c:
            c.execute("DELETE FROM worktrees WHERE id=? AND provision='creating'", (wid,))
    return msgs


# ---------------------------------------------------------------------------
# adopt
# ---------------------------------------------------------------------------
@dataclass
class AdoptResult:
    worktree_id: int
    path: Path
    branch: str | None
    checkpoint_id: int


def adopt(app: App, path: Path, issue: Issue, *, dry_run: bool = False) -> AdoptResult | WorktreeInfo:
    repo = app.repo
    target = normalize(path)
    if not target.is_dir():
        raise WtError(f"ディレクトリがありません: {target}")
    top = app.git.toplevel(target)
    if top is None or not same_path(top, target):
        raise WtError(f"worktree のルートディレクトリを指定してください: {target}")
    if not same_path(app.git.common_dir(target), app.git.common_dir(repo)):
        raise SafetyError(f"{target} は管理対象リポジトリ {repo} の worktree ではありません。")
    worktrees = app.git.worktree_list(repo)
    info = find_worktree(worktrees, target)
    if info is None:
        raise SafetyError(f"git worktree list に {target} がありません。")
    if same_path(info.path, worktrees[0].path):
        raise SafetyError("main worktree は取り込めません。")

    with app.db.read() as c:
        if (row := app.db.get_active_by_path(c, path_key(target))) is not None:
            raise ConflictError(f"{target} は既に Issue #{row['issue_number']} として管理されています。")
        if (row := app.db.get_active_by_issue(c, issue.number)) is not None:
            raise ConflictError(f"Issue #{issue.number} は既に {row['path']} に対応付けられています。")
    if dry_run:
        return info

    ts = now_iso()
    try:
        with app.db.transaction() as c:
            app.db.upsert_issue(c, issue.number, issue.title, issue.body, issue.state, issue.url, issue.source)
            cur = c.execute(
                "INSERT INTO worktrees(issue_number, name, path, path_key, branch, base_ref, state, provision, origin,"
                " agent, created_at, updated_at, last_checked_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (issue.number, target.name, str(target), path_key(target), info.branch, None,
                 "WORKING", "ready", "adopt", None, ts, ts, ts),
            )
            wid = int(cur.lastrowid)
            c.execute("INSERT INTO state_history(worktree_id, from_state, to_state, reason, changed_at)"
                      " VALUES (?,?,?,?,?)", (wid, None, "WORKING", "wtm adopt", ts))
    except sqlite3.IntegrityError as e:
        raise ConflictError("同じ Issue またはパスが並列に登録されました。") from e
    # 既存の Claude セッションを検索して対応付ける（失敗しても取り込みは続ける）
    try:
        sessions_mod.sync_sessions(app, issue.number)
    except (WtError, OSError):
        pass
    cp_id = create_checkpoint(app, wid, next_action="", completed="", remaining="", blockers="",
                              note="wtm adopt による初回 Checkpoint")
    return AdoptResult(wid, target, info.branch, cp_id)


# ---------------------------------------------------------------------------
# checkpoint（Phase 1 ではメタデータ取得部分のみ。adopt の初回 Checkpoint で使用）
# ---------------------------------------------------------------------------
def create_checkpoint(app: App, worktree_id: int, *, completed: str, remaining: str, next_action: str,
                      blockers: str, session_id: str | None = None, note: str = "") -> int:
    with app.db.read() as c:
        row = c.execute("SELECT * FROM worktrees WHERE id=?", (worktree_id,)).fetchone()
    if row is None:
        raise NotManagedError(f"worktree id {worktree_id} がありません。")
    path = Path(row["path"])
    head = app.git.head(path)
    st = app.git.status(path)
    if session_id is None:  # 取得可能なら最後に使ったセッションを記録する
        with app.db.read() as c:
            sessions = app.db.list_sessions(c, worktree_id)
        session_id = sessions[0]["session_id"] if sessions else None
    data: dict[str, Any] = {
        "worktree_id": worktree_id,
        "issue_number": row["issue_number"],
        "created_at": now_iso(),
        "worktree_path": str(path),
        "head": head,
        "diffstat": app.git.diffstat(path),
        "uncommitted": st.all_files(),
        "completed": completed,
        "remaining": remaining,
        "next_action": next_action,
        "blockers": blockers,
        "session_id": session_id,
    }
    out_dir = app.config.checkpoints_dir / f"issue-{row['issue_number']}"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    file_path = out_dir / f"{stamp}.json"
    with open(file_path, "x", encoding="utf-8", newline="\n") as f:
        json.dump({**data, "note": note, "wt_version": __version__}, f, ensure_ascii=False, indent=2)
    data["file_path"] = str(file_path)
    with app.db.transaction() as c:
        return app.db.insert_checkpoint(c, data)


def import_checkpoint_file(app: App, worktree_id: int, path: Path) -> tuple[int, list[str]]:
    """AI が生成した Checkpoint ファイルを取り込む。元のファイルは変更しない。"""
    values, warnings = load_checkpoint_file(path)
    cp_id = create_checkpoint(app, worktree_id, note=f"imported from {path}", **values)
    return cp_id, warnings


# ---------------------------------------------------------------------------
# list / status
# ---------------------------------------------------------------------------
def days_since(iso: str | None, now: datetime) -> int | None:
    dt = parse_iso(iso)
    return None if dt is None else max(0, (now - dt).days)


def base_ref_for(app: App, row: sqlite3.Row) -> str:
    """worktree の比較対象となる基準の参照。"""
    base = row["base_ref"] or f"{app.config.repository.remote}/{app.config.repository.base_branch}"
    return base if app.git.ref_exists(app.repo, base) else app.config.repository.base_branch


def restore_steps(app: App, row: sqlite3.Row) -> list[str]:
    """削除された worktree を元のパスに戻すコマンド。実行はせず、提示だけに使う。"""
    repo, path, branch = app.repo, row["path"], row["branch"]
    remote = app.config.repository.remote
    steps: list[str] = []
    if find_worktree(app.git.worktree_list(repo), Path(path)) is not None:
        steps.append(f'git -C "{repo}" worktree prune')
    if branch and app.git.branch_exists(repo, branch):
        steps.append(f'git -C "{repo}" worktree add "{path}" {branch}')
    elif branch and app.git.ref_exists(repo, f"refs/remotes/{remote}/{branch}"):
        steps.append(f'git -C "{repo}" worktree add --track -b {branch} "{path}" {remote}/{branch}')
    else:
        steps = [f"ブランチ {branch or '(不明)'} がローカルにもリモートにも見つかりません。"
                 " git reflog などで作業内容を確認してください。"]
    return steps


def _missing_worktree_message(app: App, row: sqlite3.Row) -> str:
    return "復元方法（自動では実行しません）:\n" + "\n".join(f"  {s}" for s in restore_steps(app, row))


def _require_worktree(app: App, row: sqlite3.Row) -> Path:
    path = Path(row["path"])
    if find_worktree(app.git.worktree_list(app.repo), path) is None or not path.is_dir():
        raise WtError(f"worktree が存在しません: {path}", hint=_missing_worktree_message(app, row))
    return path


def lookup_pr(app: App, branch: str | None) -> tuple[PullRequest | None, str | None]:
    """branch の最新 PR を返す。(PR または None, 取得失敗時の警告)。"""
    if not branch or not app.config.forge.enabled:
        return None, None
    try:
        pulls = app.forge.find_pulls(branch)
    except WtError as e:
        return None, f"PR を取得できません: {e.message.splitlines()[0]}"
    open_pulls = [p for p in pulls if p.state == "open"]
    return (open_pulls or pulls or [None])[0], None


def collect_list(app: App, *, now: datetime | None = None, offline: bool = False) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    list_warnings: list[str] = []
    stale_days = app.config.management.stale_days
    worktrees = app.git.worktree_list(app.repo)
    with app.db.read() as c:
        rows = app.db.list_worktrees(c)
        issues = {r["number"]: r for r in c.execute("SELECT * FROM issues").fetchall()}
        checkpoints = {r["worktree_id"]: r["created_at"] for r in c.execute(
            "SELECT worktree_id, MAX(created_at) AS created_at FROM checkpoints GROUP BY worktree_id").fetchall()}

    entries: list[dict[str, Any]] = []
    managed_keys = set()
    for row in rows:
        managed_keys.add(row["path_key"])
        path = Path(row["path"])
        info = find_worktree(worktrees, path)
        warnings: list[str] = []
        dirty = untracked = None
        last_commit_at = None
        actual_branch = info.branch if info else None
        if row["provision"] == "creating":
            warnings.append("作成処理が未完了です")
        if info is None:
            warnings.append("git worktree list に存在しません")
        elif not path.is_dir():
            warnings.append("ディレクトリが存在しません（prunable）")
        else:
            try:
                st = app.git.status(path)
                dirty, untracked = st.dirty, st.has_untracked
                lc = app.git.last_commit(path)
                last_commit_at = lc[1] if lc else None
            except WtError as e:
                warnings.append(f"Git 状態を取得できません: {e.message}")
            if row["branch"] and actual_branch != row["branch"]:
                warnings.append(f"ブランチが記録と異なります（記録: {row['branch']} / 実際: {actual_branch or 'detached'}）")
        pr = None
        if not offline and list_warnings == []:
            pr, pr_warning = lookup_pr(app, actual_branch or row["branch"])
            if pr_warning:  # 接続できない場合は以降の問い合わせを省略する
                list_warnings.append(pr_warning)
        days = days_since(row["last_checked_at"], now)
        issue = issues.get(row["issue_number"])
        entries.append({
            "issue": row["issue_number"],
            "title": issue["title"] if issue else None,
            "issue_state": issue["state"] if issue else None,
            "path": row["path"],
            "branch": actual_branch or row["branch"],
            "agent": row["agent"],
            "state": row["state"],
            "provision": row["provision"],
            "exists": info is not None and path.is_dir(),
            "locked": bool(info and info.locked),
            "dirty": dirty,
            "untracked": untracked,
            "last_commit_at": last_commit_at,
            "last_checkpoint_at": checkpoints.get(row["id"]),
            "pr_state": pr.status if pr else None,
            "pr": pr.to_dict() if pr else None,
            "created_at": row["created_at"],
            "last_checked_at": row["last_checked_at"],
            "days_since_checked": days,
            "stale": days is not None and days >= stale_days,
            "warnings": warnings,
        })
    unmanaged = [
        {"path": str(w.path), "branch": w.branch, "locked": w.locked, "prunable": w.prunable}
        for i, w in enumerate(worktrees)
        if i > 0 and path_key(w.path) not in managed_keys
    ]
    return {
        "schema_version": LIST_SCHEMA_VERSION,
        "generated_at": now.isoformat(timespec="seconds"),
        "repository": str(app.repo),
        "stale_days": stale_days,
        "worktrees": entries,
        "unmanaged": unmanaged,
        "warnings": list_warnings,
    }


def get_managed(app: App, issue_number: int) -> sqlite3.Row:
    with app.db.read() as c:
        row = app.db.get_active_by_issue(c, issue_number)
    if row is not None:
        return row
    with app.db.read() as c:
        archived = c.execute("SELECT archived_at FROM worktrees WHERE issue_number=? AND state='ARCHIVED'"
                             " ORDER BY id DESC LIMIT 1", (issue_number,)).fetchone()
    if archived is not None:
        raise NotManagedError(f"Issue #{issue_number} の worktree は整理済みです（{archived['archived_at']}）。",
                              hint=f"履歴は wtm history {issue_number}、記録は wtm list --archived で参照できます。"
                                   f"作業を再開するには wtm start --issue {issue_number} で作り直してください"
                                   "（Issue がクローズ済みなら --allow-closed）。")
    raise NotManagedError(f"Issue #{issue_number} の管理対象 worktree はありません。",
                          hint="wtm start --issue または wtm adopt で登録してください。")


def collect_status(app: App, issue_number: int, *, refresh_issue: bool = True, touch: bool = True) -> dict[str, Any]:
    row = get_managed(app, issue_number)
    warnings: list[str] = []
    issue_data: dict[str, Any] | None = None
    if refresh_issue and app.config.forge.enabled:
        try:
            iss = app.forge.get_issue(issue_number)
            with app.db.transaction() as c:
                app.db.upsert_issue(c, iss.number, iss.title, iss.body, iss.state, iss.url, iss.source)
        except WtError as e:
            warnings.append(f"{app.config.forge.label} から Issue を取得できません（キャッシュを表示）: {e.message}")
    with app.db.read() as c:
        cached = app.db.get_issue(c, issue_number)
        cp = app.db.latest_checkpoint(c, row["id"])
        sessions = [dict(s) for s in app.db.list_sessions(c, row["id"])]
        history = [dict(h) for h in c.execute(
            "SELECT from_state, to_state, reason, changed_at FROM state_history WHERE worktree_id=? ORDER BY id",
            (row["id"],)).fetchall()]
        runs = [dict(r) for r in app.db.running_agents(c, row["id"])]
    if cached is not None:
        issue_data = {k: cached[k] for k in ("number", "title", "body", "state", "url", "source", "fetched_at")}
        if cached["state"] == "closed" and row["state"] not in ("DONE", "ARCHIVED"):
            warnings.append("Issue はクローズ済みですが作業状態が完了になっていません。")

    path = Path(row["path"])
    git_info: dict[str, Any] = {"exists": False}
    info = find_worktree(app.git.worktree_list(app.repo), path)
    if info is not None and path.is_dir():
        st = app.git.status(path)
        lc = app.git.last_commit(path)
        base = base_ref_for(app, row)
        ab = app.git.ahead_behind(path, base)
        git_info = {
            "exists": True,
            "branch": info.branch,
            "head": info.head,
            "locked": info.locked,
            "staged": st.staged,
            "modified": st.modified,
            "untracked": st.untracked,
            "conflicted": st.conflicted,
            "last_commit": {"sha": lc[0], "date": lc[1], "subject": lc[2]} if lc else None,
            "base": base,
            "ahead": ab[0] if ab else None,
            "behind": ab[1] if ab else None,
        }
    else:
        warnings.append("worktree が存在しません。" + _missing_worktree_message(app, row))

    pr = None
    if refresh_issue:
        pr, pr_warning = lookup_pr(app, git_info.get("branch") or row["branch"])
        if pr_warning:
            warnings.append(pr_warning)
    next_action = (cp["next_action"] if cp and cp["next_action"] else None)
    if touch:
        app.db.touch_checked(row["id"])
    blocked_reason = None
    if row["state"] == "BLOCKED" and history and history[-1]["to_state"] == "BLOCKED":
        blocked_reason = history[-1]["reason"]
    return {
        "blocked_reason": blocked_reason,
        "issue": issue_data,
        "worktree": {k: row[k] for k in row.keys()},
        "git": git_info,
        "pr_state": pr.status if pr else None,
        "pr": pr.to_dict() if pr else None,
        "sessions": sessions,
        "agent_runs": runs,
        "last_checkpoint": dict(cp) if cp else None,
        "next_action": next_action,
        "state_history": history,
        "warnings": warnings,
    }


# ---------------------------------------------------------------------------
# Phase 2: Issue 作成（wtm start --new）
# ---------------------------------------------------------------------------
def create_issue(app: App, title: str, body: str) -> Issue:
    """GitHub / Gitea に Issue を作成する（実環境への書き込み。呼び出し側で確認を取ること）。"""
    if not app.config.forge.enabled:
        raise WtError(f"{app.config.forge.label} 連携が無効です。",
                      hint="config.toml の [forge].enabled を true にしてください。")
    title = title.strip()
    if not title:
        raise WtError("Issue のタイトルが空です。")
    issue = app.forge.create_issue(title, body)
    with app.db.transaction() as c:
        app.db.upsert_issue(c, issue.number, issue.title, issue.body, issue.state, issue.url, issue.source)
    return issue


# ---------------------------------------------------------------------------
# Phase 2: Checkpoint の Issue コメント投稿
# ---------------------------------------------------------------------------
MAX_COMMENT_FILES = 50


def format_checkpoint_comment(cp: sqlite3.Row | dict[str, Any], branch: str | None) -> str:
    """Issue に投稿する進捗コメント。AI セッションの内容・ID は含めない。"""
    files = json.loads(cp["uncommitted_json"] or "[]")
    lines = [f"## wtm checkpoint ({cp['created_at']})", ""]
    lines.append(f"- ブランチ: `{branch or '-'}`")
    lines.append(f"- HEAD: `{(cp['head'] or '-')[:12]}`")
    lines.append(f"- 未コミットファイル: {len(files)} 件")
    for title, key in (("完了事項", "completed"), ("未完了事項", "remaining"),
                       ("次のアクション", "next_action"), ("ブロッカー", "blockers")):
        lines += ["", f"### {title}", (cp[key] or "").strip() or "（なし）"]
    if files:
        shown = files[:MAX_COMMENT_FILES]
        lines += ["", "<details><summary>未コミットファイル</summary>", ""]
        lines += [f"- `{f}`" for f in shown]
        if len(files) > len(shown):
            lines.append(f"- ...他 {len(files) - len(shown)} 件")
        lines += ["", "</details>"]
    if cp["diffstat"]:
        lines += ["", "<details><summary>差分統計</summary>", "", "```", cp["diffstat"], "```", "", "</details>"]
    lines += ["", "<sub>posted by wtm</sub>"]
    return "\n".join(lines)


def get_checkpoint(app: App, checkpoint_id: int) -> sqlite3.Row:
    with app.db.read() as c:
        cp = c.execute("SELECT * FROM checkpoints WHERE id=?", (checkpoint_id,)).fetchone()
    if cp is None:
        raise NotManagedError(f"Checkpoint id={checkpoint_id} がありません。")
    return cp


def publish_checkpoint(app: App, checkpoint_id: int) -> str:
    """Checkpoint を Issue にコメント投稿する（実環境への書き込み）。投稿本文を返す。"""
    if not app.config.forge.enabled:
        raise WtError(f"{app.config.forge.label} 連携が無効です。")
    cp = get_checkpoint(app, checkpoint_id)
    with app.db.read() as c:
        row = c.execute("SELECT branch FROM worktrees WHERE id=?", (cp["worktree_id"],)).fetchone()
    body = format_checkpoint_comment(cp, row["branch"] if row else None)
    app.forge.add_comment(cp["issue_number"], body)
    with app.db.transaction() as c:
        c.execute("UPDATE checkpoints SET published_at=? WHERE id=?", (now_iso(), checkpoint_id))
    return body


# ---------------------------------------------------------------------------
# Phase 2: finish
# ---------------------------------------------------------------------------
def possibly_running_agents(app: App, worktree_id: int) -> list[sqlite3.Row]:
    """動作中または状態不明のエージェント記録。停止を確認できたものは exited に更新する。"""
    result: list[sqlite3.Row] = []
    with app.db.transaction() as c:
        for r in app.db.running_agents(c, worktree_id):
            if r["pid"] and pid_alive(r["pid"]) is False:
                c.execute("UPDATE agent_runs SET status='exited', ended_at=? WHERE id=?", (now_iso(), r["id"]))
            else:
                result.append(r)
    return result


@dataclass
class FinishPlan:
    issue_number: int
    worktree_id: int
    path: Path
    branch: str
    base_branch: str
    state: str
    issue_title: str
    blockers: list[str] = field(default_factory=list)  # 1 つでもあれば終了処理しない
    warnings: list[str] = field(default_factory=list)
    checkpoint: sqlite3.Row | None = None
    ahead_of_base: int | None = None
    remote_exists: bool = False
    unpushed: int | None = None  # リモートブランチに無いコミット数（リモート未作成なら None）
    pr: PullRequest | None = None

    @property
    def needs_push(self) -> bool:
        return not self.remote_exists or bool(self.unpushed)

    @property
    def can_create_pr(self) -> bool:
        return self.pr is None and bool(self.ahead_of_base)


def plan_finish(app: App, issue_number: int, *, allow_dirty: bool = False,
                skip_checkpoint: bool = False, fetch: bool = True) -> FinishPlan:
    row = get_managed(app, issue_number)
    cfg = app.config
    path = Path(row["path"])
    if find_worktree(app.git.worktree_list(app.repo), path) is None or not path.is_dir():
        raise WtError(f"worktree が存在しません: {path}")
    branch = app.git.current_branch(path)
    if branch is None:
        raise WtError(f"{path} は detached HEAD です。ブランチ上で作業してください。")
    remote = cfg.repository.remote
    base_branch = cfg.repository.base_branch
    if row["base_ref"] and row["base_ref"].startswith(f"{remote}/"):
        base_branch = row["base_ref"].removeprefix(f"{remote}/")
    elif row["base_ref"]:
        base_branch = row["base_ref"]
    with app.db.read() as c:
        issue = app.db.get_issue(c, issue_number)
        cp = app.db.latest_checkpoint(c, row["id"])
    plan = FinishPlan(issue_number, row["id"], path, branch, base_branch, row["state"],
                      issue["title"] if issue else f"Issue #{issue_number}", checkpoint=cp)

    # 1) 作業状態と AI 実行状態
    if row["state"] in ("DONE", "ARCHIVED"):
        plan.blockers.append(f"作業状態が {row['state']} です。")
    elif row["state"] == "REVIEW":
        plan.warnings.append("既に REVIEW です（PR の確認のみ行います）。")
    for r in possibly_running_agents(app, row["id"]):
        plan.blockers.append(f"{r['agent']} が動作中の可能性があります (pid={r['pid']})。終了してから実行してください。")
    # 2) 未コミット変更
    st = app.git.status(path)
    if not st.clean:
        msg = (f"未コミットの変更があります（staged {len(st.staged)} / modified {len(st.modified)} / "
               f"untracked {len(st.untracked)} / conflicted {len(st.conflicted)}）: "
               + ", ".join(st.all_files()[:10]))
        (plan.warnings if allow_dirty and not st.conflicted else plan.blockers).append(msg)
    # 3) Checkpoint
    if cp is None:
        (plan.warnings if skip_checkpoint else plan.blockers).append(
            "Checkpoint がありません（wtm checkpoint で記録するか --skip-checkpoint を指定）。")
    # push 状態
    if fetch and remote in app.git.remotes(app.repo):
        fr = app.git.fetch(app.repo, remote)
        if not fr.ok:
            plan.warnings.append(f"git fetch に失敗しました: {fr.stderr.strip()}")
    base_ref = f"{remote}/{base_branch}" if app.git.ref_exists(app.repo, f"{remote}/{base_branch}") else base_branch
    plan.ahead_of_base = app.git.count_commits(path, f"{base_ref}..HEAD")
    plan.remote_exists = app.git.ref_exists(app.repo, f"refs/remotes/{remote}/{branch}")
    if plan.remote_exists:
        plan.unpushed = app.git.count_commits(path, f"{remote}/{branch}..HEAD")
    if plan.ahead_of_base == 0:
        plan.warnings.append(f"{base_ref} に対する新しいコミットがありません（PR は作成できません）。")
    # 4) 既存 PR
    if cfg.forge.enabled:
        try:
            pulls = app.forge.find_pulls(branch)
        except WtError as e:
            plan.warnings.append(f"PR を検索できません: {e.message.splitlines()[0]}")
            pulls = []
        open_pulls = [p for p in pulls if p.state == "open"]
        # 同じ名前のブランチを再利用した場合、以前マージした PR は今回の作業の PR ではない
        head = app.git.head(path)
        merged_pulls = [p for p in pulls if p.merged and (not p.head_sha or p.head_sha == head)]
        plan.pr = (open_pulls or merged_pulls or [None])[0]
    return plan


def build_pr_body(plan: FinishPlan) -> str:
    lines = [f"Closes #{plan.issue_number}", ""]
    cp = plan.checkpoint
    if cp is not None:
        for title, key in (("完了事項", "completed"), ("未完了事項", "remaining")):
            if (cp[key] or "").strip():
                lines += [f"### {title}", cp[key].strip(), ""]
    lines.append("<sub>created by wtm</sub>")
    return "\n".join(lines)


@dataclass
class FinishResult:
    pushed: bool = False
    pr: PullRequest | None = None
    pr_created: bool = False
    state: str = ""


def execute_finish(app: App, plan: FinishPlan, *, push: bool, create_pr: bool,
                   title: str | None = None, draft: bool = False) -> FinishResult:
    """push / PR 作成（指定時のみ）を行い REVIEW に遷移する。"""
    if plan.blockers:
        raise SafetyError("終了処理を中断しました:\n- " + "\n- ".join(plan.blockers))
    # 実行直前にもう一度 AI 実行状態を確認する
    if running := possibly_running_agents(app, plan.worktree_id):
        raise SafetyError(f"{running[0]['agent']} が動作中の可能性があるため中断しました。")
    res = FinishResult(pr=plan.pr)
    remote = app.config.repository.remote
    if create_pr and plan.pr is None:
        if not plan.ahead_of_base:
            raise WtError("基準ブランチに対する新しいコミットが無いため PR を作成できません。")
        if plan.needs_push and not push:
            raise SafetyError(f"ブランチ {plan.branch} がリモートに push されていません。",
                              hint="--push を指定してください。")
    if push and plan.needs_push:
        app.git.push(plan.path, remote, plan.branch)
        res.pushed = True
    if create_pr and plan.pr is None:
        res.pr = app.forge.create_pull(plan.branch, plan.base_branch, title or plan.issue_title,
                                       build_pr_body(plan), draft=draft)
        res.pr_created = True
    with app.db.transaction() as c:
        if plan.state != "REVIEW":
            reason = "wtm finish" + (f" (PR #{res.pr.number})" if res.pr else "")
            app.db.set_state(c, plan.worktree_id, "REVIEW", reason)
    res.state = "REVIEW"
    return res


# ---------------------------------------------------------------------------
# Phase 3: 引き継ぎ情報
# ---------------------------------------------------------------------------
def write_handoff(app: App, row: sqlite3.Row, status: dict[str, Any]) -> tuple[Path, str]:
    """`.wtm/handoff.md` を生成し、`.wtm/` を Git の追跡対象外にする。(パス, 本文) を返す。"""
    path = Path(row["path"])
    text = handoff_mod.render_handoff(
        handoff_mod.load_instructions(app.config.management_dir),
        issue_number=row["issue_number"], task_file=app.config.task.file_name, status=status)
    target = handoff_mod.write_handoff(path, text)
    with app.git.lock(app.repo):
        ensure_excluded(app.git.common_dir(app.repo), f"{handoff_mod.WTM_DIR}/")
    return target, text


def create_handoff(app: App, issue_number: int, *, offline: bool = False) -> tuple[Path, str, list[str]]:
    row = get_managed(app, issue_number)
    _require_worktree(app, row)
    status = collect_status(app, issue_number, refresh_issue=not offline)
    target, text = write_handoff(app, row, status)
    return target, text, status["warnings"]


# ---------------------------------------------------------------------------
# agent launch
# ---------------------------------------------------------------------------
# 再開の直後にエラー終了した場合だけ再開失敗とみなす（対話セッションの異常終了と区別するため）
RESUME_FAILURE_WINDOW_SECONDS = 15.0


@dataclass
class LaunchResult:
    rc: int
    agent: str
    session_id: str | None
    resumed: bool
    resume_failed: bool
    handoff_path: Path
    instructions_passed: bool
    checkpoint_id: int | None = None
    notes: list[str] = field(default_factory=list)


def _after_agent_exit(app: App, row: sqlite3.Row, started_at: float, res: LaunchResult) -> None:
    """AI 終了後の Checkpoint 処理。失敗してもエージェントの実行結果には影響させない。"""
    path = Path(row["path"])
    new_file = handoff_mod.find_new_checkpoint_file(path, started_at)
    if not app.config.management.auto_checkpoint:
        if new_file is not None:
            res.notes.append(f"AI が Checkpoint ファイルを書きました。取り込むには: "
                             f'wtm checkpoint {row["issue_number"]} --from-file "{new_file}"')
        return
    try:
        if new_file is not None:
            res.checkpoint_id, warnings = import_checkpoint_file(app, row["id"], new_file)
            res.notes += warnings
        else:
            res.checkpoint_id = create_checkpoint(app, row["id"], completed="", remaining="", next_action="",
                                                  blockers="", note="auto_checkpoint: AI 終了時のメタデータのみ")
    except WtError as e:
        res.notes.append(f"Checkpoint を自動で記録できませんでした: {e.message}")
        return
    res.notes.append(f"Checkpoint を自動で記録しました (id={res.checkpoint_id})。")


def _reject_if_done(state: str, issue_number: int) -> None:
    if state == "DONE":
        raise ConflictError(f"Issue #{issue_number} は DONE のためエージェントを起動できません。",
                            hint=f'再開する場合は wtm state {issue_number} WORKING --reason "理由" を実行してください。')


def launch_agent(app: App, issue_number: int, agent: str, session_id: str | None = None,
                 *, status: dict[str, Any] | None = None) -> LaunchResult:
    """エージェントを worktree で前面起動し、終了まで待つ。

    session_id を指定すると再開する。指定が無く対応していれば、新しいセッション ID を割り当てて記録する。
    status は引き継ぎ情報に使う collect_status の結果（無ければローカルのキャッシュから作る）。
    """
    row = get_managed(app, issue_number)
    path = _require_worktree(app, row)
    _reject_if_done(row["state"], issue_number)
    adapter = app.adapter(agent)
    if not adapter.available():
        raise WtError(f"{agent} CLI が見つかりません: {adapter.command[0]}",
                      hint="インストールするか config.toml の [agents.commands] を設定してください。")
    with app.db.transaction() as c:
        current_state = c.execute("SELECT state FROM worktrees WHERE id=?", (row["id"],)).fetchone()["state"]
        _reject_if_done(current_state, issue_number)  # 確認後に別プロセスが DONE にした場合
        for run_row in app.db.running_agents(c, row["id"]):
            alive = pid_alive(run_row["pid"]) if run_row["pid"] else None
            if alive is False:
                c.execute("UPDATE agent_runs SET status='exited', ended_at=? WHERE id=?", (now_iso(), run_row["id"]))
                continue
            raise ConflictError(
                f"この worktree では既に {run_row['agent']} が動作中の可能性があります (pid={run_row['pid']})。",
                hint="停止を確認できない場合は状態不明として扱います。wtm doctor で確認してください。")
        cur = c.execute("INSERT INTO agent_runs(worktree_id, agent, pid, session_id, status, started_at)"
                        " VALUES (?,?,?,?,?,?)", (row["id"], agent, None, session_id, "running", now_iso()))
        run_id = int(cur.lastrowid)
        c.execute("UPDATE worktrees SET agent=?, updated_at=? WHERE id=?", (agent, now_iso(), row["id"]))
        if current_state in ("PLANNED", "BLOCKED", "REVIEW"):
            app.db.set_state(c, row["id"], "WORKING", f"{agent} 起動")
    new_session_id = None
    if session_id is None and adapter.supports(Capability.ASSIGN_SESSION_ID):
        new_session_id = str(uuid.uuid4())
    instructions_passed = adapter.supports(Capability.INSTRUCTIONS_FILE)
    try:
        handoff_file, _ = write_handoff(app, row, status or collect_status(app, issue_number, refresh_issue=False))
        started_at = time.time()
        started_monotonic = time.monotonic()
        proc = adapter.launch(path, session_id=session_id, new_session_id=new_session_id,
                              instructions_file=handoff_file if instructions_passed else None)
    except BaseException:
        with app.db.transaction() as c:
            c.execute("UPDATE agent_runs SET status='failed', ended_at=? WHERE id=?", (now_iso(), run_id))
        raise
    used_session = session_id or new_session_id
    with app.db.transaction() as c:
        c.execute("UPDATE agent_runs SET pid=?, session_id=? WHERE id=?", (proc.pid, used_session, run_id))
        if new_session_id:  # 異常終了しても対応が残るよう、終了を待たずに記録する
            app.db.upsert_session(c, row["id"], agent, new_session_id)
    while True:
        try:
            rc = proc.wait()
            break
        except KeyboardInterrupt:  # 子プロセス側で処理させる
            continue
    elapsed = time.monotonic() - started_monotonic
    resume_failed = session_id is not None and rc != 0 and elapsed < RESUME_FAILURE_WINDOW_SECONDS
    with app.db.transaction() as c:
        c.execute("UPDATE agent_runs SET status='exited', exit_code=?, ended_at=? WHERE id=?",
                  (rc, now_iso(), run_id))
        if used_session and not resume_failed:  # 再開できなかったセッションは次回の再開候補にしない
            app.db.upsert_session(c, row["id"], agent, used_session)
    res = LaunchResult(rc, agent, used_session, session_id is not None, resume_failed, handoff_file,
                       instructions_passed)
    if not resume_failed:
        _after_agent_exit(app, row, started_at, res)
        if adapter.supports(Capability.LIST_SESSIONS):
            res.notes += sessions_mod.backup_after_agent_exit(app, issue_number)
    return res


# ---------------------------------------------------------------------------
# Phase 3: resume
# ---------------------------------------------------------------------------
_SESSION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")


@dataclass
class ResumePlan:
    issue_number: int
    agent: str
    path: Path
    session_id: str | None
    state: str
    status: dict[str, Any]
    warnings: list[str] = field(default_factory=list)


def plan_resume(app: App, issue_number: int, *, agent: str | None = None, session: str | None = None,
                new_session: bool = False, offline: bool = False) -> ResumePlan:
    if session is not None and new_session:
        raise WtError("--session と --new-session は同時に指定できません。")
    if session is not None and not _SESSION_ID.fullmatch(session):
        raise WtError(f"セッション ID の形式が不正です: {session}")
    row = get_managed(app, issue_number)
    path = _require_worktree(app, row)  # 削除済みなら再作成せず、復元方法を提示して止める
    _reject_if_done(row["state"], issue_number)
    agent_name = agent or row["agent"] or app.config.agents.default
    adapter = app.adapter(agent_name)
    if session is not None and not adapter.supports(Capability.RESUME):
        raise UnsupportedOperationError(f"{agent_name} のセッション再開には対応していません。",
                                        hint="--session を外すと新規セッションで起動します。")

    status = collect_status(app, issue_number, refresh_issue=not offline)
    warnings = list(status["warnings"])
    if adapter.supports(Capability.LIST_SESSIONS):  # wtm を通さず起動したセッションも再開候補にする
        warnings += sessions_mod.sync_sessions(app, issue_number)
    with app.db.read() as c:
        known = [s for s in app.db.list_sessions(c, row["id"]) if s["agent"] == agent_name]
        elsewhere = None
        if session is not None:
            elsewhere = c.execute(
                "SELECT w.issue_number FROM agent_sessions s JOIN worktrees w ON w.id = s.worktree_id"
                " WHERE s.session_id=? AND s.worktree_id<>?", (session, row["id"])).fetchone()
    if elsewhere is not None:
        warnings.append(f"セッション {session} は Issue #{elsewhere['issue_number']} の worktree で記録されています。")

    session_id = session
    if session is None and not new_session and known:
        if adapter.supports(Capability.RESUME):
            session_id = known[0]["session_id"]
    if not adapter.available():
        warnings.append(f"{agent_name} CLI が見つかりません: {adapter.command[0]}")
    if status["issue"] and status["issue"]["state"] == "closed":
        warnings.append(f"Issue #{issue_number} はクローズ済みです。")
    if status["pr_state"] == "merged":
        warnings.append("PR はマージ済みです。このブランチで作業を続ける必要があるか確認してください。")
    if status["last_checkpoint"] is None:
        warnings.append("Checkpoint がありません。Issue の情報だけで引き継ぎます。")
    if row["state"] == "REVIEW":
        warnings.append("作業状態は REVIEW です。起動すると WORKING に戻ります。")
    if row["state"] == "BLOCKED":
        reason = f"（理由: {status['blocked_reason']}）" if status["blocked_reason"] else ""
        warnings.append(f"作業状態は BLOCKED です{reason}。起動すると WORKING に戻ります。")
    for r in possibly_running_agents(app, row["id"]):
        warnings.append(f"{r['agent']} が動作中の可能性があります (pid={r['pid']})。")
    return ResumePlan(issue_number, agent_name, path, session_id, row["state"], status, warnings)


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------
@dataclass
class Check:
    name: str
    status: str  # ok | warn | error | skip
    message: str


STALE_CREATING_AFTER = timedelta(minutes=10)
STALE_PIDLESS_RUN_AFTER = timedelta(minutes=5)


def doctor(explicit_config: str | None, cwd: Path, git: Git | None = None, *, fix: bool = False) -> list[Check]:
    git = git or Git()
    checks: list[Check] = []
    v = sys.version_info
    checks.append(Check("Python", "ok" if v >= (3, 12) else "error", f"{v.major}.{v.minor}.{v.micro}"))
    try:
        checks.append(Check("git", "ok", git.version()))
    except WtError as e:
        checks.append(Check("git", "error", e.message))
        return checks

    cfg: Config | None = None
    try:
        cfg = load_config(find_config_path(explicit_config, cwd, git.config_get))
        checks.append(Check("設定ファイル", "ok", str(cfg.config_path)))
    except WtError as e:
        checks.append(Check("設定ファイル", "warn", e.message + (f" ({e.hint})" if e.hint else "")))

    fcfg = cfg.forge if cfg else ForgeConfig()
    label, cli = fcfg.label, fcfg.cli
    client = (make_forge_client(cfg, git) if cfg
              else GhClient(cli, cwd) if fcfg.provider == "github" else TeaClient(cli, cwd))
    conn = f"{label} 接続"
    name = DEFAULT_CLI[fcfg.provider]  # チェック名は設定した実行ファイル名によらず gh / tea
    if which(cli) is None:
        checks.append(Check(name, "warn", f"{cli} が見つかりません（{label} 連携は --offline で代替）"))
        checks.append(Check(conn, "skip", f"{cli} がありません"))
    else:
        try:
            checks.append(Check(name, "ok", client.version()))
        except WtError as e:
            checks.append(Check(name, "warn", e.message))
        if cfg is None or not cfg.forge.enabled:
            checks.append(Check(conn, "skip", f"{label} 連携が無効、または未初期化"))
        else:
            try:
                who = client.whoami()
                checks.append(Check(conn, "ok", who.splitlines()[0] if who else "接続可"))
            except WtError as e:
                checks.append(Check(conn, "warn", e.message.splitlines()[0]))

    agent_cmds = {"claude": ["claude"], "codex": ["codex"]}
    if cfg:
        agent_cmds = {k: cfg.agent_command(k) for k in ("claude", "codex")}
    for name, cmd in agent_cmds.items():
        adapter = get_adapter(name, cmd)
        if not adapter.available():
            checks.append(Check(f"{name} CLI", "warn", f"{cmd[0]} が見つかりません"))
            continue
        try:
            checks.append(Check(f"{name} CLI", "ok", adapter.version()))
        except WtError as e:
            checks.append(Check(f"{name} CLI", "warn", e.message.splitlines()[0]))

    claude_dir = claude_projects_dir()
    if claude_dir.is_dir():
        count = len(ClaudeSessionStore(claude_dir).scan())
        checks.append(Check("Claude セッション保存先", "ok", f"{claude_dir}（{count} 件、読み取りのみ）"))
    else:
        checks.append(Check("Claude セッション保存先", "warn", f"{claude_dir} が見つかりません"))

    if cfg is None:
        return checks

    repo = cfg.repository.path
    try:
        top = git.toplevel(repo)
        if top is None or not same_path(top, repo):
            raise WtError(f"{repo} は Git リポジトリのルートではありません")
        checks.append(Check("Git リポジトリ", "ok", str(repo)))
    except WtError as e:
        checks.append(Check("Git リポジトリ", "error", e.message))
        return checks

    db = Database(cfg.db_path)
    try:
        db.check_schema()
        with db.read() as c:
            ic = c.execute("PRAGMA integrity_check").fetchone()[0]
        checks.append(Check("管理 DB", "ok" if ic == "ok" else "error", f"{cfg.db_path} integrity={ic}"))
    except WtError as e:
        checks.append(Check("管理 DB", "error", e.message))
        return checks

    worktrees = git.worktree_list(repo)
    now = datetime.now(UTC)
    problems: list[str] = []
    fixed: list[str] = []
    with db.read() as c:
        rows = db.list_worktrees(c)
        runs = c.execute("SELECT * FROM agent_runs WHERE status IN ('running','unknown')").fetchall()
    for row in rows:
        info = find_worktree(worktrees, Path(row["path"]))
        if row["provision"] == "creating":
            created = parse_iso(row["created_at"]) or now
            if info is None and now - created > STALE_CREATING_AFTER:
                if fix:
                    with db.transaction() as c:
                        c.execute("DELETE FROM worktrees WHERE id=? AND provision='creating'", (row["id"],))
                    fixed.append(f"中断した作成処理の予約を削除: Issue #{row['issue_number']}")
                else:
                    problems.append(f"Issue #{row['issue_number']}: 中断した作成処理の予約が残っています（--fix で削除）")
            else:
                problems.append(f"Issue #{row['issue_number']}: 作成処理中です（他プロセスが実行中の可能性）")
        elif info is None:
            problems.append(f"Issue #{row['issue_number']}: worktree {row['path']} が Git に存在しません（自動修正しません）")
        elif info.prunable:
            problems.append(f"Issue #{row['issue_number']}: worktree が prunable です: {info.prunable_reason}")
    for r in runs:
        alive = pid_alive(r["pid"]) if r["pid"] else None
        started = parse_iso(r["started_at"]) or now
        if alive is False or (r["pid"] is None and now - started > STALE_PIDLESS_RUN_AFTER):
            if fix:
                with db.transaction() as c:
                    c.execute("UPDATE agent_runs SET status='exited', ended_at=? WHERE id=?", (now_iso(), r["id"]))
                fixed.append(f"停止済みエージェント記録を更新: run {r['id']}")
            else:
                problems.append(f"エージェント記録 run {r['id']} (pid={r['pid']}) は停止済みです（--fix で更新）")
    managed = {row["path_key"] for row in rows}
    unmanaged = [w for w in worktrees[1:] if path_key(w.path) not in managed]
    if unmanaged:
        problems.append(f"管理外の worktree が {len(unmanaged)} 件あります（wtm adopt で取り込み可能）")
    msg = "; ".join(problems) if problems else f"{len(rows)} 件の管理対象に問題なし"
    checks.append(Check("worktree メタデータ", "warn" if problems else "ok", msg))
    if fixed:
        checks.append(Check("修復", "ok", "; ".join(fixed)))
    return checks

