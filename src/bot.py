from __future__ import annotations

import base64
import hashlib
import json
import logging
import shlex
import subprocess
import sys
import zlib
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove, Update, WebAppInfo
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
    COLLECT_NOW_BUTTON = "Собрать заказ сейчас"

    def __init__(self, settings: Settings, store: StateStore, provider: BaseProvider) -> None:
        self.settings = settings
        self.store = store
        self.provider = provider

    def _today(self) -> str:
        return datetime.now(self.settings.timezone).strftime("%Y-%m-%d")

    def _collection_schedule_text(self) -> str:
        return ", ".join(t.strftime("%H:%M") for t in self.settings.collection_times)

    @staticmethod
    def _snapshot_id(items: list[object], day: str) -> str:
        raw = "|".join(sorted(str(x.item_id) for x in items))
        return hashlib.sha1(f"{day}|{raw}".encode("utf-8")).hexdigest()[:12]

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

    async def _send(self, app: Application, text: str, **kwargs) -> None:
        chat_id = self._get_chat_id()
        if chat_id is None:
            LOGGER.warning("Chat is not bound yet, message skipped: %s", text[:80])
            return
        await app.bot.send_message(chat_id=chat_id, text=text, **kwargs)

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
        return groups, favorites, ready_food

    def _build_mini_app_url(self, user_id: int | None) -> str | None:
        if not self.settings.mini_app_url:
            return None

        day = self._today()
        items = self.store.list_items(day)
        if not items:
            return self.settings.mini_app_url

        groups, favorites, ready_food = self._mini_groups(items)
        snapshot_id = self._snapshot_id(items, day)
        regular_count = sum(len(g["items"]) for g in groups)

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

        compact_payload = {
            "d": day,
            "sid": snapshot_id,
            "m": [
                [str(item.item_id), str(item.name), float(item.discount_price)]
                for item in unique_items
            ],
            "g": group_indexes,
            "f": favorite_indexes,
            "r": ready_food_indexes,
            "rc": regular_count,
            "cap": 18,
        }

        raw_payload = json.dumps(
            compact_payload,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        packed = base64.urlsafe_b64encode(zlib.compress(raw_payload, level=9)).decode("ascii").rstrip("=")

        parts = urlsplit(self.settings.mini_app_url)
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        query["data"] = packed
        query["enc"] = "z"
        query["v"] = datetime.now(self.settings.timezone).strftime("%Y%m%d%H%M%S")
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))

    def _regular_inshop_count(self, items: list[object]) -> int:
        count = 0
        for item in items:
            if self._is_favorite_item(item.name, item.source):
                continue
            if self._is_ready_food_offer(item.source):
                continue
            count += 1
        return count

    def _open_showcase_markup(self, user_id: int | None = None) -> InlineKeyboardMarkup:
        # Group-safe markup only. Telegram rejects web_app buttons in groups.
        rows: list[list[InlineKeyboardButton]] = [
            [InlineKeyboardButton("Open Showcase", callback_data="b|o|0")]
        ]
        return InlineKeyboardMarkup(rows)

    @staticmethod
    def _private_app_deeplink(bot_username: str | None) -> str | None:
        if not bot_username:
            return None
        username = bot_username.strip().lstrip("@")
        if not username:
            return None
        return f"https://t.me/{username}?start=open_app"

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
        await self._collect_impl(context.application)
        if update.message:
            await update.message.reply_text("Скидки обновлены.")

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
        items = self.store.list_items(day)
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
        ]
        await update.message.reply_text("\n".join(lines))

    async def _collect_impl(self, app: Application, skip_if_full: bool = False) -> None:
        now = datetime.now(self.settings.timezone)
        day = now.strftime("%Y-%m-%d")
        if skip_if_full:
            existing = self.store.list_items(day)
            regular_count = self._regular_inshop_count(existing)
            if regular_count >= 18:
                LOGGER.info("Skip scheduled collect: already have %s regular inshop items for %s", regular_count, day)
                return
        try:
            items = self.provider.fetch(now)
        except Exception as exc:
            await self._send(
                app,
                f"Сбор скидок не удался: {exc}. Проверь сессию ВкусВилл и настройки сборщика.",
            )
            LOGGER.exception("Collect failed")
            return

        fresh, removed = self.store.sync_items(day, [x.to_row() for x in items])
        all_items = self.store.list_items(day)
        regular_count = self._regular_inshop_count(all_items)
        favorite_count = sum(1 for x in all_items if self._is_favorite_item(x.name, x.source))
        ready_food_count = sum(1 for x in all_items if self._is_ready_food_offer(x.source))
        await self._send(
            app,
            (
                f"Сбор {now.strftime('%H:%M')} завершен. "
                f"В базе: {len(all_items)} (новых {len(fresh)}, удалено {removed}). "
                f"Подборки 20%: {regular_count}/18, любимый: {favorite_count}, готовая еда: {ready_food_count}."
            ),
            reply_markup=self._open_showcase_markup(),
        )

    async def scheduled_collect(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._collect_impl(context.application, skip_if_full=True)

    async def shop(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_user is None:
            return
        day = self._today()
        items = self.store.list_items(day)
        if not items:
            if update.message:
                await update.message.reply_text(
                    f"Товары обновляются автоматически по расписанию: {self._collection_schedule_text()} (Europe/Moscow)."
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
            await update.message.reply_text("MINI_APP_URL is not set in .env.")
            return

        chat_type = update.effective_chat.type if update.effective_chat else ""
        if chat_type != "private":
            deep_link = self._private_app_deeplink(getattr(context.bot, "username", None))
            if deep_link:
                await update.message.reply_text(
                    "Mini App cannot be opened directly in group. Open it in DM:",
                    reply_markup=InlineKeyboardMarkup(
                        [[InlineKeyboardButton("Open Mini App", url=deep_link)]]
                    ),
                )
            else:
                await update.message.reply_text("Open bot in private chat and run /app.")
            return

        web_url = self._build_mini_app_url(update.effective_user.id) or self.settings.mini_app_url
        rows: list[list[InlineKeyboardButton]] = [
            [InlineKeyboardButton("Open Mini App", web_app=WebAppInfo(url=web_url))]
        ]
        if self._user_is_owner(update.effective_user.id):
            rows.append([InlineKeyboardButton("Finalize Now", callback_data="ctl|collectnow")])
        await update.message.reply_text(
            (
                "Open Mini App using the button below.\n"
                "Send your choices inside Mini App."
            ),
            reply_markup=InlineKeyboardMarkup(rows),
        )

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.message is None:
            return
        args = context.args or []
        if args and args[0].strip().lower() in {"open_app", "app", "miniapp"}:
            await self.app(update, context)
            return
        await update.message.reply_text("Open Mini App with /app")

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
        if action != "collectnow":
            await query.answer()
            return

        if not self._user_is_owner(query.from_user.id):
            await query.answer("Only owner can finalize.", show_alert=True)
            return

        await query.answer("Finalizing...")
        await self._finalize_impl(context.application)
        if query.message is not None:
            await query.message.reply_text("Итог собран сейчас.")

    async def on_text_button(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.message is None:
            return
        text = (update.message.text or "").strip()
        if text != self.COLLECT_NOW_BUTTON:
            return
        if not await self._check_owner_or_reply(update):
            return
        await self._finalize_impl(context.application)
        await update.message.reply_text("Итог собран сейчас.")

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
            await update.message.reply_text("Mini App payload parse error.")
            return

        day = self._today()
        user_id = update.effective_user.id
        user_name = update.effective_user.full_name
        items = self.store.list_items(day)
        items_by_id = {x.item_id: x for x in items}
        snapshot_id = self._snapshot_id(items, day)

        ptype = payload.get("type")
        self._trace_webapp(
            f"parsed type={ptype} payload_day={payload.get('day')} payload_snapshot={payload.get('snapshot_id')} "
            f"items_today={len(items)}"
        )
        if ptype == "single_choice":
            item_id = str(payload.get("item_id") or "")
            qty = int(payload.get("qty") or 0)
            if item_id not in items_by_id:
                self._trace_webapp(f"single_choice_not_found item_id={item_id}")
                await update.message.reply_text("Item not found for today.")
                return
            self.store.set_vote(day, user_id, user_name, item_id, max(0, qty))
            self._trace_webapp(f"single_choice_saved item_id={item_id} qty={max(0, qty)}")
            await update.message.reply_text(f"Saved: {items_by_id[item_id].name} -> {max(0, qty)}")
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
            for item_id, raw_qty in qty_map.items():
                if item_id not in items_by_id:
                    continue
                qty = max(0, int(raw_qty))
                self.store.set_vote(day, user_id, user_name, item_id, qty)
                touched += 1
                if qty > 0:
                    selected_positive += 1

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
                    f"Сохранено: {selected_positive} товаров (обновлено {touched}). "
                    "Часть позиций могла измениться после обновления, это нормально."
                )
            else:
                msg = f"Сохранено: {selected_positive} товаров (обновлено {touched})."
            await update.message.reply_text(msg)
            bound_chat_id = self._get_chat_id()
            current_chat = update.effective_chat.id if update.effective_chat else None
            if bound_chat_id is not None and current_chat is not None and bound_chat_id != current_chat:
                await self._send(
                    context.application,
                    f"{user_name}: выбрано {selected_positive} товаров (обновлено {touched}).",
                )
            return

        self._trace_webapp(f"unknown_type type={ptype}")
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
                f"Скидок пока нет. Автообновление: {self._collection_schedule_text()} (Europe/Moscow)."
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
                f"На сегодня данных пока нет. Автообновление: {self._collection_schedule_text()} (Europe/Moscow)."
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
        if not await self._check_owner_or_reply(update):
            return
        await self._finalize_impl(context.application)
        if update.message:
            await update.message.reply_text("Итоговый заказ сформирован.")

    async def collectnow(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._check_owner_or_reply(update):
            return
        await self._finalize_impl(context.application)
        if update.message:
            await update.message.reply_text("Итог собран сейчас.")

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
            await self._send(app, f"Итог за {day}: никто не выбрал товары.")
            return

        lines = [f"Итоговый заказ за {day}:"]
        for row in payload["items"]:
            lines.append(
                f"- {row['name']}: {int(row['qty'])} шт x {float(row['discount_price']):.2f} RUB"
            )
        lines.append(f"Сумма: {payload['total_sum_discount_price']:.2f} RUB")
        lines.append(f"Файл: {out_path}")
        if self.settings.dry_run:
            lines.append("Режим: DRY_RUN (автооформление отключено)")

        await self._send(app, "\n".join(lines))
        await self._run_executor_if_needed(app, out_path)

    async def _run_executor_if_needed(self, app: Application, out_path: Path) -> None:
        if self.settings.dry_run:
            return
        if not self.settings.order_executor_command:
            await self._send(app, "ORDER_EXECUTOR_COMMAND не задан. Автооформление пропущено.")
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
            "/app - открыть Mini App\n"
            "/status - текущие выборы\n"
            "/where - диагностика чата/owner\n"
            "/bind - привязать текущий чат (owner)\n"
            "/collect - обновить скидки из ВкусВилл (owner)\n"
            "/collectnow - собрать итоговый заказ сейчас (owner)\n"
            "/finalize - собрать итоговый заказ (owner)\n"
            "/resetday - очистить данные текущего дня (owner)\n"
            "/cart - сверить корзину с сегодняшними скидками (owner)\n"
            "/setowner - назначить/проверить owner\n"
            "/selftest - быстрая проверка состояния (owner)\n"
            "/hidekbd - скрыть клавиатуру\n"
            "/help - справка"
        )

    def build_app(self) -> Application:
        app = Application.builder().token(self.settings.bot_token).build()

        app.add_handler(CommandHandler("start", self.start))
        app.add_handler(CommandHandler("bind", self.bind))
        app.add_handler(CommandHandler("collect", self.collect))
        app.add_handler(CommandHandler("setowner", self.setowner))
        app.add_handler(CommandHandler("where", self.where))
        app.add_handler(CommandHandler("selftest", self.selftest))
        app.add_handler(CommandHandler("shop", self.shop))
        app.add_handler(CommandHandler("browse", self.shop))
        app.add_handler(CommandHandler("app", self.app))
        app.add_handler(CommandHandler("hidekbd", self.hidekbd))
        app.add_handler(CommandHandler("status", self.status))
        app.add_handler(CommandHandler("cart", self.cart))
        app.add_handler(CommandHandler("finalize", self.finalize))
        app.add_handler(CommandHandler("collectnow", self.collectnow))
        app.add_handler(CommandHandler("resetday", self.resetday))
        app.add_handler(CommandHandler("help", self.help))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.on_text_button))
        app.add_handler(CallbackQueryHandler(self.on_control, pattern=r"^ctl\|"))
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
