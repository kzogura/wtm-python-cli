"""リポジトリ単位のプロセス間ロック。

Git は worktree 作成中の管理ファイル（.git/worktrees/<name>/commondir 等）を、別プロセスの
`git worktree list` や `git fetch` が読むと失敗することがある。worktree メタデータに触れる
Git 操作をこのロックで直列化する。スレッド間（threading.RLock）とプロセス間（ファイルロック）の
両方を排他し、同一スレッド内では再入可能。
"""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path
from typing import IO

from .errors import WtError

DEFAULT_TIMEOUT = 3600.0  # 大きなリポジトリの worktree 作成や push を待てる長さにする
WAIT_NOTICE_AFTER = 5.0


def _try_lock(fh: IO[bytes]) -> bool:
    try:
        if sys.platform == "win32":
            import msvcrt

            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _unlock(fh: IO[bytes]) -> None:
    if sys.platform == "win32":
        import msvcrt

        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


class RepoLock:
    def __init__(self, path: Path, timeout: float = DEFAULT_TIMEOUT) -> None:
        self.path = path
        self.timeout = timeout
        self._rlock = threading.RLock()
        self._depth = 0
        self._fh: IO[bytes] | None = None

    def __enter__(self) -> RepoLock:
        if not self._rlock.acquire(timeout=self.timeout):
            raise WtError(f"Git 操作のロックを取得できません（タイムアウト）: {self.path}")
        if self._depth == 0:
            try:
                self._acquire_file()
            except BaseException:
                self._rlock.release()
                raise
        self._depth += 1
        return self

    def __exit__(self, *exc: object) -> None:
        self._depth -= 1
        if self._depth == 0 and self._fh is not None:
            try:
                _unlock(self._fh)
            finally:
                self._fh.close()
                self._fh = None
        self._rlock.release()

    def _acquire_file(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+b")
        started = time.monotonic()
        deadline = started + self.timeout
        notified = False
        while not _try_lock(fh):
            if not notified and time.monotonic() - started > WAIT_NOTICE_AFTER:
                print("他の wtm プロセスの Git 操作が終わるのを待っています...", file=sys.stderr)
                notified = True
            if time.monotonic() > deadline:
                fh.close()
                raise WtError(f"他の wtm プロセスが Git 操作中です（ロック待ちタイムアウト）: {self.path}",
                              hint="しばらく待ってから再実行してください。")
            time.sleep(0.05)
        self._fh = fh


_registry: dict[str, RepoLock] = {}
_registry_guard = threading.Lock()


def repo_lock(lock_path: Path) -> RepoLock:
    key = os.path.normcase(str(lock_path))
    with _registry_guard:
        lock = _registry.get(key)
        if lock is None:
            lock = _registry[key] = RepoLock(lock_path)
        return lock
