from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import time
from zoneinfo import ZoneInfo


def _parse_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _parse_chat_id(value: str | None) -> int | None:
    if not value:
        return None
    return int(value.strip())


def _parse_clock(value: str) -> time:
    value = value.strip()
    h, m = value.split(":")
    return time(hour=int(h), minute=int(m))


def _parse_collection_times(value: str | None) -> list[time]:
    raw = value or "10:00"
    times = [_parse_clock(part) for part in raw.split(",") if part.strip()]
    if not times:
        raise ValueError("COLLECTION_TIMES is empty")
    return times


@dataclass(frozen=True)
class Settings:
    bot_token: str
    chat_id: int | None
    timezone: ZoneInfo
    collection_times: list[time]
    order_deadline: time
    provider: str
    discounts_json_path: str
    rpa_command: str | None
    order_executor_command: str | None
    mini_app_url: str | None
    dry_run: bool
    db_path: str
    out_dir: str


def load_settings() -> Settings:
    bot_token = os.getenv("BOT_TOKEN", "").strip()
    if not bot_token:
        raise ValueError("BOT_TOKEN is required")

    return Settings(
        bot_token=bot_token,
        chat_id=_parse_chat_id(os.getenv("CHAT_ID")),
        timezone=ZoneInfo(os.getenv("TIMEZONE", "Europe/Moscow")),
        collection_times=_parse_collection_times(os.getenv("COLLECTION_TIMES")),
        order_deadline=_parse_clock(os.getenv("ORDER_DEADLINE", "19:30")),
        provider=os.getenv("PROVIDER", "manual_json").strip().lower(),
        discounts_json_path=os.getenv("DISCOUNTS_JSON_PATH", "data/today_discounts.json"),
        rpa_command=(os.getenv("RPA_COMMAND") or "").strip() or None,
        order_executor_command=(os.getenv("ORDER_EXECUTOR_COMMAND") or "").strip() or None,
        mini_app_url=(os.getenv("MINI_APP_URL") or "").strip() or None,
        dry_run=_parse_bool(os.getenv("DRY_RUN"), True),
        db_path=os.getenv("DB_PATH", "data/state.db"),
        out_dir=os.getenv("OUT_DIR", "out"),
    )
