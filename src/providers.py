from __future__ import annotations

import hashlib
import json
import logging
import random
import subprocess
import re
from typing import Any
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .command_utils import command_to_args, project_root
from .config import Settings
from .store import ItemRow


LOGGER = logging.getLogger(__name__)


@dataclass
class DiscountItem:
    item_id: str
    name: str
    price: float
    discount_price: float
    source: str = "unknown"
    image_url: str = ""
    stock_qty: int | None = None
    availability_status: str = "unknown"
    availability_reason: str = ""

    def to_row(self) -> ItemRow:
        return ItemRow(
            item_id=self.item_id,
            name=self.name,
            price=self.price,
            discount_price=self.discount_price,
            source=self.source,
            image_url=self.image_url,
            stock_qty=self.stock_qty,
            availability_status=self.availability_status,
            availability_reason=self.availability_reason,
        )


def _slug(name: str) -> str:
    raw = name.strip().lower().encode("utf-8")
    return hashlib.sha1(raw).hexdigest()[:16]


def _trim_text(value: str, max_chars: int = 280) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1] + "…"


def _repair_mojibake_text(value: str) -> str:
    raw = str(value or "")
    if not raw:
        return raw
    suspicious = raw.count("Р") + raw.count("С") + raw.count("Ð") + raw.count("Ñ")
    if suspicious == 0:
        return raw
    try:
        fixed = raw.encode("cp1251").decode("utf-8")
    except Exception:
        return raw
    fixed_cyr = sum(1 for ch in fixed if "А" <= ch <= "я" or ch in {"Ё", "ё"})
    raw_cyr = sum(1 for ch in raw if "А" <= ch <= "я" or ch in {"Ё", "ё"})
    return fixed if fixed_cyr >= raw_cyr else raw


def _repair_mojibake_obj(obj: Any) -> Any:
    if isinstance(obj, str):
        return _repair_mojibake_text(obj)
    if isinstance(obj, list):
        return [_repair_mojibake_obj(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _repair_mojibake_obj(v) for k, v in obj.items()}
    return obj


def _decode_process_output(raw: bytes) -> str:
    if not raw:
        return ""
    for enc in ("utf-8", "utf-8-sig", "cp1251", "cp866"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _extract_process_error_hint(stdout: str, stderr: str, returncode: int) -> str:
    merged = "\n".join(x for x in [stderr, stdout] if x).strip()
    if not merged:
        return f"collector exit status {returncode}"

    # Prefer business-relevant hints from known collector failures.
    patterns = [
        r"ABORT: disk space low[^\n]*",
        r"disk full[^\n]*",
        r"Delivery location mismatch[^\n]*",
        r"Failed to open Chrome profile[^\n]*",
        r"VkusVill login required[^\n]*",
        r"Login required[^\n]*",
        r"not logged in[^\n]*",
        r"State file not found[^\n]*",
    ]
    for pat in patterns:
        m = re.search(pat, merged, flags=re.IGNORECASE)
        if m:
            return _trim_text(m.group(0))

    lines = [ln.strip() for ln in merged.splitlines() if ln.strip()]
    return _trim_text(lines[-1] if lines else f"collector exit status {returncode}")


class BaseProvider:
    def fetch(self, now: datetime) -> list[DiscountItem]:
        raise NotImplementedError


class ManualJsonProvider(BaseProvider):
    def __init__(self, path: str) -> None:
        self.path = path

    def fetch(self, now: datetime) -> list[DiscountItem]:
        try:
            payload = json.loads(Path(self.path).read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            LOGGER.warning("corrupted JSON at %s, returning empty list", self.path)
            return []
        items: list[DiscountItem] = []
        for item in payload:
            name = str(item["name"]).strip()
            item_id = str(item.get("item_id") or _slug(name))
            items.append(
                DiscountItem(
                    item_id=item_id,
                    name=name,
                    price=float(item.get("price", 0)),
                    discount_price=float(item.get("discount_price", item.get("price", 0))),
                    source=str(item.get("source", "manual_json")),
                    image_url=str(item.get("image_url", "") or ""),
                    stock_qty=(
                        int(item.get("stock_qty"))
                        if item.get("stock_qty") not in (None, "")
                        else None
                    ),
                    availability_status=str(item.get("availability_status") or "unknown"),
                    availability_reason=str(item.get("availability_reason") or ""),
                )
            )
        return items


class MockProvider(BaseProvider):
    _catalog = [
        ("Йогурт греческий", 129),
        ("Филе куриное", 399),
        ("Сыр гауда", 269),
        ("Кефир 1%", 99),
        ("Творог 5%", 139),
        ("Гранола", 249),
        ("Бананы", 119),
        ("Яблоки", 159),
        ("Лосось", 799),
        ("Паста томатная", 129),
        ("Авокадо", 179),
        ("Хлеб зерновой", 89),
        ("Оливковое масло", 459),
        ("Сок апельсиновый", 199),
        ("Сметана 20%", 109),
        ("Орехи микс", 329),
        ("Кофе зерновой", 699),
        ("Шоколад горький", 149),
        ("Куриное яйцо C1", 129),
        ("Моцарелла", 189),
        ("Огурцы", 119),
        ("Помидоры черри", 219),
        ("Молоко 3.2%", 109),
        ("Печенье овсяное", 129),
    ]

    def fetch(self, now: datetime) -> list[DiscountItem]:
        seed = int(now.strftime("%Y%m%d%H"))
        random.seed(seed)
        picked = random.sample(self._catalog, 6)
        items: list[DiscountItem] = []
        for name, price in picked:
            disc = round(price * random.uniform(0.7, 0.9), 2)
            items.append(
                DiscountItem(
                    item_id=_slug(name),
                    name=name,
                    price=float(price),
                    discount_price=disc,
                    source="mock",
                    image_url="",
                    stock_qty=None,
                    availability_status="unknown",
                    availability_reason="",
                )
            )
        return items


class RPACommandProvider(BaseProvider):
    def __init__(self, command: str, timeout_sec: int = 180) -> None:
        self.command = command
        self.timeout_sec = timeout_sec

    def fetch(self, now: datetime) -> list[DiscountItem]:
        args = command_to_args(self.command)
        if not args:
            raise ValueError("RPA_COMMAND is empty after expansion")
        proc = subprocess.run(
            args,
            shell=False,
            check=False,
            capture_output=True,
            text=False,
            cwd=str(project_root()),
            timeout=self.timeout_sec,
        )
        stdout = _decode_process_output(proc.stdout).strip()
        stderr = _decode_process_output(proc.stderr).strip()
        if proc.returncode != 0:
            hint = _extract_process_error_hint(stdout, stderr, proc.returncode)
            raise ValueError(f"collect_command_failed: {hint}")
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError:
            # Fallback: tolerate accidental diagnostic lines and parse last JSON line.
            payload = None
            for line in reversed([ln.strip() for ln in stdout.splitlines() if ln.strip()]):
                if line.startswith("[") or line.startswith("{"):
                    try:
                        payload = json.loads(line)
                        break
                    except json.JSONDecodeError:
                        continue
            if payload is None:
                raise ValueError(
                    "RPA command output is not valid JSON. "
                    f"stdout={stdout[:500]!r}; stderr={stderr[:500]!r}"
                )
        payload = _repair_mojibake_obj(payload)
        items: list[DiscountItem] = []
        for item in payload:
            name = str(item["name"]).strip()
            item_id = str(item.get("item_id") or _slug(name))
            items.append(
                DiscountItem(
                    item_id=item_id,
                    name=name,
                    price=float(item.get("price", 0)),
                    discount_price=float(item.get("discount_price", item.get("price", 0))),
                    source=str(item.get("source", "rpa")),
                    image_url=str(item.get("image_url", "") or ""),
                    stock_qty=(
                        int(item.get("stock_qty"))
                        if item.get("stock_qty") not in (None, "")
                        else None
                    ),
                    availability_status=str(item.get("availability_status") or "unknown"),
                    availability_reason=str(item.get("availability_reason") or ""),
                )
            )
        return items


def create_provider(settings: Settings) -> BaseProvider:
    if settings.provider == "manual_json":
        return ManualJsonProvider(settings.discounts_json_path)
    if settings.provider == "mock":
        return MockProvider()
    if settings.provider == "rpa_command":
        if not settings.rpa_command:
            raise ValueError("RPA_COMMAND is required when PROVIDER=rpa_command")
        return RPACommandProvider(settings.rpa_command, timeout_sec=settings.collect_timeout_sec)
    raise ValueError(f"Unsupported provider: {settings.provider}")
