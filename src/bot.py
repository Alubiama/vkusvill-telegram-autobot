from __future__ import annotations

import base64
import hashlib
import json
import logging
import shlex
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, WebAppInfo
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .config import Settings
from .providers import BaseProvider
from .store import StateStore

LOGGER = logging.getLogger(__name__)


class VkusvillGroupBot:
    def __init__(self, settings: Settings, store: StateStore, provider: BaseProvider) -> None:
        self.settings = settings
        self.store = store
        self.provider = provider

    def _today(self) -> str:
        return datetime.now(self.settings.timezone).strftime("%Y-%m-%d")

    @staticmethod
    def _snapshot_id(items: list[object], day: str) -> str:
        raw = "|".join(sorted(str(x.item_id) for x in items))
        return hashlib.sha1(f"{day}|{raw}".encode("utf-8")).hexdigest()[:12]

    def _get_chat_id(self) -> int | None:
        if self.settings.chat_id is not None:
            return self.settings.chat_id
        raw = self.store.get_meta("chat_id")
        return int(raw) if raw else None

    async def _send(self, app: Application, text: str, **kwargs) -> None:
        chat_id = self._get_chat_id()
        if chat_id is None:
            LOGGER.warning("Chat is not bound yet, message skipped: %s", text[:80])
            return
        await app.bot.send_message(chat_id=chat_id, text=text, **kwargs)

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
        return groups, favorites, ready_food[:9]

    def _build_mini_app_url(self, user_id: int | None) -> str | None:
        if not self.settings.mini_app_url:
            return None

        day = self._today()
        items = self.store.list_items(day)
        if not items:
            return self.settings.mini_app_url

        totals = {row["item_id"]: int(row["qty"]) for row in self.store.totals_by_item(day)}
        your: dict[str, int] = {}
        if user_id is not None:
            for item in items:
                your[item.item_id] = self.store.get_user_qty(day, user_id, item.item_id)

        groups, favorites, ready_food = self._mini_groups(items)
        snapshot_id = self._snapshot_id(items, day)
        regular_count = sum(len(g["items"]) for g in groups)

        def pack_item(item: object) -> dict:
            return {
                "i": item.item_id,
                "n": item.name,
                "p": float(item.price),
                "d": float(item.discount_price),
                "s": item.source,
            }

        payload = {
            "day": day,
            "snapshot_id": snapshot_id,
            "groups": [
                {
                    "id": g["id"],
                    "title": g["title"],
                    "items": [pack_item(item) for item in g["items"]],
                }
                for g in groups
            ],
            "favorite": [pack_item(item) for item in favorites[:1]],
            "extra_ready_food": [pack_item(item) for item in ready_food],
            "items": [pack_item(item) for item in items],
            "totals": totals,
            "your": your,
            "regular_count": regular_count,
            "regular_capacity": 18,
        }

        packed = base64.urlsafe_b64encode(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ).decode("ascii").rstrip("=")

        # Keep URL reasonably short for Telegram clients.
        if len(packed) > 7000:
            packed = base64.urlsafe_b64encode(
                json.dumps(
                    {
                        "day": day,
                        "snapshot_id": payload["snapshot_id"],
                        "groups": payload["groups"],
                        "favorite": payload["favorite"],
                        "extra_ready_food": payload["extra_ready_food"],
                        "items": payload["items"],
                        "regular_count": payload["regular_count"],
                        "regular_capacity": payload["regular_capacity"],
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).decode("ascii").rstrip("=")

        parts = urlsplit(self.settings.mini_app_url)
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        query["data"] = packed
        query["v"] = datetime.now(self.settings.timezone).strftime("%Y%m%d%H%M%S")
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))

    def _open_showcase_markup(self, user_id: int | None = None) -> InlineKeyboardMarkup:
        rows: list[list[InlineKeyboardButton]] = [
            [InlineKeyboardButton("Open Showcase", callback_data="b|o|0")]
        ]
        if self.settings.mini_app_url:
            web_url = self._build_mini_app_url(user_id) or self.settings.mini_app_url
            rows.append(
                [
                    InlineKeyboardButton(
                        "Open App Window",
                        web_app=WebAppInfo(url=web_url),
                    )
                ]
            )
        return InlineKeyboardMarkup(rows)

    def _browser_keyboard(self, item_id: str, idx: int, total: int) -> InlineKeyboardMarkup:
        prev_idx = (idx - 1) % total
        next_idx = (idx + 1) % total
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("Prev", callback_data=f"b|n|{prev_idx}"),
                    InlineKeyboardButton(f"{idx + 1}/{total}", callback_data=f"b|s|{idx}"),
                    InlineKeyboardButton("Next", callback_data=f"b|n|{next_idx}"),
                ],
                [
                    InlineKeyboardButton("-1", callback_data=f"b|q|{item_id}|-1|{idx}"),
                    InlineKeyboardButton("+1", callback_data=f"b|q|{item_id}|1|{idx}"),
                    InlineKeyboardButton("+2", callback_data=f"b|q|{item_id}|2|{idx}"),
                ],
                [
                    InlineKeyboardButton("Reset", callback_data=f"b|q|{item_id}|0|{idx}"),
                    InlineKeyboardButton("Totals", callback_data=f"b|t|{idx}"),
                ],
            ]
        )

    def _build_browser_text(
        self,
        day: str,
        item: object,
        idx: int,
        total: int,
        user_id: int,
        total_qty: int,
    ) -> str:
        your_qty = self.store.get_user_qty(day, user_id, item.item_id)
        savings = float(item.price) - float(item.discount_price)
        return (
            f"Showcase {idx + 1}/{total}\n\n"
            f"{item.name}\n"
            f"Regular: {float(item.price):.2f} RUB\n"
            f"Discount: {float(item.discount_price):.2f} RUB\n"
            f"Savings: {savings:.2f} RUB per item\n\n"
            f"Your qty: {your_qty}\n"
            f"Group qty: {total_qty}"
        )

    async def bind(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_chat is None or update.message is None:
            return
        chat_id = update.effective_chat.id
        self.store.set_meta("chat_id", str(chat_id))
        await update.message.reply_text(f"Chat is bound: {chat_id}")

    async def collect(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._collect_impl(context.application)
        if update.message:
            await update.message.reply_text("Discounts refreshed.")

    async def _collect_impl(self, app: Application) -> None:
        now = datetime.now(self.settings.timezone)
        day = now.strftime("%Y-%m-%d")
        try:
            items = self.provider.fetch(now)
        except Exception as exc:
            await self._send(
                app,
                f"Collect failed: {exc}. Check VkusVill session and collector settings.",
            )
            LOGGER.exception("Collect failed")
            return

        fresh, removed = self.store.sync_items(day, [x.to_row() for x in items])
        all_items = self.store.list_items(day)
        await self._send(
            app,
            (
                f"Collection {now.strftime('%H:%M')} complete. "
                f"Items in base: {len(all_items)}, new: {len(fresh)}, removed: {removed}."
            ),
            reply_markup=self._open_showcase_markup(),
        )

    async def scheduled_collect(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._collect_impl(context.application)

    async def shop(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_user is None:
            return
        day = self._today()
        items = self.store.list_items(day)
        if not items:
            if update.message:
                await update.message.reply_text(
                    "Today items are updated automatically at 10:00 (Europe/Moscow). Please check again later."
                )
            return

        item = items[0]
        totals = {row["item_id"]: int(row["qty"]) for row in self.store.totals_by_item(day)}
        text = self._build_browser_text(
            day=day,
            item=item,
            idx=0,
            total=len(items),
            user_id=update.effective_user.id,
            total_qty=totals.get(item.item_id, 0),
        )
        if update.message:
            await update.message.reply_text(
                text=text,
                reply_markup=self._browser_keyboard(item.item_id, 0, len(items)),
            )

    async def app(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.message is None or update.effective_user is None:
            return
        if not self.settings.mini_app_url:
            await update.message.reply_text(
                "Mini App URL is not configured yet. Set MINI_APP_URL in .env first."
            )
            return
        web_url = self._build_mini_app_url(update.effective_user.id) or self.settings.mini_app_url
        await update.message.reply_text(
            "Open app window:",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("Open App Window", web_app=WebAppInfo(url=web_url))]]
            ),
        )

    async def on_webapp_data(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.message is None or update.effective_user is None:
            return
        wad = update.message.web_app_data
        if wad is None or not wad.data:
            return

        try:
            payload = json.loads(wad.data)
        except json.JSONDecodeError:
            await update.message.reply_text("Mini App payload parse error.")
            return

        day = self._today()
        user_id = update.effective_user.id
        user_name = update.effective_user.full_name
        items = self.store.list_items(day)
        items_by_id = {x.item_id: x for x in items}
        snapshot_id = self._snapshot_id(items, day)

        ptype = payload.get("type")
        if ptype == "single_choice":
            item_id = str(payload.get("item_id") or "")
            qty = int(payload.get("qty") or 0)
            if item_id not in items_by_id:
                await update.message.reply_text("Item not found for today.")
                return
            self.store.set_vote(day, user_id, user_name, item_id, max(0, qty))
            await update.message.reply_text(f"Saved: {items_by_id[item_id].name} -> {max(0, qty)}")
            return

        if ptype == "all_choices":
            payload_day = str(payload.get("day") or "")
            payload_snapshot = str(payload.get("snapshot_id") or "")
            if payload_day and payload_day != day:
                await update.message.reply_text(
                    f"Data is stale ({payload_day} vs {day}). Reopen Mini App via /app."
                )
                return
            if payload_snapshot and payload_snapshot != snapshot_id:
                await update.message.reply_text(
                    "Data snapshot is outdated. Reopen Mini App via /app and submit again."
                )
                return

            qty_map = payload.get("qty") or {}
            selected_positive = 0
            touched = 0
            for item_id, raw_qty in qty_map.items():
                if item_id not in items_by_id:
                    continue
                qty = max(0, int(raw_qty))
                self.store.set_vote(day, user_id, user_name, item_id, qty)
                touched += 1
                if qty > 0:
                    selected_positive += 1

            if touched == 0:
                await update.message.reply_text(
                    "Nothing saved: no matching items for today's snapshot. Reopen Mini App via /app."
                )
                return

            await update.message.reply_text(
                f"Saved: {selected_positive} selected items (updated {touched} entries)."
            )
            return

        await update.message.reply_text("Unknown Mini App payload type.")

    async def on_browser(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if query is None or query.from_user is None or query.data is None:
            return
        answered = False

        parts = query.data.split("|")
        if len(parts) < 2:
            return

        day = self._today()
        items = self.store.list_items(day)
        if not items:
            await query.edit_message_text(
                "No discounts yet. Automatic update is scheduled daily at 10:00 (Europe/Moscow)."
            )
            return

        action = parts[1]
        idx = 0
        if action in {"o", "n", "s", "t"} and len(parts) >= 3:
            idx = max(0, min(int(parts[2]), len(items) - 1))

        if action == "q" and len(parts) >= 5:
            item_id = parts[2]
            delta = int(parts[3])
            idx = max(0, min(int(parts[4]), len(items) - 1))
            current = self.store.get_user_qty(day, query.from_user.id, item_id)
            if delta == 0:
                new_qty = 0
            else:
                new_qty = max(0, current + delta)
            self.store.set_vote(
                day=day,
                user_id=query.from_user.id,
                user_name=query.from_user.full_name,
                item_id=item_id,
                qty=new_qty,
            )
            await query.answer(text=f"Your qty: {new_qty}")
            answered = True

        if action == "t":
            totals = self.store.totals_by_item(day)
            picked = [r for r in totals if int(r["qty"]) > 0]
            if not picked:
                await query.answer("No selected items yet.", show_alert=True)
            else:
                total_sum = sum(float(r["discount_price"]) * int(r["qty"]) for r in picked)
                await query.answer(f"Selected: {len(picked)} items, total: {total_sum:.2f} RUB", show_alert=True)
            answered = True

        if not answered:
            await query.answer()

        item = items[idx]
        totals_map = {row["item_id"]: int(row["qty"]) for row in self.store.totals_by_item(day)}
        text = self._build_browser_text(
            day=day,
            item=item,
            idx=idx,
            total=len(items),
            user_id=query.from_user.id,
            total_qty=totals_map.get(item.item_id, 0),
        )
        await query.edit_message_text(
            text=text,
            reply_markup=self._browser_keyboard(item.item_id, idx, len(items)),
        )

    async def on_vote_legacy(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if query is None or query.from_user is None or query.data is None:
            return
        day = self._today()
        try:
            _, item_id, delta_raw = query.data.split("|")
            delta = int(delta_raw)
        except Exception:
            return

        current = self.store.get_user_qty(day, query.from_user.id, item_id)
        if delta == 0:
            new_qty = 0
        else:
            new_qty = max(0, current + delta)
        self.store.set_vote(
            day,
            query.from_user.id,
            query.from_user.full_name,
            item_id,
            new_qty,
        )
        await query.answer(text=f"Your qty: {new_qty}")

    async def status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.message is None:
            return
        day = self._today()
        totals = self.store.totals_by_item(day)
        if not totals:
            await update.message.reply_text(
                "No items yet for today. Auto update runs daily at 10:00 (Europe/Moscow)."
            )
            return

        lines = [f"Status for {day}:"]
        for row in totals:
            lines.append(
                f"- {row['name']}: {int(row['qty'])} pcs ({float(row['discount_price']):.2f} RUB)"
            )
        await update.message.reply_text("\n".join(lines))

    async def cart(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.message is None:
            return
        await update.message.reply_text("Scanning cart and matching with today discounts...")

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
            proc = subprocess.run(cmd, capture_output=True, text=True, check=True)
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
        await self._finalize_impl(context.application)
        if update.message:
            await update.message.reply_text("Final order prepared.")

    async def resetday(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.message is None:
            return
        day = self._today()
        self.store.clear_day(day)
        await update.message.reply_text(
            f"Day state cleared for {day}. Automatic update will refill items at 10:00 (Europe/Moscow)."
        )

    def _schedule_startup_collect_if_needed(self, app: Application) -> None:
        day = self._today()
        if self.store.list_items(day):
            return
        if not self.settings.collection_times:
            return
        first_time = min(self.settings.collection_times)
        now = datetime.now(self.settings.timezone)
        if (now.hour, now.minute) < (first_time.hour, first_time.minute):
            return
        app.job_queue.run_once(self.scheduled_collect, when=5, name="collect-startup-catchup")

    def _build_final_payload(self, day: str) -> dict:
        totals = self.store.totals_by_item(day)
        users = self.store.votes_by_user(day)
        selected = [row for row in totals if int(row["qty"]) > 0]
        total_sum = sum(float(row["discount_price"]) * int(row["qty"]) for row in selected)
        return {
            "day": day,
            "items": selected,
            "votes_by_user": users,
            "total_sum_discount_price": round(total_sum, 2),
            "dry_run": self.settings.dry_run,
        }

    async def _finalize_impl(self, app: Application) -> None:
        day = self._today()
        payload = self._build_final_payload(day)
        Path(self.settings.out_dir).mkdir(parents=True, exist_ok=True)
        out_path = Path(self.settings.out_dir) / f"order_{day}.json"
        out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

        if not payload["items"]:
            await self._send(app, f"Deadline {day}. No one selected items.")
            return

        lines = [f"Final order for {day}:"]
        for row in payload["items"]:
            lines.append(
                f"- {row['name']}: {int(row['qty'])} pcs x {float(row['discount_price']):.2f} RUB"
            )
        lines.append(f"Total: {payload['total_sum_discount_price']:.2f} RUB")
        lines.append(f"File: {out_path}")
        if self.settings.dry_run:
            lines.append("Mode: DRY_RUN (no auto checkout)")

        await self._send(app, "\n".join(lines))
        await self._run_executor_if_needed(app, out_path)

    async def _run_executor_if_needed(self, app: Application, out_path: Path) -> None:
        if self.settings.dry_run:
            return
        if not self.settings.order_executor_command:
            await self._send(app, "ORDER_EXECUTOR_COMMAND is not set. Auto checkout skipped.")
            return

        cmd = self.settings.order_executor_command.replace("{order_file}", str(out_path))
        try:
            proc = subprocess.run(
                shlex.split(cmd),
                check=True,
                capture_output=True,
                text=True,
            )
            output = (proc.stdout or "").strip()
            if output:
                await self._send(app, f"Executor OK:\n{output[:3000]}")
            else:
                await self._send(app, "Executor OK.")
        except subprocess.CalledProcessError as exc:
            err = (exc.stderr or exc.stdout or str(exc)).strip()
            await self._send(app, f"Executor FAILED:\n{err[:3000]}")

    async def scheduled_finalize(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._finalize_impl(context.application)

    async def help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.message is None:
            return
        await update.message.reply_text(
            "/bind - bind current chat\n"
            "/shop - open scrollable showcase\n"
            "/status - show current selections\n"
            "/cart - scan cart and sort matched discounts\n"
            "/finalize - prepare final order\n"
            "/resetday - clear today items and votes\n"
            "/app - open Mini App button (if URL configured)\n"
            "/help - help"
        )

    def build_app(self) -> Application:
        app = Application.builder().token(self.settings.bot_token).build()

        app.add_handler(CommandHandler("bind", self.bind))
        app.add_handler(CommandHandler("shop", self.shop))
        app.add_handler(CommandHandler("browse", self.shop))
        app.add_handler(CommandHandler("app", self.app))
        app.add_handler(CommandHandler("status", self.status))
        app.add_handler(CommandHandler("cart", self.cart))
        app.add_handler(CommandHandler("finalize", self.finalize))
        app.add_handler(CommandHandler("resetday", self.resetday))
        app.add_handler(CommandHandler("help", self.help))
        app.add_handler(CallbackQueryHandler(self.on_browser, pattern=r"^b\|"))
        app.add_handler(CallbackQueryHandler(self.on_vote_legacy, pattern=r"^v\|"))
        app.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, self.on_webapp_data))

        for t in self.settings.collection_times:
            app.job_queue.run_daily(
                self.scheduled_collect,
                time=t,
                name=f"collect-{t.hour:02d}:{t.minute:02d}",
            )
        app.job_queue.run_daily(
            self.scheduled_finalize,
            time=self.settings.order_deadline,
            name="finalize",
        )
        self._schedule_startup_collect_if_needed(app)
        return app
