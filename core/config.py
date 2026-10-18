"""Đường dẫn, múi giờ và biến môi trường dùng chung.

Múi giờ dùng offset cố định +07:00: Việt Nam không có giờ mùa hè, và Python trên Windows không có
sẵn cơ sở dữ liệu IANA (`zoneinfo` cần gói `tzdata`).
"""

from __future__ import annotations

import os
from datetime import timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
STAGED_DIR = DATA_DIR / "staged"
QA_DIR = DATA_DIR / "qa"
CONFIG_DIR = ROOT / "config"
EVAL_DIR = ROOT / "eval"

VN_TZ = timezone(timedelta(hours=7), "Asia/Ho_Chi_Minh")

_dotenv_loaded = False


def load_dotenv(path: Path = ROOT / ".env") -> None:
    """Nạp `.env` vào os.environ (không ghi đè biến đã có). Chỉ đọc cú pháp KEY=VALUE đơn giản."""
    global _dotenv_loaded
    if _dotenv_loaded or not path.exists():
        _dotenv_loaded = True
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value
    _dotenv_loaded = True


def env(name: str, default: str | None = None) -> str | None:
    """Biến môi trường, sau khi nạp `.env`."""
    load_dotenv()
    return os.environ.get(name, default)
