"""CLI（Typer）経由のエンドツーエンドテスト。Gitea はオフラインモードで回避する。"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest
from typer.testing import CliRunner

from wt.cli import app, main

from .conftest import git

runner = CliRunner()


def test_command_name_is_wtm(monkeypatch, capsys):
    # どの起動方法でもヘルプの Usage は wtm になる（python -m wt.cli など）
    monkeypatch.setattr("sys.argv", ["cli.py", "--help"])
    with pytest.raises(SystemExit) as e:
        main()
    assert e.value.code == 0
    assert "wtm [OPTIONS]" in capsys.readouterr().out

    r = runner.invoke(app, ["--version"])
    assert r.exit_code == 0 and r.output.startswith("wtm ")

    with open(Path(__file__).parents[1] / "pyproject.toml", "rb") as f:
        assert tomllib.load(f)["project"]["scripts"] == {"wtm": "wt.cli:main"}


def test_cli_end_to_end(repo, workspace, monkeypatch):
    monkeypatch.chdir(repo)
    r = runner.invoke(app, ["init", "--yes", "--no-gitea"])
    assert r.exit_code == 0, r.output

    r = runner.invoke(app, ["start", "--issue", "101", "--offline", "--title", "Add login form", "--no-launch"])
    assert r.exit_code == 0, r.output
    path = workspace / "worktrees" / "wt-101-add-login-form"
    assert (path / "TASK.md").exists()

    r = runner.invoke(app, ["start", "--issue", "101", "--offline", "--title", "dup", "--no-launch"])
    assert r.exit_code == 1 and "既に" in r.output

    r = runner.invoke(app, ["start", "--issue", "102", "--offline", "--title", "dry", "--dry-run"])
    assert r.exit_code == 0 and not (workspace / "worktrees" / "wt-102-dry").exists()

    r = runner.invoke(app, ["list", "--json"])
    assert r.exit_code == 0, r.output
    data = json.loads(r.stdout)
    assert data["worktrees"][0]["issue"] == 101
    assert data["worktrees"][0]["branch"] == "wt-101-add-login-form"

    r = runner.invoke(app, ["list"])
    assert r.exit_code == 0 and "#101" in r.output

    r = runner.invoke(app, ["status", "101", "--offline"])
    assert r.exit_code == 0 and "Add login form" in r.output

    other = workspace / "既存"
    git(repo, "worktree", "add", "-b", "topic/y", str(other))
    r = runner.invoke(app, ["adopt", str(other), "--issue", "7", "--offline", "--title", "existing"])
    assert r.exit_code == 0, r.output

    r = runner.invoke(app, ["doctor", "--json"])
    checks = {c["name"]: c["status"] for c in json.loads(r.stdout)}
    assert checks["管理 DB"] == "ok" and checks["worktree メタデータ"] == "ok"

    # linked worktree の中からでも設定を探索できる
    monkeypatch.chdir(path)
    r = runner.invoke(app, ["status", "101", "--offline", "--json"])
    assert r.exit_code == 0 and json.loads(r.stdout)["worktree"]["issue_number"] == 101


def test_cli_without_init(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    r = runner.invoke(app, ["list"])
    assert r.exit_code == 1 and "wtm init" in r.output
