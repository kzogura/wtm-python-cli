"""Issue / PR ホスティング（GitHub・Gitea など）の共通定義。

各サービスの実装は provider ごとのモジュールに置く:
  - github: gh CLI（wt.github.GhClient）
  - gitea:  tea CLI（wt.gitea.TeaClient）
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

from .errors import IssueFetchError

PROVIDERS = ("github", "gitea")
DEFAULT_PROVIDER = "github"
DEFAULT_CLI = {"github": "gh", "gitea": "tea"}
PROVIDER_LABEL = {"github": "GitHub", "gitea": "Gitea"}


@dataclass
class Issue:
    number: int
    title: str
    body: str = ""
    state: str = "unknown"  # 'open' | 'closed' | 'unknown'
    url: str = ""
    source: str = "gitea"  # provider 名（'github' | 'gitea'）または 'manual'

    @property
    def is_open(self) -> bool:
        return self.state == "open"


@dataclass
class PullRequest:
    number: int
    title: str
    state: str  # 'open' | 'closed'
    merged: bool
    url: str
    head: str
    base: str
    draft: bool = False
    head_sha: str = ""  # PR の head コミット。squash マージ後の到達確認に使う

    @property
    def status(self) -> str:
        """表示用の状態: merged | open | draft | closed。"""
        if self.merged:
            return "merged"
        if self.state == "open":
            return "draft" if self.draft else "open"
        return "closed"

    def to_dict(self) -> dict:
        return {"number": self.number, "title": self.title, "status": self.status, "url": self.url,
                "head": self.head, "base": self.base}


class ForgeClient(Protocol):
    """wtm が利用する Issue / PR 操作。テストでは Fake 実装に差し替える。"""

    def get_issue(self, number: int) -> Issue: ...

    def create_issue(self, title: str, body: str) -> Issue: ...

    def add_comment(self, number: int, body: str) -> None: ...

    def find_pulls(self, branch: str) -> list[PullRequest]: ...

    def create_pull(self, head: str, base: str, title: str, body: str, draft: bool = False) -> PullRequest: ...


def _url_parts(url: str) -> tuple[str, str] | None:
    """リモート URL を (host, path) に分ける。ローカルパスは None。"""
    url = url.strip()
    if not url:
        return None
    if "://" in url:
        rest = url.split("://", 1)[1]
        authority, _, path = rest.partition("/")
        host = authority.rsplit("@", 1)[-1]
        if host.startswith("["):  # IPv6
            host = host.split("]", 1)[0] + "]"
        else:
            host = host.split(":", 1)[0]
        return host, path
    if m := re.match(r"^[^/\\]+@([^/:]+):(.*)$", url):  # scp 形式 git@host:owner/name.git
        return m.group(1), m.group(2)
    return None  # ローカルパスのリモートは対象外


def repo_slug_from_url(url: str) -> str | None:
    """Git リモート URL から "owner/name" を取り出す。

    対応形式: ssh://user@host[:port]/owner/name(.git), user@host:owner/name(.git),
    http(s)://host[:port][/sub-path]/owner/name(.git)。末尾 2 要素を owner/name とみなす。
    """
    parts = _url_parts(url)
    if parts is None:
        return None
    segs = [p for p in parts[1].split("/") if p]
    if len(segs) < 2:
        return None
    owner, name = segs[-2], segs[-1].removesuffix(".git")
    return f"{owner}/{name}" if owner and name else None


def host_from_url(url: str) -> str | None:
    """Git リモート URL からホスト名を取り出す（ポートは除く）。"""
    parts = _url_parts(url)
    return parts[0] if parts and parts[0] else None


def pull_from_api(data: object) -> PullRequest | None:
    """Gitea / GitHub の PR JSON を PullRequest にする。

    GitHub の一覧 API は `merged` を返さず `merged_at` だけを返すため両方を見る。
    """
    if not isinstance(data, dict) or not isinstance(data.get("number"), int):
        return None
    head = data.get("head") or {}
    base = data.get("base") or {}
    return PullRequest(
        number=data["number"],
        title=str(data.get("title") or ""),
        state=str(data.get("state") or ""),
        merged=bool(data.get("merged") or data.get("merged_at")),
        url=str(data.get("html_url") or ""),
        head=_head_branch(head) if isinstance(head, dict) else "",
        base=str(base.get("ref") or "") if isinstance(base, dict) else "",
        draft=bool(data.get("draft")),
        head_sha=str(head.get("sha") or "") if isinstance(head, dict) else "",
    )


def _head_branch(head: dict) -> str:
    """PR の head ブランチ名。

    Gitea は head ブランチが削除されると `ref` を `refs/pull/<n>/head` に変え、
    ブランチ名は `label` にだけ残る（Gitea 1.26.1 の応答で確認済み）。
    """
    ref = str(head.get("ref") or "")
    return str(head.get("label") or ref) if ref.startswith("refs/pull/") else ref


def issue_from_api(data: object, number: int, source: str = "gitea") -> Issue:
    if not isinstance(data, dict):
        raise IssueFetchError(f"Issue #{number} の応答形式が不正です。")
    if "number" not in data or "title" not in data:
        msg = data.get("message") if isinstance(data.get("message"), str) else None
        hint = None
        if msg in ("GetUserByName", "Not Found"):
            hint = ("リポジトリの owner/name を解決できていない可能性があります。"
                    "config.toml の [forge].repo に \"owner/name\" を設定してください。")
        raise IssueFetchError(f"Issue #{number} を取得できません" + (f": {msg}" if msg else "。"), hint=hint)
    if data.get("pull_request"):
        raise IssueFetchError(f"#{number} は Pull Request です。Issue 番号を指定してください。")
    return Issue(
        number=int(data["number"]),
        title=str(data.get("title") or ""),
        body=str(data.get("body") or ""),
        state=str(data.get("state") or "unknown"),
        url=str(data.get("html_url") or ""),
        source=source,
    )
