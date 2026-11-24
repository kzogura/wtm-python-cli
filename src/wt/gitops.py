"""Git CLI ラッパー。全操作は subprocess (shell=False) 経由。"""

from __future__ import annotations

import functools
import os
from dataclasses import dataclass, field
from pathlib import Path

from .errors import WtError
from .locks import RepoLock, repo_lock
from .paths import normalize, same_path
from .proc import CmdResult, run

# fetch 等で認証プロンプトが出てハングしないようにする
_GIT_ENV = {"GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0"}


@dataclass
class WorktreeInfo:
    path: Path
    head: str | None = None
    branch: str | None = None  # refs/heads/ を除いた名前
    detached: bool = False
    bare: bool = False
    locked: bool = False
    locked_reason: str = ""
    prunable: bool = False
    prunable_reason: str = ""


@dataclass
class GitStatus:
    staged: list[str] = field(default_factory=list)
    modified: list[str] = field(default_factory=list)
    untracked: list[str] = field(default_factory=list)
    conflicted: list[str] = field(default_factory=list)

    @property
    def dirty(self) -> bool:
        return bool(self.staged or self.modified or self.conflicted)

    @property
    def has_untracked(self) -> bool:
        return bool(self.untracked)

    @property
    def clean(self) -> bool:
        return not (self.dirty or self.has_untracked)

    def all_files(self) -> list[str]:
        return sorted(set(self.staged + self.modified + self.untracked + self.conflicted))


def parse_worktree_porcelain_z(out: str) -> list[WorktreeInfo]:
    """`git worktree list --porcelain -z` の出力をパースする。

    各属性は NUL 終端、レコード間は空要素（連続 NUL）で区切られる。
    """
    result: list[WorktreeInfo] = []
    cur: WorktreeInfo | None = None
    for token in out.split("\0"):
        if token == "":
            if cur is not None:
                result.append(cur)
                cur = None
            continue
        key, _, value = token.partition(" ")
        if key == "worktree":
            if cur is not None:
                result.append(cur)
            cur = WorktreeInfo(path=normalize(value))
        elif cur is None:
            continue
        elif key == "HEAD":
            cur.head = value
        elif key == "branch":
            cur.branch = value.removeprefix("refs/heads/")
        elif key == "detached":
            cur.detached = True
        elif key == "bare":
            cur.bare = True
        elif key == "locked":
            cur.locked = True
            cur.locked_reason = value
        elif key == "prunable":
            cur.prunable = True
            cur.prunable_reason = value
    if cur is not None:
        result.append(cur)
    return result


def parse_status_porcelain_z(out: str) -> GitStatus:
    """`git status --porcelain=v1 -z` の出力をパースする。"""
    st = GitStatus()
    tokens = out.split("\0")
    i = 0
    while i < len(tokens):
        entry = tokens[i]
        i += 1
        if len(entry) < 4:
            continue
        x, y, path = entry[0], entry[1], entry[3:]
        if x in "RC":  # rename/copy は元パスが次要素に続く
            i += 1
        if x == "?" and y == "?":
            st.untracked.append(path)
        elif x == "!":
            continue
        elif x == "U" or y == "U" or (x, y) in {("A", "A"), ("D", "D")}:
            st.conflicted.append(path)
        else:
            if x not in " ?":
                st.staged.append(path)
            if y not in " ?":
                st.modified.append(path)
    return st


LOCK_FILE_NAME = "wt.lock"


def _locked(method):
    """worktree メタデータに触れる操作をリポジトリ単位のロックで直列化する。第 1 引数はリポジトリ内のパス。"""

    @functools.wraps(method)
    def wrapper(self: Git, path: Path, *args, **kwargs):
        with self.lock(path):
            return method(self, path, *args, **kwargs)
    return wrapper


class Git:
    def __init__(self, exe: str = "git") -> None:
        self.exe = exe
        self._lock_paths: dict[str, Path] = {}

    def lock(self, path: Path) -> RepoLock:
        """<git-common-dir>/wt.lock によるロック（同一リポジトリの全 worktree で共通）。"""
        key = os.path.normcase(str(path))
        lock_path = self._lock_paths.get(key)
        if lock_path is None:
            lock_path = self._lock_paths[key] = self.common_dir(path) / LOCK_FILE_NAME
        return repo_lock(lock_path)

    def run(self, args: list[str], cwd: str | Path, *, check: bool = True,
            timeout: float | None = 120) -> CmdResult:
        return run([self.exe, *args], cwd=cwd, check=check, timeout=timeout, env=_GIT_ENV)

    def version(self) -> str:
        return run([self.exe, "--version"], check=True).stdout.strip()

    # --- repository discovery --------------------------------------------
    def toplevel(self, cwd: Path) -> Path | None:
        r = self.run(["rev-parse", "--show-toplevel"], cwd, check=False)
        return normalize(r.stdout.strip()) if r.ok and r.stdout.strip() else None

    def common_dir(self, cwd: Path) -> Path:
        r = self.run(["rev-parse", "--path-format=absolute", "--git-common-dir"], cwd, check=False)
        if not r.ok:
            raise WtError(f"Git リポジトリではありません: {cwd}", hint=r.stderr.strip() or None)
        return normalize(r.stdout.strip())

    def is_bare(self, cwd: Path) -> bool:
        r = self.run(["rev-parse", "--is-bare-repository"], cwd, check=False)
        return r.ok and r.stdout.strip() == "true"

    def config_get(self, cwd: Path, key: str) -> str | None:
        try:
            r = self.run(["config", "--get", key], cwd, check=False)
        except WtError:
            return None
        return r.stdout.strip() if r.ok and r.stdout.strip() else None

    def config_set_local(self, cwd: Path, key: str, value: str) -> None:
        self.run(["config", "--local", key, value], cwd)

    # --- worktrees -------------------------------------------------------
    @_locked
    def worktree_list(self, repo: Path) -> list[WorktreeInfo]:
        r = self.run(["worktree", "list", "--porcelain", "-z"], repo)
        return parse_worktree_porcelain_z(r.stdout)

    @_locked
    def worktree_add(self, repo: Path, path: Path, *, branch: str, new_branch: bool,
                     base: str | None = None) -> None:
        if new_branch:
            if not base:
                raise ValueError("base is required for a new branch")
            # --no-track: 基準ブランチを upstream にしない（push 先は feature ブランチ自身）
            args = ["worktree", "add", "--no-track", "-b", branch, str(path), base]
        else:
            args = ["worktree", "add", str(path), branch]
        # 所要時間はリポジトリの大きさと LFS に依存する。途中で止めると壊れた worktree が残るので打ち切らない
        self.run(args, repo, timeout=None)

    @_locked
    def worktree_remove(self, repo: Path, path: Path) -> None:
        """--force は使わない。未コミット変更や未追跡ファイルがあれば Git が拒否する。打ち切らない。"""
        self.run(["worktree", "remove", str(path)], repo, timeout=None)

    @_locked
    def worktree_unlock(self, repo: Path, path: Path) -> None:
        self.run(["worktree", "unlock", str(path)], repo)

    # --- refs ------------------------------------------------------------
    def ref_exists(self, repo: Path, ref: str) -> bool:
        r = self.run(["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], repo, check=False)
        return r.ok

    def branch_exists(self, repo: Path, branch: str) -> bool:
        r = self.run(["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"], repo, check=False)
        return r.ok

    def local_branches(self, repo: Path, pattern: str | None = None) -> list[str]:
        args = ["for-each-ref", "--format=%(refname:short)"]
        args.append(f"refs/heads/{pattern}" if pattern else "refs/heads/")
        r = self.run(args, repo)
        return [line for line in r.stdout.splitlines() if line]

    @_locked
    def delete_branch_safe(self, repo: Path, branch: str) -> bool:
        """マージ済みチェック付きの `git branch -d`。失敗しても例外にしない。"""
        r = self.run(["branch", "-d", branch], repo, check=False)
        return r.ok

    def remotes(self, repo: Path) -> list[str]:
        r = self.run(["remote"], repo, check=False)
        return [x for x in r.stdout.splitlines() if x] if r.ok else []

    def remote_url(self, repo: Path, remote: str) -> str | None:
        r = self.run(["remote", "get-url", remote], repo, check=False)
        return r.stdout.strip() if r.ok else None

    @_locked
    def fetch(self, repo: Path, remote: str) -> CmdResult:
        return self.run(["fetch", "--prune", remote], repo, check=False, timeout=None)

    def remote_default_branch(self, repo: Path, remote: str) -> str | None:
        r = self.run(["symbolic-ref", "--quiet", "--short", f"refs/remotes/{remote}/HEAD"], repo, check=False)
        if r.ok and r.stdout.strip():
            return r.stdout.strip().removeprefix(f"{remote}/")
        return None

    def current_branch(self, path: Path) -> str | None:
        """unborn ブランチ（コミット無し）でも名前を返す。detached なら None。"""
        r = self.run(["symbolic-ref", "--quiet", "--short", "HEAD"], path, check=False)
        return r.stdout.strip() if r.ok and r.stdout.strip() else None

    # --- state -----------------------------------------------------------
    def status(self, path: Path) -> GitStatus:
        r = self.run(["status", "--porcelain=v1", "-z", "--untracked-files=all"], path)
        return parse_status_porcelain_z(r.stdout)

    def ignored_files(self, path: Path) -> list[str] | None:
        """Git の無視対象のファイル（ディレクトリは 1 件にまとめる）。取得できなければ None。

        `git worktree remove` は無視対象のファイルを確認なしで削除するため、削除前の表示に使う。
        """
        r = self.run(["ls-files", "--others", "--ignored", "--exclude-standard", "--directory", "-z"],
                     path, check=False)
        return [x for x in r.stdout.split("\0") if x] if r.ok else None

    def head(self, path: Path) -> str | None:
        r = self.run(["rev-parse", "--verify", "--quiet", "HEAD"], path, check=False)
        return r.stdout.strip() if r.ok and r.stdout.strip() else None

    def last_commit(self, path: Path) -> tuple[str, str, str] | None:
        """(sha, ISO8601 コミット日時, 件名)。コミットが無ければ None。"""
        r = self.run(["log", "-1", "--format=%H%x00%cI%x00%s"], path, check=False)
        if not r.ok or not r.stdout.strip():
            return None
        parts = r.stdout.rstrip("\n").split("\0")
        if len(parts) < 3:
            return None
        return parts[0], parts[1], parts[2]

    def ahead_behind(self, path: Path, base: str) -> tuple[int, int] | None:
        """(ahead, behind) = HEAD が base より何コミット進み/遅れているか。"""
        r = self.run(["rev-list", "--left-right", "--count", f"{base}...HEAD"], path, check=False)
        if not r.ok:
            return None
        try:
            behind, ahead = (int(x) for x in r.stdout.split())
        except ValueError:
            return None
        return ahead, behind

    def diffstat(self, path: Path) -> str:
        """作業ツリー（ステージ含む）の HEAD に対する差分統計。"""
        if self.head(path) is None:
            return ""
        r = self.run(["diff", "--stat", "HEAD"], path, check=False)
        return r.stdout.strip() if r.ok else ""

    def count_commits_not_in(self, path: Path, refs: list[str]) -> int | None:
        """HEAD から到達でき、refs のどれからも到達できないコミットの数。"""
        r = self.run(["rev-list", "--count", "HEAD", "--not", *refs], path, check=False)
        try:
            return int(r.stdout.strip()) if r.ok else None
        except ValueError:
            return None

    def changed_files(self, path: Path, base: str) -> list[str] | None:
        """base との分岐点から HEAD までに変更されたファイル。取得できなければ None。"""
        r = self.run(["diff", "--name-only", "-z", f"{base}...HEAD"], path, check=False)
        return [x for x in r.stdout.split("\0") if x] if r.ok else None

    def commits(self, path: Path, rev_range: str, limit: int) -> list[dict[str, str]]:
        """rev_range のコミットを新しい順に返す。取得できなければ空。"""
        r = self.run(["log", f"--max-count={limit}", "--format=%h%x00%cI%x00%s", rev_range], path, check=False)
        if not r.ok:
            return []
        result = []
        for line in r.stdout.splitlines():
            parts = line.split("\0")
            if len(parts) == 3:
                result.append({"sha": parts[0], "date": parts[1], "subject": parts[2]})
        return result

    def is_tracked(self, path: Path, rel: str) -> bool:
        r = self.run(["ls-files", "--error-unmatch", "--", rel], path, check=False)
        return r.ok

    def count_commits(self, path: Path, rev_range: str) -> int | None:
        r = self.run(["rev-list", "--count", rev_range], path, check=False)
        try:
            return int(r.stdout.strip()) if r.ok else None
        except ValueError:
            return None

    @_locked
    def push(self, path: Path, remote: str, branch: str) -> None:
        """`git push -u <remote> <branch>`。--force は使わない（非 fast-forward は Git が拒否する）。"""
        self.run(["push", "-u", remote, f"refs/heads/{branch}:refs/heads/{branch}"], path, timeout=None)  # LFS の転送を含む


def find_worktree(worktrees: list[WorktreeInfo], path: Path) -> WorktreeInfo | None:
    for w in worktrees:
        if same_path(w.path, path):
            return w
    return None

