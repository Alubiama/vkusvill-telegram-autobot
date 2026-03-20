from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts.vkusvill_collect_discounts import (
    DiscountItem,
    _collect_offers_ready_food,
    _collect_waves,
    _ensure_disk_headroom,
    _load_today_pool,
    _merge_items_latest,
    _write_today_pool,
    _load_existing_items,
    _looks_unavailable_text,
)
from src.bot import VkusvillGroupBot


def _item(
    item_id: str,
    *,
    source: str,
    stock_qty: int | None = None,
    discount_price: float = 100.0,
) -> DiscountItem:
    return DiscountItem(
        item_id=item_id,
        name=item_id,
        price=discount_price + 20.0,
        discount_price=discount_price,
        source=source,
        image_url="",
        stock_qty=stock_qty,
    )


class CollectGuardsTest(unittest.TestCase):
    def test_collect_offers_ready_food_skips_http_404(self) -> None:
        class _Response:
            status = 404

        class _Page:
            def goto(self, url, wait_until=None, timeout=None):
                return _Response()

            def wait_for_timeout(self, timeout):
                return None

        items = _collect_offers_ready_food(_Page(), "https://example.invalid/offers", 10)
        self.assertEqual(items, [])

    def test_ready_food_unavailable_marker_detects_ne_ostalos(self) -> None:
        self.assertTrue(_looks_unavailable_text("Не осталось"))
        self.assertTrue(_looks_unavailable_text("", "Привозите больше\nНе осталось"))
        self.assertFalse(_looks_unavailable_text("В наличии 7 шт"))

    def _bot_stub(self) -> VkusvillGroupBot:
        bot = VkusvillGroupBot.__new__(VkusvillGroupBot)
        bot._filter_excluded_items = lambda day, items: list(items)
        bot._non_ready_food_items = lambda items: [item for item in items if not str(item.source).startswith("vkusvill_offers_ready_food")]
        bot._only_ready_food_items = lambda items: [item for item in items if str(item.source).startswith("vkusvill_offers_ready_food")]

        def _merge_unique_items(base, extra):
            merged = {}
            for item in list(base) + list(extra):
                merged[str(item.item_id)] = item
            return list(merged.values())

        bot._merge_unique_items = _merge_unique_items
        return bot

    def test_load_existing_items_ignores_previous_day_snapshot_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out_path = Path(tmp) / "today_discounts.json"
            out_path.write_text(
                json.dumps(
                    [
                        {
                            "item_id": "old_1",
                            "name": "old_1",
                            "price": 120,
                            "discount_price": 100,
                            "source": "vkusvill_web_system_chrome",
                        }
                    ],
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            yesterday = datetime.now() - timedelta(days=1)
            ts = yesterday.timestamp()
            os.utime(out_path, (ts, ts))

            loaded = _load_existing_items(out_path, run_day=datetime.now().strftime("%Y-%m-%d"))

            self.assertEqual(loaded, [])

    def test_today_pool_resets_by_day_and_updates_existing_item(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out_path = Path(tmp) / "today_discounts.json"
            day = datetime.now().strftime("%Y-%m-%d")
            old_day = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
            item_old = _item("shared", source="vkusvill_web_system_chrome", stock_qty=1, discount_price=100.0)
            item_new = _item("shared", source="vkusvill_web_system_chrome", stock_qty=9, discount_price=80.0)
            another = _item("another", source="vkusvill_web_system_chrome", stock_qty=2, discount_price=90.0)

            _write_today_pool(out_path, old_day, [item_old])
            self.assertEqual(_load_today_pool(out_path, day), [])

            merged = _merge_items_latest([item_old], [item_new, another])
            _write_today_pool(out_path, day, merged)
            loaded = _load_today_pool(out_path, day)
            by_id = {item.item_id: item for item in loaded}

            self.assertEqual(len(loaded), 2)
            self.assertEqual(by_id["shared"].discount_price, 80.0)
            self.assertEqual(by_id["shared"].stock_qty, 9)

    def test_collect_mode_regular_replaces_regular_but_keeps_ready_food(self) -> None:
        bot = self._bot_stub()
        existing = [
            _item("inshop_old", source="vkusvill_web_system_chrome", stock_qty=5),
            _item("fav_old", source="vkusvill_web_system_chrome_favorite", stock_qty=6),
            _item("offers_keep", source="vkusvill_offers_ready_food", stock_qty=11),
        ]
        fetched = [
            _item("inshop_new_1", source="vkusvill_web_system_chrome", stock_qty=1),
            _item("inshop_new_2", source="vkusvill_web_system_chrome", stock_qty=2),
            _item("fav_new", source="vkusvill_web_system_chrome_favorite", stock_qty=3),
            _item("offers_should_not_replace", source="vkusvill_offers_ready_food", stock_qty=99),
        ]

        merged, label = bot._merge_items_for_collect_mode("2026-03-16", existing, fetched, "regular")
        by_id = {item.item_id: item for item in merged}

        self.assertEqual(label, "regular_only")
        self.assertIn("inshop_new_1", by_id)
        self.assertIn("inshop_new_2", by_id)
        self.assertIn("fav_new", by_id)
        self.assertIn("offers_keep", by_id)
        self.assertIn("inshop_old", by_id)
        self.assertIn("fav_old", by_id)
        self.assertNotIn("offers_should_not_replace", by_id)
        self.assertEqual(by_id["inshop_old"].stock_qty, 0)
        self.assertEqual(by_id["fav_old"].stock_qty, 0)
        self.assertEqual(by_id["offers_keep"].stock_qty, 11)

    def test_collect_mode_ready_replaces_ready_food_but_keeps_regular(self) -> None:
        bot = self._bot_stub()
        existing = [
            _item("inshop_keep_1", source="vkusvill_web_system_chrome", stock_qty=5),
            _item("inshop_keep_2", source="vkusvill_web_system_chrome", stock_qty=6),
            _item("fav_keep", source="vkusvill_web_system_chrome_favorite", stock_qty=7),
            _item("offers_old", source="vkusvill_offers_ready_food", stock_qty=3),
        ]
        fetched = [
            _item("offers_new_1", source="vkusvill_offers_ready_food", stock_qty=13),
            _item("offers_new_2", source="vkusvill_offers_ready_food", stock_qty=14),
        ]

        merged, label = bot._merge_items_for_collect_mode("2026-03-16", existing, fetched, "ready")
        by_id = {item.item_id: item for item in merged}

        self.assertEqual(label, "ready_only")
        self.assertIn("inshop_keep_1", by_id)
        self.assertIn("inshop_keep_2", by_id)
        self.assertIn("fav_keep", by_id)
        self.assertIn("offers_new_1", by_id)
        self.assertIn("offers_new_2", by_id)
        self.assertIn("offers_old", by_id)
        self.assertEqual(by_id["offers_old"].stock_qty, 0)
        self.assertEqual(by_id["offers_new_1"].stock_qty, 13)

    def test_collect_mode_ready_without_fresh_ready_food_keeps_existing_snapshot(self) -> None:
        bot = self._bot_stub()
        existing = [
            _item("inshop_keep", source="vkusvill_web_system_chrome", stock_qty=5),
            _item("fav_keep", source="vkusvill_web_system_chrome_favorite", stock_qty=6),
            _item("offers_keep", source="vkusvill_offers_ready_food", stock_qty=9),
        ]
        fetched = [
            _item("inshop_noise", source="vkusvill_web_system_chrome", stock_qty=99),
        ]

        merged, label = bot._merge_items_for_collect_mode("2026-03-16", existing, fetched, "ready")
        by_id = {item.item_id: item for item in merged}

        self.assertEqual(label, "ready_only_no_fresh")
        self.assertIn("inshop_keep", by_id)
        self.assertIn("fav_keep", by_id)
        self.assertIn("offers_keep", by_id)
        self.assertNotIn("inshop_noise", by_id)
        self.assertEqual(by_id["offers_keep"].stock_qty, 9)

    def test_collect_mode_all_trusts_fetched_today_pool(self) -> None:
        bot = self._bot_stub()
        existing = [
            _item("stale_regular", source="vkusvill_web_system_chrome", stock_qty=5),
            _item("stale_favorite", source="vkusvill_web_system_chrome_favorite", stock_qty=6),
            _item("stale_ready", source="vkusvill_offers_ready_food", stock_qty=7),
        ]
        fetched = [
            _item("fresh_regular", source="vkusvill_web_system_chrome", stock_qty=2),
            _item("fresh_favorite", source="vkusvill_web_system_chrome_favorite", stock_qty=3),
        ]

        merged, label = bot._merge_items_for_collect_mode("2026-03-20", existing, fetched, "all")

        self.assertEqual(label, "all")
        self.assertEqual({item.item_id for item in merged}, {"fresh_regular", "fresh_favorite"})

    def test_collect_waves_stops_after_refresh_rejected_once(self) -> None:
        current = [_item(f"inshop_{idx}", source="vkusvill_web_system_chrome", stock_qty=idx) for idx in range(1, 7)]

        with (
            patch("scripts.vkusvill_collect_discounts._open_discounts_area"),
            patch("scripts.vkusvill_collect_discounts._collect_from_inshop_modal", return_value=current),
            patch("scripts.vkusvill_collect_discounts._collect_favorite_from_personal", return_value=[]),
            patch("scripts.vkusvill_collect_discounts._click_refresh_discounts", return_value=(False, False, True)) as refresh_mock,
            patch("scripts.vkusvill_collect_discounts._save_debug"),
        ):
            merged = _collect_waves(SimpleNamespace(), "vkusvill_web_system_chrome", waves=3, require_distinct_waves=True)

        self.assertEqual(refresh_mock.call_count, 1)
        self.assertEqual({item.item_id for item in merged}, {item.item_id for item in current})

    def test_disk_headroom_aborts_when_free_space_is_too_low(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch(
            "scripts.vkusvill_collect_discounts.shutil.disk_usage",
            return_value=SimpleNamespace(free=200 * 1024 * 1024),
        ):
            with self.assertRaises(SystemExit) as ctx:
                _ensure_disk_headroom(Path(tmp), min_free_mb=500)

        self.assertEqual(ctx.exception.code, 2)
