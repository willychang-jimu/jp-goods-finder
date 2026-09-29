"""讀取 config.yaml 與環境變數（密鑰只從環境變數來）。"""
from __future__ import annotations

import os
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


def load_config(path: str | Path | None = None) -> dict:
    path = Path(path) if path else ROOT / "config.yaml"
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_dotenv(path: Path = ROOT / ".env") -> None:
    """本機測試用的極簡 .env 讀取；已存在的環境變數優先。"""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()
