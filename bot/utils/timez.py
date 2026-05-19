from __future__ import annotations

from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo


def now_in(tz_name: str) -> datetime:
    return datetime.now(ZoneInfo(tz_name))


def to_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        raise ValueError("datetime sem tzinfo não pode ser convertido para UTC")
    return dt.astimezone(timezone.utc)


def parse_hhmm(s: str) -> time:
    hh, mm = s.split(":")
    return time(int(hh), int(mm))


def is_within_window(now: datetime, target: time, slack_minutes: int = 5) -> bool:
    """True se now está entre target e target+slack."""
    if now.hour != target.hour:
        return False
    if now.minute < target.minute:
        return False
    return now.minute <= target.minute + slack_minutes
