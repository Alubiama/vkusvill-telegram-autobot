from __future__ import annotations

import hashlib
import json
import random
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .command_utils import command_to_args, project_root
from .config import Settings
from .store import ItemRow


@dataclass
class DiscountItem:
    item_id: str
    name: str
    price: float
    discount_price: float
    source: str = "unknown"
    image_url: str = ""

    def to_row(self) -> ItemRow:
        return ItemRow(
            item_id=self.item_id,
            name=self.name,
            price=self.price,
            discount_price=self.discount_price,
            source=self.source,
            image_url=self.image_url,
        )


def _slug(name: str) -> str:
    raw = name.strip().lower().encode("utf-8")
    return hashlib.sha1(raw).hexdigest()[:16]


class BaseProvider:
    def fetch(self, now: datetime) -> list[DiscountItem]:
        raise NotImplementedError


class ManualJsonProvider(BaseProvider):
    def __init__(self, path: str) -> None:
        self.path = path

    def fetch(self, now: datetime) -> list[DiscountItem]:
        payload = json.loads(Path(self.path).read_text(encoding="utf-8"))
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
                )
            )
        return items


class RPACommandProvider(BaseProvider):
    def __init__(self, command: str) -> None:
        self.command = command

    def fetch(self, now: datetime) -> list[DiscountItem]:
        args = command_to_args(self.command)
        if not args:
            raise ValueError("RPA_COMMAND is empty after expansion")
        proc = subprocess.run(
            args,
            shell=False,
            check=True,
            capture_output=True,
            text=True,
            cwd=str(project_root()),
            timeout=420,
        )
        stdout = (proc.stdout or "").strip()
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
                stderr = (proc.stderr or "").strip()
                raise ValueError(
                    "RPA command output is not valid JSON. "
                    f"stdout={stdout[:500]!r}; stderr={stderr[:500]!r}"
                )
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
        return RPACommandProvider(settings.rpa_command)
    raise ValueError(f"Unsupported provider: {settings.provider}")
