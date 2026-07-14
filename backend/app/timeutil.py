from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from .config import settings

BEIJING = ZoneInfo(settings.timezone or "Asia/Shanghai")


def now_beijing() -> datetime:
    return datetime.now(BEIJING)


def now_beijing_iso() -> str:
    """Beijing local time as ISO string without timezone suffix, e.g. 2026-07-14T18:11:00."""
    return now_beijing().replace(tzinfo=None).isoformat(timespec="seconds")


def now_beijing_mmdd() -> str:
    return now_beijing().strftime("%m/%d")
