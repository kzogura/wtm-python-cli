"""config.toml の読み書きと探索。

探索順:
  1. --config オプション
  2. 環境変数 WT_CONFIG
  3. 対象リポジトリのローカル Git 設定 `wt.config`（wtm init が設定する）
"""

from __future__ import annotations

import os
import string
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import ConfigError
from .forge import DEFAULT_CLI, DEFAULT_PROVIDER, PROVIDER_LABEL, PROVIDERS
from .paths import normalize, to_config_str

CONFIG_FILE_NAME = "config.toml"
DB_FILE_NAME = "wt.db"
GIT_CONFIG_KEY = "wt.config"
ENV_CONFIG = "WT_CONFIG"

DEFAULT_AGENT_COMMANDS: dict[str, list[str]] = {
    "claude": ["claude"],
    "codex": ["codex"],
}


@dataclass
class RepositoryConfig:
    path: Path
    remote: str = "origin"
    base_branch: str = "main"
    worktree_root: Path = Path(".")


@dataclass
class ForgeConfig:
    """Issue / PR ホスティングとの連携（[forge] セクション）。"""

    provider: str = DEFAULT_PROVIDER  # 'github' | 'gitea'
    enabled: bool = True
    cli: str = ""  # 空なら provider の既定（gh / tea）
    login: str = ""  # gitea: tea のログイン名
    host: str = ""  # github: GitHub Enterprise Server のホスト名（空なら github.com）
    repo: str = ""  # "owner/name"。空ならリモート URL から求める

    def __post_init__(self) -> None:
        self.provider = str(self.provider).lower()
        if self.provider not in PROVIDERS:
            raise ConfigError(f"[forge].provider は {' / '.join(PROVIDERS)} のいずれかを指定してください: "
                              f"{self.provider!r}")
        if not self.cli:
            self.cli = DEFAULT_CLI[self.provider]

    @property
    def label(self) -> str:
        return PROVIDER_LABEL[self.provider]


GiteaConfig = ForgeConfig  # 旧名（互換用）


@dataclass
class AgentsConfig:
    default: str = "claude"
    commands: dict[str, list[str]] = field(default_factory=lambda: dict(DEFAULT_AGENT_COMMANDS))


@dataclass
class ManagementConfig:
    stale_days: int = 7
    require_issue: bool = True
    auto_checkpoint: bool = False  # AI 終了時に Checkpoint を自動で記録する
    diverged_commits: int = 50  # wtm review: 基準からこのコミット数以上離れたら警告
    large_change_files: int = 50  # wtm review: 変更ファイルがこの数以上なら警告


@dataclass
class CleanupConfig:
    require_confirmation: bool = True
    require_clean_worktree: bool = True
    require_session_backup: bool = True


@dataclass
class TaskConfig:
    file_name: str = "TASK.md"
    # TASK.md を <git-common-dir>/info/exclude に登録して Git の追跡対象外にする
    exclude: bool = True


@dataclass
class NamingConfig:
    """ブランチ名・worktree ディレクトリ名のテンプレート。{issue} 必須、{slug} 任意。"""

    branch: str = "wt-{issue}-{slug}"
    worktree: str = "wt-{issue}-{slug}"


NAMING_FIELDS = {"issue", "slug"}


def validate_naming(naming: NamingConfig) -> None:
    for key in ("branch", "worktree"):
        template = getattr(naming, key)
        try:
            fields = {f for _, f, _, _ in string.Formatter().parse(template) if f is not None}
        except ValueError as e:
            raise ConfigError(f"[naming].{key} の形式が不正です: {template!r}: {e}") from e
        if "issue" not in fields:
            raise ConfigError(f"[naming].{key} には {{issue}} を含めてください: {template!r}")
        if unknown := fields - NAMING_FIELDS:
            raise ConfigError(f"[naming].{key} に未対応のプレースホルダがあります: {', '.join(sorted(unknown))}")
    if any(c in naming.worktree for c in "/\\:"):
        raise ConfigError(f"[naming].worktree にパス区切り文字は使えません: {naming.worktree!r}")


@dataclass
class Config:
    config_path: Path
    repository: RepositoryConfig
    forge: ForgeConfig = field(default_factory=ForgeConfig)
    agents: AgentsConfig = field(default_factory=AgentsConfig)
    management: ManagementConfig = field(default_factory=ManagementConfig)
    cleanup: CleanupConfig = field(default_factory=CleanupConfig)
    task: TaskConfig = field(default_factory=TaskConfig)
    naming: NamingConfig = field(default_factory=NamingConfig)

    @property
    def management_dir(self) -> Path:
        return self.config_path.parent

    @property
    def db_path(self) -> Path:
        return self.management_dir / DB_FILE_NAME

    @property
    def checkpoints_dir(self) -> Path:
        return self.management_dir / "checkpoints"

    @property
    def sessions_dir(self) -> Path:
        return self.management_dir / "sessions"

    @property
    def archive_dir(self) -> Path:
        return self.management_dir / "archive"

    @property
    def logs_dir(self) -> Path:
        return self.management_dir / "logs"

    def agent_command(self, agent: str) -> list[str]:
        cmd = self.agents.commands.get(agent) or DEFAULT_AGENT_COMMANDS.get(agent)
        if not cmd:
            raise ConfigError(f"エージェント '{agent}' のコマンドが設定されていません。",
                              hint=f"config.toml の [agents.commands] に {agent} = [...] を追加してください。")
        return list(cmd)

    # --- serialize ---------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "repository": {
                "path": to_config_str(self.repository.path),
                "remote": self.repository.remote,
                "base_branch": self.repository.base_branch,
                "worktree_root": to_config_str(self.repository.worktree_root),
            },
            "forge": {
                "provider": self.forge.provider,
                "enabled": self.forge.enabled,
                "cli": self.forge.cli,
                "login": self.forge.login,
                "host": self.forge.host,
                "repo": self.forge.repo,
            },
            "agents": {"default": self.agents.default},
            "agents.commands": dict(self.agents.commands),
            "management": {
                "stale_days": self.management.stale_days,
                "require_issue": self.management.require_issue,
                "auto_checkpoint": self.management.auto_checkpoint,
                "diverged_commits": self.management.diverged_commits,
                "large_change_files": self.management.large_change_files,
            },
            "cleanup": {
                "require_confirmation": self.cleanup.require_confirmation,
                "require_clean_worktree": self.cleanup.require_clean_worktree,
                "require_session_backup": self.cleanup.require_session_backup,
            },
            "task": {"file_name": self.task.file_name, "exclude": self.task.exclude},
            "naming": {"branch": self.naming.branch, "worktree": self.naming.worktree},
        }

    def to_toml(self) -> str:
        lines = ["# wtm 設定ファイル（認証情報は書かないこと）", ""]
        for section, values in self.to_dict().items():
            lines.append(f"[{section}]")
            for k, v in values.items():
                lines.append(f"{_toml_key(k)} = {_toml_value(v)}")
            lines.append("")
        return "\n".join(lines)


def _toml_key(k: str) -> str:
    if k and all(c.isalnum() or c in "-_" for c in k) and k.isascii():
        return k
    return _toml_value(k)


def _toml_value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, str):
        escaped = (v.replace("\\", "\\\\").replace('"', '\\"')
                   .replace("\n", "\\n").replace("\t", "\\t"))
        return f'"{escaped}"'
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    raise TypeError(f"unsupported TOML value: {v!r}")


def load_config(path: str | os.PathLike[str]) -> Config:
    p = normalize(path)
    try:
        data = tomllib.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise ConfigError(f"設定ファイルがありません: {p}", hint="wtm init を実行してください。") from e
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"設定ファイルの形式が不正です: {p}: {e}") from e

    repo = data.get("repository") or {}
    if "path" not in repo:
        raise ConfigError(f"[repository].path がありません: {p}")
    repo_path = normalize(repo["path"])
    agents = data.get("agents") or {}
    commands = dict(DEFAULT_AGENT_COMMANDS)
    for name, cmd in (agents.get("commands") or {}).items():
        if isinstance(cmd, str):
            cmd = [cmd]
        if not isinstance(cmd, list) or not all(isinstance(x, str) for x in cmd) or not cmd:
            raise ConfigError(f"[agents.commands].{name} は文字列の配列で指定してください。")
        commands[name] = cmd
    try:
        cfg = Config(
            config_path=p,
            repository=RepositoryConfig(
                path=repo_path,
                remote=str(repo.get("remote", "origin")),
                base_branch=str(repo.get("base_branch", "main")),
                worktree_root=normalize(repo.get("worktree_root") or repo_path.parent / "worktrees"),
            ),
            forge=_load_forge(data),
            agents=AgentsConfig(default=str(agents.get("default", "claude")), commands=commands),
            management=ManagementConfig(**_pick(data.get("management"), ManagementConfig)),
            cleanup=CleanupConfig(**_pick(data.get("cleanup"), CleanupConfig)),
            task=TaskConfig(**_pick(data.get("task"), TaskConfig)),
            naming=NamingConfig(**_pick(data.get("naming"), NamingConfig)),
        )
    except TypeError as e:
        raise ConfigError(f"設定値が不正です: {e}") from e
    validate_naming(cfg.naming)
    return cfg


def _load_forge(data: dict[str, Any]) -> ForgeConfig:
    """[forge] を読む。無ければ旧形式の [gitea]（provider=gitea）を読む。"""
    if "forge" in data:
        return ForgeConfig(**_pick(data["forge"], ForgeConfig))
    if "gitea" in data:
        return ForgeConfig(**{**_pick(data["gitea"], ForgeConfig), "provider": "gitea"})
    return ForgeConfig()


def _pick(section: Any, cls: type) -> dict[str, Any]:
    if not section:
        return {}
    allowed = cls.__dataclass_fields__.keys()
    return {k: v for k, v in section.items() if k in allowed}


def write_config(cfg: Config) -> None:
    """新規作成のみ。既存ファイルは上書きしない。"""
    cfg.config_path.parent.mkdir(parents=True, exist_ok=True)
    with open(cfg.config_path, "x", encoding="utf-8", newline="\n") as f:
        f.write(cfg.to_toml())


def find_config_path(explicit: str | None, cwd: Path, git_config_get) -> Path:
    """設定ファイルのパスを決定する。git_config_get(cwd, key) -> str | None。"""
    if explicit:
        return normalize(explicit)
    env = os.environ.get(ENV_CONFIG)
    if env:
        return normalize(env)
    value = git_config_get(cwd, GIT_CONFIG_KEY)
    if value:
        return normalize(value)
    raise ConfigError(
        "wtm の設定が見つかりません。",
        hint="管理対象リポジトリで wtm init を実行するか、--config / WT_CONFIG で指定してください。",
    )
