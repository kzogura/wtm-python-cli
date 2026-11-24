"""外部コマンド実行。shell=False・引数配列で呼び出し、stdout/stderr/終了コードを取得する。"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from .errors import CommandFailedError, CommandNotFoundError


@dataclass(frozen=True)
class CmdResult:
    args: list[str]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def which(cmd: str) -> str | None:
    """PATH 上の実行ファイルを解決する（Windows の .cmd/.exe も含む）。"""
    if os.path.sep in cmd or (os.path.altsep and os.path.altsep in cmd):
        return cmd if Path(cmd).exists() else None
    return shutil.which(cmd)


def run(
    args: list[str],
    cwd: str | Path | None = None,
    *,
    check: bool = False,
    timeout: float | None = 120,
    env: dict[str, str] | None = None,
    input: str | None = None,
) -> CmdResult:
    if not args:
        raise ValueError("args must not be empty")
    exe = which(args[0])
    if exe is None:
        raise CommandNotFoundError(
            f"コマンドが見つかりません: {args[0]}",
            hint=f"{args[0]} をインストールして PATH を通してください。",
        )
    full_env = None
    if env:
        full_env = os.environ.copy()
        full_env.update(env)
    try:
        cp = subprocess.run(
            [exe, *args[1:]],
            cwd=str(cwd) if cwd is not None else None,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
            timeout=timeout,
            env=full_env,
            input=input,
            stdin=None if input is not None else subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired as e:
        raise CommandFailedError(list(args), -1, "", f"タイムアウトしました ({timeout}s)") from e
    except OSError as e:
        raise CommandFailedError(list(args), -1, "", str(e)) from e
    result = CmdResult(list(args), cp.returncode, cp.stdout or "", cp.stderr or "")
    if check and not result.ok:
        raise CommandFailedError(result.args, result.returncode, result.stdout, result.stderr)
    return result


def pid_alive(pid: int) -> bool | None:
    """プロセスの生存確認。True=生存, False=停止, None=判定不能。

    PID 再利用があり得るため、True は「動作中の可能性がある」と解釈すること。
    """
    if pid <= 0:
        return False
    if sys.platform == "win32":
        try:
            r = run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"], timeout=15)
        except Exception:
            return None
        if not r.ok:
            return None
        return f'"{pid}"' in r.stdout
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return None
    return True
