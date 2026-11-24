"""AI エージェントアダプタ。

各エージェントは対応できる操作を Capability として宣言し、未対応の操作は
UnsupportedOperationError を返す。起動時の cwd は必ず worktree ルートにする。
"""

from __future__ import annotations

import subprocess
from abc import ABC
from enum import Flag, auto
from pathlib import Path

from . import claude_store
from .errors import CommandNotFoundError, NotManagedError, UnsupportedOperationError
from .proc import run, which


class Capability(Flag):
    NONE = 0
    LAUNCH = auto()
    RESUME = auto()
    LIST_SESSIONS = auto()
    GET_SESSION = auto()
    EXPORT_SESSION = auto()
    ASSIGN_SESSION_ID = auto()  # 新規セッションの ID を起動側で指定できる
    INSTRUCTIONS_FILE = auto()  # 起動時に指示ファイルを渡せる


class AgentAdapter(ABC):
    name: str = ""
    capabilities: Capability = Capability.NONE

    def __init__(self, command: list[str]) -> None:
        self.command = list(command)

    def supports(self, cap: Capability) -> bool:
        return cap in self.capabilities

    def _require(self, cap: Capability) -> None:
        if not self.supports(cap):
            raise UnsupportedOperationError(f"{self.name} アダプタは {cap.name} に対応していません。")

    def available(self) -> bool:
        return which(self.command[0]) is not None

    def version(self) -> str:
        r = run([*self.command, "--version"], check=True, timeout=30)
        return r.stdout.strip().splitlines()[0] if r.stdout.strip() else ""

    def build_launch_args(self, session_id: str | None = None, *, new_session_id: str | None = None,
                          instructions_file: Path | None = None) -> list[str]:
        return list(self.command)

    def launch(self, cwd: Path, session_id: str | None = None, *, new_session_id: str | None = None,
               instructions_file: Path | None = None) -> subprocess.Popen:
        """対話セッションを前面で起動する（端末を継承）。呼び出し側で wait() する。

        session_id は既存セッションの再開、new_session_id は新規セッションへの ID 割り当て。
        """
        self._require(Capability.LAUNCH)
        if session_id is not None:
            self._require(Capability.RESUME)
        if new_session_id is not None:
            self._require(Capability.ASSIGN_SESSION_ID)
        if instructions_file is not None:
            self._require(Capability.INSTRUCTIONS_FILE)
        args = self.build_launch_args(session_id, new_session_id=new_session_id, instructions_file=instructions_file)
        exe = which(args[0])
        if exe is None:
            raise CommandNotFoundError(f"{self.name} CLI が見つかりません: {args[0]}",
                                       hint="インストールするか config.toml の [agents.commands] を設定してください。")
        return subprocess.Popen([exe, *args[1:]], cwd=str(cwd), shell=False)

    def resume(self, cwd: Path, session_id: str) -> subprocess.Popen:
        return self.launch(cwd, session_id=session_id)

    def list_sessions(self, project_path: Path) -> list[dict]:
        self._require(Capability.LIST_SESSIONS)
        raise NotImplementedError

    def get_session(self, session_id: str) -> dict:
        self._require(Capability.GET_SESSION)
        raise NotImplementedError

    def export_session(self, session_id: str, dest: Path) -> Path:
        self._require(Capability.EXPORT_SESSION)
        raise NotImplementedError


class ClaudeAdapter(AgentAdapter):
    """Claude Code CLI。

    `--resume <id>` と `--session-id <uuid>` は `claude --help` (2.1.311) で確認済み。
    `--append-system-prompt-file` は --help に個別の説明が無いため、`claude -p` での実行で動作を確認した。
    セッションの一覧・取得・出力は公式 CLI に無いため、互換性レイヤー claude_store を使う。
    """

    name = "claude"
    capabilities = (Capability.LAUNCH | Capability.RESUME | Capability.ASSIGN_SESSION_ID
                    | Capability.INSTRUCTIONS_FILE | Capability.LIST_SESSIONS | Capability.GET_SESSION
                    | Capability.EXPORT_SESSION)

    def list_sessions(self, project_path: Path) -> list[dict]:
        store = claude_store.ClaudeSessionStore.default()
        return [vars(claude_store.read_summary(h.path)) for h in store.for_project(project_path)]

    def _session_path(self, session_id: str) -> Path:
        path = claude_store.ClaudeSessionStore.default().find(session_id)
        if path is None:
            raise NotManagedError(f"セッション {session_id} が保存先に見つかりません。")
        return path

    def get_session(self, session_id: str) -> dict:
        return vars(claude_store.read_summary(self._session_path(session_id)))

    def export_session(self, session_id: str, dest: Path) -> Path:
        """Markdown で出力する。既存ファイルは上書きしない。"""
        summary, messages = claude_store.read_messages(self._session_path(session_id))
        with open(dest, "x", encoding="utf-8", newline="\n") as f:
            f.write(claude_store.render_markdown(summary, messages, origin="live"))
        return dest

    def build_launch_args(self, session_id: str | None = None, *, new_session_id: str | None = None,
                          instructions_file: Path | None = None) -> list[str]:
        args = list(self.command)
        if session_id:
            args += ["--resume", session_id]
        elif new_session_id:
            args += ["--session-id", new_session_id]
        if instructions_file:
            args += ["--append-system-prompt-file", str(instructions_file)]
        return args


class CodexAdapter(AgentAdapter):
    """Codex CLI。開発環境に未導入でオプションを確認できないため、オプションなしの起動のみ対応。

    指示は渡せないので、利用者が `.wtm/handoff.md` を読むよう伝える。
    """

    name = "codex"
    capabilities = Capability.LAUNCH


ADAPTERS: dict[str, type[AgentAdapter]] = {
    "claude": ClaudeAdapter,
    "codex": CodexAdapter,
}


def get_adapter(name: str, command: list[str]) -> AgentAdapter:
    cls = ADAPTERS.get(name)
    if cls is None:
        raise UnsupportedOperationError(f"未対応のエージェントです: {name} (対応: {', '.join(ADAPTERS)})")
    return cls(command)
