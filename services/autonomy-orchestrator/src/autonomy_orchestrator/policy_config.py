from __future__ import annotations

from datetime import datetime
from pathlib import Path

import yaml


def load_policy_config(path: Path) -> dict[str, object]:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def update_policy_config(path: Path, updates: dict[str, object]) -> dict[str, object]:
    payload = load_policy_config(path)
    payload.update(updates)
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return payload


def is_market_hours(config: dict[str, object], now: datetime | None = None) -> bool:
    trading_hours = dict(config.get("trading_hours", {}))
    if not trading_hours.get("enabled", True):
        return True
    try:
        import zoneinfo

        timezone_name = str(trading_hours.get("timezone", "America/New_York"))
        current = now.astimezone(zoneinfo.ZoneInfo(timezone_name)) if now else datetime.now(zoneinfo.ZoneInfo(timezone_name))
    except Exception:
        return True
    days = list(trading_hours.get("days", ["Mon", "Tue", "Wed", "Thu", "Fri"]))
    if current.strftime("%a") not in days:
        return False
    start_hour, start_minute = [int(part) for part in str(trading_hours.get("start", "09:30")).split(":")]
    end_hour, end_minute = [int(part) for part in str(trading_hours.get("end", "16:00")).split(":")]
    start = current.replace(hour=start_hour, minute=start_minute, second=0, microsecond=0)
    end = current.replace(hour=end_hour, minute=end_minute, second=0, microsecond=0)
    return start <= current <= end
