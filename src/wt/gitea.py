"""Gitea 連携（tea CLI 経由）。共通の型は wt.forge にある。

Issue 取得は `tea api` で Gitea REST API (`GET /repos/{owner}/{repo}/issues/{index}`) を呼ぶ。
`tea issues <n>` の詳細表示は出力形式が安定しない可能性があるため使用しない。
(tea 0.16.0 の `tea api --help` で確認済み)

{owner}/{repo} の置換を tea のリモート推定に任せると、ログインの SSH ホスト/ポートと
リモート URL が一致しない環境で空文字に置換されてしまう（`/repos///issues/1`）。
そのため wtm はリモート URL から owner/name を求め、常に `--repo` を明示する。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from .errors import CommandFailedError, CommandNotFoundError, ForgeError, IssueFetchError
from .forge import (  # noqa: F401  (旧 import 先の互換用に再エクスポート)
    ForgeClient,
    Issue,
    PullRequest,
    issue_from_api,
    pull_from_api,
    repo_slug_from_url,
)
from .proc import run

_ANSI = re.compile(r"\x1b\[[0-9;]*m")

# 旧名（互換用）
GiteaClient = ForgeClient
IssueProvider = ForgeClient

PULLS_PAGE_LIMIT = 50
PULLS_MAX_PAGES = 20


class TeaClient:
    def __init__(self, cli: str, repo_path: Path, login: str = "", repo_slug: str = "",
                 timeout: float = 60) -> None:
        self.cli = cli
        self.repo_path = repo_path
        self.login = login
        self.repo_slug = repo_slug
        self.timeout = timeout

    def _context_args(self) -> list[str]:
        args: list[str] = []
        if self.login:
            args += ["--login", self.login]
        if self.repo_slug:
            args += ["--repo", self.repo_slug]
        return args

    def version(self) -> str:
        r = run([self.cli, "--version"], check=True, timeout=30)
        return " ".join(_ANSI.sub("", r.stdout).split())

    def logins(self) -> list[dict]:
        r = run([self.cli, "logins", "list", "--output", "json"], check=True, timeout=30)
        try:
            data = json.loads(r.stdout or "[]")
        except json.JSONDecodeError as e:
            raise IssueFetchError(f"tea logins の出力を解釈できません: {e}") from e
        return data if isinstance(data, list) else []

    def whoami(self) -> str:
        args = [self.cli, "whoami"]
        if self.login:
            args += ["--login", self.login]
        r = run(args, cwd=self.repo_path, check=True, timeout=self.timeout)
        return r.stdout.strip()

    def api_get(self, endpoint: str) -> object:
        args = [self.cli, "api", *self._context_args(), endpoint]
        try:
            r = run(args, cwd=self.repo_path, check=True, timeout=self.timeout)
        except CommandNotFoundError as e:
            raise IssueFetchError(e.message, hint="tea をインストールするか --offline --title を指定してください。") from e
        except CommandFailedError as e:
            raise IssueFetchError(f"Gitea API 呼び出しに失敗しました: {endpoint}\n{(e.stderr or e.stdout).strip()}",
                                  hint="tea logins list でログイン設定を確認してください。") from e
        try:
            return json.loads(r.stdout)
        except json.JSONDecodeError as e:
            raise IssueFetchError(f"tea api の出力が JSON ではありません: {r.stdout[:200]!r}") from e

    def _run_write(self, args: list[str], what: str, input: str | None = None) -> str:
        try:
            r = run(args, cwd=self.repo_path, check=True, timeout=self.timeout, input=input)
        except CommandNotFoundError as e:
            raise ForgeError(e.message) from e
        except CommandFailedError as e:
            raise ForgeError(f"{what}に失敗しました:\n{(e.stderr or e.stdout).strip()}") from e
        return r.stdout

    def get_issue(self, number: int) -> Issue:
        data = self.api_get(f"/repos/{{owner}}/{{repo}}/issues/{number}")
        return issue_from_api(data, number, "gitea")

    def create_issue(self, title: str, body: str) -> Issue:
        """Issue を作成する。

        `tea issues create` は作成した番号を機械可読に返さないため、`tea api -X POST`
        （Gitea REST API: POST /repos/{owner}/{repo}/issues）で JSON 応答を受け取る。
        本文は stdin (`-d @-`) で渡す。
        """
        payload = json.dumps({"title": title, "body": body}, ensure_ascii=False)
        out = self._run_write([self.cli, "api", *self._context_args(), "-X", "POST", "-d", "@-",
                               "/repos/{owner}/{repo}/issues"], "Issue の作成", input=payload)
        try:
            data = json.loads(out)
        except json.JSONDecodeError as e:
            raise ForgeError(f"Issue 作成の応答が JSON ではありません: {out[:200]!r}") from e
        number = data.get("number") if isinstance(data, dict) else None
        if not isinstance(number, int):
            msg = data.get("message") if isinstance(data, dict) else None
            raise ForgeError("Issue を作成できませんでした" + (f": {msg}" if msg else "。"))
        return issue_from_api(data, number, "gitea")

    def add_comment(self, number: int, body: str) -> None:
        self._run_write([self.cli, "comments", "add", *self._context_args(), "--description", body, str(number)],
                        f"Issue #{number} へのコメント投稿")

    def find_pulls(self, branch: str) -> list[PullRequest]:
        """head ブランチが branch の PR を新しい順に返す（クローズ・マージ済みを含む）。"""
        found: list[PullRequest] = []
        for page in range(1, PULLS_MAX_PAGES + 1):
            data = self.api_get(f"/repos/{{owner}}/{{repo}}/pulls?state=all&page={page}&limit={PULLS_PAGE_LIMIT}")
            if not isinstance(data, list):
                msg = data.get("message") if isinstance(data, dict) else None
                raise IssueFetchError("PR 一覧を取得できません" + (f": {msg}" if msg else "。"))
            found += [pr for pr in (pull_from_api(x) for x in data) if pr and pr.head == branch]
            if len(data) < PULLS_PAGE_LIMIT:
                break
        return sorted(found, key=lambda p: p.number, reverse=True)

    def create_pull(self, head: str, base: str, title: str, body: str, draft: bool = False) -> PullRequest:
        args = [self.cli, "pulls", "create", *self._context_args(), "--head", head, "--base", base,
                "--title", title, "--description-file", "-"]
        if draft:
            args.append("--draft")
        self._run_write(args, "PR の作成", input=body)
        pulls = [p for p in self.find_pulls(head) if p.state == "open"]
        if not pulls:
            raise ForgeError(f"PR を作成しましたが {head} の PR を確認できません。Gitea で確認してください。")
        return pulls[0]
