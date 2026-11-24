"""パスの正規化と比較。DB には正規化済み絶対パスと比較用キーを保存する。"""

from __future__ import annotations

import os
from pathlib import Path


def normalize(p: str | os.PathLike[str]) -> Path:
    """~ 展開・絶対化・シンボリックリンク解決を行う（存在しなくてもよい）。"""
    return Path(p).expanduser().resolve(strict=False)


def path_key(p: str | os.PathLike[str]) -> str:
    """大文字小文字・区切り文字の差を吸収した比較用キー。"""
    return os.path.normcase(str(normalize(p)))


def same_path(a: str | os.PathLike[str], b: str | os.PathLike[str]) -> bool:
    return path_key(a) == path_key(b)


def is_within(child: str | os.PathLike[str], parent: str | os.PathLike[str]) -> bool:
    c, p = path_key(child), path_key(parent)
    try:
        return os.path.commonpath([c, p]) == p
    except ValueError:  # 異なるドライブ
        return False


def to_config_str(p: str | os.PathLike[str]) -> str:
    """設定ファイル用に OS 非依存（/ 区切り）の文字列にする。"""
    return normalize(p).as_posix()
