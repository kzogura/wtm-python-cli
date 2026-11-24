"""GitHub 連携（gh CLI 経由）。

すべての操作を `gh api`（GitHub REST API）で行い、JSON 応答を受け取る。
`gh issue view` / `gh pr create` などの高水準コマンドは出力形式やリポジトリ推定の挙動に
依存するため使わない。endpoint の owner/name は wtm がリモート URL から求めて埋め込む
（`gh api` は `--repo` を持たず、`{owner}/{repo}` はカレントディレクトリから推定されるため）。

GitHub Enterprise Server は `--hostname`（[forge].host）で指定する。
"""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import quote

from .errors import CommandFailedError, CommandNotFoundError, ForgeError, IssueFetchError
from .forge import Issue, PullRequest, issue_from_api, pull_from_api
from .proc import run

PER_PAGE = 100
MAX_PAGES = 20


class GhClient:
    def __init__(self, cli: str, repo_path: Path, host: str = "", repo_slug: str = "",
                 timeout: float = 60) -> None:
        self.cli = cli
        self.repo_path = repo_path
        self.host = host
        self.repo_slug = repo_slug
        self.timeout = timeout

    # --- 低水準 ---------------------------------------------------------------
    def _host_args(self) -> list[str]:
        return ["--hostname", self.host] if self.host else []

    def _repo_path(self, suffix: str) -> str:
        if not self.repo_slug:
            raise IssueFetchError("GitHub リポジトリの owner/name を特定できません。",
                                  hint="config.toml の [forge].repo に \"owner/name\" を設定してください。")
        return f"repos/{self.repo_slug}{suffix}"

    def _api(self, endpoint: str, *, method: str = "GET", payload: dict | None = None,
             what: str = "") -> object:
        args = [self.cli, "api", *self._host_args()]
        if method != "GET":
            args += ["-X", method]
        if payload is not None:
            args += ["--input", "-"]
        args.append(endpoint)
        body = json.dumps(payload, ensure_ascii=False) if payload is not None else None
        write = method != "GET"
        err = ForgeError if write else IssueFetchError
        try:
            r = run(args, cwd=self.repo_path, check=True, timeout=self.timeout, input=body)
        except CommandNotFoundError as e:
            raise err(e.message, hint="gh をインストールするか --offline を指定してください。") from e
        except CommandFailedError as e:
            detail = (e.stderr or e.stdout).strip()
            label = what or f"GitHub API 呼び出し ({endpoint})"
            raise err(f"{label}に失敗しました:\n{detail}",
                      hint="gh auth status でログイン状態を確認してください。") from e
        try:
            return json.loads(r.stdout) if r.stdout.strip() else None
        except json.JSONDecodeError as e:
            raise err(f"gh api の出力が JSON ではありません: {r.stdout[:200]!r}") from e

    # --- 診断 -----------------------------------------------------------------
    def version(self) -> str:
        r = run([self.cli, "--version"], check=True, timeout=30)
        return (r.stdout.strip().splitlines() or [""])[0]

    def auth_hosts(self) -> dict[str, list[str]]:
        """{host: [login, ...]}（アクティブなアカウントを先頭に）。"""
        r = run([self.cli, "auth", "status", "--json", "hosts"], check=True, timeout=30)
        try:
            data = json.loads(r.stdout or "{}")
        except json.JSONDecodeError as e:
            raise IssueFetchError(f"gh auth status の出力を解釈できません: {e}") from e
        hosts: dict[str, list[str]] = {}
        for host, accounts in (data.get("hosts") or {}).items():
            if not isinstance(accounts, list):
                continue
            ok = [a for a in accounts if isinstance(a, dict) and a.get("state") == "success"]
            ok.sort(key=lambda a: not a.get("active"))
            hosts[host] = [str(a.get("login", "")) for a in ok]
        return hosts

    def whoami(self) -> str:
        data = self._api("user")
        return str(data.get("login") or "") if isinstance(data, dict) else ""

    # --- ForgeClient ------------------------------------------------------------
    def get_issue(self, number: int) -> Issue:
        try:
            data = self._api(self._repo_path(f"/issues/{number}"))
        except IssueFetchError as e:
            if "Not Found" in e.message or "HTTP 404" in e.message:
                raise IssueFetchError(f"Issue #{number} を取得できません: Not Found",
                                      hint="Issue 番号と config.toml の [forge].repo を確認してください。") from e
            raise
        return issue_from_api(data, number, "github")

    def create_issue(self, title: str, body: str) -> Issue:
        data = self._api(self._repo_path("/issues"), method="POST",
                         payload={"title": title, "body": body}, what="Issue の作成")
        number = data.get("number") if isinstance(data, dict) else None
        if not isinstance(number, int):
            msg = data.get("message") if isinstance(data, dict) else None
            raise ForgeError("Issue を作成できませんでした" + (f": {msg}" if msg else "。"))
        return issue_from_api(data, number, "github")

    def add_comment(self, number: int, body: str) -> None:
        self._api(self._repo_path(f"/issues/{number}/comments"), method="POST",
                  payload={"body": body}, what=f"Issue #{number} へのコメント投稿")

    def find_pulls(self, branch: str) -> list[PullRequest]:
        """head ブランチが branch の PR を新しい順に返す（クローズ・マージ済みを含む）。

        GitHub の `head` フィルタは "owner:branch" 形式で、同一リポジトリのブランチを対象にする。
        """
        owner = self.repo_slug.split("/", 1)[0]
        found: list[PullRequest] = []
        for page in range(1, MAX_PAGES + 1):
            q = f"?state=all&head={quote(f'{owner}:{branch}', safe='')}&per_page={PER_PAGE}&page={page}"
            data = self._api(self._repo_path("/pulls" + q))
            if not isinstance(data, list):
                msg = data.get("message") if isinstance(data, dict) else None
                raise IssueFetchError("PR 一覧を取得できません" + (f": {msg}" if msg else "。"))
            found += [pr for pr in (pull_from_api(x) for x in data) if pr and pr.head == branch]
            if len(data) < PER_PAGE:
                break
        return sorted(found, key=lambda p: p.number, reverse=True)

    def create_pull(self, head: str, base: str, title: str, body: str, draft: bool = False) -> PullRequest:
        data = self._api(self._repo_path("/pulls"), method="POST",
                         payload={"head": head, "base": base, "title": title, "body": body, "draft": draft},
                         what="PR の作成")
        pr = pull_from_api(data)
        if pr is None:
            msg = data.get("message") if isinstance(data, dict) else None
            raise ForgeError("PR を作成できませんでした" + (f": {msg}" if msg else "。"))
        return pr
