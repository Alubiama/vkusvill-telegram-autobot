from __future__ import annotations

import gc
import base64
import json
import os
import sqlite3
import tempfile
import zlib
import unittest
from datetime import time as dtime
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from zoneinfo import ZoneInfo
from urllib.parse import parse_qs, urlsplit

from src.bot import VkusvillGroupBot
from src.config import Settings
from src.providers import MockProvider
from src.store import ItemRow, StateStore


def _make_settings(
    db_path: str,
    out_dir: str,
    *,
    discounts_json_path: str = "data/today_discounts.json",
    collect_min_items: int = 10,
    collection_times: list[dtime] | None = None,
    chat_id: int | None = 111,
    owner_user_id: int | None = 222,
) -> Settings:
    return Settings(
        bot_token="123:abc",
        chat_id=chat_id,
        owner_user_id=owner_user_id,
        timezone=ZoneInfo("Europe/Moscow"),
        telegram_proxy_url=None,
        collection_times=collection_times or [dtime(10, 0)],
        morning_audit_times=[dtime(9, 0)],
        order_deadline=dtime(19, 30),
        provider="mock",
        discounts_json_path=discounts_json_path,
        rpa_command=None,
        order_executor_command=None,
        mini_app_url="https://example.invalid/",
        dry_run=True,
        db_path=db_path,
        out_dir=out_dir,
        out_retention_days=30,
        auto_publish_pages=False,
        publish_pages_command=None,
        collect_failover_enabled=False,
        fallback_rpa_command=None,
        fallback_discounts_json_path=None,
        failover_min_regular_items=18,
        failover_require_min_regular=False,
        collect_timeout_sec=180,
        order_executor_timeout_sec=180,
        collect_min_items=collect_min_items,
    )


class BotBackendGuardsTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.prev_cwd = os.getcwd()
        os.chdir(self.tmpdir.name)
        Path("webapp").mkdir(parents=True, exist_ok=True)
        self.db_path = str(Path(self.tmpdir.name) / "state.db")
        self.out_dir = str(Path(self.tmpdir.name) / "out")
        self.store = StateStore(self.db_path)
        self.store.sync_items(
            "2026-03-19",
            [ItemRow("a", "Alpha", 100.0, 80.0, "mock", "", None)],
        )
        self.bot = VkusvillGroupBot(_make_settings(self.db_path, self.out_dir), self.store, MockProvider())

    def tearDown(self) -> None:
        self.store = None
        gc.collect()
        os.chdir(self.prev_cwd)
        self.tmpdir.cleanup()

    async def test_morning_audit_blocks_when_mobile_session_check_fails(self) -> None:
        self.bot._check_mobile_vkusvill_session = AsyncMock(return_value=(False, "expired"))
        self.bot._collect_impl = AsyncMock(return_value=True)
        app = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()))

        await self.bot._run_morning_audit(app, source="scheduled")

        self.bot._collect_impl.assert_not_awaited()
        app.bot.send_message.assert_awaited_once()
        self.assertEqual(self.store.get_meta("last_mobile_sessioncheck_status"), "error")
        self.assertIn("expired", self.store.get_meta("last_mobile_sessioncheck_detail") or "")

    async def test_scheduled_db_backup_writes_and_prunes_files(self) -> None:
        backup_dir = Path(self.tmpdir.name) / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        today = self.bot._today()
        old_days = [
            "2026-03-01",
            "2026-03-02",
            "2026-03-03",
            "2026-03-04",
            "2026-03-05",
            "2026-03-06",
            "2026-03-07",
            "2026-03-08",
            "2026-03-09",
        ]
        for day in old_days:
            (backup_dir / f"state_{day}.db").write_bytes(b"seed")

        self.bot._db_backup_dir = lambda: backup_dir
        app = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()))
        context = SimpleNamespace(application=app)

        await self.bot.scheduled_db_backup(context)

        backup_path = backup_dir / f"state_{today}.db"
        self.assertTrue(backup_path.exists())
        with sqlite3.connect(backup_path) as conn:
            self.assertGreaterEqual(conn.execute("SELECT COUNT(*) FROM items").fetchone()[0], 1)

        dated = sorted(path.name for path in backup_dir.glob("state_*.db"))
        self.assertEqual(len(dated), 7)
        self.assertEqual(self.store.get_meta("last_db_backup_status"), "ok")

    def test_mini_app_url_uses_stale_payload_when_today_is_empty(self) -> None:
        url = self.bot._build_mini_app_url(user_id=123)
        self.assertIsNotNone(url)
        parsed = parse_qs(urlsplit(url).query)
        self.assertEqual(parsed.get("enc", [""])[0], "z")
        packed = parsed.get("data", [""])[0]
        self.assertTrue(packed)
        padded = packed + "=" * ((4 - len(packed) % 4) % 4)
        payload = json.loads(zlib.decompress(base64.urlsafe_b64decode(padded)).decode("utf-8"))
        self.assertEqual(payload.get("d"), self.bot._today())
        self.assertIn("groups", payload)
        self.assertGreater(len(payload.get("groups") or []), 0)
        self.assertEqual(str(payload.get("round_status", {}).get("k") or ""), "warning")

    def test_mini_app_url_forces_stale_when_collect_not_verified(self) -> None:
        day = self.bot._today()
        self.store.sync_items(
            day,
            [ItemRow("today_1", "Today Item", 100.0, 80.0, "mock", "", None)],
        )
        self.store.set_meta("last_collect_day", day)
        self.store.set_meta("last_collect_status", "guard_preserve_full")

        url = self.bot._build_mini_app_url(user_id=123)
        self.assertIsNotNone(url)
        parsed = parse_qs(urlsplit(url).query)
        packed = parsed.get("data", [""])[0]
        padded = packed + "=" * ((4 - len(packed) % 4) % 4)
        payload = json.loads(zlib.decompress(base64.urlsafe_b64decode(padded)).decode("utf-8"))

        self.assertTrue(bool(payload.get("force_stale")))
        self.assertEqual(payload.get("d"), day)
        self.assertIn("не подтвержден", str(payload.get("round_status", {}).get("n") or ""))

    def test_best_available_items_prefers_verified_live_rows_over_richer_snapshot(self) -> None:
        day = self.bot._today()
        live_items = [
            ItemRow("fav_live", "Любимый тунец", 200.0, 150.0, "vkusvill_web_system_chrome_favorite", "", 9),
            ItemRow("fresh_1", "Fresh One", 120.0, 90.0, "vkusvill_web_system_chrome", "", 4),
        ]
        snapshot_items = live_items + [
            ItemRow("stale_1", "Stale One", 300.0, 210.0, "vkusvill_web_system_chrome", "", 0),
            ItemRow("stale_2", "Stale Two", 310.0, 220.0, "vkusvill_web_system_chrome", "", 0),
        ]

        self.store.sync_items(day, live_items, allow_delete=True)
        self.store.save_day_snapshot(
            day=day,
            snapshot_id="older-rich",
            items=snapshot_items,
            regular_count=3,
            status="guard_keep_richer_snapshot",
            created_at="2026-03-20T01:00:00+03:00",
        )
        self.store.set_meta("last_collect_day", day)
        self.store.set_meta("last_collect_status", "ok")

        items, source = self.bot._best_available_items(day)

        self.assertEqual(source, "live_verified")
        self.assertEqual([item.item_id for item in items], ["fav_live", "fresh_1"])

    def test_startup_collect_is_scheduled_when_discounts_file_is_stale(self) -> None:
        day = self.bot._today()
        stale_path = Path(self.tmpdir.name) / "today_discounts.json"
        stale_path.write_text("[]", encoding="utf-8")
        yesterday = datetime.now(self.bot.settings.timezone) - timedelta(days=1)
        ts = yesterday.timestamp()
        os.utime(stale_path, (ts, ts))

        self.bot = VkusvillGroupBot(
            _make_settings(
                self.db_path,
                self.out_dir,
                discounts_json_path=str(stale_path),
                collection_times=[dtime(0, 0)],
            ),
            self.store,
            MockProvider(),
        )
        self.store.sync_items(
            day,
            [ItemRow("today_1", "Today", 100.0, 80.0, "mock", "", None)],
            allow_delete=True,
        )
        self.store.set_meta("last_collect_day", day)
        self.store.set_meta("last_collect_status", "ok")

        job_queue = SimpleNamespace(run_once=Mock())
        app = SimpleNamespace(job_queue=job_queue)

        self.bot._schedule_startup_collect_if_needed(app)

        job_queue.run_once.assert_called_once()
        self.assertIn("startup catchup scheduled", self.store.get_meta("startup_recovery_note") or "")

    def test_low_item_alert_fingerprint_is_deduplicated(self) -> None:
        day = self.bot._today()

        self.assertTrue(self.bot._should_notify_low_item_count(day, 6))
        self.assertFalse(self.bot._should_notify_low_item_count(day, 6))
        self.assertTrue(self.bot._should_notify_low_item_count(day, 7))

    async def test_send_missing_chat_id_alerts_owner_once(self) -> None:
        bot = VkusvillGroupBot(
            _make_settings(self.db_path, self.out_dir, chat_id=None, owner_user_id=222),
            self.store,
            MockProvider(),
        )
        app = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()))

        await bot._send(app, "hello group")
        await bot._send(app, "hello again")

        app.bot.send_message.assert_awaited_once()
        kwargs = app.bot.send_message.await_args.kwargs
        self.assertEqual(kwargs["chat_id"], 222)
        self.assertIn("CHAT_ID не привязан", kwargs["text"])

    async def test_startup_sanity_reports_missing_chat_binding(self) -> None:
        day = self.bot._today()
        self.store.sync_items(
            day,
            [ItemRow("today_1", "Today", 100.0, 80.0, "mock", "", None)],
            allow_delete=True,
        )
        bot = VkusvillGroupBot(
            _make_settings(self.db_path, self.out_dir, chat_id=None, owner_user_id=222),
            self.store,
            MockProvider(),
        )
        app = SimpleNamespace(bot=SimpleNamespace(get_chat=AsyncMock(), send_message=AsyncMock()))

        result = await bot._run_startup_sanity_check(app)

        self.assertEqual(result["status"], "critical")
        self.assertTrue(any("CHAT_ID не привязан" in item for item in result["issues"]))
        app.bot.send_message.assert_awaited_once()
