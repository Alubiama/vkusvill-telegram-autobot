from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import shutil
import zlib
from datetime import datetime, timedelta
from pathlib import Path
from urllib.request import urlopen
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
    WebAppInfo,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    Defaults,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.request import HTTPXRequest

from .config import Settings
from .command_utils import command_to_args, project_root
from .providers import BaseProvider, ManualJsonProvider, RPACommandProvider
from .runtime_guard import current_project_root, describe_runtime_root
from .store import ItemRow, OrderCycle, StateStore
from .vkusvill_gateway import STATUS_SOLD_OUT, STATUS_TOMORROW, VkusvillGateway

LOGGER = logging.getLogger(__name__)


class VkusvillGroupBot:
    def __init__(self, settings: Settings, store: StateStore, provider: BaseProvider) -> None:
        self.settings = settings
        self.store = store
        self.provider = provider
        self.gateway = VkusvillGateway()
        self._finalize_lock = asyncio.Lock()
        self._collect_lock = asyncio.Lock()

    def _today(self) -> str:
        return datetime.now(self.settings.timezone).strftime("%Y-%m-%d")

    def _collection_schedule_text(self) -> str:
        return ", ".join(t.strftime("%H:%M") for t in self.settings.collection_times)

    def _discounts_snapshot_day(self) -> str:
        path = Path(self.settings.discounts_json_path)
        if not path.exists():
            return ""
        try:
            return datetime.fromtimestamp(path.stat().st_mtime, self.settings.timezone).strftime("%Y-%m-%d")
        except OSError:
            return ""

    def _discounts_snapshot_is_fresh_for_today(self, day: str) -> bool:
        return self._discounts_snapshot_day() == day

    def _collect_is_verified_for_app(self, day: str) -> tuple[bool, str]:
        collect_day = str(self.store.get_meta("last_collect_day") or "")
        collect_status = str(self.store.get_meta("last_collect_status") or "").strip().lower()
        if collect_day != day:
            return False, "Свежий сбор за сегодня не подтвержден."
        if collect_status != "ok":
            suffix = f" ({collect_status})" if collect_status else ""
            return False, f"Свежий сбор за сегодня не подтвержден{suffix}."
        return True, ""

    def _build_stale_mini_app_payload(self, day: str, note: str) -> dict[str, object]:
        stale_note = str(note or "Свежий срез за сегодня не подтвержден.")
        return {
            "d": day,
            "sid": f"stale-{day}",
            "force_stale": True,
            "groups": [
                {"id": "stale-1", "title": "Сегодняшний сбор не удался", "items": []},
                {"id": "stale-2", "title": "Вчерашние скидки не показываем", "items": []},
                {"id": "stale-3", "title": "Обнови Mini App позже", "items": []},
            ],
            "favorite": [],
            "extra_ready_food": [],
            "regular_count": 0,
            "cap": 18,
            "round_status": {
                "k": "warning",
                "t": "Сегодняшний сбор не подтвержден",
                "s": "Вчерашние скидки не показываем.",
                "b": "Проверить",
                "n": stale_note,
            },
            "generated_at": self._now_iso(),
        }

    def _runtime_root_payload(self) -> dict[str, str]:
        state, detail, registered_root = describe_runtime_root()
        return {
            "state": state,
            "detail": detail,
            "current_root": str(current_project_root()),
            "registered_root": str(registered_root) if registered_root is not None else "n/a",
        }

    @staticmethod
    def _availability_payload(item: object) -> tuple[str, str]:
        status = str(getattr(item, "availability_status", "") or "").strip() or "unknown"
        reason = str(getattr(item, "availability_reason", "") or "").strip()
        stock_qty = getattr(item, "stock_qty", None)
        if status == "unknown":
            if stock_qty is not None and int(stock_qty) <= 0:
                status = STATUS_SOLD_OUT
        return status, reason

    @staticmethod
    def _snapshot_id(items: list[object], day: str) -> str:
        raw = "|".join(sorted(str(x.item_id) for x in items))
        return hashlib.sha1(f"{day}|{raw}".encode("utf-8")).hexdigest()[:12]

    def _ready_food_source_enabled(self) -> bool:
        joined = " ".join(
            x
            for x in [
                str(self.settings.rpa_command or ""),
                str(self.settings.fallback_rpa_command or ""),
            ]
            if x
        ).lower()
        return "--offers-ready-food-url" in joined

    def _excluded_item_ids(self, day: str) -> set[str]:
        raw = (self.store.get_meta(f"excluded_item_ids:{day}") or "").strip()
        if not raw:
            return set()
        return {part.strip() for part in raw.split(",") if part.strip()}

    def _filter_excluded_items(self, day: str, items: list[object]) -> list[object]:
        excluded = self._excluded_item_ids(day)
        if not excluded:
            return list(items)
        return [item for item in items if str(getattr(item, "item_id", "")) not in excluded]

    def _non_ready_food_items(self, items: list[object]) -> list[object]:
        return [item for item in items if not self._is_ready_food_offer(getattr(item, "source", ""))]

    def _only_ready_food_items(self, items: list[object]) -> list[object]:
        return [item for item in items if self._is_ready_food_offer(getattr(item, "source", ""))]

    @staticmethod
    def _merge_unique_items(primary: list[object], extra: list[object]) -> list[object]:
        merged: dict[str, object] = {}
        for item in primary:
            merged[str(getattr(item, "item_id", ""))] = item
        for item in extra:
            item_id = str(getattr(item, "item_id", ""))
            if item_id and item_id not in merged:
                merged[item_id] = item
        return list(merged.values())

    @staticmethod
    def _item_to_row(item: object) -> ItemRow:
        if isinstance(item, ItemRow):
            return item
        if hasattr(item, "to_row"):
            row = item.to_row()
            if isinstance(row, ItemRow):
                return row
        return ItemRow(
            item_id=str(getattr(item, "item_id", "")),
            name=str(getattr(item, "name", "")),
            price=float(getattr(item, "price", 0) or 0),
            discount_price=float(getattr(item, "discount_price", 0) or 0),
            source=str(getattr(item, "source", "")),
            image_url=str(getattr(item, "image_url", "") or ""),
            stock_qty=(
                int(getattr(item, "stock_qty"))
                if getattr(item, "stock_qty", None) not in (None, "")
                else None
            ),
            availability_status=str(getattr(item, "availability_status", "") or "unknown"),
            availability_reason=str(getattr(item, "availability_reason", "") or ""),
        )

    @staticmethod
    def _clone_item_with_stock(item: object, stock_qty: int | None) -> object:
        availability_status = str(getattr(item, "availability_status", "") or "").strip() or "unknown"
        availability_reason = str(getattr(item, "availability_reason", "") or "").strip()
        if stock_qty is not None and int(stock_qty) <= 0 and availability_status == "unknown":
            availability_status = STATUS_SOLD_OUT
        return item.__class__(
            item_id=str(getattr(item, "item_id", "")),
            name=str(getattr(item, "name", "")),
            price=float(getattr(item, "price", 0) or 0),
            discount_price=float(getattr(item, "discount_price", 0) or 0),
            source=str(getattr(item, "source", "")),
            image_url=str(getattr(item, "image_url", "") or ""),
            stock_qty=stock_qty,
            availability_status=availability_status,
            availability_reason=availability_reason,
        )

    def _preserve_missing_as_unavailable(self, existing_items: list[object], fetched_items: list[object]) -> list[object]:
        fetched_by_id = {str(getattr(item, "item_id", "")): item for item in fetched_items}
        merged: list[object] = list(fetched_items)
        for item in existing_items:
            item_id = str(getattr(item, "item_id", ""))
            if not item_id or item_id in fetched_by_id:
                continue
            merged.append(self._clone_item_with_stock(item, 0))
        return merged

    def _merge_snapshot_base_with_live_ready_food(
        self,
        day: str,
        snapshot_items: list[object],
        live_items: list[object],
    ) -> list[object]:
        snapshot_base = self._non_ready_food_items(self._filter_excluded_items(day, snapshot_items))
        live_ready_food = self._only_ready_food_items(self._filter_excluded_items(day, live_items))
        return self._merge_unique_items(snapshot_base, live_ready_food)

    def _merge_items_for_collect_mode(
        self,
        day: str,
        existing_items: list[object],
        fetched_items: list[object],
        mode: str,
    ) -> tuple[list[object], str]:
        existing_items = self._filter_excluded_items(day, existing_items)
        fetched_items = self._filter_excluded_items(day, fetched_items)
        if mode == "regular":
            merged_regular = self._preserve_missing_as_unavailable(
                self._non_ready_food_items(existing_items),
                self._non_ready_food_items(fetched_items),
            )
            return (
                self._merge_unique_items(
                    merged_regular,
                    self._only_ready_food_items(existing_items),
                ),
                "regular_only",
            )
        if mode == "ready":
            fresh_ready = self._only_ready_food_items(fetched_items)
            if not fresh_ready:
                return (
                    self._merge_unique_items(
                        self._non_ready_food_items(existing_items),
                        self._only_ready_food_items(existing_items),
                    ),
                    "ready_only_no_fresh",
                )
            merged_ready = self._preserve_missing_as_unavailable(
                self._only_ready_food_items(existing_items),
                fresh_ready,
            )
            return (
                self._merge_unique_items(
                    self._non_ready_food_items(existing_items),
                    merged_ready,
                ),
                "ready_only",
            )
        return list(fetched_items), "all"

    def _assess_day_integrity(self, day: str, latest_override: dict | None = None) -> dict[str, object]:
        items = self._filter_excluded_items(day, self.store.list_items(day))
        item_ids = {str(getattr(item, "item_id", "")) for item in items if str(getattr(item, "item_id", ""))}
        regular = [item for item in items if not self._is_ready_food_offer(item.source) and not self._is_favorite_item(item.name, item.source)]
        favorites = [item for item in items if self._is_favorite_item(item.name, item.source)]
        ready_food = [item for item in items if self._is_ready_food_offer(item.source)]

        latest_path = Path("webapp") / "latest.json"
        latest_error = ""
        latest: dict[str, object] = latest_override if isinstance(latest_override, dict) else {}
        if not latest:
            try:
                latest = json.loads(latest_path.read_text(encoding="utf-8"))
            except Exception as exc:
                latest_error = self._repair_mojibake(str(exc))

        latest_rows = latest.get("m") if isinstance(latest, dict) else []
        if not isinstance(latest_rows, list):
            latest_rows = []
        latest_by_id = {
            str(row[0]): row
            for row in latest_rows
            if isinstance(row, list) and len(row) >= 5 and str(row[0])
        }

        missing_in_latest = sorted(item_ids - set(latest_by_id.keys()))
        stale_in_latest = sorted(set(latest_by_id.keys()) - item_ids)
        stock_mismatches = []
        for item in items:
            row = latest_by_id.get(str(item.item_id))
            if not row:
                continue
            if row[4] != getattr(item, "stock_qty", None):
                stock_mismatches.append(str(item.item_id))

        regular_with_stock = sum(1 for item in regular if getattr(item, "stock_qty", None) is not None)
        ready_with_stock = sum(1 for item in ready_food if getattr(item, "stock_qty", None) is not None)

        groups = latest.get("g") if isinstance(latest, dict) else []
        latest_group_sizes = [len(group) for group in groups] if isinstance(groups, list) else []
        latest_favorite_count = len(latest.get("f") or []) if isinstance(latest, dict) else 0
        latest_ready_count = len(latest.get("r") or []) if isinstance(latest, dict) else 0
        latest_day = str(latest.get("d") or "n/a") if isinstance(latest, dict) else "n/a"
        image_health = self._assess_image_health(items, latest if isinstance(latest, dict) else None)

        critical: list[str] = []
        warnings: list[str] = []

        if latest_error:
            critical.append(f"latest.json не читается ({latest_error})")
        if len(regular) < 18:
            critical.append(f"обычные скидки неполные: {len(regular)}/18")
        if len(favorites) < 1:
            warnings.append("любимый товар не найден")
        if self._ready_food_source_enabled() and len(ready_food) == 0:
            warnings.append("готовая еда не собрана")
        if missing_in_latest:
            critical.append(f"latest.json отстает от базы: нет {len(missing_in_latest)} item_id")
        if stale_in_latest:
            warnings.append(f"latest.json содержит {len(stale_in_latest)} stale item_id")
        if stock_mismatches:
            warnings.append(f"рассинхрон остатков: {len(stock_mismatches)} поз.")
        if regular and regular_with_stock < len(regular):
            warnings.append(f"остатки у обычных скидок заполнены не везде ({regular_with_stock}/{len(regular)})")
        if ready_food and ready_with_stock < len(ready_food):
            warnings.append(f"остатки у готовой еды заполнены не везде ({ready_with_stock}/{len(ready_food)})")
        if latest_group_sizes[:3] != [6, 6, 6]:
            warnings.append(f"группы в latest.json выглядят странно: {latest_group_sizes}")
        if image_health["placeholder_ids"]:
            warnings.append(f"placeholder-картинки у {len(image_health['placeholder_ids'])} поз.")
        if image_health["missing_mirror_ids"]:
            warnings.append(f"нет mirror-картинок у {len(image_health['missing_mirror_ids'])} поз.")
        if image_health["missing_local_files"]:
            critical.append(f"mirror-файлы отсутствуют у {len(image_health['missing_local_files'])} поз.")
        if image_health["stale_mirror_ids"]:
            warnings.append(f"stale mirror item_id: {len(image_health['stale_mirror_ids'])}")

        state = "CRITICAL" if critical else ("WARNING" if warnings else "OK")
        return {
            "state": state,
            "items": items,
            "regular": regular,
            "favorites": favorites,
            "ready_food": ready_food,
            "latest_rows": latest_rows,
            "latest_group_sizes": latest_group_sizes,
            "latest_favorite_count": latest_favorite_count,
            "latest_ready_count": latest_ready_count,
            "latest_day": latest_day,
            "missing_in_latest": missing_in_latest,
            "stale_in_latest": stale_in_latest,
            "stock_mismatches": stock_mismatches,
            "regular_with_stock": regular_with_stock,
            "ready_with_stock": ready_with_stock,
            "image_health": image_health,
            "critical": critical,
            "warnings": warnings,
        }

    def _build_day_integrity_text(self, day: str) -> str:
        report = self._assess_day_integrity(day)
        items = list(report["items"])
        regular = list(report["regular"])
        favorites = list(report["favorites"])
        ready_food = list(report["ready_food"])
        latest_rows = list(report["latest_rows"])
        latest_group_sizes = list(report["latest_group_sizes"])
        latest_favorite_count = int(report["latest_favorite_count"])
        latest_ready_count = int(report["latest_ready_count"])
        latest_day = str(report["latest_day"])
        missing_in_latest = list(report["missing_in_latest"])
        stale_in_latest = list(report["stale_in_latest"])
        stock_mismatches = list(report["stock_mismatches"])
        regular_with_stock = int(report["regular_with_stock"])
        ready_with_stock = int(report["ready_with_stock"])
        image_health = dict(report["image_health"])
        critical = list(report["critical"])
        warnings = list(report["warnings"])
        state = str(report["state"])
        lines = [
            f"Проверка дня {day}: {state}",
            f"- база: regular={len(regular)}/18, favorite={len(favorites)}, ready_food={len(ready_food)}, total={len(items)}",
            f"- остатки: regular={regular_with_stock}/{len(regular) if regular else 0}, ready_food={ready_with_stock}/{len(ready_food) if ready_food else 0}",
            f"- latest.json: day={latest_day}, groups={latest_group_sizes}, favorite={latest_favorite_count}, ready_food={latest_ready_count}, items={len(latest_rows)}",
            f"- sync: missing_in_latest={len(missing_in_latest)}, stale_in_latest={len(stale_in_latest)}, stock_mismatch={len(stock_mismatches)}",
            (
                "- картинки: "
                f"placeholder={len(image_health['placeholder_ids'])}, "
                f"missing_mirror={len(image_health['missing_mirror_ids'])}, "
                f"missing_files={len(image_health['missing_local_files'])}, "
                f"stale_mirror={len(image_health['stale_mirror_ids'])}"
            ),
        ]
        if critical:
            lines.append("- критично:")
            lines.extend([f"  * {msg}" for msg in critical[:6]])
        if warnings:
            lines.append("- предупреждения:")
            lines.extend([f"  * {msg}" for msg in warnings[:8]])
        if not critical and not warnings:
            lines.append("- все ключевые проверки пройдены")
        return "\n".join(lines)

    def _autonomy_status_payload(self, day: str, latest_override: dict | None = None) -> dict[str, str]:
        report = self._assess_day_integrity(day, latest_override=latest_override)
        state = str(report["state"] or "WARNING")
        critical = list(report["critical"])
        warnings = list(report["warnings"])
        if state == "CRITICAL":
            note = critical[0] if critical else "день требует вмешательства"
            return {
                "state": "degraded",
                "title": "Работаем по аварийному режиму",
                "subtitle": "Свежий срез за сегодня не подтвержден.",
                "badge": "Degraded",
                "note": note,
            }
        if state == "WARNING":
            note = warnings[0] if warnings else "есть предупреждения по дню"
            return {
                "state": "warning",
                "title": "День требует проверки",
                "subtitle": "Свежий срез за сегодня не подтвержден.",
                "badge": "Проверить",
                "note": note,
            }
        return {
            "state": "healthy",
            "title": "День актуален",
            "subtitle": "Срез и Mini App подтверждены.",
            "badge": "OK",
            "note": "",
        }

    def _update_autonomy_meta(self, day: str) -> dict[str, str]:
        payload = self._autonomy_status_payload(day)
        self.store.set_meta("autonomy_state_day", day)
        self.store.set_meta("autonomy_state", payload["state"])
        self.store.set_meta("autonomy_title", payload["title"])
        self.store.set_meta("autonomy_subtitle", payload["subtitle"])
        self.store.set_meta("autonomy_badge", payload["badge"])
        self.store.set_meta("autonomy_note", payload["note"])
        self.store.set_meta("autonomy_updated_at", self._now_iso())
        return payload

    def _should_attempt_autonomy_repair(self, state: str, cooldown_minutes: int | None = None) -> bool:
        if state == "healthy":
            return False
        last_at_raw = self.store.get_meta("autonomy_last_repair_at") or ""
        last_state = self.store.get_meta("autonomy_last_repair_state") or ""
        now = datetime.now(self.settings.timezone)
        if not cooldown_minutes:
            cooldown_minutes = 20 if state == "degraded" else 45

        if last_state != state:
            self.store.set_meta("autonomy_last_repair_state", state)
            self.store.set_meta("autonomy_last_repair_at", self._now_iso())
            return True

        try:
            last_at = datetime.fromisoformat(last_at_raw)
            if last_at.tzinfo is None:
                last_at = last_at.replace(tzinfo=self.settings.timezone)
        except Exception:
            self.store.set_meta("autonomy_last_repair_at", self._now_iso())
            return True

        if now - last_at >= timedelta(minutes=cooldown_minutes):
            self.store.set_meta("autonomy_last_repair_at", self._now_iso())
            return True
        return False

    def _record_publish_result(self, ok: bool, detail: str) -> None:
        self.store.set_meta("last_publish_at", self._now_iso())
        self.store.set_meta("last_publish_status", "ok" if ok else "error")
        self.store.set_meta("last_publish_detail", detail)

    async def _attempt_autonomy_repair(self, app: Application, source: str) -> tuple[bool, str]:
        day = self._today()
        payload = self._update_autonomy_meta(day)
        state = payload["state"]
        if not self._should_attempt_autonomy_repair(state):
            return False, "cooldown"

        report = self._assess_day_integrity(day)
        actions: list[str] = []
        restored_items, restored_source = self._best_available_items(day, restore_into_live=True)
        if restored_source.startswith("snapshot:"):
            actions.append(f"restore={restored_source}")
            report = self._assess_day_integrity(day)

        needs_latest_rewrite = bool(
            str(report["latest_day"]) != day
            or list(report["missing_in_latest"])
            or list(report["stale_in_latest"])
            or list(report["stock_mismatches"])
        )
        if needs_latest_rewrite:
            items_for_app = restored_items if restored_items else self.store.list_items(day)
            if items_for_app:
                self._write_webapp_latest_snapshot(day, items_for_app)
                actions.append("rewrite_latest")
                report = self._assess_day_integrity(day)

        public_stale = (self.store.get_meta("last_publish_public_check_status") or "") == "error"
        should_publish = bool(
            self.settings.auto_publish_pages
            and (needs_latest_rewrite or public_stale or state == "degraded")
        )
        if should_publish:
            ok, detail = await self._publish_pages()
            self._record_publish_result(ok, detail)
            actions.append(f"publish={'ok' if ok else 'error'}")

        session_status = (self.store.get_meta("last_sessioncheck_status") or "").strip().lower()
        if session_status == "error":
            ok, detail = await self._run_session_probe()
            self.store.set_meta("last_sessioncheck_at", self._now_iso())
            self.store.set_meta("last_sessioncheck_status", "ok" if ok else "error")
            self.store.set_meta("last_sessioncheck_detail", detail)
            actions.append(f"session={'ok' if ok else 'error'}")

        updated = self._update_autonomy_meta(day)
        note = updated["note"] or "ok"
        if actions:
            self.store.set_meta("autonomy_last_repair_result", ",".join(actions))
            await self._send_owner(
                app,
                (
                    f"Автовосстановление ({source}): {', '.join(actions)}.\n"
                    f"Итог: {updated['title']}. Причина: {note}"
                )[:3900],
            )
            return True, ",".join(actions)
        return False, "no_actions"

    async def _run_autonomy_cycle(self, app: Application, source: str) -> None:
        day = self._today()
        payload = self._update_autonomy_meta(day)
        if payload["state"] != "healthy":
            try:
                await self._attempt_autonomy_repair(app, source)
            except Exception as exc:
                self.store.set_meta("autonomy_last_repair_result", f"error:{self._repair_mojibake(str(exc))[:200]}")
        await self._notify_autonomy_if_needed(app, source)

    async def _notify_autonomy_if_needed(self, app: Application, source: str) -> None:
        day = self._today()
        payload = self._update_autonomy_meta(day)
        state = payload["state"]
        if state == "healthy":
            self.store.set_meta("autonomy_last_alert_fp", "")
            return
        fingerprint = f"{day}|{state}|{payload['note']}"
        if (self.store.get_meta("autonomy_last_alert_fp") or "") == fingerprint:
            return
        self.store.set_meta("autonomy_last_alert_fp", fingerprint)
        self.store.set_meta("autonomy_last_alert_at", self._now_iso())
        await self._send_owner(
            app,
            (
                f"Автоконтроль ({source}): {payload['title']}.\n"
                f"{payload['subtitle']}\n"
                f"Причина: {payload['note'] or 'см. /daycheck'}"
            )[:3900],
        )

    async def _run_collect_action(
        self,
        app: Application,
        mode: str,
        started_text: str,
        success_text: str,
        failure_text: str,
        timeout_text: str,
    ) -> None:
        if self._collect_lock.locked():
            await self._send_owner(app, "Обновление уже идет. Подожди завершения текущего прогона.")
            return
        await self._send_owner(app, started_text)
        timeout_sec = max(30, int(self.settings.collect_timeout_sec) + 15)
        async with self._collect_lock:
            try:
                ok = await asyncio.wait_for(
                    self._collect_impl(
                        app,
                        quiet_errors_in_group=False,
                        mode=mode,
                    ),
                    timeout=timeout_sec,
                )
            except asyncio.TimeoutError:
                await self._send_owner(app, timeout_text)
                return
            except Exception as exc:
                await self._send_owner(app, f"{failure_text}\nПричина: {self._repair_mojibake(str(exc))[:240]}")
                return
        await self._send_owner(app, success_text if ok else failure_text)

    def _daycheck_actions_markup(self, day: str) -> InlineKeyboardMarkup | None:
        report = self._assess_day_integrity(day)
        state = str(report["state"])
        buttons: list[list[InlineKeyboardButton]] = []
        if state == "CRITICAL":
            buttons.append([InlineKeyboardButton("Обновить скидки", callback_data="ctl|collectregular")])
            if self._ready_food_source_enabled():
                buttons.append([InlineKeyboardButton("Обновить Еду", callback_data="ctl|collectready")])
            buttons.append([InlineKeyboardButton("Статус заказов", callback_data="ctl|cyclestatus")])
        elif state == "WARNING":
            if self._ready_food_source_enabled():
                buttons.append(
                    [
                        InlineKeyboardButton("Обновить скидки", callback_data="ctl|collectregular"),
                        InlineKeyboardButton("Обновить Еду", callback_data="ctl|collectready"),
                    ]
                )
            else:
                buttons.append([InlineKeyboardButton("Обновить скидки", callback_data="ctl|collectregular")])
            buttons.append([InlineKeyboardButton("Повторить проверку", callback_data="ctl|daycheck")])
        else:
            buttons.append([InlineKeyboardButton("Обновить скидки", callback_data="ctl|collectregular")])
            if self._ready_food_source_enabled():
                buttons.append([InlineKeyboardButton("Обновить Еду", callback_data="ctl|collectready")])
        return InlineKeyboardMarkup(buttons) if buttons else None

    def _snapshot_is_usable(self, items: list[object]) -> bool:
        if self._ready_food_source_enabled():
            return True
        return not any(self._is_ready_food_offer(getattr(item, "source", "")) for item in items)

    def _best_available_items(self, day: str, restore_into_live: bool = False) -> tuple[list[object], str]:
        live_items = self.store.list_items(day)
        if not self._snapshot_is_usable(live_items):
            live_items = [item for item in live_items if not self._is_ready_food_offer(item.source)]
        live_items = self._filter_excluded_items(day, live_items)
        if (
            live_items
            and (self.store.get_meta("last_collect_day") or "") == day
            and (self.store.get_meta("last_collect_status") or "").strip().lower() == "ok"
        ):
            return live_items, "live_verified"
        best_snapshot = self.store.get_best_day_snapshot(day)
        if best_snapshot is not None:
            best_snapshot.items = self._filter_excluded_items(day, best_snapshot.items)
        if best_snapshot is None or not best_snapshot.items or not self._snapshot_is_usable(best_snapshot.items):
            return live_items, "live"

        live_regular = self._regular_inshop_count(live_items)
        best_regular = self._regular_inshop_count(best_snapshot.items)
        use_snapshot = (
            not live_items
            or best_regular > live_regular
            or (best_regular == live_regular and best_snapshot.total_items > len(live_items))
        )
        if not use_snapshot:
            return live_items, "live"

        merged_items = self._merge_snapshot_base_with_live_ready_food(day, best_snapshot.items, live_items)

        if restore_into_live:
            self.store.sync_items(
                day,
                [self._item_to_row(item) for item in merged_items],
                allow_delete=True,
            )
            self.store.set_meta("last_snapshot_restore_at", self._now_iso())
            self.store.set_meta("last_snapshot_restore_day", day)
            self.store.set_meta("last_snapshot_restore_id", best_snapshot.snapshot_id)
            self.store.set_meta("last_snapshot_restore_status", best_snapshot.status)
        return merged_items, f"snapshot:{best_snapshot.snapshot_id}"

    def _archive_day_snapshot(self, day: str, items: list[object], status: str) -> str | None:
        if not items:
            return None
        snapshot_id = self._snapshot_id(items, day)
        rows = [self._item_to_row(x) for x in items]
        saved = self.store.save_day_snapshot(
            day=day,
            snapshot_id=snapshot_id,
            items=rows,
            regular_count=self._regular_inshop_count(items),
            status=status,
            created_at=self._now_iso(),
        )
        self.store.set_meta("last_day_snapshot_id", snapshot_id)
        self.store.set_meta("last_day_snapshot_day", day)
        self.store.set_meta("last_day_snapshot_status", status)
        self.store.set_meta("last_day_snapshot_saved_new", "true" if saved else "false")
        best = self.store.get_best_day_snapshot(day)
        if best is not None:
            self.store.set_meta("best_day_snapshot_id", best.snapshot_id)
            self.store.set_meta("best_day_snapshot_day", best.day)
            self.store.set_meta("best_day_snapshot_regular_count", str(best.regular_count))
            self.store.set_meta("best_day_snapshot_total_items", str(best.total_items))
            self.store.set_meta("best_day_snapshot_status", best.status)
        return snapshot_id

    def _get_chat_id(self) -> int | None:
        if self.settings.chat_id is not None:
            return self.settings.chat_id
        raw = self.store.get_meta("chat_id")
        return int(raw) if raw else None

    def _get_owner_user_id(self) -> int | None:
        if self.settings.owner_user_id is not None:
            return self.settings.owner_user_id
        raw = self.store.get_meta("owner_user_id")
        return int(raw) if raw else None

    def _set_owner_user_id(self, user_id: int) -> None:
        self.store.set_meta("owner_user_id", str(user_id))

    def _user_is_owner(self, user_id: int | None) -> bool:
        if user_id is None:
            return False
        owner_id = self._get_owner_user_id()
        if owner_id is None:
            return False
        return int(user_id) == int(owner_id)

    async def _check_owner_or_reply(self, update: Update) -> bool:
        if update.message is None or update.effective_user is None:
            return False
        if self._user_is_owner(update.effective_user.id):
            return True
        owner_id = self._get_owner_user_id()
        if owner_id is None:
            await update.message.reply_text("Владелец не задан. Сначала выполни /setowner.")
            return False
        await update.message.reply_text(f"Только владелец может это сделать. OWNER_USER_ID={owner_id}")
        return False

    async def _alert_owner_once(self, app: Application, key: str, text: str) -> None:
        fingerprint = f"{self._today()}|{key}|{text[:120]}"
        meta_key = f"alert_once:{key}"
        if (self.store.get_meta(meta_key) or "") == fingerprint:
            return
        self.store.set_meta(meta_key, fingerprint)
        await self._send_owner(app, text)

    async def _run_startup_sanity_check(self, app: Application) -> dict[str, object]:
        day = self._today()
        runtime_root = self._runtime_root_payload()
        report = self._assess_day_integrity(day)
        issues: list[str] = []
        warnings: list[str] = []

        owner_id = self._get_owner_user_id()
        chat_id = self._get_chat_id()
        if owner_id is None:
            issues.append("OWNER_USER_ID не задан")
        if chat_id is None:
            issues.append("CHAT_ID не привязан")

        if runtime_root["state"] == "error":
            issues.append(f"runtime root error: {runtime_root['detail']}")
        elif runtime_root["state"] == "warning":
            warnings.append(f"runtime root warning: {runtime_root['detail']}")

        collect_ok, collect_note = self._collect_is_verified_for_app(day)
        if not collect_ok:
            warnings.append(collect_note)

        items = list(report["items"])
        latest_day = str(report["latest_day"] or "")
        missing_in_latest = list(report["missing_in_latest"])
        if not items:
            issues.append("в базе нет товаров за сегодня")
        if latest_day != day:
            issues.append(f"latest.json не синхронизирован по дню ({latest_day or 'n/a'} vs {day})")
        if missing_in_latest:
            issues.append(f"latest.json отстает от базы: {len(missing_in_latest)} item_id")

        if chat_id is not None:
            try:
                chat = await app.bot.get_chat(chat_id)
                self.store.set_meta("startup_chat_probe_at", self._now_iso())
                self.store.set_meta("startup_chat_probe_status", "ok")
                self.store.set_meta("startup_chat_probe_title", str(getattr(chat, "title", "") or ""))
            except Exception as exc:
                detail = self._repair_mojibake(str(exc))[:200]
                self.store.set_meta("startup_chat_probe_at", self._now_iso())
                self.store.set_meta("startup_chat_probe_status", "error")
                self.store.set_meta("startup_chat_probe_title", "")
                issues.append(f"Telegram get_chat({chat_id}) failed: {detail}")
        else:
            self.store.set_meta("startup_chat_probe_at", self._now_iso())
            self.store.set_meta("startup_chat_probe_status", "error")
            self.store.set_meta("startup_chat_probe_title", "")

        status = "ok"
        if issues:
            status = "critical"
        elif warnings:
            status = "warning"
        detail = " | ".join((issues + warnings)[:6]) if (issues or warnings) else "ok"
        self.store.set_meta("last_startup_sanity_at", self._now_iso())
        self.store.set_meta("last_startup_sanity_status", status)
        self.store.set_meta("last_startup_sanity_detail", detail)

        if issues or warnings:
            lines = [
                f"Startup sanity {day}: {status.upper()}",
            ]
            lines.extend([f"- critical: {msg}" for msg in issues[:4]])
            lines.extend([f"- warning: {msg}" for msg in warnings[:4]])
            await self._alert_owner_once(app, "startup_sanity", "\n".join(lines)[:3900])

        return {
            "status": status,
            "issues": issues,
            "warnings": warnings,
            "day": day,
            "chat_id": chat_id,
        }

    async def scheduled_startup_sanity(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._run_startup_sanity_check(context.application)

    async def _send(self, app: Application, text: str, **kwargs) -> None:
        chat_id = self._get_chat_id()
        if chat_id is None:
            LOGGER.warning("Chat is not bound yet, message skipped: %s", text[:80])
            await self._alert_owner_once(
                app,
                "missing_chat_id",
                "⚠️ CHAT_ID не привязан. Групповые сообщения сейчас не отправляются. Проверь `.env` или выполни /bind в нужной группе.",
            )
            return
        try:
            await app.bot.send_message(chat_id=chat_id, text=text, **kwargs)
        except Exception as exc:
            detail = self._repair_mojibake(str(exc))[:240]
            LOGGER.warning("Failed to send group message to chat_id=%s: %s", chat_id, detail)
            await self._alert_owner_once(
                app,
                "group_send_failed",
                f"⚠️ Не удалось отправить сообщение в группу {chat_id}: {detail}",
            )
            raise

    async def _send_owner(self, app: Application, text: str, **kwargs) -> None:
        owner_id = self._get_owner_user_id()
        if owner_id is None:
            return
        try:
            await app.bot.send_message(chat_id=owner_id, text=text, **kwargs)
        except Exception:
            LOGGER.warning("Failed to send private owner message: %s", text[:120])

    def _trace_webapp(self, message: str) -> None:
        try:
            out_dir = Path(self.settings.out_dir)
            out_dir.mkdir(parents=True, exist_ok=True)
            log_path = out_dir / "webapp_events.log"
            ts = datetime.now(self.settings.timezone).strftime("%Y-%m-%d %H:%M:%S")
            with log_path.open("a", encoding="utf-8") as fp:
                fp.write(f"[{ts}] {message}\n")
        except Exception:
            pass

    def _cleanup_out_dir(self) -> int:
        days = max(1, int(self.settings.out_retention_days))
        out_dir = Path(self.settings.out_dir)
        if not out_dir.exists():
            return 0

        cutoff = datetime.now(self.settings.timezone) - timedelta(days=days)
        cutoff_ts = cutoff.timestamp()
        removed = 0
        for path in out_dir.rglob("*"):
            if not path.is_file():
                continue
            try:
                if path.stat().st_mtime < cutoff_ts:
                    path.unlink()
                    removed += 1
            except Exception:
                continue

        # Optional tidy-up: remove empty folders left after file cleanup.
        for path in sorted(out_dir.rglob("*"), key=lambda p: len(p.parts), reverse=True):
            if not path.is_dir():
                continue
            try:
                next(path.iterdir())
            except StopIteration:
                try:
                    path.rmdir()
                except Exception:
                    pass
            except Exception:
                continue
        return removed

    def _cleanup_temp_profiles(self, max_age_hours: int = 24) -> int:
        temp_root = Path("out") / "tmp"
        if not temp_root.exists():
            return 0
        cutoff_ts = (datetime.now(self.settings.timezone) - timedelta(hours=max(1, int(max_age_hours)))).timestamp()
        removed = 0
        for path in sorted(temp_root.glob("order-profile-*")):
            try:
                if not path.exists() or not path.is_dir():
                    continue
                if path.stat().st_mtime < cutoff_ts:
                    shutil.rmtree(path, ignore_errors=True)
                    removed += 1
            except Exception:
                continue
        return removed

    async def scheduled_cleanup(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        removed = self._cleanup_out_dir()
        removed_profiles = self._cleanup_temp_profiles()
        if removed > 0:
            LOGGER.info("Out-dir cleanup removed %s file(s) older than %s days", removed, self.settings.out_retention_days)
        if removed_profiles > 0:
            LOGGER.info("Temp profile cleanup removed %s stale dir(s)", removed_profiles)

    def _db_backup_dir(self) -> Path:
        return Path("data") / "backups"

    def _prune_db_backups(self, backup_dir: Path, keep: int = 7) -> int:
        dated = sorted(
            [path for path in backup_dir.glob("state_*.db") if path.is_file() and path.name != "state_startup.db"],
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        removed = 0
        for path in dated[keep:]:
            try:
                path.unlink()
                removed += 1
            except Exception:
                continue
        return removed

    def backup_state_db(self, backup_name: str) -> Path:
        backup_dir = self._db_backup_dir()
        backup_dir.mkdir(parents=True, exist_ok=True)
        dest = backup_dir / backup_name
        self.store.backup_to(str(dest))
        return dest

    async def scheduled_db_backup(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        backup_day = self._today()
        backup_name = f"state_{backup_day}.db"
        status = "ok"
        detail = ""
        try:
            dest = self.backup_state_db(backup_name)
            pruned = self._prune_db_backups(self._db_backup_dir(), keep=7)
            detail = f"{dest.name}; pruned={pruned}"
            LOGGER.info("DB backup created: %s", dest)
        except Exception as exc:
            status = "error"
            detail = str(exc)
            await self._send_owner(context.application, f"DB backup failed: {detail}")
            LOGGER.warning("DB backup failed: %s", exc)
        self.store.set_meta("last_db_backup_at", self._now_iso())
        self.store.set_meta("last_db_backup_status", status)
        self.store.set_meta("last_db_backup_detail", detail)

    def _now_iso(self) -> str:
        return datetime.now(self.settings.timezone).isoformat(timespec="seconds")

    def _iso_is_today(self, raw: str) -> bool:
        value = str(raw or "").strip()
        if not value or value == "n/a":
            return False
        try:
            dt = datetime.fromisoformat(value)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=self.settings.timezone)
            return dt.astimezone(self.settings.timezone).strftime("%Y-%m-%d") == self._today()
        except Exception:
            return False

    @staticmethod
    def _batch_label(batch_id: int | None) -> str:
        return f"batch #{int(batch_id)}" if batch_id is not None else "batch ?"

    @staticmethod
    def _cycle_state_human(status: str) -> str:
        mapping = {
            "open": "открыт",
            "finalizing": "собирается",
            "partially_added": "частично добавлен",
            "added_waiting_payment": "ждет оплаты",
            "closed": "закрыт",
            "cancelled": "отменен",
        }
        return mapping.get(status, status)

    def _owner_controls_markup(self) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("Собрать заказ в корзину", callback_data="ctl|collectnow")],
                [InlineKeyboardButton("Кто что выбрал", callback_data="ctl|whochose")],
                [InlineKeyboardButton("Закрыть после оплаты", callback_data="ctl|closecycle")],
                [InlineKeyboardButton("Еще", callback_data="ctl|more")],
            ]
        )

    def _owner_more_controls_markup(self) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("Добрать недостающее", callback_data="ctl|retrymissing")],
                [InlineKeyboardButton("Долги и оплаты", callback_data="ctl|debts")],
                [InlineKeyboardButton("Статус заказов", callback_data="ctl|cyclestatus")],
                [InlineKeyboardButton("Обновить скидки", callback_data="ctl|collectregular")],
                [InlineKeyboardButton("Обновить Еду", callback_data="ctl|collectready")],
                [InlineKeyboardButton("Проверить целостность дня", callback_data="ctl|daycheck")],
                [InlineKeyboardButton("Отменить текущий заказ", callback_data="ctl|cancelcycle")],
                [InlineKeyboardButton("Назад", callback_data="ctl|mainmenu")],
            ]
        )

    def _format_cycle_line(self, cycle: OrderCycle) -> str:
        return (
            f"- {self._batch_label(cycle.batch_id)}: {self._cycle_state_human(cycle.status)}, "
            f"позиций={cycle.selected_positions}, людей={cycle.selected_users}, "
            f"сумма={float(cycle.total_sum):.2f} RUB"
        )

    def _build_cycle_status_text(self, day: str) -> str:
        cycles = self.store.list_cycles(day)
        if not cycles:
            return f"Циклов за {day} пока нет."
        lines = [f"Циклы за {day}:"]
        for cycle in cycles[:6]:
            lines.append(self._format_cycle_line(cycle))
            if cycle.status in {"partially_added", "added_waiting_payment"}:
                missing = self.store.get_missing_cycle_items(day, cycle.batch_id)
                if missing:
                    lines.append(f"  недобрано: {len(missing)} поз.")
        return "\n".join(lines)

    @staticmethod
    def _format_selection_preview(selected_rows: list[tuple[str, int]], limit: int = 5) -> str:
        preview_rows = selected_rows[:limit]
        preview = "\n".join([f"- {name}: {qty} шт" for name, qty in preview_rows])
        extra_count = max(0, len(selected_rows) - limit)
        if extra_count > 0:
            preview = (
                f"{preview}\n- ... и еще {extra_count} поз."
                if preview
                else f"- ... и еще {extra_count} поз."
            )
        return preview

    @staticmethod
    def _format_money(value: float) -> str:
        return f"{float(value):.2f} RUB"

    def _debts_actions_markup(self) -> InlineKeyboardMarkup | None:
        balances = self.store.outstanding_balances()
        if not balances:
            return None
        buttons: list[list[InlineKeyboardButton]] = []
        for row in balances[:12]:
            user_id = int(row["user_id"])
            name = str(row["user_name"] or f"user {user_id}")
            short_name = (name[:18] + "…") if len(name) > 19 else name
            balance = float(row.get("balance") or 0)
            buttons.append(
                [
                    InlineKeyboardButton(
                        f"Оплатил: {short_name} · {balance:.0f}₽",
                        callback_data=f"ctl|paiduser|{user_id}",
                    )
                ]
            )
        return InlineKeyboardMarkup(buttons)

    def _build_debts_text(self) -> str:
        balances = self.store.outstanding_balances()
        if not balances:
            return "Сейчас долгов нет."
        lines = ["Баланс задолженности:"]
        for row in balances[:30]:
            lines.append(
                (
                    f"- {row['user_name']}: долг {self._format_money(float(row['balance']))} "
                    f"(начислено {self._format_money(float(row['charges_total']))}, "
                    f"оплачено {self._format_money(float(row['payments_total']))})"
                )
            )
        extra = max(0, len(balances) - 30)
        if extra > 0:
            lines.append(f"... и еще {extra} чел.")
        return "\n".join(lines)

    def _format_who_chose_text(self, day: str, cycle: OrderCycle | None) -> str:
        if cycle is None:
            return f"За {day} сейчас нет активного batch."
        votes = [row for row in self.store.votes_by_user(day, batch_id=cycle.batch_id) if int(row.get("qty") or 0) > 0]
        if not votes:
            return f"{self._batch_label(cycle.batch_id)} пока пуст."

        items, _snapshot_source = self._best_available_items(day, restore_into_live=True)
        items_by_id = {str(x.item_id): str(x.name) for x in items}
        by_user: dict[int, dict[str, object]] = {}
        for row in votes:
            user_id = int(row["user_id"])
            user_name = str(row["user_name"] or f"user {user_id}")
            entry = by_user.setdefault(user_id, {"name": user_name, "rows": [], "sum": 0.0})
            item_name = items_by_id.get(str(row["item_id"]), str(row["item_id"]))
            entry["rows"].append((item_name, int(row["qty"])))
        totals_by_user = {
            int(row["user_id"]): float(row.get("total_sum") or 0)
            for row in self.store.totals_by_user(day, batch_id=cycle.batch_id)
        }
        for user_id, entry in by_user.items():
            entry["sum"] = totals_by_user.get(user_id, 0.0)

        ordered = sorted(
            by_user.values(),
            key=lambda x: (-float(x["sum"]), -len(x["rows"]), str(x["name"]).casefold()),
        )
        lines = [f"Кто что выбрал в {self._batch_label(cycle.batch_id)}:"]
        for entry in ordered[:20]:
            rows = list(entry["rows"])
            lines.append(f"{entry['name']}: {len(rows)} поз. · {self._format_money(float(entry['sum']))}")
            lines.append(self._format_selection_preview(rows, limit=4))
        extra_users = max(0, len(ordered) - 20)
        if extra_users > 0:
            lines.append(f"... и еще {extra_users} чел.")
        return "\n".join(lines)[:3900]

    def _round_status_payload(self, day: str, latest_override: dict | None = None) -> dict[str, object]:
        autonomy = self._autonomy_status_payload(day, latest_override=latest_override)
        if autonomy["state"] == "degraded":
            return {
                "k": "partial",
                "t": autonomy["title"],
                "s": autonomy["subtitle"],
                "b": autonomy["badge"],
                "n": autonomy["note"],
            }
        if autonomy["state"] == "warning":
            return {
                "k": "waiting",
                "t": autonomy["title"],
                "s": autonomy["subtitle"],
                "b": autonomy["badge"],
                "n": autonomy["note"],
            }
        open_cycle = self._current_open_cycle(day)
        waiting_cycle = self._waiting_payment_cycle(day)
        partial_cycle = self._partial_cycle(day)

        if partial_cycle is not None:
            missing = len(self.store.get_missing_cycle_items(day, partial_cycle.batch_id))
            subtitle = "Owner доберет недостающее отдельно."
            if missing > 0:
                subtitle = f"Не добавилось {missing} поз. Owner доберет их отдельно."
            return {
                "k": "partial",
                "t": "Заказ требует добора",
                "s": subtitle,
                "b": "Нужен добор",
            }

        if waiting_cycle is not None:
            subtitle = "Ждет оплаты владельцем."
            badge = "Ждет оплаты"
            if open_cycle is not None and open_cycle.batch_id != waiting_cycle.batch_id:
                subtitle = "Новые выборы уже идут в следующий раунд."
                badge = "Два раунда"
            return {
                "k": "waiting",
                "t": "Заказ в корзине",
                "s": subtitle,
                "b": badge,
            }

        if open_cycle is not None:
            subtitle = "Можно выбирать товары."
            if open_cycle.selected_users > 0 or open_cycle.selected_positions > 0:
                subtitle = f"{open_cycle.selected_users} чел. · {open_cycle.selected_positions} поз."
            return {
                "k": "open",
                "t": "Сбор открыт",
                "s": subtitle,
                "b": "Идет сбор",
            }

        return {
            "k": "idle",
            "t": "Новый раунд готов",
            "s": "Можно выбирать товары.",
            "b": "Готово",
        }

    def _current_open_cycle(self, day: str) -> OrderCycle | None:
        return self.store.get_open_cycle(day)

    def _waiting_payment_cycle(self, day: str) -> OrderCycle | None:
        return self.store.get_latest_cycle(day, ("added_waiting_payment",))

    def _partial_cycle(self, day: str) -> OrderCycle | None:
        return self.store.get_latest_cycle(day, ("partially_added",))

    def _close_waiting_cycle(self, day: str) -> str:
        cycle = self._waiting_payment_cycle(day)
        if cycle is None:
            return "Нет цикла со статусом «ждет оплаты»."
        charges = self.store.ensure_cycle_charges(day, cycle.batch_id, note=f"batch {cycle.batch_id} closed")
        self.store.update_cycle_status(day, cycle.batch_id, "closed", closed_at=self._now_iso(), paid_at=self._now_iso())
        lines = [f"{self._batch_label(cycle.batch_id)} закрыт. Следующие выборы пойдут в новый open batch."]
        if charges:
            lines.append("Начислил суммы по людям:")
            for row in charges[:12]:
                lines.append(f"- {row.user_name}: {self._format_money(float(row.amount))}")
        return "\n".join(lines)

    def _cancel_open_cycle(self, day: str) -> str:
        cycle = self._current_open_cycle(day)
        if cycle is None:
            return "Сейчас нет open batch для отмены."
        self.store.update_cycle_status(day, cycle.batch_id, "cancelled", closed_at=self._now_iso())
        return (
            f"{self._batch_label(cycle.batch_id)} отменен. "
            "Он больше не будет участвовать в сборе. Следующие выборы пойдут в новый open batch."
        )

    def _sanitize_selected_rows(
        self,
        day: str,
        selected: list[dict],
    ) -> tuple[list[dict], list[dict], list[dict]]:
        items_by_id = {str(item.item_id): item for item in self.store.list_items(day)}
        validation = self.gateway.validate_selected_rows(items_by_id, selected)
        return validation.kept, validation.skipped, validation.reduced

    def _reconcile_users_for_payload(
        self,
        day: str,
        batch_id: int,
        selected: list[dict],
        votes: list[dict],
    ) -> tuple[list[dict], list[dict]]:
        selected_by_id = {str(row["item_id"]): int(row["qty"]) for row in selected}
        items_by_id = {str(item.item_id): item for item in self.store.list_items(day)}
        remaining = dict(selected_by_id)
        reconciled_votes: list[dict] = []

        for row in votes:
            item_id = str(row.get("item_id") or "")
            requested_qty = int(row.get("qty") or 0)
            if requested_qty <= 0 or item_id not in remaining:
                continue
            grant = min(requested_qty, max(0, int(remaining[item_id])))
            if grant <= 0:
                continue
            remaining[item_id] -= grant
            reconciled_votes.append(
                {
                    "user_id": int(row["user_id"]),
                    "user_name": str(row["user_name"]),
                    "item_id": item_id,
                    "qty": grant,
                }
            )

        totals: dict[int, dict] = {}
        for row in reconciled_votes:
            user_id = int(row["user_id"])
            bucket = totals.setdefault(
                user_id,
                {
                    "user_id": user_id,
                    "user_name": str(row["user_name"]),
                    "positions": 0,
                    "qty_total": 0,
                    "total_sum": 0.0,
                },
            )
            bucket["positions"] += 1
            bucket["qty_total"] += int(row["qty"])
            item = items_by_id.get(str(row["item_id"]))
            if item is not None:
                bucket["total_sum"] += float(item.discount_price) * int(row["qty"])

        totals_list = sorted(
            (
                {
                    **row,
                    "total_sum": round(float(row["total_sum"]), 2),
                }
                for row in totals.values()
            ),
            key=lambda x: (-float(x["total_sum"]), str(x["user_name"])),
        )
        return reconciled_votes, totals_list

    def _recover_cycles_on_startup(self) -> str | None:
        day = self._today()
        busy = self.store.get_latest_cycle(day, ("finalizing",))
        if busy is None:
            return None

        rows = self.store.list_cycle_item_results(day, busy.batch_id)
        if not rows:
            self.store.update_cycle_status(
                day,
                busy.batch_id,
                "open",
                executor_status="recovered_restart_reopened",
            )
            note = f"startup_recovery: {self._batch_label(busy.batch_id)} был в finalizing без результатов, возвращен в open."
            self.store.set_meta("startup_recovery_note", note)
            self.store.set_meta("startup_recovery_at", self._now_iso())
            return note

        missing = self.store.get_missing_cycle_items(day, busy.batch_id)
        next_status = "partially_added" if missing else "added_waiting_payment"
        self.store.update_cycle_status(
            day,
            busy.batch_id,
            next_status,
            executor_status="recovered_after_restart",
        )
        note = (
            f"startup_recovery: {self._batch_label(busy.batch_id)} был в finalizing, "
            f"переведен в {self._cycle_state_human(next_status)}."
        )
        self.store.set_meta("startup_recovery_note", note)
        self.store.set_meta("startup_recovery_at", self._now_iso())
        return note

    @staticmethod
    def _finalize_outcome_human(code: str) -> str:
        mapping = {
            "no_open_cycle": "нет открытого batch",
            "no_partial_cycle": "нет частично добавленного batch",
            "empty_open_batch": "в batch нет выбранных товаров",
            "missing_already_done": "все недостающие позиции уже добраны",
            "session_preflight_failed": "сессия ВкусВилл не подтверждена",
            "added_waiting_payment": "добавлен в корзину и ждет оплаты",
            "partially_added": "частично добавлен, нужно добрать недостающее",
        }
        return mapping.get(code, code)

    async def _preflight_finalize_session(self, app: Application, mode: str) -> bool:
        if self.settings.dry_run or not self.settings.order_executor_command:
            return True
        ok, detail = await self._run_executor_session_preflight(app, allow_refresh=False)
        self.store.set_meta("last_sessioncheck_at", self._now_iso())
        self.store.set_meta("last_sessioncheck_status", "ok" if ok else "error")
        self.store.set_meta("last_sessioncheck_detail", detail)
        if ok:
            return True
        action = "добором недостающих" if mode == "missing" else "сборкой batch"
        await self._send_owner(
            app,
            f"Остановил прогон перед {action}: сессия ВкусВилл не подтверждена ({detail}).",
        )
        return False

    async def _run_executor_session_preflight(self, app: Application, allow_refresh: bool) -> tuple[bool, str]:
        if not self.settings.order_executor_command:
            return True, "executor_preflight_not_configured"

        dummy_out = Path(self.settings.out_dir) / "_session_preflight.json"
        args = self._build_command_args(self.settings.order_executor_command, dummy_out, ["--check-session-only"])
        if not args:
            return False, "executor_preflight_args_empty"

        try:
            proc = await asyncio.to_thread(self._run_cmd_capture, args, 90)
        except Exception as exc:
            return False, self._repair_mojibake(str(exc))

        raw = "\n".join(x for x in [(proc.stdout or "").strip(), (proc.stderr or "").strip()] if x).strip()
        payload = self._extract_payload(raw)
        ok = proc.returncode == 0 and (not isinstance(payload, dict) or bool(payload.get("ok", True)))
        if ok:
            detail = str(payload.get("message") or "ok") if isinstance(payload, dict) else "ok"
            return True, self._repair_mojibake(detail)

        detail = ""
        if isinstance(payload, dict):
            detail = str(payload.get("error") or payload.get("message") or payload.get("status") or "").strip()
        if not detail:
            detail = (raw or f"returncode={proc.returncode}").splitlines()[-1]

        if not allow_refresh:
            return False, self._repair_mojibake(detail[:240])

        refresh_args = args + [
            "--interactive-login",
            "--interactive-login-wait-sec",
            "180",
            "--no-headless",
        ]
        try:
            refreshed = await asyncio.to_thread(self._run_cmd_capture, refresh_args, 240)
        except Exception as exc:
            return False, self._repair_mojibake(str(exc))
        refresh_raw = "\n".join(
            x for x in [(refreshed.stdout or "").strip(), (refreshed.stderr or "").strip()] if x
        ).strip()
        refresh_payload = self._extract_payload(refresh_raw)
        refresh_ok = refreshed.returncode == 0 and (
            not isinstance(refresh_payload, dict) or bool(refresh_payload.get("ok", True))
        )
        if refresh_ok:
            refresh_detail = str(refresh_payload.get("message") or "ok") if isinstance(refresh_payload, dict) else "ok"
            return True, self._repair_mojibake(refresh_detail)
        refresh_detail = ""
        if isinstance(refresh_payload, dict):
            refresh_detail = str(
                refresh_payload.get("error") or refresh_payload.get("message") or refresh_payload.get("status") or ""
            ).strip()
        if not refresh_detail:
            refresh_detail = (refresh_raw or f"returncode={refreshed.returncode}").splitlines()[-1]
        return False, self._repair_mojibake(refresh_detail[:240])

    def _webapp_build_id(self) -> str:
        try:
            raw = (Path("webapp") / "index.html").read_text(encoding="utf-8")
            match = re.search(r'const BUILD_ID = "([^"]+)"', raw)
            if match:
                build_id = match.group(1).strip()
                if build_id:
                    self.store.set_meta("webapp_build_id", build_id)
                    return build_id
        except Exception:
            pass
        cached = (self.store.get_meta("webapp_build_id") or "").strip()
        return cached or "unknown"

    def _find_batch_user_by_name(self, day: str, name: str, batch_id: int | None = None) -> tuple[int | None, str | None, str | None]:
        target_name = str(name or "").strip()
        if not target_name:
            return None, None, "Не удалось определить имя пользователя."
        rows = self.store.votes_by_user(day, batch_id=batch_id)
        exact: dict[int, str] = {}
        folded: dict[int, str] = {}
        for row in rows:
            user_id = int(row.get("user_id") or 0)
            user_name = str(row.get("user_name") or "").strip()
            if not user_id or not user_name:
                continue
            if user_name == target_name:
                exact[user_id] = user_name
            if user_name.casefold() == target_name.casefold():
                folded[user_id] = user_name
        candidates = exact or folded
        if len(candidates) == 1:
            user_id, user_name = next(iter(candidates.items()))
            return user_id, user_name, None
        if len(candidates) > 1:
            return None, None, "Нашлось несколько людей с таким именем. Лучше ответь на сообщение самого человека."
        return None, None, f"В текущем batch не нашел пользователя «{target_name}»."

    def _resolve_clearuser_target(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        day: str,
        batch_id: int | None = None,
    ) -> tuple[int | None, str | None, str | None]:
        message = update.message
        if message is None:
            return None, None, "Нет сообщения для разбора."

        reply = message.reply_to_message
        if reply is not None:
            reply_user = reply.from_user
            if reply_user is not None and not bool(reply_user.is_bot):
                display_name = " ".join(
                    part for part in [reply_user.first_name or "", reply_user.last_name or ""] if part
                ).strip() or reply_user.username or f"user {reply_user.id}"
                return int(reply_user.id), display_name, None

            reply_text = (reply.text or reply.caption or "").strip()
            if reply_text:
                first_line = reply_text.splitlines()[0].strip()
                match = re.match(r"^(?P<name>.+?):\s*(?:batch\s*#\d+,\s*)?выбрано\b", first_line, re.IGNORECASE)
                if match:
                    return self._find_batch_user_by_name(day, match.group("name"), batch_id=batch_id)

        if context.args:
            raw = " ".join(context.args).strip()
            if raw.isdigit():
                user_id = int(raw)
                rows = self.store.votes_by_user(day, batch_id=batch_id)
                for row in rows:
                    if int(row.get("user_id") or 0) == user_id:
                        return user_id, str(row.get("user_name") or f"user {user_id}"), None
                return None, None, f"В текущем batch нет пользователя с id {user_id}."
            return self._find_batch_user_by_name(day, raw, batch_id=batch_id)

        return None, None, "Ответь на сообщение человека или на бот-сводку командой /clearuser."

    @staticmethod
    def _repair_mojibake(text: str) -> str:
        raw = str(text or "")
        if not raw:
            return raw
        # Common Windows mojibake pattern: UTF-8 bytes decoded as cp1251.
        # Example: "Р›РµРЅРёРЅ..." -> "Ленин..."
        if "Р" in raw or "С" in raw:
            try:
                fixed = raw.encode("cp1251").decode("utf-8")
                return fixed
            except Exception:
                pass
        return raw

    def _short_collect_error(self, exc: Exception) -> str:
        text = self._repair_mojibake(str(exc))
        if "collect_command_failed:" in text:
            return text.split("collect_command_failed:", 1)[1].strip()[:240]
        if "All collect sources failed." in text:
            # Keep only compact diagnostic tail for owner DM.
            tail = text.split("Attempts:", 1)[-1].strip() if "Attempts:" in text else text
            return f"all_sources_failed: {tail[:220]}"
        return text[:240]

    def _should_notify_collect_error(self, message: str, cooldown_minutes: int = 30) -> bool:
        fingerprint = hashlib.sha1(message.encode("utf-8", errors="ignore")).hexdigest()[:16]
        last_fp = self.store.get_meta("last_collect_error_fp") or ""
        last_at_raw = self.store.get_meta("last_collect_error_notified_at") or ""
        now = datetime.now(self.settings.timezone)

        if fingerprint != last_fp:
            self.store.set_meta("last_collect_error_fp", fingerprint)
        self.store.set_meta("last_collect_error_notified_at", self._now_iso())
        return True

    def _should_notify_low_item_count(self, day: str, unique_items: int) -> bool:
        marker = f"{day}:{unique_items}"
        last_marker = str(self.store.get_meta("last_collect_low_items_alert") or "")
        if marker == last_marker:
            return False
        self.store.set_meta("last_collect_low_items_alert", marker)
        return True

        try:
            last_at = datetime.fromisoformat(last_at_raw)
            if last_at.tzinfo is None:
                last_at = last_at.replace(tzinfo=self.settings.timezone)
        except Exception:
            self.store.set_meta("last_collect_error_notified_at", self._now_iso())
            return True

        if now - last_at >= timedelta(minutes=cooldown_minutes):
            self.store.set_meta("last_collect_error_notified_at", self._now_iso())
            return True
        return False

    def _build_command_args(self, command_template: str, out_path: Path, extra: list[str] | None = None) -> list[str]:
        cmd = (command_template or "").replace("{order_file}", str(out_path))
        cmd = os.path.expandvars(cmd)
        args = command_to_args(cmd)
        if extra:
            args.extend(extra)
        return args

    @staticmethod
    def _extract_payload(raw_text: str) -> dict | None:
        text = (raw_text or "").strip()
        if not text:
            return None
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        for line in reversed(lines):
            try:
                parsed = json.loads(line)
            except Exception:
                continue
            if isinstance(parsed, dict):
                return parsed
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            try:
                parsed = json.loads(text[start : end + 1])
                if isinstance(parsed, dict):
                    return parsed
            except Exception:
                return None
        return None

    def _run_cmd_capture(self, args: list[str], timeout_sec: int | None = None) -> subprocess.CompletedProcess[str]:
        if not args:
            raise ValueError("Empty command args")
        return subprocess.run(
            args,
            check=False,
            capture_output=True,
            text=True,
            shell=False,
            cwd=str(project_root()),
            timeout=timeout_sec,
        )

    def _build_rpa_probe_args(self) -> list[str]:
        if self.settings.provider != "rpa_command" or not self.settings.rpa_command:
            return []

        args = command_to_args(self.settings.rpa_command)
        if not args:
            return []

        drop_flags = {
            "--interactive-login",
            "--require-distinct-waves",
        }
        drop_with_value = {
            "--waves",
            "--max-items",
            "--offers-ready-food-url",
            "--offers-ready-food-max",
            "--out-file",
        }

        cleaned: list[str] = []
        i = 0
        while i < len(args):
            arg = args[i]
            key = arg.split("=", 1)[0] if arg.startswith("--") else arg
            if key in drop_flags:
                i += 1
                continue
            if key in drop_with_value:
                if "=" in arg:
                    i += 1
                else:
                    i += 2
                continue
            cleaned.append(arg)
            i += 1

        probe_out = str(Path(self.settings.out_dir) / "session_probe.json")
        cleaned.extend(
            [
                "--waves",
                "1",
                "--max-items",
                "1",
                "--out-file",
                probe_out,
            ]
        )
        if "--headless" not in cleaned and "--no-headless" not in cleaned:
            cleaned.append("--headless")
        return cleaned

    async def _run_session_probe(self) -> tuple[bool, str]:
        args = self._build_rpa_probe_args()
        if not args:
            return True, "probe_not_configured"

        try:
            proc = await asyncio.to_thread(self._run_cmd_capture, args, 180)
        except Exception as exc:
            return False, str(exc)

        out = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()
        raw = "\n".join(x for x in [out, err] if x).strip()
        payload = self._extract_payload(raw)

        if proc.returncode == 0:
            ok_msg = "ok"
            if isinstance(payload, dict):
                ok_msg = str(payload.get("message") or ok_msg)
            return True, self._repair_mojibake(ok_msg)

        detail = ""
        if isinstance(payload, dict):
            detail = str(payload.get("error") or payload.get("message") or payload.get("status") or "").strip()
        if not detail:
            detail = (raw or f"returncode={proc.returncode}").splitlines()[-1]
        return False, self._repair_mojibake(detail[:240])

    async def _check_vkusvill_session(self, app: Application) -> tuple[bool | None, str]:
        script_path = Path("scripts") / "vkusvill_session_check.py"
        state_path = Path("data") / "vkusvill_storage_state.json"
        if not script_path.exists():
            return None, "session_check_script_missing"
        if not state_path.exists():
            return None, "session_state_missing"

        args = [
            sys.executable,
            str(script_path),
            "--state-file",
            str(state_path),
            "--url",
            "https://vkusvill.ru/personal/",
        ]
        try:
            proc = await asyncio.to_thread(self._run_cmd_capture, args, 180)
        except subprocess.TimeoutExpired:
            return None, "session_check_timeout"
        except Exception as exc:
            return None, str(exc)

        out = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()
        raw = "\n".join(x for x in [out, err] if x).strip()
        payload = self._extract_payload(raw)
        if isinstance(payload, dict) and "ok" in payload:
            ok_value = bool(payload.get("ok"))
            detail = str(payload.get("url") or payload.get("message") or payload.get("error") or "").strip()
            if not detail:
                detail = "session_ok" if ok_value else "session_check_failed"
            return ok_value, self._repair_mojibake(detail[:240])

        lowered = raw.lower()
        if "state file not found" in lowered:
            return None, self._repair_mojibake(raw[:240])
        if proc.returncode == 0:
            return True, "session_ok"
        if raw:
            return None, self._repair_mojibake(raw[:240])
        return None, f"returncode={proc.returncode}"

    async def _check_mobile_vkusvill_session(self, app: Application) -> tuple[bool | None, str]:
        script_path = Path("scripts") / "vkusvill_mobile_session_check.py"
        env_path = project_root() / ".env"
        if not script_path.exists():
            return None, "mobile_session_check_script_missing"

        args = [
            sys.executable,
            str(script_path),
            "--env-file",
            str(env_path),
        ]
        try:
            proc = await asyncio.to_thread(self._run_cmd_capture, args, 120)
        except subprocess.TimeoutExpired:
            return None, "mobile_session_check_timeout"
        except Exception as exc:
            return None, str(exc)

        out = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()
        raw = "\n".join(x for x in [out, err] if x).strip()
        payload = self._extract_payload(raw)
        if isinstance(payload, dict):
            ok_value = payload.get("ok")
            status = str(payload.get("status") or "").strip().lower()
            detail = str(payload.get("detail") or payload.get("message") or payload.get("error") or "").strip()
            if ok_value is True:
                if not detail:
                    detail = "mobile_session_ok"
                return True, self._repair_mojibake(detail[:240])
            if status in {"missing_config", "unavailable"}:
                if not detail:
                    detail = status or "mobile_session_unavailable"
                return None, self._repair_mojibake(detail[:240])
            if ok_value is False:
                if not detail:
                    detail = status or "mobile_session_failed"
                return False, self._repair_mojibake(detail[:240])
            if status == "missing_config":
                if not detail:
                    detail = "mobile_session_missing_config"
                return None, self._repair_mojibake(detail[:240])

        lowered = raw.lower()
        if "missing_config" in lowered or "vv_access_token" in lowered or "vv_refresh_token" in lowered:
            return None, self._repair_mojibake(raw[:240])
        if proc.returncode == 0:
            return True, "mobile_session_ok"
        if raw:
            return None, self._repair_mojibake(raw[:240])
        return None, f"returncode={proc.returncode}"

    async def _refresh_image_mirror(self, day: str) -> tuple[bool, str]:
        script_path = Path("scripts") / "build_image_mirror.py"
        if not script_path.exists():
            return False, "script_missing"

        args = [
            sys.executable,
            str(script_path),
            "--db-path",
            self.settings.db_path,
            "--day",
            day,
            "--out-dir",
            "webapp/img-cache/current",
        ]
        try:
            proc = await asyncio.to_thread(self._run_cmd_capture, args, 240)
        except Exception as exc:
            return False, str(exc)

        raw = "\n".join(x for x in [(proc.stdout or "").strip(), (proc.stderr or "").strip()] if x).strip()
        payload = self._extract_payload(raw)
        if proc.returncode != 0:
            detail = (raw or f"returncode={proc.returncode}").splitlines()[-1]
            return False, detail[:240]
        if isinstance(payload, dict):
            mirrored = int(payload.get("mirrored") or 0)
            failed = int(payload.get("failed") or 0)
            return True, f"mirrored={mirrored}, failed={failed}"
        return True, "ok"

    async def _backfill_missing_images(self, day: str) -> tuple[bool, str]:
        script_path = Path("scripts") / "vkusvill_backfill_images.py"
        if not script_path.exists():
            return False, "script_missing"

        args = [
            sys.executable,
            str(script_path),
            "--db-path",
            self.settings.db_path,
            "--day",
            day,
            "--chrome-user-data-dir",
            "data/chrome-user-data",
            "--chrome-profile-name",
            "Default",
            "--headless",
        ]
        try:
            proc = await asyncio.to_thread(self._run_cmd_capture, args, 240)
        except Exception as exc:
            return False, self._repair_mojibake(str(exc))[:240]

        raw = self._repair_mojibake("\n".join(x for x in [(proc.stdout or "").strip(), (proc.stderr or "").strip()] if x).strip())
        payload = self._extract_payload(raw)
        if proc.returncode != 0:
            detail = ""
            if isinstance(payload, dict):
                detail = str(payload.get("error") or payload.get("message") or "").strip()
            if not detail:
                detail = (raw.splitlines()[-1] if raw else f"returncode={proc.returncode}")
            return False, detail[:240]
        if isinstance(payload, dict):
            updated = int(payload.get("updated") or 0)
            return True, f"updated={updated}"
        return True, "ok"

    async def _publish_pages(self) -> tuple[bool, str]:
        cmd = (self.settings.publish_pages_command or "").strip()
        if not cmd:
            return False, "publish_command_missing"

        parsed = command_to_args(os.path.expandvars(cmd))
        if not parsed:
            return False, "publish_command_empty"
        # .cmd/.bat requires cmd /c when shell=False.
        if len(parsed) == 1 and parsed[0].lower().endswith((".cmd", ".bat")):
            args = ["cmd", "/c", parsed[0]]
        else:
            args = parsed

        try:
            proc = await asyncio.to_thread(self._run_cmd_capture, args, 420)
        except Exception as exc:
            return False, str(exc)

        if proc.returncode != 0:
            tail = ((proc.stderr or proc.stdout or "").strip().splitlines() or [f"returncode={proc.returncode}"])[-1]
            return False, tail[:240]
        day = self._today()
        await asyncio.sleep(8)
        smoke_ok, smoke_detail = await self._public_pages_smoke_check(day)
        self.store.set_meta("last_publish_public_check_at", self._now_iso())
        self.store.set_meta("last_publish_public_check_status", "ok" if smoke_ok else "error")
        self.store.set_meta("last_publish_public_check_detail", smoke_detail)
        if not smoke_ok:
            return False, f"publish_ok_but_public_stale: {smoke_detail}"
        return True, smoke_detail

    @staticmethod
    def _is_favorite_item(name: str, source: str) -> bool:
        src = (source or "").lower()
        if "favorite" in src or "fav" in src:
            return True
        title = (name or "").lower()
        markers = ("любим", "подобрали для вас", "назначить новый")
        return any(marker in title for marker in markers)

    @staticmethod
    def _is_ready_food_offer(source: str) -> bool:
        src = (source or "").lower()
        return src.startswith("vkusvill_offers_ready_food")

    def _build_collect_sources(self) -> list[tuple[str, BaseProvider]]:
        sources: list[tuple[str, BaseProvider]] = [("primary", self.provider)]
        if self.settings.collect_failover_enabled:
            if self.settings.fallback_rpa_command:
                sources.append(("fallback_rpa", RPACommandProvider(self.settings.fallback_rpa_command)))
            if self.settings.fallback_discounts_json_path:
                sources.append(
                    (
                        "fallback_json",
                        ManualJsonProvider(self.settings.fallback_discounts_json_path),
                    )
                )
        return sources

    @staticmethod
    def _format_collect_attempts(attempts: list[dict]) -> str:
        parts: list[str] = []
        for row in attempts:
            name = str(row.get("name") or "source")
            status = str(row.get("status") or "unknown")
            if status == "ok":
                total = int(row.get("total") or 0)
                regular = int(row.get("regular") or 0)
                parts.append(f"{name}:ok({regular}/18, total={total})")
            else:
                err = str(row.get("error") or "error").splitlines()[0][:100]
                parts.append(f"{name}:err({err})")
        return "; ".join(parts)

    async def _fetch_items_with_failover(self, now: datetime) -> tuple[list[object], dict]:
        sources = self._build_collect_sources()
        attempts: list[dict] = []
        min_regular = max(1, int(self.settings.failover_min_regular_items))

        selected_items: list[object] | None = None
        selected_source = ""
        selected_regular = -1
        selected_total = 0
        used_failover = False

        best_items: list[object] | None = None
        best_source = ""
        best_regular = -1
        best_total = 0

        for idx, (source_name, provider) in enumerate(sources):
            try:
                items = await asyncio.to_thread(provider.fetch, now)
                regular = self._regular_inshop_count(items)
                total = len(items)
                attempts.append(
                    {
                        "name": source_name,
                        "status": "ok",
                        "regular": regular,
                        "total": total,
                    }
                )

                if regular > best_regular or (regular == best_regular and total > best_total):
                    best_items = items
                    best_source = source_name
                    best_regular = regular
                    best_total = total

                if regular >= min_regular:
                    selected_items = items
                    selected_source = source_name
                    selected_regular = regular
                    selected_total = total
                    used_failover = idx > 0
                    break
            except Exception as exc:
                attempts.append(
                    {
                        "name": source_name,
                        "status": "error",
                        "error": self._repair_mojibake(str(exc)),
                    }
                )

        if selected_items is not None:
            return selected_items, {
                "selected_source": selected_source,
                "selected_regular": selected_regular,
                "selected_total": selected_total,
                "used_failover": used_failover,
                "attempts": attempts,
                "meets_min_regular": True,
            }

        if best_items is not None:
            meets = best_regular >= min_regular
            if self.settings.failover_require_min_regular and not meets:
                raise ValueError(
                    "No source reached required regular threshold "
                    f"({best_regular}/{min_regular}). Attempts: {self._format_collect_attempts(attempts)}"
                )
            return best_items, {
                "selected_source": best_source,
                "selected_regular": best_regular,
                "selected_total": best_total,
                "used_failover": best_source != "primary",
                "attempts": attempts,
                "meets_min_regular": meets,
            }

        raise ValueError(f"All collect sources failed. Attempts: {self._format_collect_attempts(attempts)}")

    def _mini_groups(self, items: list[object]) -> tuple[list[dict], list[object], list[object]]:
        favorites: list[object] = []
        regular: list[object] = []
        ready_food: list[object] = []
        for item in items:
            if self._is_favorite_item(item.name, item.source):
                favorites.append(item)
            elif self._is_ready_food_offer(item.source):
                ready_food.append(item)
            else:
                regular.append(item)

        regular = regular[:18]
        groups: list[dict] = []
        for idx in range(3):
            start = idx * 6
            chunk = regular[start : start + 6]
            groups.append(
                {
                    "id": f"g{idx + 1}",
                    "title": f"Подборка {idx + 1}",
                    "items": chunk,
                }
            )
        return groups, favorites, ready_food

    @staticmethod
    def _compact_image_url_for_webapp(url: str) -> str:
        raw = (url or "").strip()
        if not raw:
            return ""
        base = raw.split("?", 1)[0]
        prefix = "https://img.vkusvill.ru/pim/images/"
        if base.startswith(prefix):
            tail = base[len(prefix) :]
            m = re.match(r"^(site/)?site_MiniWebP/([0-9a-fA-F-]{36})\.webp$", tail)
            if m:
                variant = "1" if m.group(1) else "0"
                return f"vi:{variant}:{m.group(2).lower()}"
            return f"vv:{tail}"
        return base

    @staticmethod
    def _image_url_is_placeholder(url: str) -> bool:
        raw = (url or "").strip().lower()
        if not raw:
            return True
        return "no-image.svg" in raw

    @staticmethod
    def _load_webapp_mirror_map() -> dict[str, str]:
        map_path = Path("webapp") / "img-cache" / "current" / "map.json"
        try:
            raw = json.loads(map_path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        items = raw.get("items")
        if not isinstance(items, dict):
            return {}
        return {str(k): str(v) for k, v in items.items() if k and v}

    def _assess_image_health(self, items: list[object], latest_payload: dict | None = None) -> dict[str, object]:
        mirror_map = self._load_webapp_mirror_map()
        latest_mirror = latest_payload.get("im") if isinstance(latest_payload, dict) else {}
        if not isinstance(latest_mirror, dict):
            latest_mirror = {}

        placeholder_ids: list[str] = []
        missing_mirror_ids: list[str] = []
        missing_local_files: list[str] = []
        stale_mirror_ids = sorted(set(mirror_map.keys()) - {str(getattr(item, "item_id", "")) for item in items})
        image_root = Path("webapp")

        for item in items:
            item_id = str(getattr(item, "item_id", "") or "")
            if not item_id:
                continue
            if self._image_url_is_placeholder(str(getattr(item, "image_url", "") or "")):
                placeholder_ids.append(item_id)
            rel = str(mirror_map.get(item_id) or latest_mirror.get(item_id) or "")
            if not rel:
                missing_mirror_ids.append(item_id)
                continue
            if not (image_root / rel).exists():
                missing_local_files.append(item_id)

        broken_ids = sorted(set(placeholder_ids) | set(missing_mirror_ids) | set(missing_local_files))
        return {
            "placeholder_ids": sorted(placeholder_ids),
            "missing_mirror_ids": sorted(missing_mirror_ids),
            "missing_local_files": sorted(missing_local_files),
            "stale_mirror_ids": stale_mirror_ids,
            "broken_ids": broken_ids,
        }

    def _public_asset_url(self, rel_path: str) -> str | None:
        base = (self.settings.mini_app_url or "").strip()
        rel = (rel_path or "").strip().lstrip("/")
        if not base or not rel:
            return None
        if not base.endswith("/"):
            base = f"{base}/"
        return f"{base}{rel}"

    def _fetch_public_asset_head(self, rel_path: str) -> tuple[bool, str]:
        url = self._public_asset_url(rel_path)
        if not url:
            return False, "mini_app_url_missing"
        try:
            with urlopen(url, timeout=20) as resp:
                code = int(getattr(resp, "status", 200) or 200)
                if code >= 400:
                    return False, f"http_{code}"
                resp.read(1)
            return True, "ok"
        except Exception as exc:
            return False, self._repair_mojibake(str(exc))[:160]

    def _build_public_webapp_snapshot(self, day: str, items: list[object]) -> dict:
        groups, favorites, ready_food = self._mini_groups(items)
        snapshot_id = self._snapshot_id(items, day)
        regular_count = sum(len(g["items"]) for g in groups)
        mirror_map = self._load_webapp_mirror_map()
        totals_map = {
            str(row["item_id"]): int(row["qty"])
            for row in self.store.totals_by_item(day)
            if int(row.get("qty") or 0) > 0
        }

        unique_items: list[object] = []
        index_by_item_id: dict[str, int] = {}

        def register(item: object) -> int:
            item_id = str(item.item_id)
            idx = index_by_item_id.get(item_id)
            if idx is not None:
                return idx
            idx = len(unique_items)
            unique_items.append(item)
            index_by_item_id[item_id] = idx
            return idx

        group_indexes: list[list[int]] = []
        for g in groups:
            group_indexes.append([register(item) for item in g["items"]])
        favorite_indexes = [register(item) for item in favorites[:1]]
        ready_food_indexes = [register(item) for item in ready_food]
        group_totals = [
            [idx, totals_map.get(str(item.item_id), 0)]
            for idx, item in enumerate(unique_items)
            if totals_map.get(str(item.item_id), 0) > 0
        ]
        filtered_mirror_map = {
            item_id: rel_path
            for item_id, rel_path in mirror_map.items()
            if item_id in index_by_item_id
        }

        compact_rows = [
            [
                str(item.item_id),
                str(item.name),
                float(item.discount_price),
                self._compact_image_url_for_webapp(str(getattr(item, "image_url", "") or "")),
                (
                    int(getattr(item, "stock_qty"))
                    if getattr(item, "stock_qty", None) is not None
                    else None
                ),
                self._availability_payload(item)[0],
                self._availability_payload(item)[1],
            ]
            for item in unique_items
        ]
        candidate_payload = {
            "d": day,
            "sid": snapshot_id,
            "m": compact_rows,
            "g": group_indexes,
            "f": favorite_indexes,
            "r": ready_food_indexes,
            "gt": group_totals,
            "rc": regular_count,
            "cap": 18,
            "im": filtered_mirror_map,
        }
        round_status = self._round_status_payload(day, latest_override=candidate_payload)

        return {
            "d": day,
            "sid": snapshot_id,
            "m": compact_rows,
            "g": group_indexes,
            "f": favorite_indexes,
            "r": ready_food_indexes,
            "gt": group_totals,
            "rc": regular_count,
            "cap": 18,
            "rs": round_status,
            "im": filtered_mirror_map,
            "generated_at": self._now_iso(),
        }

    def _build_public_webapp_stale_snapshot(self, day: str, note: str) -> dict:
        stale_payload = self._build_stale_mini_app_payload(day, note)
        return {
            "d": day,
            "sid": str(stale_payload.get("sid") or f"stale-{day}"),
            "force_stale": True,
            "m": [],
            "g": [[], [], []],
            "f": [],
            "r": [],
            "gt": [],
            "rc": 0,
            "cap": 18,
            "rs": stale_payload.get("round_status") or {},
            "im": {},
            "generated_at": self._now_iso(),
        }

    def _write_webapp_latest_snapshot(self, day: str, items: list[object]) -> None:
        out_path = Path("webapp") / "latest.json"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        payload = self._build_public_webapp_snapshot(day, items)
        out_path.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    def _write_webapp_stale_snapshot(self, day: str, note: str) -> None:
        out_path = Path("webapp") / "latest.json"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        payload = self._build_public_webapp_stale_snapshot(day, note)
        out_path.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    @staticmethod
    def _encode_webapp_payload(payload: dict) -> str:
        raw_payload = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(zlib.compress(raw_payload, level=9)).decode("ascii").rstrip("=")

    def _build_mini_app_url(self, user_id: int | None) -> str | None:
        if not self.settings.mini_app_url:
            return None

        day = self._today()
        collect_verified, collect_note = self._collect_is_verified_for_app(day)
        items, _snapshot_source = self._best_available_items(day) if collect_verified else ([], "collect_unverified")
        parts = urlsplit(self.settings.mini_app_url)
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        query["v"] = datetime.now(self.settings.timezone).strftime("%Y%m%d%H%M%S")
        query["cb"] = str(int(datetime.now(self.settings.timezone).timestamp()))
        query["ui"] = self._webapp_build_id()

        if not items:
            try:
                self._write_webapp_stale_snapshot(
                    day,
                    collect_note or "Свежий срез за сегодня не подтвержден.",
                )
            except Exception as exc:
                LOGGER.warning("Failed to write stale webapp snapshot: %s", exc)
            stale_payload = self._build_stale_mini_app_payload(
                day,
                collect_note or "Свежий срез за сегодня не подтвержден.",
            )
            query["data"] = self._encode_webapp_payload(stale_payload)
            query["enc"] = "z"
            query["sid"] = stale_payload["sid"]
            return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))

        groups, favorites, ready_food = self._mini_groups(items)
        snapshot_id = self._snapshot_id(items, day)
        regular_count = sum(len(g["items"]) for g in groups)
        round_status = self._round_status_payload(day)
        totals_map = {
            str(row["item_id"]): int(row["qty"])
            for row in self.store.totals_by_item(day)
            if int(row.get("qty") or 0) > 0
        }

        # Compact payload: dictionary of unique items + group indexes.
        # This keeps URL safely short for Telegram WebApp buttons.
        unique_items: list[object] = []
        index_by_item_id: dict[str, int] = {}

        def register(item: object) -> int:
            item_id = str(item.item_id)
            idx = index_by_item_id.get(item_id)
            if idx is not None:
                return idx
            idx = len(unique_items)
            unique_items.append(item)
            index_by_item_id[item_id] = idx
            return idx

        group_indexes: list[list[int]] = []
        for g in groups:
            group_indexes.append([register(item) for item in g["items"]])
        favorite_indexes = [register(item) for item in favorites[:1]]
        ready_food_indexes = [register(item) for item in ready_food]
        group_totals = [
            [idx, totals_map.get(str(item.item_id), 0)]
            for idx, item in enumerate(unique_items)
            if totals_map.get(str(item.item_id), 0) > 0
        ]

        def _compact_image_url(url: str) -> str:
            raw = (url or "").strip()
            if not raw:
                return ""
            # Remove unstable cache query and shorten common host prefix.
            base = raw.split("?", 1)[0]
            prefix = "https://img.vkusvill.ru/pim/images/"
            if base.startswith(prefix):
                tail = base[len(prefix) :]
                # Ultra-compact form for most VkusVill image paths.
                # vi:0:<uuid> -> /site_MiniWebP/<uuid>.webp
                # vi:1:<uuid> -> /site/site_MiniWebP/<uuid>.webp
                m = re.match(r"^(site/)?site_MiniWebP/([0-9a-fA-F-]{36})\.webp$", tail)
                if m:
                    variant = "1" if m.group(1) else "0"
                    return f"vi:{variant}:{m.group(2).lower()}"
                return f"vv:{tail}"
            return base

        compact_payload = {
            "d": day,
            "sid": snapshot_id,
            "m": [
                [
                    str(item.item_id),
                    str(item.name),
                    float(item.discount_price),
                    _compact_image_url(str(getattr(item, "image_url", "") or "")),
                    (
                        int(getattr(item, "stock_qty"))
                        if getattr(item, "stock_qty", None) is not None
                        else None
                    ),
                    self._availability_payload(item)[0],
                    self._availability_payload(item)[1],
                ]
                for item in unique_items
            ],
            "g": group_indexes,
            "f": favorite_indexes,
            "r": ready_food_indexes,
            "gt": group_totals,
            "rc": regular_count,
            "cap": 18,
            "rs": round_status,
        }
        query["data"] = self._encode_webapp_payload(compact_payload)
        query["enc"] = "z"
        query["sid"] = snapshot_id
        out_url = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))

        # Safety fallback: if URL gets too long, drop image URLs and keep text mode stable.
        if len(out_url) > 7000:
            compact_payload["m"] = [
                [
                    str(item.item_id),
                    str(item.name),
                    float(item.discount_price),
                    "",
                    (
                        int(getattr(item, "stock_qty"))
                        if getattr(item, "stock_qty", None) is not None
                        else None
                    ),
                    self._availability_payload(item)[0],
                    self._availability_payload(item)[1],
                ]
                for item in unique_items
            ]
            query["data"] = self._encode_webapp_payload(compact_payload)
            query["ui"] = self._webapp_build_id()
            query["sid"] = snapshot_id
            out_url = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))

        return out_url

    def _public_latest_url(self) -> str | None:
        base = (self.settings.mini_app_url or "").strip()
        if not base:
            return None
        if not base.endswith("/"):
            base = f"{base}/"
        return f"{base}latest.json?v={int(datetime.now(self.settings.timezone).timestamp())}"

    def _fetch_public_latest_snapshot(self) -> tuple[bool, dict | None, str]:
        url = self._public_latest_url()
        if not url:
            return False, None, "mini_app_url_missing"
        try:
            with urlopen(url, timeout=25) as resp:
                raw = resp.read().decode("utf-8")
            payload = json.loads(raw)
        except Exception as exc:
            return False, None, self._repair_mojibake(str(exc))[:240]
        if not isinstance(payload, dict):
            return False, None, "public_latest_not_json_object"
        return True, payload, "ok"

    async def _public_pages_smoke_check(self, day: str) -> tuple[bool, str]:
        local_items = self.store.list_items(day)
        local_payload = self._build_public_webapp_snapshot(day, local_items)
        public_ok, public_payload, detail = await asyncio.to_thread(self._fetch_public_latest_snapshot)
        if not public_ok or public_payload is None:
            return False, f"public_latest_unreachable ({detail})"

        local_sid = str(local_payload.get("sid") or "")
        public_sid = str(public_payload.get("sid") or "")
        local_day = str(local_payload.get("d") or "")
        public_day = str(public_payload.get("d") or "")
        local_total = len(local_payload.get("m") or [])
        public_total = len(public_payload.get("m") or [])
        local_regular = int(local_payload.get("rc") or 0)
        public_regular = int(public_payload.get("rc") or 0)
        local_ready = len(local_payload.get("r") or [])
        public_ready = len(public_payload.get("r") or [])

        if public_day != local_day:
            return False, f"public_day_mismatch local={local_day} public={public_day}"
        if public_regular < local_regular:
            return False, f"public_regular_stale local={local_regular} public={public_regular}"
        if public_total < local_total:
            return False, f"public_total_stale local={local_total} public={public_total}"
        if public_ready < local_ready:
            return False, f"public_ready_stale local={local_ready} public={public_ready}"
        if local_sid and public_sid and local_sid != public_sid:
            return False, f"public_snapshot_stale local={local_sid} public={public_sid}"

        public_mirror = public_payload.get("im") if isinstance(public_payload, dict) else {}
        if not isinstance(public_mirror, dict):
            public_mirror = {}
        image_sample_ids = list(public_mirror.keys())[:5]
        if image_sample_ids:
            broken_public_images: list[str] = []
            for item_id in image_sample_ids:
                rel = str(public_mirror.get(item_id) or "")
                asset_ok, asset_detail = await asyncio.to_thread(self._fetch_public_asset_head, rel)
                if not asset_ok:
                    broken_public_images.append(f"{item_id}:{asset_detail}")
            if broken_public_images:
                return False, "public_image_unreachable " + ", ".join(broken_public_images[:3])
        return True, (
            f"public_ok day={public_day}, regular={public_regular}/18, "
            f"ready={public_ready}, total={public_total}, sid={public_sid or 'n/a'}, "
            f"images_checked={len(image_sample_ids)}"
        )

    def _regular_inshop_count(self, items: list[object]) -> int:
        count = 0
        for item in items:
            if self._is_favorite_item(item.name, item.source):
                continue
            if self._is_ready_food_offer(item.source):
                continue
            count += 1
        return count

    @staticmethod
    def _private_app_deeplink(bot_username: str | None) -> str | None:
        if not bot_username:
            return None
        username = bot_username.strip().lstrip("@")
        if not username:
            return None
        return f"https://t.me/{username}?start=open_app"

    async def bind(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_chat is None or update.message is None or update.effective_user is None:
            return
        if not await self._check_owner_or_reply(update):
            return
        force_private = bool(context.args and str(context.args[0]).lower() == "force")
        if update.effective_chat.type == "private" and not force_private:
            await update.message.reply_text(
                "Сейчас это личный чат. Для рабочего режима привяжи группу командой /bind в группе.\n"
                "Если нужно оставить личный чат, используй /bind force."
            )
            return

        chat_id = update.effective_chat.id
        self.store.set_meta("chat_id", str(chat_id))
        chat_type = update.effective_chat.type
        await update.message.reply_text(
            f"Чат привязан: {chat_id} ({chat_type}). Теперь служебные сообщения идут сюда."
        )

    async def setowner(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.message is None or update.effective_user is None:
            return
        user_id = update.effective_user.id

        if self.settings.owner_user_id is not None:
            if user_id == self.settings.owner_user_id:
                await update.message.reply_text(f"OWNER_USER_ID зафиксирован в .env. Ты владелец: {user_id}")
            else:
                await update.message.reply_text(
                    f"Владелец зафиксирован в .env: {self.settings.owner_user_id}. Из чата изменить нельзя."
                )
            return

        current_owner = self._get_owner_user_id()
        if current_owner is None:
            self._set_owner_user_id(user_id)
            await update.message.reply_text(f"Владелец установлен: {user_id}")
            return
        if current_owner != user_id:
            await update.message.reply_text(f"Только текущий владелец ({current_owner}) может подтвердить owner.")
            return
        self._set_owner_user_id(user_id)
        await update.message.reply_text(f"Владелец подтвержден: {user_id}")

    async def collect(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._check_owner_or_reply(update):
            return
        ok = await self._collect_impl(context.application, quiet_errors_in_group=False)
        if update.message and ok:
            await update.message.reply_text("Скидки обновлены.")
        elif update.message:
            await update.message.reply_text("Сбор не удался. Подробности отправил владельцу в личку.")

    async def collectregular(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._check_owner_or_reply(update):
            return
        ok = await self._collect_impl(context.application, quiet_errors_in_group=False, mode="regular")
        if update.message and ok:
            await update.message.reply_text("Скидочные подборки обновлены.")
        elif update.message:
            await update.message.reply_text("Обновление скидочных подборок не удалось.")

    async def collectready(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._check_owner_or_reply(update):
            return
        ok = await self._collect_impl(context.application, quiet_errors_in_group=False, mode="ready")
        if update.message and ok:
            await update.message.reply_text("Готовая еда обновлена.")
        elif update.message:
            await update.message.reply_text("Обновление готовой еды не удалось.")

    async def morningaudit(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.message is None:
            return
        if not await self._check_owner_or_reply(update):
            return
        await update.message.reply_text("Запускаю жесткий утренний прогон: скидки, еда, проверка целостности.")
        await self._run_morning_audit(context.application, source="manual")

    async def mirror(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.message is None:
            return
        if not await self._check_owner_or_reply(update):
            return
        day = self._today()
        if context.args:
            candidate = str(context.args[0]).strip()
            if len(candidate) == 10 and candidate[4] == "-" and candidate[7] == "-":
                day = candidate
        ok, detail = await self._refresh_image_mirror(day)
        self.store.set_meta("last_mirror_at", self._now_iso())
        self.store.set_meta("last_mirror_status", "ok" if ok else "error")
        self.store.set_meta("last_mirror_detail", detail)
        if ok:
            await update.message.reply_text(
                f"Кэш картинок обновлен за {day}: {detail}.\n"
                "Чтобы это увидели все в Mini App, запусти /publishapp."
            )
            return
        await update.message.reply_text(f"Кэш картинок не обновлен: {detail}")

    async def publishapp(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.message is None:
            return
        if not await self._check_owner_or_reply(update):
            return
        ok, detail = await self._publish_pages()
        self.store.set_meta("last_publish_at", self._now_iso())
        self.store.set_meta("last_publish_status", "ok" if ok else "error")
        self.store.set_meta("last_publish_detail", detail)
        if ok:
            await update.message.reply_text("Mini App опубликован на GitHub Pages.")
            return
        await update.message.reply_text(f"Публикация Mini App не удалась: {detail}")

    async def where(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.message is None:
            return
        bound_chat_id = self._get_chat_id()
        owner_id = self._get_owner_user_id()
        current_chat_id = update.effective_chat.id if update.effective_chat else None
        current_chat_type = update.effective_chat.type if update.effective_chat else "unknown"
        current_user_id = update.effective_user.id if update.effective_user else None
        lines = [
            f"Current chat: {current_chat_id} ({current_chat_type})",
            f"Bound chat: {bound_chat_id}",
            f"Owner: {owner_id}",
            f"You: {current_user_id}",
        ]
        if bound_chat_id is None:
            lines.append("Подсказка: запусти /bind в группе заказа.")
        await update.message.reply_text("\n".join(lines))

    async def selftest(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.message is None:
            return
        if not await self._check_owner_or_reply(update):
            return

        day = self._today()
        items, snapshot_source = self._best_available_items(day)
        integrity = self._assess_day_integrity(day)
        runtime_root = self._runtime_root_payload()
        regular_count = self._regular_inshop_count(items)
        favorite_count = sum(1 for x in items if self._is_favorite_item(x.name, x.source))
        ready_food_count = sum(1 for x in items if self._is_ready_food_offer(x.source))
        lines = [
            f"Selftest {day}",
            f"- bound_chat: {self._get_chat_id()}",
            f"- owner: {self._get_owner_user_id()}",
            f"- provider: {self.settings.provider}",
            f"- dry_run: {self.settings.dry_run}",
            f"- collection_times: {', '.join(t.strftime('%H:%M') for t in self.settings.collection_times)}",
            f"- order_deadline: {self.settings.order_deadline.strftime('%H:%M')}",
            f"- items_total: {len(items)}",
            f"- inshop_regular: {regular_count}/18",
            f"- favorite: {favorite_count}",
            f"- ready_food: {ready_food_count}",
            f"- source_for_app: {snapshot_source}",
            f"- runtime_root: state={runtime_root['state']}, current={runtime_root['current_root']}, canonical={runtime_root['registered_root']}",
        ]
        await update.message.reply_text("\n".join(lines))

    async def sessioncheck(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.message is None:
            return
        if not await self._check_owner_or_reply(update):
            return

        ok, detail = await self._run_session_probe()
        self.store.set_meta("last_sessioncheck_at", self._now_iso())
        self.store.set_meta("last_sessioncheck_status", "ok" if ok else "error")
        self.store.set_meta("last_sessioncheck_detail", detail)

        result_text = "Сессия ВкусВилл: OK." if ok else f"Сессия ВкусВилл требует входа: {detail}"
        chat_type = update.effective_chat.type if update.effective_chat else "private"
        if chat_type != "private":
            await self._send_owner(context.application, result_text)
            await update.message.reply_text("Проверил. Результат отправил владельцу в личку.")
            return

        if ok:
            await update.message.reply_text(result_text)
        else:
            await update.message.reply_text(result_text)

    async def mobilecheck(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.message is None:
            return
        if not await self._check_owner_or_reply(update):
            return

        ok, detail = await self._check_mobile_vkusvill_session(context.application)
        self.store.set_meta("last_mobile_sessioncheck_at", self._now_iso())
        self.store.set_meta("last_mobile_sessioncheck_status", "ok" if ok is True else ("error" if ok is False else "warning"))
        self.store.set_meta("last_mobile_sessioncheck_detail", detail)

        if ok is True:
            result_text = "Мобильная VkusVill API сессия: OK."
        elif ok is False:
            result_text = f"Мобильная VkusVill API сессия требует входа: {detail}"
        else:
            result_text = f"Мобильная VkusVill API сессия пока не подтверждена: {detail}"

        chat_type = update.effective_chat.type if update.effective_chat else "private"
        if chat_type != "private":
            await self._send_owner(context.application, result_text)
            await update.message.reply_text("Проверил mobile API. Результат отправил владельцу в личку.")
            return

        await update.message.reply_text(result_text)

    async def _collect_impl(
        self,
        app: Application,
        skip_if_full: bool = False,
        quiet_errors_in_group: bool = True,
        announce_success_in_group: bool = True,
        mode: str = "all",
    ) -> bool:
        now = datetime.now(self.settings.timezone)
        day = now.strftime("%Y-%m-%d")
        existing = self.store.list_items(day)
        existing_regular_count = self._regular_inshop_count(existing)
        if skip_if_full:
            if existing_regular_count >= 18:
                LOGGER.info(
                    "Skip scheduled collect: already have %s regular inshop items for %s",
                    existing_regular_count,
                    day,
                )
                return True
        try:
            items, collect_meta = await self._fetch_items_with_failover(now)
        except Exception as exc:
            short_reason = self._short_collect_error(exc)
            self.store.set_meta("last_collect_at", self._now_iso())
            self.store.set_meta("last_collect_status", "error")
            self.store.set_meta("last_collect_source", "none")
            self.store.set_meta("last_collect_attempts", short_reason[:1000])
            if not quiet_errors_in_group:
                await self._send(
                    app,
                    "Сбор скидок не удался. Детали отправлены владельцу в личку.",
                )
            owner_msg = (
                "Сбор скидок не удался.\n"
                f"Причина: {short_reason}\n"
                "Проверь сессию ВкусВилл и fallback-настройки."
            )
            if self._should_notify_collect_error(owner_msg):
                await self._send_owner(app, owner_msg)
            LOGGER.exception("Collect failed")
            return False

        items, collect_mode_label = self._merge_items_for_collect_mode(day, existing, items, mode)
        fetched_regular_count = self._regular_inshop_count(items)
        # Guard against accidental downgrade when replacement limit is reached:
        # if we already have full 18/18, do not overwrite with a partial wave.
        if mode == "regular" and existing_regular_count >= 18 and fetched_regular_count < 18:
            merged_guard_items = self._merge_snapshot_base_with_live_ready_food(day, existing, items)
            existing_favorite_count = sum(1 for x in merged_guard_items if self._is_favorite_item(x.name, x.source))
            existing_ready_food_count = sum(1 for x in merged_guard_items if self._is_ready_food_offer(x.source))
            self.store.sync_items(day, [self._item_to_row(x) for x in merged_guard_items], allow_delete=True)
            archived_snapshot_id = self._archive_day_snapshot(day, merged_guard_items, "guard_preserve_full")
            self.store.set_meta("last_collect_at", self._now_iso())
            self.store.set_meta("last_collect_status", "guard_preserve_full")
            self.store.set_meta("last_collect_day", day)
            self.store.set_meta("last_collect_regular_count", str(existing_regular_count))
            self.store.set_meta("last_collect_total_items", str(len(merged_guard_items)))
            if announce_success_in_group:
                await self._send(
                    app,
                    (
                        f"Сбор {now.strftime('%H:%M')} пропущен защитой: "
                        f"новый срез {fetched_regular_count}/18, сохранен предыдущий полный набор {existing_regular_count}/18. "
                        f"В базе осталось: {len(merged_guard_items)} (любимый: {existing_favorite_count}, готовая еда: {existing_ready_food_count}). "
                        f"Снэпшот: {archived_snapshot_id or 'n/a'}."
                    ),
                )
            else:
                await self._send_owner(
                    app,
                    (
                        f"Сбор {now.strftime('%H:%M')} пропущен защитой: "
                        f"новый срез {fetched_regular_count}/18, сохранен предыдущий полный набор {existing_regular_count}/18. "
                        f"В базе осталось: {len(merged_guard_items)} (любимый: {existing_favorite_count}, готовая еда: {existing_ready_food_count}). "
                        f"Снэпшот: {archived_snapshot_id or 'n/a'}."
                    ),
                )
            return True

        # Additional downgrade protection:
        # keep richer same-day snapshot when a new collect brings fewer regular slots.
        if mode == "regular" and existing_regular_count > fetched_regular_count and existing_regular_count > 0:
            merged_guard_items = self._merge_snapshot_base_with_live_ready_food(day, existing, items)
            fresh, removed = self.store.sync_items(day, [self._item_to_row(x) for x in merged_guard_items], allow_delete=True)
            all_items = self.store.list_items(day)
            regular_count = self._regular_inshop_count(all_items)
            favorite_count = sum(1 for x in all_items if self._is_favorite_item(x.name, x.source))
            ready_food_count = sum(1 for x in all_items if self._is_ready_food_offer(x.source))
            archived_snapshot_id = self._archive_day_snapshot(day, all_items, "guard_keep_richer_snapshot")
            self.store.set_meta("last_collect_at", self._now_iso())
            self.store.set_meta("last_collect_status", "guard_keep_richer_snapshot")
            self.store.set_meta("last_collect_day", day)
            self.store.set_meta("last_collect_regular_count", str(regular_count))
            self.store.set_meta("last_collect_total_items", str(len(all_items)))
            selected_source = str(collect_meta.get("selected_source") or "primary")
            self.store.set_meta("last_collect_source", selected_source)
            self.store.set_meta("last_collect_attempts", self._format_collect_attempts(collect_meta.get("attempts") or []))
            self.store.set_meta(
                "last_collect_failover_used",
                "true" if bool(collect_meta.get("used_failover")) else "false",
            )
            await self._send_owner(
                app,
                (
                    "Сработала защита от деградации подборок.\n"
                    f"Новый срез: {fetched_regular_count}/18, оставлен более полный: {existing_regular_count}/18.\n"
                    f"Итог в базе: {len(all_items)} (любимый: {favorite_count}, готовая еда: {ready_food_count}).\n"
                    f"Резервный снэпшот: {archived_snapshot_id or 'n/a'}.\n"
                    f"Попытки: {self._format_collect_attempts(collect_meta.get('attempts') or [])}"
                ),
            )
            return True

        fresh, removed = self.store.sync_items(day, [self._item_to_row(x) for x in items], allow_delete=True)
        all_items = self.store.list_items(day)
        regular_count = self._regular_inshop_count(all_items)
        favorite_count = sum(1 for x in all_items if self._is_favorite_item(x.name, x.source))
        ready_food_count = sum(1 for x in all_items if self._is_ready_food_offer(x.source))
        archived_snapshot_id = self._archive_day_snapshot(day, all_items, "ok")
        try:
            self._write_webapp_latest_snapshot(day, all_items)
        except Exception as exc:
            LOGGER.warning("Failed to write latest webapp snapshot: %s", exc)
        self.store.set_meta("last_collect_at", self._now_iso())
        self.store.set_meta("last_collect_status", "ok")
        self.store.set_meta("last_collect_error_fp", "")
        self.store.set_meta("last_collect_error_notified_at", "")
        self.store.set_meta("last_collect_day", day)
        self.store.set_meta("last_collect_regular_count", str(regular_count))
        self.store.set_meta("last_collect_total_items", str(len(all_items)))
        selected_source = str(collect_meta.get("selected_source") or "primary")
        self.store.set_meta("last_collect_source", selected_source)
        self.store.set_meta("last_collect_mode", collect_mode_label)
        self.store.set_meta("last_collect_attempts", self._format_collect_attempts(collect_meta.get("attempts") or []))
        self.store.set_meta(
            "last_collect_failover_used",
            "true" if bool(collect_meta.get("used_failover")) else "false",
        )
        unique_item_count = len({str(item.item_id) for item in all_items if str(item.item_id)})
        backfill_ok, backfill_detail = await self._backfill_missing_images(day)
        self.store.set_meta("last_image_backfill_at", self._now_iso())
        self.store.set_meta("last_image_backfill_status", "ok" if backfill_ok else "error")
        self.store.set_meta("last_image_backfill_detail", backfill_detail)
        mirror_ok, mirror_detail = await self._refresh_image_mirror(day)
        self.store.set_meta("last_mirror_at", self._now_iso())
        self.store.set_meta("last_mirror_status", "ok" if mirror_ok else "error")
        self.store.set_meta("last_mirror_detail", mirror_detail)
        publish_suffix = ""
        if mirror_ok and self.settings.auto_publish_pages:
            publish_ok, publish_detail = await self._publish_pages()
            self.store.set_meta("last_publish_at", self._now_iso())
            self.store.set_meta("last_publish_status", "ok" if publish_ok else "error")
            self.store.set_meta("last_publish_detail", publish_detail)
            publish_suffix = f" Публикация Pages: {publish_detail}."
        image_health = self._assess_image_health(all_items)
        self.store.set_meta("last_image_health_at", self._now_iso())
        self.store.set_meta(
            "last_image_health_status",
            "ok" if not image_health["broken_ids"] and not image_health["stale_mirror_ids"] else "warning",
        )
        self.store.set_meta(
            "last_image_health_detail",
            (
                f"placeholder={len(image_health['placeholder_ids'])}, "
                f"missing_mirror={len(image_health['missing_mirror_ids'])}, "
                f"missing_files={len(image_health['missing_local_files'])}, "
                f"stale_mirror={len(image_health['stale_mirror_ids'])}"
            ),
        )
        summary_text = (
            f"Сбор {now.strftime('%H:%M')} завершен ({collect_mode_label}). "
            f"В базе: {len(all_items)} (новых {len(fresh)}, удалено {removed}). "
            f"Подборки 20%: {regular_count}/18, любимый: {favorite_count}, готовая еда: {ready_food_count}. "
            f"Backfill картинок: {backfill_detail}. Кэш картинок: {mirror_detail}. "
            f"Снэпшот: {archived_snapshot_id or 'n/a'}.{publish_suffix}"
        )
        await self._send_owner(app, summary_text)
        if image_health["broken_ids"] or image_health["stale_mirror_ids"]:
            await self._send_owner(
                app,
                (
                    "Проверка картинок после сбора: есть проблемы.\n"
                    f"- placeholder: {len(image_health['placeholder_ids'])}\n"
                    f"- без mirror: {len(image_health['missing_mirror_ids'])}\n"
                    f"- missing files: {len(image_health['missing_local_files'])}\n"
                    f"- stale mirror: {len(image_health['stale_mirror_ids'])}\n"
                    "Смотри /daycheck или перезапусти image pipeline."
                )[:3900],
            )
        if bool(collect_meta.get("used_failover")):
            await self._send_owner(
                app,
                (
                    "Сбор выполнен через fallback-источник.\n"
                    f"Выбран: {selected_source}\n"
                    f"Попытки: {self._format_collect_attempts(collect_meta.get('attempts') or [])}"
                ),
            )
        if (
            unique_item_count < self.settings.collect_min_items
            and self._should_notify_low_item_count(day, unique_item_count)
        ):
            await self._send_owner(
                app,
                (
                    f"⚠️ Сбор завершен, но найдено только {unique_item_count} товаров. "
                    "Проверь сессию VkusVill."
                ),
            )
        return True

    async def scheduled_collect(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._collect_impl(
            context.application,
            skip_if_full=True,
            quiet_errors_in_group=True,
            announce_success_in_group=False,
        )
        await self._run_autonomy_cycle(context.application, source="scheduled_collect")

    async def _run_morning_audit(self, app: Application, source: str) -> None:
        session_ok, session_detail = await self._check_mobile_vkusvill_session(app)
        self.store.set_meta("last_mobile_sessioncheck_at", self._now_iso())
        self.store.set_meta("last_mobile_sessioncheck_status", "ok" if session_ok is True else ("warning" if session_ok is None else "error"))
        self.store.set_meta("last_mobile_sessioncheck_detail", session_detail)
        if session_ok is False:
            await self._send_owner(
                app,
                f"Утренний аудит остановлен: mobile API сессия ВкусВилл не здорова ({session_detail}).",
            )
            return
        ok_regular = await self._collect_impl(
            app,
            skip_if_full=False,
            quiet_errors_in_group=True,
            announce_success_in_group=False,
            mode="regular",
        )
        ok_ready = await self._collect_impl(
            app,
            skip_if_full=False,
            quiet_errors_in_group=True,
            announce_success_in_group=False,
            mode="ready",
        )
        report = self._build_day_integrity_text(self._today())
        self.store.set_meta("last_morning_audit_at", self._now_iso())
        self.store.set_meta("last_morning_audit_status", "ok" if (ok_regular and ok_ready) else "warning")
        self.store.set_meta("last_morning_audit_source", source)
        self._update_autonomy_meta(self._today())
        await self._send_owner(
            app,
            (
                f"Жесткий прогон {source}: скидки={'ok' if ok_regular else 'warn'}, "
                f"еда={'ok' if ok_ready else 'warn'}.\n{report}"
            )[:3900],
        )
        await self._run_autonomy_cycle(app, source=f"morning_audit:{source}")

    async def scheduled_morning_audit(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._run_morning_audit(context.application, source="scheduled")

    async def scheduled_sessioncheck(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        ok, detail = await self._run_session_probe()
        self.store.set_meta("last_sessioncheck_at", self._now_iso())
        self.store.set_meta("last_sessioncheck_status", "ok" if ok else "error")
        self.store.set_meta("last_sessioncheck_detail", detail)
        self._update_autonomy_meta(self._today())
        if not ok:
            await self._send_owner(
                context.application,
                f"Проверка сессии ВкусВилл: требуется вход в Chrome-профиль ({detail}).",
            )
        await self._run_autonomy_cycle(context.application, source="sessioncheck")

    async def scheduled_autonomy_watchdog(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._run_autonomy_cycle(context.application, source="autonomy_watchdog")

    async def app(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.message is None or update.effective_user is None:
            return
        if not self.settings.mini_app_url:
            await update.message.reply_text("MINI_APP_URL не задан в .env.")
            return

        chat_type = update.effective_chat.type if update.effective_chat else ""
        if chat_type != "private":
            deep_link = self._private_app_deeplink(getattr(context.bot, "username", None))
            if deep_link:
                await update.message.reply_text(
                    "В группе Mini App напрямую не открывается. Открой в личке с ботом:",
                    reply_markup=InlineKeyboardMarkup(
                        [[InlineKeyboardButton("Открыть Mini App", url=deep_link)]]
                    ),
                )
            else:
                await update.message.reply_text("Открой бота в личке и выполни /app.")
            return

        web_url = self._build_mini_app_url(update.effective_user.id) or self.settings.mini_app_url
        keyboard_rows: list[list[KeyboardButton]] = [
            [KeyboardButton("Открыть скидки", web_app=WebAppInfo(url=web_url))]
        ]
        kb = ReplyKeyboardMarkup(
            keyboard_rows,
            resize_keyboard=True,
            one_time_keyboard=True,
            input_field_placeholder="Открыть скидки",
        )
        await update.message.reply_text(
            "Открой Mini App кнопкой снизу.",
            reply_markup=kb,
        )
        if self._user_is_owner(update.effective_user.id):
            await update.message.reply_text(
                "Управление владельца:",
                reply_markup=self._owner_controls_markup(),
            )

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.message is None:
            return
        args = context.args or []
        if args and args[0].strip().lower() in {"open_app", "app", "miniapp"}:
            await self.app(update, context)
            return
        await self.app(update, context)

    async def hidekbd(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.message is None:
            return
        await update.message.reply_text("Клавиатура скрыта.", reply_markup=ReplyKeyboardRemove())


    async def on_control(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if query is None or query.data is None or query.from_user is None:
            return
        parts = query.data.split("|")
        if len(parts) < 2:
            await query.answer()
            return

        action = parts[1]
        if not self._user_is_owner(query.from_user.id):
            await query.answer("Только owner может это делать.", show_alert=True)
            return

        if action == "collectnow":
            await query.answer("Собираю заказ...")
            if query.message is not None:
                await query.message.reply_text("Запускаю сбор заказа по текущему срезу.")
            await self._finalize_impl(context.application, mode="open")
            return

        if action == "more":
            await query.answer()
            if query.message is not None:
                await query.edit_message_text(
                    "Дополнительные действия владельца:",
                    reply_markup=self._owner_more_controls_markup(),
                )
            return

        if action == "mainmenu":
            await query.answer()
            if query.message is not None:
                await query.edit_message_text(
                    "Основное управление владельца:",
                    reply_markup=self._owner_controls_markup(),
                )
            return

        if action == "collectregular":
            await query.answer("Обновляю скидки...")
            context.application.create_task(
                self._run_collect_action(
                    context.application,
                    mode="regular",
                    started_text="Запускаю обновление скидочных подборок...",
                    success_text="Скидочные подборки обновлены.",
                    failure_text="Обновление скидочных подборок не удалось.",
                    timeout_text="Обновление скидочных подборок зависло по таймауту. Лучше проверить /health и /sessioncheck.",
                )
            )
            return

        if action == "collectready":
            await query.answer("Обновляю Еду...")
            context.application.create_task(
                self._run_collect_action(
                    context.application,
                    mode="ready",
                    started_text="Запускаю обновление готовой еды...",
                    success_text="Готовая еда обновлена.",
                    failure_text="Обновление готовой еды не удалось.",
                    timeout_text="Обновление готовой еды зависло по таймауту. Лучше проверить /health и /sessioncheck.",
                )
            )
            return

        if action == "retrymissing":
            await query.answer("Добираю недостающее...")
            await self._finalize_impl(context.application, mode="missing")
            return

        if action == "closecycle":
            await query.answer("Закрываю цикл...")
            day = self._today()
            closed = self._close_waiting_cycle(day)
            if query.message is not None:
                await query.message.reply_text(closed)
            return

        if action == "cancelcycle":
            await query.answer("Отменяю open batch...")
            day = self._today()
            cancelled = self._cancel_open_cycle(day)
            if query.message is not None:
                await query.message.reply_text(cancelled)
            return

        if action == "cyclestatus":
            await query.answer()
            if query.message is not None:
                await query.message.reply_text(self._build_cycle_status_text(self._today()))
            return

        if action == "whochose":
            await query.answer()
            day = self._today()
            cycle = self._current_open_cycle(day) or self._waiting_payment_cycle(day) or self._partial_cycle(day)
            if query.message is not None:
                await query.message.reply_text(self._format_who_chose_text(day, cycle))
            return

        if action == "debts":
            await query.answer()
            if query.message is not None:
                await query.message.reply_text(
                    self._build_debts_text(),
                    reply_markup=self._debts_actions_markup(),
                )
            return

        if action == "paiduser" and len(parts) >= 3:
            user_id = int(parts[2])
            balance_row = self.store.get_outstanding_balance(user_id)
            if not balance_row:
                await query.answer("Уже закрыто.", show_alert=True)
                return
            amount = float(balance_row.get("balance") or 0)
            user_name = str(balance_row.get("user_name") or f"user {user_id}")
            paid = self.store.register_payment(
                user_id=user_id,
                user_name=user_name,
                amount=amount,
                note=f"manual owner mark on {self._today()}",
            )
            await query.answer(f"Погасил {paid:.2f} RUB", show_alert=True)
            if query.message is not None:
                await query.message.reply_text(
                    f"{user_name}: отмечено как оплачено на {self._format_money(paid)}.",
                    reply_markup=self._debts_actions_markup(),
                )
            return

        if action == "daycheck":
            await query.answer()
            if query.message is not None:
                day = self._today()
                await query.message.reply_text(
                    self._build_day_integrity_text(day),
                    reply_markup=self._daycheck_actions_markup(day),
                )
            return

        await query.answer()

    async def on_webapp_data(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.message is None or update.effective_user is None:
            return
        wad = update.message.web_app_data
        if wad is None or not wad.data:
            self._trace_webapp(
                f"empty_webapp_data chat={update.effective_chat.id if update.effective_chat else 'na'} "
                f"user={update.effective_user.id}"
            )
            return

        self._trace_webapp(
            f"incoming chat={update.effective_chat.id if update.effective_chat else 'na'} "
            f"user={update.effective_user.id} len={len(wad.data)}"
        )
        try:
            payload = json.loads(wad.data)
        except json.JSONDecodeError:
            self._trace_webapp("json_decode_error")
            await update.message.reply_text("Не смог разобрать ответ Mini App. Просто открой /app заново и повтори.")
            return

        day = self._today()
        user_id = update.effective_user.id
        user_name = update.effective_user.full_name
        items, _snapshot_source = self._best_available_items(day, restore_into_live=True)
        items_by_id = {x.item_id: x for x in items}
        snapshot_id = self._snapshot_id(items, day)

        ptype = payload.get("type")
        self._trace_webapp(
            f"parsed type={ptype} payload_day={payload.get('day')} payload_snapshot={payload.get('snapshot_id')} "
            f"items_today={len(items)}"
        )
        if ptype == "single_choice":
            item_id = str(payload.get("item_id") or "")
            try:
                qty = int(payload.get("qty") or 0)
            except (TypeError, ValueError):
                await update.message.reply_text("Некорректное значение qty в Mini App payload.")
                return
            if item_id not in items_by_id:
                self._trace_webapp(f"single_choice_not_found item_id={item_id}")
                await update.message.reply_text("Этот товар уже обновился и исчез из сегодняшнего набора. Открой /app заново.")
                return
            batch_id = self.store.set_vote(day, user_id, user_name, item_id, max(0, qty))
            self._trace_webapp(f"single_choice_saved item_id={item_id} qty={max(0, qty)}")
            await update.message.reply_text(
                f"Выбор принят в {self._batch_label(batch_id)}: {items_by_id[item_id].name} -> {max(0, qty)}"
            )
            return

        if ptype == "all_choices":
            payload_day = str(payload.get("day") or "")
            payload_snapshot = str(payload.get("snapshot_id") or "")
            if payload_day and payload_day != day:
                self._trace_webapp(f"stale_day payload_day={payload_day} day={day}")
                await update.message.reply_text(
                    f"Данные устарели ({payload_day} vs {day}). Открой Mini App заново через /app."
                )
                return
            snapshot_mismatch = bool(payload_snapshot and payload_snapshot != snapshot_id)
            if snapshot_mismatch:
                self._trace_webapp(f"stale_snapshot payload={payload_snapshot} actual={snapshot_id}")

            qty_map = payload.get("qty") or {}
            selected_positive = 0
            touched = 0
            batch_id: int | None = None
            for item_id, raw_qty in qty_map.items():
                if item_id not in items_by_id:
                    continue
                try:
                    qty = max(0, int(raw_qty))
                except (TypeError, ValueError):
                    continue
                batch_id = self.store.set_vote(day, user_id, user_name, item_id, qty)
                touched += 1
                if qty > 0:
                    selected_positive += 1

            selected_rows: list[tuple[str, int]] = []
            subtotal = 0.0
            for item in items:
                qty = int(self.store.get_user_qty(day, user_id, item.item_id))
                if qty > 0:
                    selected_rows.append((item.name, qty))
                    subtotal += float(item.discount_price) * qty
            selected_preview = self._format_selection_preview(selected_rows, limit=5)

            if touched == 0:
                self._trace_webapp("all_choices_touched_0")
                msg = "Ничего не сохранено: список уже обновился. Открой Mini App заново через /app."
                await update.message.reply_text(msg)
                bound_chat_id = self._get_chat_id()
                current_chat = update.effective_chat.id if update.effective_chat else None
                if bound_chat_id is not None and current_chat is not None and bound_chat_id != current_chat:
                    await self._send(
                        context.application,
                        f"{user_name}: выбор не сохранен (устаревший снимок). Нужен новый /app.",
                    )
                return

            self._trace_webapp(f"all_choices_saved selected={selected_positive} touched={touched}")
            if snapshot_mismatch:
                msg = (
                    f"Выбор принят в {self._batch_label(batch_id)}: {selected_positive} товаров (обновлено {touched}). "
                    "Часть позиций могла измениться после обновления, это нормально."
                )
            else:
                msg = f"Выбор принят в {self._batch_label(batch_id)}: {selected_positive} товаров (обновлено {touched})."
            if selected_positive == 0:
                msg = f"Выбор очищен в {self._batch_label(batch_id)}. Сейчас у тебя нет активных товаров."
            elif subtotal > 0:
                msg = f"{msg}\nСумма сейчас: {self._format_money(subtotal)}"
            if selected_preview:
                msg = f"{msg}\nТвой выбор:\n{selected_preview}"
            await update.message.reply_text(msg)
            bound_chat_id = self._get_chat_id()
            current_chat = update.effective_chat.id if update.effective_chat else None
            if bound_chat_id is not None and current_chat is not None and bound_chat_id != current_chat:
                if selected_positive == 0:
                    group_msg = f"{user_name}: выбор очищен в {self._batch_label(batch_id)}."
                else:
                    group_msg = (
                        f"{user_name}: {self._batch_label(batch_id)}, "
                        f"выбрано {selected_positive} товаров (обновлено {touched}), "
                        f"сумма {self._format_money(subtotal)}."
                    )
                if selected_preview and selected_positive > 0:
                    group_msg = f"{group_msg}\nВыбор:\n{selected_preview}"
                await self._send(
                    context.application,
                    group_msg[:3900],
                )
            return

        self._trace_webapp(f"unknown_type type={ptype}")
        await update.message.reply_text("Mini App прислал незнакомый формат. Открой /app заново и попробуй еще раз.")

    async def status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.message is None:
            return
        day = self._today()
        open_cycle = self._current_open_cycle(day)
        totals = self.store.totals_by_item(day, batch_id=open_cycle.batch_id) if open_cycle else []
        waiting = self._waiting_payment_cycle(day)
        partial = self._partial_cycle(day)
        if not totals and open_cycle is None and waiting is None and partial is None:
            await update.message.reply_text(
                f"На сегодня активного цикла пока нет. Автообновление: {self._collection_schedule_text()} (Europe/Moscow)."
            )
            return

        lines = [f"Статус за {day}:"]
        if open_cycle is not None:
            lines.append(f"Open: {self._batch_label(open_cycle.batch_id)}")
        for row in totals:
            lines.append(
                f"- {row['name']}: {int(row['qty'])} шт ({float(row['discount_price']):.2f} RUB)"
            )
        if waiting is not None:
            lines.append(f"Ждет оплаты: {self._batch_label(waiting.batch_id)}")
        if partial is not None:
            lines.append(f"Частично добавлен: {self._batch_label(partial.batch_id)}")
        await update.message.reply_text("\n".join(lines))

    async def cart(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.message is None:
            return
        if not await self._check_owner_or_reply(update):
            return
        await update.message.reply_text("Сканирую корзину и сверяю с сегодняшними скидками...")

        cmd = [
            sys.executable,
            "scripts/vkusvill_cart_report.py",
            "--discounts-json",
            self.settings.discounts_json_path,
            "--chrome-user-data-dir",
            "data/chrome-user-data",
            "--chrome-profile-name",
            "Default",
            "--headless",
        ]
        try:
            proc = await asyncio.to_thread(
                subprocess.run,
                cmd,
                capture_output=True,
                text=True,
                check=True,
            )
        except subprocess.CalledProcessError as exc:
            err = (exc.stderr or exc.stdout or str(exc)).strip()
            await update.message.reply_text(f"Cart scan failed:\n{err[:3000]}")
            return

        stdout = (proc.stdout or "").strip()
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError:
            await update.message.reply_text(f"Cart scan parse error:\n{stdout[:3000]}")
            return

        if payload.get("error"):
            await update.message.reply_text(f"Cart scan error: {payload['error']}")
            return

        cart_count = int(payload.get("cart_count") or 0)
        matched = payload.get("matches") or []
        unmatched = payload.get("unmatched") or []

        if cart_count == 0:
            await update.message.reply_text(
                "Cart is empty in web profile now. Add items to cart, then run /cart again."
            )
            return

        lines = [f"Cart scan: {cart_count} items, with today discounts: {len(matched)}."]
        if matched:
            lines.append("")
            lines.append("Top by savings:")
            for idx, row in enumerate(matched[:20], start=1):
                lines.append(
                    (
                        f"{idx}. {row['name']} x{int(row['qty'])}\n"
                        f"   {float(row['discount_price']):.2f}/{float(row['price']):.2f} RUB, "
                        f"save {float(row['saving_total']):.2f} RUB ({float(row['saving_percent']):.1f}%)"
                    )
                )
        if unmatched:
            lines.append("")
            lines.append(f"Without match in today's discounts: {len(unmatched)}.")

        await update.message.reply_text("\n".join(lines)[:3900])

    async def finalize(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._check_owner_or_reply(update):
            return
        await self._finalize_impl(context.application, mode="open")

    async def collectnow(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._check_owner_or_reply(update):
            return
        await self._finalize_impl(context.application, mode="open")

    async def retrymissing(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._check_owner_or_reply(update):
            return
        await self._finalize_impl(context.application, mode="missing")

    async def closecycle(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.message is None:
            return
        if not await self._check_owner_or_reply(update):
            return
        await update.message.reply_text(self._close_waiting_cycle(self._today()))

    async def cancelcycle(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.message is None:
            return
        if not await self._check_owner_or_reply(update):
            return
        await update.message.reply_text(self._cancel_open_cycle(self._today()))

    async def cyclestatus(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.message is None:
            return
        if not await self._check_owner_or_reply(update):
            return
        await update.message.reply_text(self._build_cycle_status_text(self._today()))

    async def whochose(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.message is None:
            return
        if not await self._check_owner_or_reply(update):
            return
        day = self._today()
        cycle = self._current_open_cycle(day) or self._waiting_payment_cycle(day) or self._partial_cycle(day)
        await update.message.reply_text(self._format_who_chose_text(day, cycle))

    async def debts(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.message is None:
            return
        if not await self._check_owner_or_reply(update):
            return
        await update.message.reply_text(
            self._build_debts_text(),
            reply_markup=self._debts_actions_markup(),
        )

    async def daycheck(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.message is None:
            return
        if not await self._check_owner_or_reply(update):
            return
        day = self._today()
        await update.message.reply_text(
            self._build_day_integrity_text(day),
            reply_markup=self._daycheck_actions_markup(day),
        )

    async def resetday(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.message is None:
            return
        if not await self._check_owner_or_reply(update):
            return
        day = self._today()
        self.store.clear_day(day)
        await update.message.reply_text(
            f"Состояние за {day} очищено. Автосбор снова заполнит товары по расписанию."
        )

    async def clearvotes(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.message is None:
            return
        if not await self._check_owner_or_reply(update):
            return
        day = self._today()
        batch_id = self.store.clear_votes(day)
        if batch_id is None:
            await update.message.reply_text(f"За {day} нет open batch для очистки.")
            return
        await update.message.reply_text(
            f"Выборы в {self._batch_label(batch_id)} очищены. Можно собирать новый заказ."
        )

    async def clearuser(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.message is None:
            return
        if not await self._check_owner_or_reply(update):
            return
        day = self._today()
        cycle = self.store.get_open_cycle(day)
        if cycle is None:
            await update.message.reply_text(f"За {day} сейчас нет open batch для точечной очистки.")
            return

        target_user_id, target_user_name, error = self._resolve_clearuser_target(
            update,
            context,
            day,
            batch_id=cycle.batch_id,
        )
        if error:
            await update.message.reply_text(error)
            return
        if target_user_id is None or target_user_name is None:
            await update.message.reply_text("Не удалось определить, чей выбор нужно снять.")
            return

        rows = [
            row
            for row in self.store.votes_by_user(day, batch_id=cycle.batch_id)
            if int(row.get("user_id") or 0) == int(target_user_id) and int(row.get("qty") or 0) > 0
        ]
        if not rows:
            await update.message.reply_text(
                f"У {target_user_name} сейчас нет активного выбора в {self._batch_label(cycle.batch_id)}."
            )
            return

        positions = len(rows)
        qty_total = sum(int(row.get("qty") or 0) for row in rows)
        batch_id = self.store.clear_user_votes(day, int(target_user_id), batch_id=cycle.batch_id)
        await update.message.reply_text(
            f"{target_user_name}: выбор в {self._batch_label(batch_id)} очищен. "
            f"Позиций: {positions}, штук: {qty_total}."
        )

    def _schedule_startup_collect_if_needed(self, app: Application) -> None:
        day = self._today()
        items, _snapshot_source = self._best_available_items(day)
        collect_day = str(self.store.get_meta("last_collect_day") or "")
        collect_status = str(self.store.get_meta("last_collect_status") or "").strip().lower()
        discounts_fresh = self._discounts_snapshot_is_fresh_for_today(day)
        if items and collect_day == day and collect_status == "ok" and discounts_fresh:
            return
        if not self.settings.collection_times:
            return
        first_time = min(self.settings.collection_times)
        now = datetime.now(self.settings.timezone)
        if (now.hour, now.minute) < (first_time.hour, first_time.minute):
            return
        reasons: list[str] = []
        if not items:
            reasons.append("items missing")
        if collect_day != day:
            reasons.append(f"last_collect_day={collect_day or 'n/a'}")
        elif collect_status != "ok":
            reasons.append(f"last_collect_status={collect_status or 'n/a'}")
        if not discounts_fresh:
            reasons.append(f"discounts_file_day={self._discounts_snapshot_day() or 'n/a'}")
        note = "startup catchup scheduled: " + ", ".join(reasons or ["stale discounts state"])
        LOGGER.warning(note)
        self.store.set_meta("startup_recovery_note", note)
        self.store.set_meta("startup_recovery_at", self._now_iso())
        app.job_queue.run_once(self.scheduled_collect, when=5, name="collect-startup-catchup")

    def _build_final_payload(self, day: str, batch_id: int, only_missing: bool = False) -> dict:
        if only_missing:
            missing_rows = self.store.get_missing_cycle_items(day, batch_id)
            selected = [
                {
                    "item_id": row.item_id,
                    "name": row.name,
                    "price": float(row.price),
                    "discount_price": float(row.discount_price),
                    "qty": int(row.requested_qty - row.added_qty),
                }
                for row in missing_rows
                if int(row.requested_qty) > int(row.added_qty)
            ]
            selected_ids = {str(row["item_id"]) for row in selected}
            users = [row for row in self.store.votes_by_user(day, batch_id=batch_id) if str(row["item_id"]) in selected_ids]
        else:
            totals = self.store.totals_by_item(day, batch_id=batch_id)
            users = self.store.votes_by_user(day, batch_id=batch_id)
            selected = [row for row in totals if int(row["qty"]) > 0]
        selected, skipped_items, reduced_items = self._sanitize_selected_rows(day, selected)
        total_sum = sum(float(row["discount_price"]) * int(row["qty"]) for row in selected)
        users, users_totals = self._reconcile_users_for_payload(day, batch_id, selected, users)
        return {
            "day": day,
            "batch_id": batch_id,
            "items": selected,
            "votes_by_user": users,
            "totals_by_user": users_totals,
            "total_sum_discount_price": round(total_sum, 2),
            "dry_run": self.settings.dry_run,
            "mode": "missing" if only_missing else "full",
            "skipped_items": skipped_items,
            "reduced_items": reduced_items,
        }

    async def _finalize_impl(self, app: Application, mode: str = "open") -> None:
        if self._finalize_lock.locked():
            await self._send_owner(app, "Finalize уже идет. Подожди завершения текущего прогона.")
            return

        async with self._finalize_lock:
            day = self._today()
            busy = self.store.get_latest_cycle(day, ("finalizing",))
            if busy is not None:
                await self._send_owner(app, f"{self._batch_label(busy.batch_id)} уже в статусе finalizing.")
                return

            if not await self._preflight_finalize_session(app, mode):
                self.store.set_meta("last_finalize_outcome", "session_preflight_failed")
                self.store.set_meta("last_finalize_outcome_at", self._now_iso())
                return

            await self._send_owner(
                app,
                f"Запускаю {('добор недостающего' if mode == 'missing' else 'сбор заказа')}."
                " Беру текущий срез и сразу добавляю в корзину.",
            )

            if mode == "missing":
                cycle = self._partial_cycle(day)
                if cycle is None:
                    self.store.set_meta("last_finalize_outcome", "no_partial_cycle")
                    self.store.set_meta("last_finalize_outcome_at", self._now_iso())
                    await self._send_owner(app, "Нет batch со статусом «частично добавлен».")
                    return
                payload = self._build_final_payload(day, cycle.batch_id, only_missing=True)
            else:
                cycle = self._current_open_cycle(day)
                if cycle is None:
                    waiting = self._waiting_payment_cycle(day)
                    self.store.set_meta("last_finalize_outcome", "no_open_cycle")
                    self.store.set_meta("last_finalize_outcome_at", self._now_iso())
                    if waiting is not None:
                        await self._send_owner(
                            app,
                            f"Сейчас нет open batch. {self._batch_label(waiting.batch_id)} уже ждет оплаты.",
                        )
                    else:
                        await self._send_owner(app, "Сейчас нет open batch для сборки.")
                    return
                payload = self._build_final_payload(day, cycle.batch_id, only_missing=False)

            Path(self.settings.out_dir).mkdir(parents=True, exist_ok=True)
            out_path = Path(self.settings.out_dir) / f"order_{day}_b{cycle.batch_id}.json"
            backup_path = (
                Path(self.settings.out_dir)
                / f"votes_backup_{day}_b{cycle.batch_id}_{datetime.now(self.settings.timezone).strftime('%H%M%S')}.json"
            )
            out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            backup_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

            self.store.set_meta("last_finalize_at", self._now_iso())
            self.store.set_meta("last_finalize_day", day)
            self.store.set_meta("last_finalize_backup", str(backup_path))
            self.store.set_meta("last_finalize_batch_id", str(cycle.batch_id))

            if not payload["items"]:
                if mode == "missing":
                    self.store.update_cycle_status(
                        day,
                        cycle.batch_id,
                        "added_waiting_payment",
                        finalized_at=self._now_iso(),
                        out_path=str(out_path),
                        backup_path=str(backup_path),
                    )
                    self.store.set_meta("last_finalize_outcome", "missing_already_done")
                    self.store.set_meta("last_finalize_outcome_at", self._now_iso())
                    await self._send_owner(
                        app,
                        f"У {self._batch_label(cycle.batch_id)} больше нет недостающих позиций. Он ждет оплаты.",
                    )
                else:
                    self.store.set_meta("last_finalize_outcome", "empty_open_batch")
                    self.store.set_meta("last_finalize_outcome_at", self._now_iso())
                    await self._send(app, f"{self._batch_label(cycle.batch_id)}: никто не выбрал товары.")
                return

            if mode != "missing":
                self.store.replace_cycle_item_results(day, cycle.batch_id, payload["items"])

            self.store.update_cycle_status(
                day,
                cycle.batch_id,
                "finalizing",
                finalized_at=self._now_iso(),
                out_path=str(out_path),
                backup_path=str(backup_path),
                total_sum=float(payload["total_sum_discount_price"]),
                selected_positions=len(payload["items"]),
                selected_users=len({int(row["user_id"]) for row in payload["totals_by_user"]}) if payload["totals_by_user"] else 0,
            )

            lines = [f"Итоговый заказ за {day} ({self._batch_label(cycle.batch_id)}):"]
            if payload["skipped_items"]:
                lines.append("Не включил в заказ позиции, которые уже пропали:")
                for row in payload["skipped_items"][:8]:
                    lines.append(f"- {row['name']} ({row['reason']})")
            if payload["reduced_items"]:
                lines.append("Подрезал количество по свежему остатку:")
                for row in payload["reduced_items"][:8]:
                    lines.append(f"- {row['name']}: было {row['requested_qty']} шт, осталось {row['final_qty']} шт")
            for row in payload["items"]:
                lines.append(
                    f"- {row['name']}: {int(row['qty'])} шт x {float(row['discount_price']):.2f} RUB"
                )
            lines.append(f"Сумма: {payload['total_sum_discount_price']:.2f} RUB")
            if payload["totals_by_user"]:
                lines.append("По людям:")
                for row in payload["totals_by_user"][:20]:
                    lines.append(
                        f"- {row['user_name']}: {int(row['positions'])} поз., {int(row['qty_total'])} шт, {self._format_money(float(row['total_sum']))}"
                    )
            if self.settings.dry_run:
                lines.append("Режим: DRY_RUN (автооформление отключено)")

            await self._send_owner(
                app,
                (
                    f"{self._batch_label(cycle.batch_id)} сохранен.\n"
                    f"Файл заказа: {out_path}\n"
                    f"Резерв голосов: {backup_path}"
                ),
            )

            exec_result = await self._run_executor_if_needed(
                app,
                out_path,
                day=day,
                batch_id=cycle.batch_id,
                notify_chat=False,
            )
            missing = self.store.get_missing_cycle_items(day, cycle.batch_id)
            if bool(exec_result.get("ok")) and not missing:
                self.store.update_cycle_status(
                    day,
                    cycle.batch_id,
                    "added_waiting_payment",
                    executor_status=str(exec_result.get("status") or "success"),
                )
                self.store.set_meta("last_finalize_outcome", "added_waiting_payment")
                self.store.set_meta("last_finalize_outcome_at", self._now_iso())
                cart_unique = int(exec_result.get("cart_unique_after") or 0)
                cart_total_qty = int(exec_result.get("cart_total_qty_after") or 0)
                final_lines = [
                    f"{self._batch_label(cycle.batch_id)} собран и добавлен в корзину.",
                    f"Сумма: {payload['total_sum_discount_price']:.2f} RUB",
                ]
                if cart_unique > 0:
                    final_lines.append(
                        f"В корзине сейчас: {cart_unique} позиций, суммарное кол-во: {cart_total_qty}."
                    )
                final_lines.append("Статус: ждет оплаты.")
                await self._send(app, "\n".join(final_lines))
            else:
                self.store.update_cycle_status(
                    day,
                    cycle.batch_id,
                    "partially_added",
                    executor_status=str(exec_result.get("status") or "partial"),
                )
                self.store.set_meta("last_finalize_outcome", "partially_added")
                self.store.set_meta("last_finalize_outcome_at", self._now_iso())
                failed = max(0, int(exec_result.get("total") or 0) - int(exec_result.get("ok_count") or 0))
                partial_lines = [
                    f"{self._batch_label(cycle.batch_id)} обработан частично.",
                    f"Сумма выбранного: {payload['total_sum_discount_price']:.2f} RUB",
                ]
                if failed > 0:
                    partial_lines.append(f"Не добавилось: {failed} поз.")
                partial_lines.append("Owner доберет недостающее отдельно.")
                await self._send(app, "\n".join(partial_lines))

    async def _run_executor_if_needed(
        self,
        app: Application,
        out_path: Path,
        day: str,
        batch_id: int,
        *,
        notify_chat: bool = True,
    ) -> dict:
        if self.settings.dry_run:
            fake_checks = [
                {
                    "name": row.name,
                    "requested_qty": int(row.requested_qty),
                    "before_qty": 0,
                    "after_qty": int(row.requested_qty),
                    "added_delta": int(row.requested_qty),
                    "ok": True,
                    "reason": "dry_run",
                }
                for row in self.store.list_cycle_item_results(day, batch_id)
            ]
            self.store.apply_executor_results(
                day=day,
                batch_id=batch_id,
                executor_status="dry_run_skip",
                ok_count=len(fake_checks),
                total=len(fake_checks),
                checks=fake_checks,
            )
            self.store.set_meta("last_executor_at", self._now_iso())
            self.store.set_meta("last_executor_status", "dry_run_skip")
            return {"ok": True, "status": "dry_run_skip"}
        if not self.settings.order_executor_command:
            await self._send_owner(app, "ORDER_EXECUTOR_COMMAND не задан. Автооформление пропущено.")
            self.store.set_meta("last_executor_at", self._now_iso())
            self.store.set_meta("last_executor_status", "missing_command")
            return {"ok": False, "status": "missing_command"}

        args = self._build_command_args(self.settings.order_executor_command, out_path)
        if not args:
            await self._send_owner(app, "ORDER_EXECUTOR_COMMAND пустой после разбора. Автооформление пропущено.")
            self.store.set_meta("last_executor_at", self._now_iso())
            self.store.set_meta("last_executor_status", "empty_command")
            return {"ok": False, "status": "empty_command"}

        log_path = Path(self.settings.out_dir) / "executor_last.log"

        try:
            # Session preflight: fail fast before cart automation.
            pre_ok, pre_detail = await self._run_executor_session_preflight(app, allow_refresh=False)
            if not pre_ok:
                await self._send_owner(
                    app,
                    "Сессия ВкусВилл недействительна. Пробую автообновление сессии через браузер...",
                )
                refresh_ok, refresh_detail = await self._run_executor_session_preflight(app, allow_refresh=True)
                if not refresh_ok:
                    await self._send_owner(
                        app,
                        f"Автооформление остановлено: не удалось подтвердить сессию ВкусВилл ({refresh_detail}).",
                    )
                    self.store.set_meta("last_executor_at", self._now_iso())
                    self.store.set_meta("last_executor_status", "session_invalid")
                    return {"ok": False, "status": "session_invalid", "log_path": str(log_path)}

            proc = await asyncio.to_thread(
                self._run_cmd_capture,
                args,
                self.settings.order_executor_timeout_sec,
            )
            output = (proc.stdout or "").strip()
            err = (proc.stderr or "").strip()
            raw = "\n".join(x for x in [output, err] if x).strip()
            payload = self._extract_payload(raw)

            try:
                log_path.parent.mkdir(parents=True, exist_ok=True)
                log_path.write_text(
                    (
                        f"cmd: {args}\n"
                        f"returncode: {proc.returncode}\n"
                        f"--- stdout ---\n{output}\n"
                        f"--- stderr ---\n{err}\n"
                    ),
                    encoding="utf-8",
                )
            except Exception:
                pass

            if isinstance(payload, dict):
                if payload.get("error"):
                    err_short = str(payload.get("error") or "").strip().splitlines()[0][:220]
                    await self._send_owner(
                        app,
                        f"Автодобавление не сработало: {self._repair_mojibake(err_short)}\nТехлог: {log_path}",
                    )
                    self.store.set_meta("last_executor_at", self._now_iso())
                    self.store.set_meta("last_executor_status", "failed_error")
                    return {"ok": False, "status": "failed_error", "log_path": str(log_path)}
                checks = payload.get("checks") or []
                ok_count = sum(1 for x in checks if bool((x or {}).get("ok")))
                total = int(payload.get("targets") or len(checks) or 0)
                self.store.apply_executor_results(
                    day=day,
                    batch_id=batch_id,
                    executor_status=("success" if bool(payload.get("ok")) else "partial"),
                    ok_count=ok_count,
                    total=total,
                    checks=checks if isinstance(checks, list) else [],
                )
                if total <= 0:
                    msg = str(payload.get("message") or "").strip()
                    if msg == "no_selected_items":
                        if notify_chat:
                            await self._send(app, "В заказе нет выбранных позиций.")
                    else:
                        await self._send_owner(app, f"Executor success_no_targets. Техлог: {log_path}")
                    self.store.set_meta("last_executor_at", self._now_iso())
                    self.store.set_meta("last_executor_status", "success_no_targets")
                    return {"ok": True, "status": "success_no_targets", "log_path": str(log_path)}
                failed = max(0, total - ok_count)
                already_in_cart = sum(
                    1 for x in checks if str((x or {}).get("reason") or "") == "already_in_cart"
                )
                newly_added = sum(
                    1
                    for x in checks
                    if bool((x or {}).get("ok"))
                    and str((x or {}).get("reason") or "") != "already_in_cart"
                )
                tomorrow_only = sum(
                    1
                    for x in checks
                    if str((x or {}).get("reason") or "") == "requires_tomorrow_delivery"
                )
                cart_unique = int(payload.get("cart_unique_after") or 0)
                cart_total_qty = int(payload.get("cart_total_qty_after") or 0)
                if bool(payload.get("ok")) and failed == 0:
                    msg = f"Корзина обновлена: {ok_count}/{total} позиций."
                    if newly_added > 0:
                        msg += f"\nДобавлено сейчас: {newly_added}."
                    if already_in_cart > 0:
                        msg += f"\nУже было в корзине: {already_in_cart}."
                    if cart_unique > 0:
                        msg += f"\nВ корзине сейчас: {cart_unique} позиций, суммарное кол-во: {cart_total_qty}."
                    if notify_chat:
                        await self._send(app, msg)
                    self.store.set_meta("last_executor_at", self._now_iso())
                    self.store.set_meta("last_executor_status", "success")
                    self.store.set_meta("last_executor_ok_count", str(ok_count))
                    self.store.set_meta("last_executor_total", str(total))
                    return {
                        "ok": True,
                        "status": "success",
                        "log_path": str(log_path),
                        "ok_count": ok_count,
                        "total": total,
                        "cart_unique_after": cart_unique,
                        "cart_total_qty_after": cart_total_qty,
                    }
                else:
                    lines = [f"Корзина обновлена частично: {ok_count}/{total}, не добавлено {failed}."]
                    if newly_added > 0:
                        lines.append(f"Добавлено сейчас: {newly_added}.")
                    else:
                        lines.append("Новых товаров в корзину сейчас не добавилось.")
                    if already_in_cart > 0:
                        lines.append(f"Уже было в корзине: {already_in_cart}.")
                    if tomorrow_only > 0:
                        lines.append(f"Только на завтра: {tomorrow_only}.")
                    if bool(payload.get("breaker_triggered")):
                        threshold = int(payload.get("breaker_threshold") or 0)
                        lines.append(
                            f"Защитная остановка: подряд ошибок {threshold}. Остальные позиции пропущены."
                        )
                    reason_map = {
                        "no_add_button": "нет кнопки добавления",
                        "no_add_button_on_product": "на странице товара нет кнопки добавления",
                        "offers_xmlid_missing": "у позиции нет xmlid для страницы акций",
                        "offers_card_not_found": "карточка товара не найдена в «Готовой еде»",
                        "offers_click_failed": "клик по кнопке на странице «Готовая еда» не сработал",
                        "offers_plus_click_failed": "клик по кнопке + на странице «Готовая еда» не сработал",
                        "no_card": "карточка товара не найдена",
                        "no_product_link": "не найдена ссылка на товар",
                        "bad_product_link": "некорректная ссылка на товар",
                        "product_page_fallback_failed": "fallback через страницу товара не сработал",
                        "requires_tomorrow_delivery": "товар доступен только в режиме «доставить завтра»",
                        "no_card_for_ajax": "для ajax fallback не нашли карточку",
                        "no_product_id_for_ajax": "для ajax fallback не нашли id товара",
                        "ajax_add_failed": "ajax-добавление вернуло ошибку",
                        "ajax_request_error": "ошибка ajax-запроса",
                        "ajax_rescue_exception": "сбой в ajax fallback",
                        "unavailable": "нет в наличии/недоступен",
                        "search_failed": "ошибка поиска",
                        "partial_added": "добавлено частично",
                        "partial_added_ajax": "добавлено частично через ajax fallback",
                        "rescued_via_ajax": "добавлено через ajax fallback",
                        "ajax_call_no_effect": "ajax fallback выполнен, но корзина не изменилась",
                        "click_no_effect_ajax_failed": "кнопка не сработала и ajax fallback тоже не добавил",
                        "click_no_effect": "кнопка нажалась, но товар не появился в корзине",
                        "circuit_breaker_open": "пропущено из-за защитной остановки",
                        "not_added_to_cart": "не удалось добавить",
                    }
                    bad = [x for x in checks if not bool((x or {}).get("ok"))][:6]
                    for row in bad:
                        reason = str(row.get("reason") or "not_added_to_cart")
                        reason_h = reason_map.get(reason, reason)
                        lines.append(
                            (
                                f"- {row.get('name', '?')}: было {row.get('before_qty', 0)}, "
                                f"стало {row.get('after_qty', 0)} ({reason_h})"
                            )
                        )
                    if cart_unique > 0:
                        lines.append(f"В корзине сейчас: {cart_unique} позиций, суммарное кол-во: {cart_total_qty}.")
                    lines.append(f"Техлог: {log_path}")
                    await self._send_owner(app, "\n".join(lines))
                    self.store.set_meta("last_executor_at", self._now_iso())
                    self.store.set_meta("last_executor_status", "partial")
                    self.store.set_meta("last_executor_ok_count", str(ok_count))
                    self.store.set_meta("last_executor_total", str(total))
                    return {
                        "ok": False,
                        "status": "partial",
                        "log_path": str(log_path),
                        "ok_count": ok_count,
                        "total": total,
                        "cart_unique_after": cart_unique,
                        "cart_total_qty_after": cart_total_qty,
                    }
            else:
                if proc.returncode == 0:
                    await self._send_owner(app, f"Executor success_no_payload. Техлог: {log_path}")
                    self.store.set_meta("last_executor_at", self._now_iso())
                    self.store.set_meta("last_executor_status", "success_no_payload")
                    return {"ok": True, "status": "success_no_payload", "log_path": str(log_path)}
                else:
                    await self._send_owner(app, f"Executor failed_no_payload. Техлог: {log_path}")
                    self.store.set_meta("last_executor_at", self._now_iso())
                    self.store.set_meta("last_executor_status", "failed_no_payload")
                    return {"ok": False, "status": "failed_no_payload", "log_path": str(log_path)}
        except subprocess.TimeoutExpired:
            await self._send_owner(
                app,
                (
                    "Автодобавление зависло и было остановлено по таймауту. "
                    f"Лимит: {self.settings.order_executor_timeout_sec} сек."
                ),
            )
            self.store.set_meta("last_executor_at", self._now_iso())
            self.store.set_meta("last_executor_status", "timeout")
            return {"ok": False, "status": "timeout", "log_path": str(log_path)}
        except Exception as exc:
            await self._send_owner(app, f"Executor FAILED: {self._repair_mojibake(str(exc))}")
            self.store.set_meta("last_executor_at", self._now_iso())
            self.store.set_meta("last_executor_status", "exception")
            return {"ok": False, "status": "exception", "error": str(exc), "log_path": str(log_path)}

    async def scheduled_finalize(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._finalize_impl(context.application)

    async def health(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.message is None:
            return
        if not await self._check_owner_or_reply(update):
            return

        day = self._today()
        items, snapshot_source = self._best_available_items(day)
        integrity = self._assess_day_integrity(day)
        runtime_root = self._runtime_root_payload()
        regular_count = self._regular_inshop_count(items)
        favorite_count = sum(1 for x in items if self._is_favorite_item(x.name, x.source))
        ready_food_count = sum(1 for x in items if self._is_ready_food_offer(x.source))
        open_cycle = self._current_open_cycle(day)
        totals = self.store.totals_by_item(day, batch_id=open_cycle.batch_id) if open_cycle else []
        active_votes = sum(1 for row in totals if int(row.get("qty") or 0) > 0)
        users = self.store.votes_by_user(day, batch_id=open_cycle.batch_id) if open_cycle else []
        waiting_cycle = self._waiting_payment_cycle(day)
        partial_cycle = self._partial_cycle(day)

        bound_chat = self._get_chat_id()
        owner_id = self._get_owner_user_id()
        has_rpa = bool(self.settings.rpa_command) if self.settings.provider == "rpa_command" else True
        has_executor = bool(self.settings.order_executor_command)
        last_collect_at = self.store.get_meta("last_collect_at") or "n/a"
        last_collect_day = self.store.get_meta("last_collect_day") or "n/a"
        last_collect_status = self.store.get_meta("last_collect_status") or "n/a"
        last_collect_total_items = int(self.store.get_meta("last_collect_total_items") or 0)
        last_sessioncheck_at = self.store.get_meta("last_sessioncheck_at") or "n/a"
        last_sessioncheck_status = self.store.get_meta("last_sessioncheck_status") or "n/a"
        last_mobile_sessioncheck_at = self.store.get_meta("last_mobile_sessioncheck_at") or "n/a"
        last_mobile_sessioncheck_status = self.store.get_meta("last_mobile_sessioncheck_status") or "n/a"
        last_mobile_sessioncheck_detail = self.store.get_meta("last_mobile_sessioncheck_detail") or "n/a"
        last_executor_at = self.store.get_meta("last_executor_at") or "n/a"
        last_executor_status = self.store.get_meta("last_executor_status") or "n/a"
        last_executor_ok = self.store.get_meta("last_executor_ok_count") or "n/a"
        last_executor_total = self.store.get_meta("last_executor_total") or "n/a"
        last_mirror_at = self.store.get_meta("last_mirror_at") or "n/a"
        last_mirror_status = self.store.get_meta("last_mirror_status") or "n/a"
        last_mirror_detail = self.store.get_meta("last_mirror_detail") or "n/a"
        last_image_backfill_at = self.store.get_meta("last_image_backfill_at") or "n/a"
        last_image_backfill_status = self.store.get_meta("last_image_backfill_status") or "n/a"
        last_image_backfill_detail = self.store.get_meta("last_image_backfill_detail") or "n/a"
        last_image_health_at = self.store.get_meta("last_image_health_at") or "n/a"
        last_image_health_status = self.store.get_meta("last_image_health_status") or "n/a"
        last_image_health_detail = self.store.get_meta("last_image_health_detail") or "n/a"
        last_publish_at = self.store.get_meta("last_publish_at") or "n/a"
        last_publish_status = self.store.get_meta("last_publish_status") or "n/a"
        last_publish_detail = self.store.get_meta("last_publish_detail") or "n/a"
        last_publish_public_at = self.store.get_meta("last_publish_public_check_at") or "n/a"
        last_publish_public_status = self.store.get_meta("last_publish_public_check_status") or "n/a"
        last_publish_public_detail = self.store.get_meta("last_publish_public_check_detail") or "n/a"
        last_finalize_outcome = self.store.get_meta("last_finalize_outcome") or "n/a"
        last_finalize_outcome_at = self.store.get_meta("last_finalize_outcome_at") or "n/a"
        startup_recovery_note = self.store.get_meta("startup_recovery_note") or "n/a"
        startup_recovery_at = self.store.get_meta("startup_recovery_at") or "n/a"
        autonomy_state = self.store.get_meta("autonomy_state") or "n/a"
        autonomy_note = self.store.get_meta("autonomy_note") or "n/a"
        autonomy_updated_at = self.store.get_meta("autonomy_updated_at") or "n/a"
        best_snapshot = self.store.get_best_day_snapshot(day)
        best_snapshot_text = (
            f"{best_snapshot.snapshot_id} ({best_snapshot.regular_count}/18, total={best_snapshot.total_items}, {best_snapshot.status})"
            if best_snapshot is not None
            else "n/a"
        )

        critical: list[str] = []
        warnings: list[str] = []
        historical: list[str] = []
        if bound_chat is None:
            critical.append("чат не привязан (/bind)")
        if owner_id is None:
            critical.append("не задан owner (/setowner)")
        if not has_rpa:
            critical.append("не задан RPA_COMMAND")
        if not has_executor and not self.settings.dry_run:
            critical.append("не задан ORDER_EXECUTOR_COMMAND")
        if regular_count < 6:
            critical.append("мало данных в подборках (меньше 6 товаров)")
        elif regular_count < 18:
            warnings.append(f"подборки еще не полные ({regular_count}/18)")
        if last_collect_day == day and last_collect_status == "ok" and 0 < last_collect_total_items < self.settings.collect_min_items:
            warnings.append(
                f"collect дал мало товаров ({last_collect_total_items}<{self.settings.collect_min_items})"
            )
        if last_collect_status == "error" and self._iso_is_today(last_collect_at):
            critical.append("последний collect завершился ошибкой")
        elif last_collect_status == "error":
            historical.append(f"старый collect был в ошибке ({last_collect_at})")
        if last_sessioncheck_status == "error" and self._iso_is_today(last_sessioncheck_at):
            warnings.append("сессия ВкусВилл требует внимания")
        elif last_sessioncheck_status == "error":
            historical.append(f"старая ошибка sessioncheck ({last_sessioncheck_at})")
        if last_mobile_sessioncheck_status == "error" and self._iso_is_today(last_mobile_sessioncheck_at):
            critical.append(f"mobile API сессия не подтверждена ({last_mobile_sessioncheck_detail})")
        elif last_mobile_sessioncheck_status == "error":
            historical.append(f"старая ошибка mobile sessioncheck ({last_mobile_sessioncheck_at})")
        if last_mobile_sessioncheck_status == "warning" and self._iso_is_today(last_mobile_sessioncheck_at):
            warnings.append(f"mobile API сессия не подтверждена ({last_mobile_sessioncheck_detail})")
        elif last_mobile_sessioncheck_status == "warning":
            historical.append(f"старое предупреждение mobile sessioncheck ({last_mobile_sessioncheck_at})")
        if last_executor_status in {"failed_error", "failed_no_payload", "exception", "session_invalid"} and self._iso_is_today(last_executor_at):
            warnings.append(f"последний executor в ошибке ({last_executor_status})")
        elif last_executor_status in {"failed_error", "failed_no_payload", "exception", "session_invalid"}:
            historical.append(f"старый executor был в ошибке ({last_executor_status}, {last_executor_at})")
        if open_cycle is not None and open_cycle.status == "finalizing":
            critical.append("batch завис в finalizing")
        if last_image_health_status == "warning" and self._iso_is_today(last_image_health_at):
            warnings.append(f"последняя проверка картинок требует внимания ({last_image_health_detail})")
        elif last_image_health_status == "warning":
            historical.append(f"старое предупреждение по картинкам ({last_image_health_at})")
        if last_publish_public_status == "error" and self._iso_is_today(last_publish_public_at):
            warnings.append("публичная Pages-публикация не подтверждена")
        elif last_publish_public_status == "error":
            historical.append(f"старый public publish error ({last_publish_public_at})")
        if runtime_root["state"] == "error":
            critical.append(f"бот запущен не из канонической папки ({runtime_root['detail']})")
        elif runtime_root["state"] == "warning":
            warnings.append(f"runtime root check: {runtime_root['detail']}")

        for msg in list(integrity.get("critical") or []):
            if msg not in critical:
                critical.append(str(msg))
        for msg in list(integrity.get("warnings") or []):
            if msg not in warnings:
                warnings.append(str(msg))

        if critical:
            state = "ТРЕБУЕТ ВНИМАНИЯ"
        elif warnings:
            state = "РАБОТАЕТ, НО ЕСТЬ ПРЕДУПРЕЖДЕНИЯ"
        else:
            state = "ВСЕ НОРМ"
        lines = [
            f"Состояние на {day}: {state}",
            f"- chat_id: {bound_chat}",
            f"- owner_id: {owner_id}",
            f"- provider: {self.settings.provider}",
            f"- dry_run: {self.settings.dry_run}",
            f"- items: total={len(items)}, regular={regular_count}/18, favorite={favorite_count}, ready_food={ready_food_count}",
            f"- source_for_app: {snapshot_source}",
            f"- runtime_root: state={runtime_root['state']}, current={runtime_root['current_root']}, canonical={runtime_root['registered_root']}",
            f"- open_cycle: {self._batch_label(open_cycle.batch_id) if open_cycle else 'n/a'}",
            f"- waiting_cycle: {self._batch_label(waiting_cycle.batch_id) if waiting_cycle else 'n/a'}",
            f"- partial_cycle: {self._batch_label(partial_cycle.batch_id) if partial_cycle else 'n/a'}",
            f"- votes: users={len(users)}, selected_positions={active_votes}",
            f"- last_collect: status={last_collect_status}, at={last_collect_at}",
            f"- last_sessioncheck: status={last_sessioncheck_status}, at={last_sessioncheck_at}",
            f"- last_mobile_sessioncheck: status={last_mobile_sessioncheck_status}, at={last_mobile_sessioncheck_at}, detail={last_mobile_sessioncheck_detail}",
            f"- last_image_backfill: status={last_image_backfill_status}, at={last_image_backfill_at}, detail={last_image_backfill_detail}",
            f"- last_image_health: status={last_image_health_status}, at={last_image_health_at}, detail={last_image_health_detail}",
            f"- last_mirror: status={last_mirror_status}, at={last_mirror_at}, detail={last_mirror_detail}",
            f"- last_publish: status={last_publish_status}, at={last_publish_at}, detail={last_publish_detail}",
            f"- last_publish_public: status={last_publish_public_status}, at={last_publish_public_at}, detail={last_publish_public_detail}",
            f"- last_executor: status={last_executor_status}, at={last_executor_at}, ok_count={last_executor_ok}/{last_executor_total}",
            f"- last_finalize_outcome: {self._finalize_outcome_human(last_finalize_outcome)} at={last_finalize_outcome_at}",
            f"- autonomy: state={autonomy_state}, at={autonomy_updated_at}, note={autonomy_note}",
            f"- startup_recovery: {startup_recovery_note} at={startup_recovery_at}",
            f"- best_snapshot: {best_snapshot_text}",
        ]
        if critical:
            lines.append("- критично:")
            lines.extend([f"  * {p}" for p in critical])
        if warnings:
            lines.append("- предупреждения:")
            lines.extend([f"  * {p}" for p in warnings])
        if historical:
            lines.append("- исторические хвосты:")
            lines.extend([f"  * {p}" for p in historical[:6]])
        await update.message.reply_text("\n".join(lines))

    async def help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.message is None:
            return
        await update.message.reply_text(
            "/app - открыть Mini App\n"
            "/status - текущие выборы\n"
            "/where - диагностика чата/owner\n"
            "/bind - привязать текущий чат (owner)\n"
            "/collect - обновить скидки из ВкусВилл (owner)\n"
            "/collectregular - обновить только 1/2/3 и любимый (owner)\n"
            "/collectready - обновить только готовую еду (owner)\n"
            "/morningaudit - жесткий прогон скидок/еды и daycheck (owner)\n"
            "/mirror [YYYY-MM-DD] - собрать локальный кэш картинок (owner)\n"
            "/publishapp - опубликовать Mini App на GitHub Pages (owner)\n"
            "/collectnow - собрать итоговый заказ сейчас (owner)\n"
            "/retrymissing - добрать только недостающие позиции (owner)\n"
            "/closecycle - закрыть batch после оплаты (owner)\n"
            "/debts - долги и быстрые отметки оплаты (owner)\n"
            "/cancelcycle - отменить случайный open batch (owner)\n"
            "/cyclestatus - статусы batch-циклов за сегодня (owner)\n"
            "/whochose - кто что выбрал в текущем batch (owner)\n"
            "/daycheck - проверить целостность сегодняшнего набора (owner)\n"
            "/finalize - собрать итоговый заказ (owner)\n"
            "/resetday - очистить данные текущего дня (owner)\n"
            "/clearuser - снять весь выбор одного человека (owner, reply)\n"
            "/clearvotes - очистить только выборы за сегодня (owner)\n"
            "/cart - сверить корзину с сегодняшними скидками (owner)\n"
            "/sessioncheck - проверка логина Chrome-профиля (owner)\n"
            "/mobilecheck - проверка mobile API session без Playwright (owner)\n"
            "/health - быстрый статус бота (owner)\n"
            "/setowner - назначить/проверить owner\n"
            "/selftest - быстрая проверка состояния (owner)\n"
            "/hidekbd - скрыть клавиатуру\n"
            "/help - справка"
        )

    def build_app(self) -> Application:
        proxy_url = self.settings.telegram_proxy_url
        common_request_kwargs = {
            "proxy": proxy_url,
            "connect_timeout": 20.0,
            "write_timeout": 20.0,
            "pool_timeout": 10.0,
        }
        request = HTTPXRequest(
            connection_pool_size=8,
            read_timeout=20.0,
            **common_request_kwargs,
        )
        get_updates_request = HTTPXRequest(
            connection_pool_size=2,
            read_timeout=60.0,
            **common_request_kwargs,
        )
        builder = (
            Application.builder()
            .token(self.settings.bot_token)
            .defaults(Defaults(tzinfo=self.settings.timezone))
            .request(request)
            .get_updates_request(get_updates_request)
        )
        if proxy_url:
            builder = builder.proxy_url(proxy_url).get_updates_proxy_url(proxy_url)
        app = builder.build()
        try:
            Path(self.settings.out_dir).mkdir(parents=True, exist_ok=True)
            removed = self._cleanup_out_dir()
            if removed > 0:
                LOGGER.info(
                    "Startup out-dir cleanup removed %s file(s) older than %s days",
                    removed,
                    self.settings.out_retention_days,
                )
            removed_profiles = self._cleanup_temp_profiles()
            if removed_profiles > 0:
                LOGGER.info("Startup temp profile cleanup removed %s stale dir(s)", removed_profiles)
        except Exception:
            pass
        try:
            recovery_note = self._recover_cycles_on_startup()
            if recovery_note:
                LOGGER.info(recovery_note)
        except Exception as exc:
            LOGGER.warning("Startup cycle recovery failed: %s", exc)

        app.add_handler(CommandHandler("start", self.start))
        app.add_handler(CommandHandler("bind", self.bind))
        app.add_handler(CommandHandler("collect", self.collect))
        app.add_handler(CommandHandler("collectregular", self.collectregular))
        app.add_handler(CommandHandler("collectready", self.collectready))
        app.add_handler(CommandHandler("morningaudit", self.morningaudit))
        app.add_handler(CommandHandler("mirror", self.mirror))
        app.add_handler(CommandHandler("publishapp", self.publishapp))
        app.add_handler(CommandHandler("setowner", self.setowner))
        app.add_handler(CommandHandler("where", self.where))
        app.add_handler(CommandHandler("selftest", self.selftest))
        app.add_handler(CommandHandler("app", self.app))
        app.add_handler(CommandHandler("hidekbd", self.hidekbd))
        app.add_handler(CommandHandler("status", self.status))
        app.add_handler(CommandHandler("cart", self.cart))
        app.add_handler(CommandHandler("health", self.health))
        app.add_handler(CommandHandler("finalize", self.finalize))
        app.add_handler(CommandHandler("collectnow", self.collectnow))
        app.add_handler(CommandHandler("retrymissing", self.retrymissing))
        app.add_handler(CommandHandler("closecycle", self.closecycle))
        app.add_handler(CommandHandler("debts", self.debts))
        app.add_handler(CommandHandler("cancelcycle", self.cancelcycle))
        app.add_handler(CommandHandler("cyclestatus", self.cyclestatus))
        app.add_handler(CommandHandler("whochose", self.whochose))
        app.add_handler(CommandHandler("daycheck", self.daycheck))
        app.add_handler(CommandHandler("resetday", self.resetday))
        app.add_handler(CommandHandler("clearuser", self.clearuser))
        app.add_handler(CommandHandler("clearvotes", self.clearvotes))
        app.add_handler(CommandHandler("sessioncheck", self.sessioncheck))
        app.add_handler(CommandHandler("mobilecheck", self.mobilecheck))
        app.add_handler(CommandHandler("help", self.help))
        app.add_handler(CallbackQueryHandler(self.on_control, pattern=r"^ctl\|"))
        app.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, self.on_webapp_data))

        for t in self.settings.collection_times:
            app.job_queue.run_daily(
                self.scheduled_collect,
                time=t,
                name=f"collect-{t.hour:02d}:{t.minute:02d}",
            )
        for t in self.settings.morning_audit_times:
            app.job_queue.run_daily(
                self.scheduled_morning_audit,
                time=t,
                name=f"morning-audit-{t.hour:02d}:{t.minute:02d}",
            )
        app.job_queue.run_daily(
            self.scheduled_db_backup,
            time=datetime.strptime("02:00", "%H:%M").time(),
            name="db-backup-02:00",
        )
        if self.settings.provider == "rpa_command":
            app.job_queue.run_daily(
                self.scheduled_sessioncheck,
                time=datetime.strptime("23:50", "%H:%M").time(),
                name="sessioncheck-23:50",
            )
        app.job_queue.run_daily(
            self.scheduled_finalize,
            time=self.settings.order_deadline,
            name="finalize",
        )
        app.job_queue.run_daily(
            self.scheduled_cleanup,
            time=datetime.strptime("03:10", "%H:%M").time(),
            name="cleanup-out-dir",
        )
        app.job_queue.run_repeating(
            self.scheduled_autonomy_watchdog,
            interval=30 * 60,
            first=180,
            name="autonomy-watchdog",
        )
        app.job_queue.run_once(
            self.scheduled_startup_sanity,
            when=20,
            name="startup-sanity",
        )
        self._schedule_startup_collect_if_needed(app)
        return app



