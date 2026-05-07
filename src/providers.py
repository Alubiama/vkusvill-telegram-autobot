from __future__ import annotations

import hashlib
import json
import logging
import random
import subprocess
import re
import time as _time
from typing import Any
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Sequence

from .command_utils import command_to_args, project_root
from .config import Settings
from .store import ItemRow
from .vkusvill_mcp_client import VkusvillMCPClient


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
        raw_path = Path(path)
        self.path = raw_path if raw_path.is_absolute() else project_root() / raw_path

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


class VkusvillMCPProvider(BaseProvider):
    DEFAULT_SEARCH_TERMS: tuple[str, ...] = (
        "скидки",
        "акция",
        "скидка",
        "выгодно",
        "молоко",
        "сыр",
        "йогурт",
        "кофе",
        "чай",
        "хлеб",
        "мясо",
        "рыба",
        "фрукты",
        "овощи",
        "десерт",
        "готовая еда",
    )

    def __init__(
        self,
        client: VkusvillMCPClient | None = None,
        discount_type: str = "card",
        max_pages: int = 3,
        timeout_sec: int = 30,
        max_results: int = 18,
        sort: str = "popularity",
        vvonly: int = 1,
    ) -> None:
        self.client = client or VkusvillMCPClient(timeout=timeout_sec)
        self.discount_type = str(discount_type or "card").strip() or "card"
        self.max_pages = max(1, int(max_pages))
        self.max_results = max(1, min(18, int(max_results)))
        self.sort = str(sort or "popularity").strip() or "popularity"
        self.vvonly = 1 if int(vvonly) else 0

    @staticmethod
    def _first_image_url(product: dict[str, Any]) -> str:
        images = product.get("images")
        if not isinstance(images, list):
            return ""
        for image in images:
            if not isinstance(image, dict):
                continue
            for key in ("medium", "small", "large"):
                value = image.get(key)
                if value:
                    return str(value)
        return ""

    @staticmethod
    def _extract_products(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
        data = payload.get("data")
        if not isinstance(data, dict):
            data = payload if isinstance(payload, dict) else {}
        products = data.get("items") or data.get("goods") or []
        if not isinstance(products, list):
            products = []
        meta = data.get("meta") if isinstance(data.get("meta"), dict) else {}
        page = int(meta.get("page") or 1)
        pages = int(meta.get("pages") or page)
        has_more = bool(meta.get("has_more")) or page < pages
        return [item for item in products if isinstance(item, dict)], has_more

    @staticmethod
    def _discount_item_from_product(product: dict[str, Any]) -> DiscountItem | None:
        price = product.get("price")
        if not isinstance(price, dict):
            return None

        current = float(price.get("current") or 0)
        if current <= 0:
            return None

        old = price.get("old")
        discount_percent = price.get("discount_percent")
        if old in (None, "") and discount_percent in (None, ""):
            return None

        old_price = float(old) if old not in (None, "") else current
        if old_price <= current and discount_percent in (None, ""):
            return None

        name = str(product.get("name") or "").strip()
        item_id = str(product.get("xml_id") or product.get("id") or "").strip()
        if not name or not item_id:
            return None

        return DiscountItem(
            item_id=item_id,
            name=name,
            price=old_price,
            discount_price=current,
            source="vkusvill_mcp",
            image_url=VkusvillMCPProvider._first_image_url(product),
            stock_qty=None,
            availability_status="unknown",
            availability_reason="",
        )

    def fetch(self, now: datetime) -> list[DiscountItem]:
        del now
        found: dict[str, DiscountItem] = {}
        for page in range(1, self.max_pages + 1):
            try:
                payload = self.client.list_discount_products(
                    page=page,
                    discount_type=self.discount_type,
                    sort=self.sort,
                    vvonly=self.vvonly,
                )
            except Exception as exc:
                LOGGER.warning("MCP discount fetch failed for page %s: %s", page, exc)
                break

            products, has_more = self._extract_products(payload)
            if not products:
                break

            for product in products:
                item = self._discount_item_from_product(product)
                if item is None or item.item_id in found:
                    continue
                found[item.item_id] = item
                if len(found) >= self.max_results:
                    return list(found.values())

            if not has_more:
                break

        return list(found.values())


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


class HttpJsonProvider(BaseProvider):
    """Fetches discount items from a remote HTTP JSON endpoint (e.g. VPS collector)."""

    def __init__(self, url: str, timeout_sec: int = 30) -> None:
        self.url = url
        self.timeout_sec = timeout_sec

    def fetch(self, now: datetime) -> list[DiscountItem]:
        del now
        import urllib.request
        try:
            with urllib.request.urlopen(self.url, timeout=self.timeout_sec) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            raise ValueError(f"HttpJsonProvider failed to fetch {self.url}: {exc}") from exc
        if isinstance(payload, dict) and payload.get("error"):
            raise ValueError(f"HttpJsonProvider got error response: {payload['error']}")
        if not isinstance(payload, list):
            raise ValueError(f"HttpJsonProvider expected list, got {type(payload).__name__}")
        items: list[DiscountItem] = []
        for item in payload:
            name = str(item.get("name", "")).strip()
            if not name:
                continue
            item_id = str(item.get("item_id") or _slug(name))
            items.append(DiscountItem(
                item_id=item_id,
                name=name,
                price=float(item.get("price", 0)),
                discount_price=float(item.get("discount_price", item.get("price", 0))),
                source=str(item.get("source", "http_json")),
                image_url=str(item.get("image_url", "") or ""),
                stock_qty=(int(item["stock_qty"]) if item.get("stock_qty") not in (None, "") else None),
                availability_status=str(item.get("availability_status") or "unknown"),
                availability_reason=str(item.get("availability_reason") or ""),
            ))
        return items


class MobileApiProvider(BaseProvider):
    """Fetches personal VkusVill discounts via mobile API — no browser needed.

    Uses the same endpoints as the VkusVill Android app:
    - Auth: OTP flow (v1/user/otp/auth + confirm/auth)
    - Discounts: GET user/privAbonement/abonementScreen
    - Token stored in a JSON file, refreshed automatically on 401.

    Token file format (data/mobile_tokens.json):
      device_id, number, phone, token, access_token, refresh_token,
      version_code, version_display, shop_no
    """

    BASE_URL = "https://mobile.vkusvill.ru/api/"
    DEFAULT_VERSION_DISPLAY = "26.5.11"
    DEFAULT_VERSION_CODE = "2605011"

    def __init__(
        self,
        token_file: str | Path,
        timeout_sec: int = 15,
        proxy: str | None = None,
    ) -> None:
        self.token_file = Path(token_file)
        self.timeout_sec = timeout_sec
        self.proxy = proxy

    def _load_tokens(self) -> dict[str, str]:
        if not self.token_file.exists():
            raise ValueError(f"Token file not found: {self.token_file}. Run /onboard first.")
        data = json.loads(self.token_file.read_text(encoding="utf-8"))
        if not data.get("access_token") or not data.get("number"):
            raise ValueError("Token file missing access_token or number. Re-run /onboard.")
        return data

    def _save_tokens(self, data: dict[str, str]) -> None:
        self.token_file.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    def _ver(self, tokens: dict[str, str]) -> tuple[str, str]:
        """Return (version_display, version_code) from tokens or defaults."""
        d = tokens.get("version_display", self.DEFAULT_VERSION_DISPLAY)
        c = tokens.get("version_code", self.DEFAULT_VERSION_CODE)
        return d, c

    def _build_headers(self, tokens: dict[str, str]) -> dict[str, str]:
        vd, vc = self._ver(tokens)
        return {
            "User-Agent": f"vkusvill/{vd} (Android; 34)",
            "Accept": "application/json",
            "X-VKUSVILL-TOKEN": tokens.get("token", ""),
            "X-VKUSVILL-TOKEN-ACCESS": tokens["access_token"],
            "X-VKUSVILL-DEVICE": "android",
            "X-VKUSVILL-SOURCE": "2",
            "X-VKUSVILL-VERSION": f"{vd} ({vc})",
            "X-VKUSVILL-MODEL": "Google Pixel 7",
        }

    def _build_str_par(self, tokens: dict[str, str]) -> str:
        _, vc = self._ver(tokens)
        ts = str(int(_time.time()))
        parts = [
            ("device_id", tokens.get("device_id", "")),
            ("version", vc),
            ("source", "2"),
            ("ts", ts),
        ]
        number = tokens.get("number", "")
        if number:
            parts.append(("user_number", number))
        return "".join(f"{{[{k}]}}{{[{v}]}}" for k, v in parts)

    def _refresh_access_token(self, tokens: dict[str, str]) -> dict[str, str]:
        import httpx

        refresh_token = tokens.get("refresh_token", "")
        if not refresh_token:
            raise ValueError("No refresh_token available. Re-run /onboard.")

        url = self.BASE_URL + "user/v1/refreshToken"
        headers = self._build_headers(tokens)
        headers["Content-Type"] = "application/json"
        # Don't send expired access_token — API returns 401 if it sees one
        headers.pop("X-VKUSVILL-TOKEN-ACCESS", None)

        client_kw: dict[str, Any] = {"timeout": self.timeout_sec}
        if self.proxy:
            client_kw["proxy"] = self.proxy
        with httpx.Client(**client_kw) as client:
            r = client.post(
                url,
                json={"refresh_token": refresh_token},
                params={"str_par": self._build_str_par(tokens)},
                headers=headers,
            )
            r.raise_for_status()
            data = r.json()

        # Handle both BaseResponse wrapper and flat response
        inner = data.get("data", data)
        new_access = inner.get("access_token") or inner.get("accessToken") or ""
        new_refresh = inner.get("refresh_token") or inner.get("refreshToken") or refresh_token
        if not new_access:
            raise ValueError(f"refreshToken returned no access_token: {data}")

        tokens["access_token"] = new_access
        tokens["refresh_token"] = new_refresh
        self._save_tokens(tokens)
        LOGGER.info("[mobile_api] tokens refreshed successfully")
        return tokens

    def fetch(self, now: datetime) -> list[DiscountItem]:
        del now
        import httpx

        tokens = self._load_tokens()
        number = tokens["number"]
        shop_no = tokens.get("shop_no", "916")
        url = self.BASE_URL + "user/privAbonement/abonementScreen"

        params = {
            "number": number,
            "source": "2",
            "shopNo": shop_no,
            "str_par": self._build_str_par(tokens),
        }
        headers = self._build_headers(tokens)

        client_kw: dict[str, Any] = {"timeout": self.timeout_sec}
        if self.proxy:
            client_kw["proxy"] = self.proxy
        with httpx.Client(**client_kw) as client:
            r = client.get(url, params=params, headers=headers)

            if r.status_code == 401:
                LOGGER.info("[mobile_api] got 401, refreshing token...")
                tokens = self._refresh_access_token(tokens)
                headers = self._build_headers(tokens)
                params["str_par"] = self._build_str_par(tokens)
                r = client.get(url, params=params, headers=headers)

            r.raise_for_status()
            data = r.json()

        return self._parse_response(data)

    def _parse_response(self, data: Any) -> list[DiscountItem]:
        """Parse abonementScreen response.

        Real structure: { items: { resources: {...}, data: [...items...] } }
        Each item: { id, title, price: { price, discount_price, discount_percent }, unit, weight_str, ... }
        """
        items_raw: list[dict[str, Any]] = []

        if isinstance(data, dict):
            items_container = data.get("items")
            if isinstance(items_container, dict):
                items_raw = items_container.get("data", [])
            elif isinstance(items_container, list):
                items_raw = items_container
            if not items_raw:
                # Fallback: try flat patterns
                for key in ("data", "products", "goods"):
                    candidate = data.get(key)
                    if isinstance(candidate, list):
                        items_raw = candidate
                        break

        if not items_raw:
            LOGGER.warning("[mobile_api] no items found, keys=%s", list(data.keys()) if isinstance(data, dict) else type(data))
            debug_path = self.token_file.parent / "mobile_api_debug_response.json"
            debug_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
            LOGGER.info("[mobile_api] raw response saved to %s", debug_path)

        result: list[DiscountItem] = []
        for item in items_raw:
            if not isinstance(item, dict):
                continue
            name = str(item.get("title") or item.get("name") or "").strip()
            if not name:
                continue

            item_id = str(item.get("id") or item.get("xml_id") or _slug(name))

            price_obj = item.get("price", {})
            if isinstance(price_obj, dict):
                price = float(price_obj.get("price", 0))
                discount_price = float(price_obj.get("discount_price", 0) or price)
            else:
                price = float(item.get("price", 0))
                discount_price = float(item.get("discount_price", 0) or price)

            # Fallback: oldPrice flat structure
            if price <= 0 and "oldPrice" in item:
                price = float(item.get("oldPrice", 0))
                discount_price = float(item.get("price", 0))

            weight = item.get("weight_str", "")
            img = str(item.get("image_url") or item.get("img") or item.get("image") or "")
            # Nested images: images[0]["images"][0]["url"]
            if not img and "images" in item and isinstance(item["images"], list) and len(item["images"]) > 0:
                first_img_group = item["images"][0]
                if isinstance(first_img_group, dict) and "images" in first_img_group:
                    sub_images = first_img_group["images"]
                    if isinstance(sub_images, list) and len(sub_images) > 0 and isinstance(sub_images[0], dict):
                        img = str(sub_images[0].get("url") or sub_images[0].get("medium") or "")

            result.append(DiscountItem(
                item_id=item_id,
                name=f"{name} ({weight})" if weight else name,
                price=max(price, discount_price),
                discount_price=min(price, discount_price) if price > 0 else discount_price,
                source="mobile_api",
                image_url=img,
            ))

        LOGGER.info("[mobile_api] parsed %d discount items", len(result))
        return result


class HttpApiProvider(BaseProvider):
    """Fetches personal VkusVill discounts via HTTP AJAX — no browser needed."""

    AJAX_URL = "https://vkusvill.ru/ajax/user_v2/cabinet/inshop_load_shop_new.php"
    PERSONAL_URL = "https://vkusvill.ru/personal/"
    READY_FOOD_URL = "https://vkusvill.ru/offers/gotovaya-eda/"
    _RE_SESSID = re.compile(r'"bitrix_sessid"\s*:\s*"([a-f0-9]+)"')
    _RE_USER_ID = re.compile(r'"user_id"\s*:\s*"(\d+)"')
    _RE_CARD = re.compile(
        r'<div[^>]*class="[^"]*js-product-cart[^"]*"[^>]*data-xmlid="(\d+)"'
    )
    _RE_FAVORITE_SLUG = re.compile(
        r'data-list-name="Любимый продукт"[\s\S]{0,3000}?data-url="(/goods/[^"]+)"'
    )
    _RE_PRODUCT_MAX = re.compile(r'data-max="(\d+)"[\s\S]{0,300}?data-xmlid="(\d+)"')

    def __init__(
        self,
        state_file: str | Path,
        waves: int = 3,
        timeout_sec: int = 20,
        proxy: str | None = None,
        wave_cache_dir: str | Path = "data",
    ) -> None:
        self.state_file = Path(state_file)
        self.waves = max(1, min(waves, 3))
        self.timeout_sec = timeout_sec
        self.proxy = proxy
        self.wave_cache_dir = Path(wave_cache_dir)

    def _wave_cache_path(self, now: datetime) -> Path:
        return self.wave_cache_dir / f"http_api_waves_{now.strftime('%Y-%m-%d')}.json"

    def _load_wave_cache(self, path: Path) -> dict[str, DiscountItem]:
        if not path.exists():
            return {}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            LOGGER.warning("[http_api] wave cache read failed: %s", e)
            return {}
        result: dict[str, DiscountItem] = {}
        for row in raw.get("items", []):
            try:
                it = DiscountItem(**row)
                result[it.item_id] = it
            except Exception:
                continue
        LOGGER.info("[http_api] resumed %d items from %s", len(result), path.name)
        return result

    def _save_wave_cache(self, path: Path, items: dict[str, DiscountItem], wave_label: str) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "saved_at": datetime.now().isoformat(),
                "last_wave": wave_label,
                "items": [it.__dict__ for it in items.values()],
            }
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            tmp.replace(path)
        except Exception as e:
            LOGGER.warning("[http_api] wave cache write failed: %s", e)

    def fetch(self, now: datetime) -> list[DiscountItem]:
        import httpx

        cookies = self._load_cookies()
        with httpx.Client(follow_redirects=True, timeout=self.timeout_sec, proxy=self.proxy) as client:
            for c in cookies:
                client.cookies.set(c["name"], c["value"], domain="vkusvill.ru")
            return self._fetch_with_client(client, now)

    def _load_cookies(self) -> list[dict[str, str]]:
        data = json.loads(self.state_file.read_text(encoding="utf-8"))
        return data["cookies"]

    def _fetch_with_client(self, client: Any, now: datetime | None = None) -> list[DiscountItem]:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        }
        ajax_headers = {
            **headers,
            "Referer": self.PERSONAL_URL,
            "X-Requested-With": "XMLHttpRequest",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        }

        r = client.get(self.PERSONAL_URL, headers=headers)
        r.raise_for_status()

        sessid_m = self._RE_SESSID.search(r.text)
        if not sessid_m:
            raise ValueError("Could not extract bitrix_sessid from /personal/")
        sessid = sessid_m.group(1)

        user_id_m = self._RE_USER_ID.search(r.text)
        if not user_id_m:
            # fallback
            user_id_m = re.search(r'USER_ID["\s:=]+(\d+)', r.text)
        if not user_id_m:
            raise ValueError("Could not determine USER_ID")
        user_id = user_id_m.group(1)

        LOGGER.info("[http_api] sessid=%s... user_id=%s", sessid[:8], user_id)

        _now = now or datetime.now()
        cache_path = self._wave_cache_path(_now)
        all_items: dict[str, DiscountItem] = self._load_wave_cache(cache_path)
        waves_done = sum(1 for it in all_items.values() if (it.source or "").startswith("vkusvill_http_w"))
        waves_done_set = {it.source for it in all_items.values() if (it.source or "").startswith("vkusvill_http_w")}
        LOGGER.info("[http_api] resumed waves: %s (items=%d)", sorted(waves_done_set), len(all_items))

        start_wave = len(waves_done_set)
        for wave in range(start_wave, self.waves):
            if wave > 0:
                LOGGER.info("[http_api] wave %d: refreshing...", wave + 1)
                r_refresh = client.post(
                    self.AJAX_URL,
                    headers=ajax_headers,
                    data={
                        "USER_ID": user_id,
                        "command": "updTovAbonement",
                        "sessid": sessid,
                    },
                )
                j_refresh = json.loads(r_refresh.text)
                if j_refresh.get("success") != "Y":
                    err = j_refresh.get("error_text") or j_refresh.get("title") or "unknown"
                    LOGGER.warning("[http_api] wave %d refresh failed: %s", wave + 1, err)
                    break
                _time.sleep(0.5)

            r_read = client.post(
                self.AJAX_URL,
                headers=ajax_headers,
                data={"USER_ID": user_id},
            )
            j_read = json.loads(r_read.text)
            if j_read.get("success") != "Y":
                LOGGER.warning("[http_api] wave %d read failed: %s", wave + 1, j_read.get("error_text", ""))
                break

            html = j_read.get("html", "")
            wave_label = f"vkusvill_http_w{wave + 1}"
            items = self._parse_cards(html, wave_label)
            new_count = 0
            for item in items:
                if item.item_id not in all_items:
                    all_items[item.item_id] = item
                    new_count += 1
            LOGGER.info("[http_api] wave %d: %d items (%d new), total=%d",
                        wave + 1, len(items), new_count, len(all_items))
            self._save_wave_cache(cache_path, all_items, wave_label)

        # Favorite product (from /personal/ → /goods/<slug>.html)
        try:
            fav_match = self._RE_FAVORITE_SLUG.search(r.text)
            if fav_match:
                fav_url = "https://vkusvill.ru" + fav_match.group(1)
                r_fav = client.get(fav_url, headers=headers)
                fav_items = self._parse_cards(r_fav.text, "vkusvill_favorite")
                for it in fav_items:
                    if it.item_id not in all_items:
                        all_items[it.item_id] = it
                LOGGER.info("[http_api] favorite: %d items", len(fav_items))
            else:
                LOGGER.info("[http_api] favorite: no marker on /personal/")
        except Exception as e:
            LOGGER.warning("[http_api] favorite fetch failed: %s", e)

        # Ready food (gotovaya-eda)
        try:
            r_food = client.get(self.READY_FOOD_URL, headers=headers)
            food_items = self._parse_cards(r_food.text, "vkusvill_offers_ready_food")
            # Enrich with per-shop stock from each item's detail page
            self._enrich_stock_qty(client, headers, food_items, r_food.text)
            for it in food_items:
                if it.item_id not in all_items:
                    all_items[it.item_id] = it
            LOGGER.info("[http_api] ready_food: %d items", len(food_items))
        except Exception as e:
            LOGGER.warning("[http_api] ready_food fetch failed: %s", e)

        return list(all_items.values())

    def _enrich_stock_qty(self, client: Any, headers: dict, items: list[DiscountItem], list_html: str) -> None:
        """Fetch each item's detail page and extract data-max as stock_qty.

        For ready-food items data-max on the product page reflects availability
        at the currently selected pickup/delivery point.
        """
        # Map xmlid -> slug. xmlid is embedded as the trailing -<id>.html in each slug.
        slug_by_xmlid: dict[str, str] = {}
        for m in re.finditer(r'data-url="(/goods/[^"]+-(\d+)\.html)"', list_html):
            slug_by_xmlid.setdefault(m.group(2), m.group(1))

        for it in items:
            xmlid = it.item_id.removeprefix("inshop_") if it.item_id.startswith("inshop_") else it.item_id
            slug = slug_by_xmlid.get(xmlid)
            if not slug:
                continue
            try:
                r = client.get(f"https://vkusvill.ru{slug}", headers=headers)
                if r.status_code != 200:
                    continue
                text = r.text
                # Detect "Только завтра" marker.
                if "Только завтра" in text or "только завтра" in text:
                    it.availability_status = "tomorrow_only"
                # Find data-max paired with our xmlid (main counter block).
                max_val: int | None = None
                paired = re.search(
                    rf'data-max="(\d+)"[\s\S]{{0,3000}}?data-xmlid="{xmlid}"',
                    text,
                )
                if paired:
                    max_val = int(paired.group(1))
                else:
                    # Fallback: if page has exactly one data-max occurrence, trust it.
                    all_max = re.findall(r'data-max="(\d+)"', text)
                    if len(all_max) == 1:
                        max_val = int(all_max[0])
                if max_val is not None:
                    # Sanity cap: ready-food items rarely have >50 units;
                    # values like 124/999 are default form maximums, not real stock.
                    if max_val > 50:
                        max_val = None
                    else:
                        it.stock_qty = max_val
            except Exception as e:
                LOGGER.debug("[http_api] stock enrich failed for %s: %s", xmlid, e)

    def _parse_cards(self, html: str, source: str) -> list[DiscountItem]:
        card_starts = list(self._RE_CARD.finditer(html))
        if not card_starts:
            return []

        items: list[DiscountItem] = []
        seen: set[str] = set()
        re_price_digits = re.compile(r'[^\d.,]')

        for i, m in enumerate(card_starts):
            start = m.start()
            end = card_starts[i + 1].start() if i + 1 < len(card_starts) else len(html)
            block = html[start:end]

            xmlid = m.group(1)
            name_m = re.search(r'js-datalayer-catalog-list-name[^>]*>([^<]+)', block)
            price_m = re.search(r'js-datalayer-catalog-list-price["\s][^>]*>([^<]+)', block)
            old_m = re.search(r'js-datalayer-catalog-list-price-old["\s][^>]*>([^<]+)', block)
            img_m = (re.search(r'<img[^>]*data-src="([^"]+)"', block)
                     or re.search(r'<img[^>]*src="([^"]+)"', block))

            if not name_m:
                continue

            name = (name_m.group(1)
                    .replace("&nbsp;", "\u00a0")
                    .replace("&amp;", "&")
                    .replace("&quot;", '"')
                    .strip())
            if not name or len(name) < 3:
                continue

            def _price(raw: str) -> float:
                c = re_price_digits.sub("", raw).replace(",", ".")
                return float(c) if c else 0.0

            price_new = _price(price_m.group(1)) if price_m else 0.0
            price_old = _price(old_m.group(1)) if old_m else 0.0
            if price_new <= 0:
                continue

            item_id = f"inshop_{xmlid}"
            if item_id in seen:
                continue
            seen.add(item_id)

            img_url = ""
            if img_m:
                u = img_m.group(1).strip()
                if u.startswith("//"):
                    u = f"https:{u}"
                elif u.startswith("/"):
                    u = f"https://vkusvill.ru{u}"
                img_url = u

            items.append(DiscountItem(
                item_id=item_id,
                name=name,
                price=max(price_old, price_new),
                discount_price=min(price_new, price_old) if price_old > 0 else price_new,
                source=source,
                image_url=img_url,
            ))

        return items


def create_provider(settings: Settings) -> BaseProvider:
    if settings.provider == "manual_json":
        return ManualJsonProvider(settings.discounts_json_path)
    if settings.provider == "mcp":
        return VkusvillMCPProvider()
    if settings.provider == "mock":
        return MockProvider()
    if settings.provider == "rpa_command":
        if not settings.rpa_command:
            raise ValueError("RPA_COMMAND is required when PROVIDER=rpa_command")
        return RPACommandProvider(settings.rpa_command, timeout_sec=settings.collect_timeout_sec)
    if settings.provider == "http_json":
        url = getattr(settings, "http_json_url", "") or ""
        if not url:
            raise ValueError("HTTP_JSON_URL is required when PROVIDER=http_json")
        return HttpJsonProvider(url)
    if settings.provider == "http_api":
        state_file = getattr(settings, "http_api_state_file", "") or "data/vkusvill_storage_state.json"
        waves = getattr(settings, "http_api_waves", 3)
        proxy = getattr(settings, "http_api_proxy", None)
        return HttpApiProvider(
            state_file=state_file,
            waves=waves,
            timeout_sec=settings.collect_timeout_sec,
            proxy=proxy,
        )
    if settings.provider == "mobile_api":
        token_file = getattr(settings, "mobile_api_token_file", "") or "data/mobile_tokens.json"
        proxy = getattr(settings, "mobile_api_proxy", None)
        return MobileApiProvider(
            token_file=token_file,
            timeout_sec=settings.collect_timeout_sec,
            proxy=proxy,
        )
    raise ValueError(f"Unsupported provider: {settings.provider}")
