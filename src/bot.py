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
import zlib
from datetime import datetime, timedelta
from pathlib import Path
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

from .config import Settings
from .command_utils import command_to_args, project_root
from .providers import BaseProvider, ManualJsonProvider, RPACommandProvider
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

    def _best_available_items(self, day: str, restore_into_live: bool = False) -> tuple[list[object], str]:
        live_items = self.store.list_items(day)
        best_snapshot = self.store.get_best_day_snapshot(day)
        if best_snapshot is None or not best_snapshot.items:
            return live_items, "live"

        live_regular = self._regular_inshop_count(live_items)
        best_regular = int(best_snapshot.regular_count)
        use_snapshot = (
            not live_items
            or best_regular > live_regular
            or (best_regular == live_regular and best_snapshot.total_items > len(live_items))
        )
        if not use_snapshot:
            return live_items, "live"

        if restore_into_live:
            self.store.upsert_items(day, best_snapshot.items)
            self.store.set_meta("last_snapshot_restore_at", self._now_iso())
            self.store.set_meta("last_snapshot_restore_day", day)
            self.store.set_meta("last_snapshot_restore_id", best_snapshot.snapshot_id)
            self.store.set_meta("last_snapshot_restore_status", best_snapshot.status)
        return list(best_snapshot.items), f"snapshot:{best_snapshot.snapshot_id}"

    def _archive_day_snapshot(self, day: str, items: list[object], status: str) -> str | None:
        if not items:
            return None
        snapshot_id = self._snapshot_id(items, day)
        rows = [x.to_row() if hasattr(x, "to_row") else x for x in items]
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

    async def _send(self, app: Application, text: str, **kwargs) -> None:
        chat_id = self._get_chat_id()
        if chat_id is None:
            LOGGER.warning("Chat is not bound yet, message skipped: %s", text[:80])
            return
        await app.bot.send_message(chat_id=chat_id, text=text, **kwargs)

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

    async def scheduled_cleanup(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        removed = self._cleanup_out_dir()
        if removed > 0:
            LOGGER.info("Out-dir cleanup removed %s file(s) older than %s days", removed, self.settings.out_retention_days)

    def _now_iso(self) -> str:
        return datetime.now(self.settings.timezone).isoformat(timespec="seconds")

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
        return True, "ok"

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

    def _build_public_webapp_snapshot(self, day: str, items: list[object]) -> dict:
        groups, favorites, ready_food = self._mini_groups(items)
        snapshot_id = self._snapshot_id(items, day)
        regular_count = sum(len(g["items"]) for g in groups)

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

        return {
            "d": day,
            "sid": snapshot_id,
            "m": [
                [
                    str(item.item_id),
                    str(item.name),
                    float(item.discount_price),
                    self._compact_image_url_for_webapp(str(getattr(item, "image_url", "") or "")),
                ]
                for item in unique_items
            ],
            "g": group_indexes,
            "f": favorite_indexes,
            "r": ready_food_indexes,
            "rc": regular_count,
            "cap": 18,
            "generated_at": self._now_iso(),
        }

    def _write_webapp_latest_snapshot(self, day: str, items: list[object]) -> None:
        out_path = Path("webapp") / "latest.json"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        payload = self._build_public_webapp_snapshot(day, items)
        out_path.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    def _build_mini_app_url(self, user_id: int | None) -> str | None:
        if not self.settings.mini_app_url:
            return None

        day = self._today()
        items, _snapshot_source = self._best_available_items(day)
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
                ]
                for item in unique_items
            ],
            "g": group_indexes,
            "f": favorite_indexes,
            "r": ready_food_indexes,
            "rc": regular_count,
            "cap": 18,
        }

        def _pack(payload: dict) -> str:
            raw_payload = json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            return base64.urlsafe_b64encode(zlib.compress(raw_payload, level=9)).decode("ascii").rstrip("=")

        packed = _pack(compact_payload)

        parts = urlsplit(self.settings.mini_app_url)
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        query["data"] = packed
        query["enc"] = "z"
        query["v"] = datetime.now(self.settings.timezone).strftime("%Y%m%d%H%M%S")
        query["cb"] = str(int(datetime.now(self.settings.timezone).timestamp()))
        out_url = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))

        # Safety fallback: if URL gets too long, drop image URLs and keep text mode stable.
        if len(out_url) > 7000:
            compact_payload["m"] = [
                [str(item.item_id), str(item.name), float(item.discount_price)]
                for item in unique_items
            ]
            query["data"] = _pack(compact_payload)
            out_url = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))

        return out_url

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
        ok = await self._collect_impl(context.application, quiet_errors_in_group=False)
        if update.message and ok:
            await update.message.reply_text("Скидки обновлены.")
        elif update.message:
            await update.message.reply_text("Сбор не удался. Подробности отправил владельцу в личку.")

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

    async def _collect_impl(
        self,
        app: Application,
        skip_if_full: bool = False,
        quiet_errors_in_group: bool = True,
        announce_success_in_group: bool = True,
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

        fetched_regular_count = self._regular_inshop_count(items)
        # Guard against accidental downgrade when replacement limit is reached:
        # if we already have full 18/18, do not overwrite with a partial wave.
        if existing_regular_count >= 18 and fetched_regular_count < 18:
            existing_favorite_count = sum(1 for x in existing if self._is_favorite_item(x.name, x.source))
            existing_ready_food_count = sum(1 for x in existing if self._is_ready_food_offer(x.source))
            archived_snapshot_id = self._archive_day_snapshot(day, existing, "guard_preserve_full")
            self.store.set_meta("last_collect_at", self._now_iso())
            self.store.set_meta("last_collect_status", "guard_preserve_full")
            self.store.set_meta("last_collect_day", day)
            self.store.set_meta("last_collect_regular_count", str(existing_regular_count))
            self.store.set_meta("last_collect_total_items", str(len(existing)))
            if announce_success_in_group:
                await self._send(
                    app,
                    (
                        f"Сбор {now.strftime('%H:%M')} пропущен защитой: "
                        f"новый срез {fetched_regular_count}/18, сохранен предыдущий полный набор {existing_regular_count}/18. "
                        f"В базе осталось: {len(existing)} (любимый: {existing_favorite_count}, готовая еда: {existing_ready_food_count}). "
                        f"Снэпшот: {archived_snapshot_id or 'n/a'}."
                    ),
                )
            else:
                await self._send_owner(
                    app,
                    (
                        f"Сбор {now.strftime('%H:%M')} пропущен защитой: "
                        f"новый срез {fetched_regular_count}/18, сохранен предыдущий полный набор {existing_regular_count}/18. "
                        f"В базе осталось: {len(existing)} (любимый: {existing_favorite_count}, готовая еда: {existing_ready_food_count}). "
                        f"Снэпшот: {archived_snapshot_id or 'n/a'}."
                    ),
                )
            return True

        # Additional downgrade protection:
        # keep richer same-day snapshot when a new collect brings fewer regular slots.
        if existing_regular_count > fetched_regular_count and existing_regular_count > 0:
            fresh = self.store.upsert_items(day, [x.to_row() for x in items])
            removed = 0
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

        fresh, removed = self.store.sync_items(day, [x.to_row() for x in items])
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
        self.store.set_meta("last_collect_attempts", self._format_collect_attempts(collect_meta.get("attempts") or []))
        self.store.set_meta(
            "last_collect_failover_used",
            "true" if bool(collect_meta.get("used_failover")) else "false",
        )
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
        summary_text = (
            f"Сбор {now.strftime('%H:%M')} завершен. "
            f"В базе: {len(all_items)} (новых {len(fresh)}, удалено {removed}). "
            f"Подборки 20%: {regular_count}/18, любимый: {favorite_count}, готовая еда: {ready_food_count}. "
            f"Кэш картинок: {mirror_detail}. "
            f"Снэпшот: {archived_snapshot_id or 'n/a'}.{publish_suffix}"
        )
        await self._send_owner(app, summary_text)
        if bool(collect_meta.get("used_failover")):
            await self._send_owner(
                app,
                (
                    "Сбор выполнен через fallback-источник.\n"
                    f"Выбран: {selected_source}\n"
                    f"Попытки: {self._format_collect_attempts(collect_meta.get('attempts') or [])}"
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

    async def scheduled_sessioncheck(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        ok, detail = await self._run_session_probe()
        self.store.set_meta("last_sessioncheck_at", self._now_iso())
        self.store.set_meta("last_sessioncheck_status", "ok" if ok else "error")
        self.store.set_meta("last_sessioncheck_detail", detail)
        if not ok:
            await self._send_owner(
                context.application,
                f"Проверка сессии ВкусВилл: требуется вход в Chrome-профиль ({detail}).",
            )

    async def shop(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_user is None or update.message is None:
            return
        chat_type = update.effective_chat.type if update.effective_chat else ""
        if chat_type != "private":
            deep_link = self._private_app_deeplink(getattr(context.bot, "username", None))
            if deep_link:
                await update.message.reply_text(
                    "Старая витрина в группе отключена. Выбор делаем только через Mini App в личке.",
                    reply_markup=InlineKeyboardMarkup(
                        [[InlineKeyboardButton("Открыть Mini App", url=deep_link)]]
                    ),
                )
            else:
                await update.message.reply_text("Старая витрина отключена. Открой Mini App через /app в личке.")
            return
        day = self._today()
        items, _snapshot_source = self._best_available_items(day, restore_into_live=True)
        if not items:
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
        await update.message.reply_text(
            text=text,
            reply_markup=self._browser_keyboard(item.item_id, 0, len(items)),
        )
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
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("Собрать заказ сейчас", callback_data="ctl|collectnow")]]
                ),
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
                try:
                    qty = max(0, int(raw_qty))
                except (TypeError, ValueError):
                    continue
                self.store.set_vote(day, user_id, user_name, item_id, qty)
                touched += 1
                if qty > 0:
                    selected_positive += 1

            selected_rows: list[tuple[str, int]] = []
            for item in items:
                qty = int(self.store.get_user_qty(day, user_id, item.item_id))
                if qty > 0:
                    selected_rows.append((item.name, qty))
            preview_limit = 8
            preview_rows = selected_rows[:preview_limit]
            selected_preview = "\n".join([f"- {name}: {qty} шт" for name, qty in preview_rows])
            extra_count = max(0, len(selected_rows) - preview_limit)
            if extra_count > 0:
                selected_preview = (
                    f"{selected_preview}\n- ... и еще {extra_count} поз."
                    if selected_preview
                    else f"- ... и еще {extra_count} поз."
                )

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
            if selected_positive == 0:
                msg = "Выбор очищен. Сейчас у тебя нет активных товаров."
            if selected_preview:
                msg = f"{msg}\nТвой выбор:\n{selected_preview}"
            await update.message.reply_text(msg)
            bound_chat_id = self._get_chat_id()
            current_chat = update.effective_chat.id if update.effective_chat else None
            if bound_chat_id is not None and current_chat is not None and bound_chat_id != current_chat:
                if selected_positive == 0:
                    group_msg = f"{user_name}: выбор очищен."
                else:
                    group_msg = f"{user_name}: выбрано {selected_positive} товаров (обновлено {touched})."
                if selected_preview and selected_positive > 0:
                    group_msg = f"{group_msg}\nВыбор:\n{selected_preview}"
                await self._send(
                    context.application,
                    group_msg[:3900],
                )
            return

        self._trace_webapp(f"unknown_type type={ptype}")
        await update.message.reply_text("Unknown Mini App payload type.")

    async def on_browser(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if query is None or query.from_user is None or query.data is None:
            return

        chat_type = query.message.chat.type if query.message and query.message.chat else "private"
        if chat_type != "private":
            try:
                await query.edit_message_reply_markup(reply_markup=None)
            except Exception:
                pass
            await query.answer("Витрина в группе отключена. Используй Mini App в личке.")
            return

        answered = False

        parts = query.data.split("|")
        if len(parts) < 2:
            return

        day = self._today()
        items, _snapshot_source = self._best_available_items(day, restore_into_live=True)
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

    async def clearvotes(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.message is None:
            return
        if not await self._check_owner_or_reply(update):
            return
        day = self._today()
        self.store.clear_votes(day)
        await update.message.reply_text(f"Выборы за {day} очищены. Можно собирать новый заказ.")

    def _schedule_startup_collect_if_needed(self, app: Application) -> None:
        day = self._today()
        items, _snapshot_source = self._best_available_items(day)
        if items:
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
        backup_path = (
            Path(self.settings.out_dir)
            / f"votes_backup_{day}_{datetime.now(self.settings.timezone).strftime('%H%M%S')}.json"
        )
        backup_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        self.store.set_meta("last_finalize_at", self._now_iso())
        self.store.set_meta("last_finalize_day", day)
        self.store.set_meta("last_finalize_backup", str(backup_path))

        if not payload["items"]:
            await self._send(app, f"Итог за {day}: никто не выбрал товары.")
            return

        lines = [f"Итоговый заказ за {day}:"]
        for row in payload["items"]:
            lines.append(
                f"- {row['name']}: {int(row['qty'])} шт x {float(row['discount_price']):.2f} RUB"
            )
        lines.append(f"Сумма: {payload['total_sum_discount_price']:.2f} RUB")
        if self.settings.dry_run:
            lines.append("Режим: DRY_RUN (автооформление отключено)")

        await self._send(app, "\n".join(lines))
        await self._send_owner(
            app,
            (
                f"Итог за {day} сохранен.\n"
                f"Файл заказа: {out_path}\n"
                f"Резерв голосов: {backup_path}"
            ),
        )
        exec_result = await self._run_executor_if_needed(app, out_path)
        if bool(exec_result.get("ok")):
            self.store.clear_votes(day)
            await self._send(app, "Выборы за текущий день сброшены. Можно собирать новый заказ с нуля.")
        else:
            await self._send_owner(
                app,
                (
                    "Выборы НЕ очищены: автооформление не завершилось успешно.\n"
                    f"Резерв голосов: {backup_path}"
                ),
            )

    async def _run_executor_if_needed(self, app: Application, out_path: Path) -> dict:
        if self.settings.dry_run:
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
            preflight_args = args + ["--check-session-only"]
            preflight = await asyncio.to_thread(self._run_cmd_capture, preflight_args, 90)
            pre_raw = "\n".join(x for x in [(preflight.stdout or "").strip(), (preflight.stderr or "").strip()] if x).strip()
            pre_payload = self._extract_payload(pre_raw)
            pre_ok = preflight.returncode == 0 and (
                not isinstance(pre_payload, dict) or bool(pre_payload.get("ok", True))
            )
            if not pre_ok:
                await self._send_owner(
                    app,
                    "Сессия ВкусВилл недействительна. Пробую автообновление сессии через браузер...",
                )
                refresh_args = preflight_args + [
                    "--interactive-login",
                    "--interactive-login-wait-sec",
                    "180",
                    "--no-headless",
                ]
                refreshed = await asyncio.to_thread(self._run_cmd_capture, refresh_args, 240)
                refresh_raw = "\n".join(
                    x for x in [(refreshed.stdout or "").strip(), (refreshed.stderr or "").strip()] if x
                ).strip()
                refresh_payload = self._extract_payload(refresh_raw)
                refresh_ok = refreshed.returncode == 0 and (
                    not isinstance(refresh_payload, dict) or bool(refresh_payload.get("ok", True))
                )
                if not refresh_ok:
                    short = (refresh_raw or "session_check_failed").splitlines()[-1][:240]
                    await self._send_owner(
                        app,
                        f"Автооформление остановлено: не удалось подтвердить сессию ВкусВилл ({self._repair_mojibake(short)}).",
                    )
                    self.store.set_meta("last_executor_at", self._now_iso())
                    self.store.set_meta("last_executor_status", "session_invalid")
                    return {"ok": False, "status": "session_invalid", "log_path": str(log_path)}

            proc = await asyncio.to_thread(self._run_cmd_capture, args, 420)
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
                if total <= 0:
                    msg = str(payload.get("message") or "").strip()
                    if msg == "no_selected_items":
                        await self._send(app, "В заказе нет выбранных позиций.")
                    else:
                        await self._send_owner(app, f"Executor success_no_targets. Техлог: {log_path}")
                    self.store.set_meta("last_executor_at", self._now_iso())
                    self.store.set_meta("last_executor_status", "success_no_targets")
                    return {"ok": True, "status": "success_no_targets", "log_path": str(log_path)}
                failed = max(0, total - ok_count)
                cart_unique = int(payload.get("cart_unique_after") or 0)
                cart_total_qty = int(payload.get("cart_total_qty_after") or 0)
                if bool(payload.get("ok")) and failed == 0:
                    msg = f"Корзина обновлена: {ok_count}/{total} позиций."
                    if cart_unique > 0:
                        msg += f"\nВ корзине сейчас: {cart_unique} позиций, суммарное кол-во: {cart_total_qty}."
                    await self._send(app, msg)
                    self.store.set_meta("last_executor_at", self._now_iso())
                    self.store.set_meta("last_executor_status", "success")
                    self.store.set_meta("last_executor_ok_count", str(ok_count))
                    self.store.set_meta("last_executor_total", str(total))
                    return {"ok": True, "status": "success", "log_path": str(log_path)}
                else:
                    lines = [f"Корзина обновлена частично: {ok_count}/{total}, не добавлено {failed}."]
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
                    return {"ok": False, "status": "partial", "log_path": str(log_path)}
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
        regular_count = self._regular_inshop_count(items)
        favorite_count = sum(1 for x in items if self._is_favorite_item(x.name, x.source))
        ready_food_count = sum(1 for x in items if self._is_ready_food_offer(x.source))
        totals = self.store.totals_by_item(day)
        active_votes = sum(1 for row in totals if int(row.get("qty") or 0) > 0)
        users = self.store.votes_by_user(day)

        bound_chat = self._get_chat_id()
        owner_id = self._get_owner_user_id()
        has_rpa = bool(self.settings.rpa_command) if self.settings.provider == "rpa_command" else True
        has_executor = bool(self.settings.order_executor_command)
        last_collect_at = self.store.get_meta("last_collect_at") or "n/a"
        last_collect_status = self.store.get_meta("last_collect_status") or "n/a"
        last_sessioncheck_at = self.store.get_meta("last_sessioncheck_at") or "n/a"
        last_sessioncheck_status = self.store.get_meta("last_sessioncheck_status") or "n/a"
        last_executor_at = self.store.get_meta("last_executor_at") or "n/a"
        last_executor_status = self.store.get_meta("last_executor_status") or "n/a"
        last_executor_ok = self.store.get_meta("last_executor_ok_count") or "n/a"
        last_executor_total = self.store.get_meta("last_executor_total") or "n/a"
        last_mirror_at = self.store.get_meta("last_mirror_at") or "n/a"
        last_mirror_status = self.store.get_meta("last_mirror_status") or "n/a"
        last_mirror_detail = self.store.get_meta("last_mirror_detail") or "n/a"
        last_publish_at = self.store.get_meta("last_publish_at") or "n/a"
        last_publish_status = self.store.get_meta("last_publish_status") or "n/a"
        last_publish_detail = self.store.get_meta("last_publish_detail") or "n/a"
        best_snapshot = self.store.get_best_day_snapshot(day)
        best_snapshot_text = (
            f"{best_snapshot.snapshot_id} ({best_snapshot.regular_count}/18, total={best_snapshot.total_items}, {best_snapshot.status})"
            if best_snapshot is not None
            else "n/a"
        )

        problems: list[str] = []
        if bound_chat is None:
            problems.append("чат не привязан (/bind)")
        if owner_id is None:
            problems.append("не задан owner (/setowner)")
        if not has_rpa:
            problems.append("не задан RPA_COMMAND")
        if not has_executor and not self.settings.dry_run:
            problems.append("не задан ORDER_EXECUTOR_COMMAND")
        if regular_count < 6:
            problems.append("мало данных в подборках (меньше 6 товаров)")
        if last_collect_status == "error":
            problems.append("последний collect завершился ошибкой")
        if last_sessioncheck_status == "error":
            problems.append("сессия ВкусВилл требует входа")
        if last_executor_status in {"failed_error", "failed_no_payload", "exception", "session_invalid"}:
            problems.append(f"последний executor в ошибке ({last_executor_status})")

        state = "HEALTHY" if not problems else "DEGRADED"
        lines = [
            f"Health {day}: {state}",
            f"- chat_id: {bound_chat}",
            f"- owner_id: {owner_id}",
            f"- provider: {self.settings.provider}",
            f"- dry_run: {self.settings.dry_run}",
            f"- items: total={len(items)}, regular={regular_count}/18, favorite={favorite_count}, ready_food={ready_food_count}",
            f"- source_for_app: {snapshot_source}",
            f"- votes: users={len(users)}, selected_positions={active_votes}",
            f"- last_collect: status={last_collect_status}, at={last_collect_at}",
            f"- last_sessioncheck: status={last_sessioncheck_status}, at={last_sessioncheck_at}",
            f"- last_mirror: status={last_mirror_status}, at={last_mirror_at}, detail={last_mirror_detail}",
            f"- last_publish: status={last_publish_status}, at={last_publish_at}, detail={last_publish_detail}",
            f"- last_executor: status={last_executor_status}, at={last_executor_at}, ok_count={last_executor_ok}/{last_executor_total}",
            f"- best_snapshot: {best_snapshot_text}",
        ]
        if problems:
            lines.append("- issues:")
            lines.extend([f"  * {p}" for p in problems])
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
            "/mirror [YYYY-MM-DD] - собрать локальный кэш картинок (owner)\n"
            "/publishapp - опубликовать Mini App на GitHub Pages (owner)\n"
            "/collectnow - собрать итоговый заказ сейчас (owner)\n"
            "/finalize - собрать итоговый заказ (owner)\n"
            "/resetday - очистить данные текущего дня (owner)\n"
            "/clearvotes - очистить только выборы за сегодня (owner)\n"
            "/cart - сверить корзину с сегодняшними скидками (owner)\n"
            "/sessioncheck - проверка логина Chrome-профиля (owner)\n"
            "/health - быстрый статус бота (owner)\n"
            "/setowner - назначить/проверить owner\n"
            "/selftest - быстрая проверка состояния (owner)\n"
            "/hidekbd - скрыть клавиатуру\n"
            "/help - справка"
        )

    def build_app(self) -> Application:
        app = (
            Application.builder()
            .token(self.settings.bot_token)
            .defaults(Defaults(tzinfo=self.settings.timezone))
            .build()
        )
        try:
            Path(self.settings.out_dir).mkdir(parents=True, exist_ok=True)
            removed = self._cleanup_out_dir()
            if removed > 0:
                LOGGER.info(
                    "Startup out-dir cleanup removed %s file(s) older than %s days",
                    removed,
                    self.settings.out_retention_days,
                )
        except Exception:
            pass

        app.add_handler(CommandHandler("start", self.start))
        app.add_handler(CommandHandler("bind", self.bind))
        app.add_handler(CommandHandler("collect", self.collect))
        app.add_handler(CommandHandler("mirror", self.mirror))
        app.add_handler(CommandHandler("publishapp", self.publishapp))
        app.add_handler(CommandHandler("setowner", self.setowner))
        app.add_handler(CommandHandler("where", self.where))
        app.add_handler(CommandHandler("selftest", self.selftest))
        app.add_handler(CommandHandler("shop", self.shop))
        app.add_handler(CommandHandler("browse", self.shop))
        app.add_handler(CommandHandler("app", self.app))
        app.add_handler(CommandHandler("hidekbd", self.hidekbd))
        app.add_handler(CommandHandler("status", self.status))
        app.add_handler(CommandHandler("cart", self.cart))
        app.add_handler(CommandHandler("health", self.health))
        app.add_handler(CommandHandler("finalize", self.finalize))
        app.add_handler(CommandHandler("collectnow", self.collectnow))
        app.add_handler(CommandHandler("resetday", self.resetday))
        app.add_handler(CommandHandler("clearvotes", self.clearvotes))
        app.add_handler(CommandHandler("sessioncheck", self.sessioncheck))
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
        self._schedule_startup_collect_if_needed(app)
        return app



