"""wtm 共通の例外。CLI 層はこれらを捕捉してメッセージと終了コードに変換する。"""

from __future__ import annotations


class WtError(Exception):
    """利用者に説明可能なエラーの基底クラス。"""

    exit_code = 1

    def __init__(self, message: str, hint: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint


class CommandNotFoundError(WtError):
    """外部 CLI（git / gh / tea / claude / codex）が見つからない。"""


class CommandFailedError(WtError):
    """外部 CLI が非ゼロ終了した。"""

    def __init__(self, args: list[str], returncode: int, stdout: str, stderr: str) -> None:
        detail = (stderr or stdout).strip()
        super().__init__(f"コマンドが失敗しました (exit {returncode}): {' '.join(args)}\n{detail}")
        self.args_list = args
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class ConfigError(WtError):
    """設定ファイルが無い・不正。"""


class NotManagedError(WtError):
    """対象が wtm の管理対象ではない。"""


class ConflictError(WtError):
    """重複登録・使用中ブランチなど、状態の衝突。"""


class IssueFetchError(WtError):
    """Issue / PR 情報の取得（GitHub・Gitea）に失敗した。"""


class UnsupportedOperationError(WtError):
    """エージェントが Capability として宣言していない操作。"""


class SafetyError(WtError):
    """安全確認に失敗したため処理を中断した。"""


class ForgeError(WtError):
    """GitHub / Gitea への書き込み（Issue 作成・コメント・PR 作成）に失敗した。"""


GiteaError = ForgeError  # 旧名（互換用）
