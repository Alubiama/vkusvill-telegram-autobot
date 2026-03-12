from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import time
from pathlib import Path
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


def _parse_positive_int(value: str | None, default: int) -> int:
    if value is None or not value.strip():
        return default
    try:
        parsed = int(value.strip())
    except ValueError:
        return default
    return parsed if parsed > 0 else default


def _load_bot_token() -> str:
    direct = (os.getenv("BOT_TOKEN") or "").strip()
    if direct:
        return direct

    token_file = (os.getenv("BOT_TOKEN_FILE") or "").strip()
    if token_file:
        path = Path(token_file).expanduser()
        if path.exists():
            value = path.read_text(encoding="utf-8").strip()
            if value:
                return value
        raise ValueError(f"BOT_TOKEN_FILE does not exist or is empty: {path}")

    raise ValueError("BOT_TOKEN is required (or set BOT_TOKEN_FILE)")


@dataclass(frozen=True)
class Settings:
    bot_token: str
    chat_id: int | None
    owner_user_id: int | None
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
    out_retention_days: int
    auto_publish_pages: bool
    publish_pages_command: str | None


def load_settings() -> Settings:
    bot_token = _load_bot_token()

    return Settings(
        bot_token=bot_token,
        chat_id=_parse_chat_id(os.getenv("CHAT_ID")),
        owner_user_id=_parse_chat_id(os.getenv("OWNER_USER_ID")),
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
        out_retention_days=_parse_positive_int(os.getenv("OUT_RETENTION_DAYS"), 30),
        auto_publish_pages=_parse_bool(os.getenv("AUTO_PUBLISH_PAGES"), False),
        publish_pages_command=(os.getenv("PUBLISH_PAGES_COMMAND") or "").strip() or "publish-github-pages.cmd",
    )
