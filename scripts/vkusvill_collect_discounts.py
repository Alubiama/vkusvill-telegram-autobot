from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import re
import shutil
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import quote_plus

from playwright.sync_api import sync_playwright


RUB_RE = re.compile(r"(\d[\d\s]*[.,]?\d*)\s*(?:₽|руб|в‚Ѕ|СЂСѓР±)", re.IGNORECASE)
STOCK_QTY_RE = re.compile(r"(?:в\s*наличии|осталось)\s*[:\\-]?\s*(\d{1,4})\s*шт", re.IGNORECASE)


@dataclass
class DiscountItem:
    item_id: str
    name: str
    price: float
    discount_price: float
    source: str
    image_url: str = ""
    stock_qty: int | None = None
    availability_status: str = "unknown"
    availability_reason: str = ""

    def as_dict(self) -> dict:
        return {
            "item_id": self.item_id,
            "name": self.name,
            "price": self.price,
            "discount_price": self.discount_price,
            "source": self.source,
            "image_url": self.image_url,
            "stock_qty": self.stock_qty,
            "availability_status": self.availability_status,
            "availability_reason": self.availability_reason,
        }


def _normalize_ws(value: str) -> str:
    # Normalize NBSP and repeated spaces to make text checks robust.
    return " ".join(value.replace("\xa0", " ").split())


def _abort(message: str, code: int = 1) -> None:
    print(message, file=sys.stderr)
    raise SystemExit(code)


def _goto_with_retry(page, url: str, *, wait_until: str = "domcontentloaded", timeout: int = 120_000, attempts: int = 3):
    last_exc: Exception | None = None
    for idx in range(max(1, attempts)):
        try:
            return page.goto(url, wait_until=wait_until, timeout=timeout)
        except Exception as exc:
            last_exc = exc
            if idx >= attempts - 1:
                break
            page.wait_for_timeout(1500 * (idx + 1))
    if last_exc is not None:
        raise last_exc


def _response_status_code(response: object) -> int | None:
    status = getattr(response, "status", None)
    if status is None:
        return None
    try:
        return int(status)
    except (TypeError, ValueError):
        return None


def _ensure_disk_headroom(path: Path, *, min_free_mb: int = 500) -> None:
    anchor = path if path.exists() else path.parent
    if not anchor.exists():
        anchor = anchor.parent
    usage = shutil.disk_usage(anchor)
    free_mb = usage.free // (1024 * 1024)
    if free_mb < min_free_mb:
        _abort(
            f"[collector] ABORT: disk space low: {free_mb}mb free on {anchor.drive or anchor}",
            code=2,
        )


def _parse_price(token: str) -> float:
    normalized = token.replace(" ", "").replace(",", ".")
    return float(normalized)


def _extract_stock_qty(text: str) -> int | None:
    if not text:
        return None
    normalized = _normalize_ws(text).lower()
    match = STOCK_QTY_RE.search(normalized)
    if not match:
        return None
    try:
        value = int(match.group(1))
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


def _coalesce_stock_qty(*values: object) -> int | None:
    for value in values:
        if value in (None, ""):
            continue
        try:
            qty = int(value)
        except (TypeError, ValueError):
            continue
        if qty >= 0:
            return qty
    return None


def _stock_qty_from_text(*values: object) -> int | None:
    merged = " ".join(str(value or "") for value in values if value not in (None, ""))
    return _extract_stock_qty(merged)


def _looks_unavailable_text(*values: object) -> bool:
    markers = (
        "не осталось",
        "нет в наличии",
        "нет вналичии",
        "раскупили",
        "закончил",
        "закончилось",
        "закончился",
        "скоро появ",
        "недоступ",
        "нет товара",
    )
    for value in values:
        text = _normalize_ws(str(value or "")).lower()
        if text and any(marker in text for marker in markers):
            return True
    return False


def _item_id(name: str) -> str:
    return hashlib.sha1(name.strip().lower().encode("utf-8")).hexdigest()[:16]


def _normalize_image_url(value: str) -> str:
    raw = (value or "").strip()
    if not raw:
        return ""
    if raw.startswith("//"):
        return f"https:{raw}"
    if raw.startswith("/"):
        return f"https://vkusvill.ru{raw}"
    return raw


def _needs_image_backfill(value: str) -> bool:
    raw = (value or "").strip().lower()
    return (not raw) or ("no-image.svg" in raw)


def _name_tokens(value: str) -> list[str]:
    norm = _normalize_ws(value).lower().replace("ё", "е")
    norm = re.sub(r"[\"'`“”„()\[\]{}:;,.!?/+\\-]", " ", norm)
    return [x for x in norm.split() if len(x) >= 3]


def _best_image_match_from_search(page, name: str) -> tuple[str, bool]:
    _goto_with_retry(
        page,
        f"https://vkusvill.ru/search/?q={quote_plus(name)}",
        wait_until="domcontentloaded",
        timeout=120_000,
    )
    page.wait_for_timeout(1100)
    rows: list[dict[str, str]] = []
    cards = page.locator(".js-datalayer-catalog-list-item[data-xmlid]")
    total = min(cards.count(), 18)
    for idx in range(total):
        card = cards.nth(idx)
        try:
            if not card.is_visible():
                continue
        except Exception:
            continue
        raw_name = ""
        try:
            raw_name = (card.locator(".js-datalayer-catalog-list-name").first.inner_text(timeout=500) or "").strip()
        except Exception:
            raw_name = ""
        if not raw_name:
            continue
        raw_image = ""
        try:
            img = card.locator("img").first
            raw_image = (img.get_attribute("src", timeout=500) or img.get_attribute("data-src", timeout=500) or "").strip()
        except Exception:
            raw_image = ""
        if not raw_image:
            continue
        rows.append({"name": raw_name, "image": raw_image})
    if not rows:
        return "", False

    query_tokens = set(_name_tokens(name))
    name_norm = _normalize_ws(name).lower().replace("ё", "е")
    best_score = -1
    best_image = ""
    exact_match = False
    for row in rows:
        rn = _normalize_ws(str(row.get("name") or "")).lower().replace("ё", "е")
        if not rn:
            continue
        rt = set(_name_tokens(rn))
        score = len(query_tokens.intersection(rt)) * 2
        if name_norm in rn or rn in name_norm:
            score += 3
        is_exactish = (
            rn == name_norm
            or rn.startswith(f"{name_norm},")
            or rn.startswith(f"{name_norm} ")
            or (name_norm in rn and query_tokens and query_tokens.issubset(rt))
        )
        if is_exactish:
            score += 4
        if score > best_score:
            best_score = score
            best_image = str(row.get("image") or "")
            exact_match = is_exactish

    image = _normalize_image_url(best_image)
    if _needs_image_backfill(image):
        return "", False
    return image, exact_match


def _repair_item_images(page, items: list[DiscountItem], max_items: int = 16) -> tuple[int, int]:
    backfilled = 0
    corrected = 0
    checked = 0
    for item in items:
        if checked >= max_items:
            break
        source = str(item.source or "").lower()
        should_verify = _needs_image_backfill(item.image_url) or ("offers_ready_food" not in source)
        if not should_verify:
            continue
        checked += 1
        try:
            image, exact_match = _best_image_match_from_search(page, item.name)
        except Exception:
            continue
        if not image:
            continue
        current = _normalize_image_url(str(item.image_url or ""))
        if _needs_image_backfill(current):
            item.image_url = image
            backfilled += 1
            continue
        if exact_match and current != image:
            item.image_url = image
            corrected += 1
    return backfilled, corrected


def _log(message: str) -> None:
    # Keep stdout clean for JSON output consumed by the bot provider.
    print(message, file=sys.stderr, flush=True)


def _extract_user_id_from_post_data(post_data: str) -> str:
    if not post_data:
        return ""
    m = re.search(r"(?:^|&)USER_ID=(\d+)(?:&|$)", post_data)
    return m.group(1) if m else ""


def _extract_delivery_hint(page) -> str:
    text = page.evaluate(
        """
        () => {
          const norm = (s) => (s || '').replace(/\\u00a0/g, ' ').replace(/\\s+/g, ' ').trim();
          const modalAddress =
            document.querySelector('#js-lk-modal-inshop-detail .VV22_LKSalesModal__Address_Selected_Text') ||
            document.querySelector('#js-lk-modal-inshop-detail .VV22_LKSalesModal__Address_Selected');
          if (modalAddress) {
            const ttl = norm(modalAddress.getAttribute('title') || '');
            const txt = norm(modalAddress.innerText || '');
            if (ttl) return ttl;
            if (txt) return txt;
          }
          const selectors = [
            '.js-delivery__shopselect--form-show',
            '.HeaderATDToggler__Link',
            '[class*="delivery"][class*="shop"]',
            '[class*="delivery"][class*="select"]',
            '[data-testid*="delivery"]',
          ];
          const candidates = [];
          for (const sel of selectors) {
            for (const el of document.querySelectorAll(sel)) {
              const txt = norm(el.innerText || '');
              if (!txt) continue;
              if (txt.length < 3) continue;
              candidates.push(txt);
            }
          }
          if (candidates.length) return candidates[0];
          return '';
        }
        """
    )
    return _normalize_ws(str(text or ""))


def _assert_delivery_hint(page, expected_hint: str, strict: bool) -> None:
    actual = _extract_delivery_hint(page)
    if not actual:
        _log("[collector] delivery hint is not visible on page")
        if strict and expected_hint:
            _save_debug(page, "delivery_hint_missing")
            raise SystemExit("Delivery hint is not visible; cannot verify location.")
        return

    _log(f"[collector] delivery context: {actual}")
    if not expected_hint:
        return

    expected_norm = _normalize_ws(expected_hint).lower()
    actual_norm = actual.lower()
    if expected_norm in actual_norm:
        _log(f"[collector] delivery check OK: expected '{expected_hint}'")
        return

    msg = (
        "Delivery location mismatch. "
        f"Expected hint '{expected_hint}', got '{actual}'."
    )
    if strict:
        _save_debug(page, "delivery_hint_mismatch")
        raise SystemExit(msg)
    _log(f"[collector] WARNING: {msg}")


def _is_favorite_marker(text: str) -> bool:
    lowered = _normalize_ws(text).lower()
    markers = [
        "подобрали для вас",
        "назначить новый",
        "любимый продукт",
        "любимый товар",
        "рїрѕрґрѕр±сђр°р»рё рґр»сџ рір°сс",
        "рѕр°р·рѕр°с‡рёс‚сњ рѕрѕріс‹р№",
    ]
    return any(marker in lowered for marker in markers)


def _collect_from_dom(page, source: str) -> list[DiscountItem]:
    raw_cards = page.evaluate(
        """
        () => {
          const selectors = [
            '.js-product-cart',
            '.lk-specials-col__lp-with-prod',
            '[data-testid*="product"]',
            '[class*="ProductCard"]',
            '[class*="product-card"]',
            '[class*="productCard"]',
            'article',
          ];
          const nodes = [];
          const collectStockText = (el) => {
            if (!el) return '';
            const stockPattern = /(?:в\\s*наличии|осталось)\\s*[:\\-]?\\s*\\d{1,4}\\s*шт\\.?/i;
            const candidates = [
              '[class*="stock"]',
              '[class*="avail"]',
              '[class*="presence"]',
              '[class*="amount"]',
              '[class*="qty"]',
              '[data-testid*="stock"]',
              '[data-testid*="avail"]',
            ];
            const texts = [];
            for (const sel of candidates) {
              for (const node of el.querySelectorAll(sel)) {
                const txt = (node.innerText || node.textContent || '').trim();
                if (!txt) continue;
                texts.push(txt);
              }
            }
            if (!texts.length) {
              let probe = el;
              for (let depth = 0; depth < 4 && probe; depth += 1, probe = probe.parentElement) {
                const full = (probe.innerText || probe.textContent || '').trim();
                const match = full.match(stockPattern);
                if (match) {
                  texts.push(match[0]);
                  break;
                }
              }
            }
            return texts.join(' | ');
          };
          for (const s of selectors) {
            for (const el of document.querySelectorAll(s)) {
              nodes.push(el);
            }
          }
          const uniq = Array.from(new Set(nodes));
          return uniq.map((el) => ({
            text: (el.innerText || '').trim(),
            name: (
              el.querySelector('.lk-specials-col__lp-with-prod--name-text')?.innerText ||
              el.querySelector('[class*=\"name\"]')?.innerText ||
              ''
            ).trim(),
            newPrice: (
              el.querySelector('.lk-specials-col__lp-with-prod--price-new')?.innerText ||
              ''
            ).trim(),
            oldPrice: (
              el.querySelector('.lk-specials-col__lp-with-prod--price-old')?.innerText ||
              ''
            ).trim(),
            image: (
              el.querySelector('img')?.getAttribute('src') ||
              el.querySelector('img')?.getAttribute('data-src') ||
              ''
            ).trim(),
            stockText: collectStockText(el),
            maxQty: (
              el.querySelector('[data-max]')?.getAttribute('data-max') ||
              el.querySelector('input[data-max]')?.getAttribute('data-max') ||
              ''
            ).trim(),
          }));
        }
        """
    )

    items: list[DiscountItem] = []
    seen_names: set[str] = set()
    for card in raw_cards:
        text = (card.get("text") or "").strip()
        if len(text) < 8:
            continue

        is_favorite = _is_favorite_marker(text)

        name = (card.get("name") or "").strip()
        if not name:
            lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
            # Skip rating/date short lines and pick first substantial line.
            name = ""
            for ln in lines:
                if len(ln) < 4:
                    continue
                if RUB_RE.search(ln):
                    continue
                if "/" in ln and "С€С‚" in ln.lower():
                    continue
                name = ln
                break
        if len(name) < 3 or len(name) > 180:
            continue
        if _is_favorite_marker(name) and not is_favorite:
            continue

        price_tokens = []
        for key in ("newPrice", "oldPrice"):
            val = (card.get(key) or "").strip()
            if val:
                price_tokens.extend(RUB_RE.findall(val))
        if not price_tokens:
            price_tokens = RUB_RE.findall(text)

        prices = []
        for x in price_tokens:
            cleaned = x.replace(" ", "").replace(",", "").replace(".", "")
            if not any(ch.isdigit() for ch in x) or len(cleaned) < 2:
                continue
            try:
                prices.append(_parse_price(x))
            except Exception:
                continue

        prices = [p for p in prices if 5 <= p <= 10000]
        if not prices:
            continue

        discount = min(prices)
        regular = max(prices)

        key = name.lower()
        if key in seen_names:
            continue
        seen_names.add(key)

        items.append(
            DiscountItem(
                item_id=_item_id(name),
                name=name,
                price=regular,
                discount_price=discount,
                source=f"{source}_favorite" if is_favorite else source,
                image_url=_normalize_image_url(str(card.get("image") or "")),
                stock_qty=_stock_qty_from_text(text, card.get("stockText")),
            )
        )

    return items


def _collect_from_inshop_modal(page, source: str) -> list[DiscountItem]:
    raw_cards = page.evaluate(
        """
        () => {
          const norm = (s) => (s || '').replace(/\\u00a0/g, ' ').replace(/\\s+/g, ' ').trim();
          const collectStockText = (el) => {
            if (!el) return '';
            const stockPattern = /(?:в\\s*наличии|осталось)\\s*[:\\-]?\\s*\\d{1,4}\\s*шт\\.?/i;
            const candidates = [
              '[class*="stock"]',
              '[class*="avail"]',
              '[class*="presence"]',
              '[class*="amount"]',
              '[class*="qty"]',
              '[data-testid*="stock"]',
              '[data-testid*="avail"]',
            ];
            const texts = [];
            for (const sel of candidates) {
              for (const node of el.querySelectorAll(sel)) {
                const txt = norm(node.innerText || node.textContent || '');
                if (!txt) continue;
                texts.push(txt);
              }
            }
            if (!texts.length) {
              let probe = el;
              for (let depth = 0; depth < 4 && probe; depth += 1, probe = probe.parentElement) {
                const full = norm(probe.innerText || probe.textContent || '');
                const match = full.match(stockPattern);
                if (match) {
                  texts.push(match[0]);
                  break;
                }
              }
            }
            return texts.join(' | ');
          };
          const root =
            document.querySelector('#js-lk-modal-inshop-detail .VV_SegmentedControl__Segment._online._active') ||
            document.querySelector('#js-lk-modal-inshop-detail .VV_SegmentedControl__Segment._online') ||
            document.querySelector('#js-lk-modal-inshop-detail');
          if (!root) return [];
          const cards = Array.from(root.querySelectorAll('.VV22_LKSalesModal__ProdTizers .js-product-cart[data-xmlid]'));
          return cards.map((el) => ({
            xmlid: (el.getAttribute('data-xmlid') || '').trim(),
            text: norm(el.innerText || ''),
            name: norm((el.querySelector('.js-datalayer-catalog-list-name') || {}).innerText || ''),
            newPrice: norm((el.querySelector('.js-datalayer-catalog-list-price') || {}).innerText || ''),
            oldPrice: norm((el.querySelector('.js-datalayer-catalog-list-price-old') || {}).innerText || ''),
            image: norm(
              (el.querySelector('img') || {}).getAttribute?.('src') ||
              (el.querySelector('img') || {}).getAttribute?.('data-src') ||
              ''
            ),
            stockText: collectStockText(el),
            maxQty: norm(
              (el.querySelector('[data-max]') || {}).getAttribute?.('data-max') ||
              (el.querySelector('input[data-max]') || {}).getAttribute?.('data-max') ||
              ''
            ),
          }));
        }
        """
    )

    items: list[DiscountItem] = []
    seen_ids: set[str] = set()
    for card in raw_cards:
        xmlid = str(card.get("xmlid") or "").strip()
        text = (card.get("text") or "").strip()
        name = (card.get("name") or "").strip()
        if not xmlid or not text or not name:
            continue

        price_tokens = []
        for key in ("newPrice", "oldPrice"):
            val = (card.get(key) or "").strip()
            if val:
                price_tokens.extend(RUB_RE.findall(val))
        if not price_tokens:
            price_tokens = RUB_RE.findall(text)

        prices = []
        for token in price_tokens:
            try:
                prices.append(_parse_price(token))
            except Exception:
                continue
        prices = [p for p in prices if 5 <= p <= 10000]
        if not prices:
            continue

        discount = min(prices)
        regular = max(prices)
        if regular <= discount:
            continue

        item_id = f"inshop_{xmlid}"
        if item_id in seen_ids:
            continue
        seen_ids.add(item_id)
        items.append(
            DiscountItem(
                item_id=item_id,
                name=name,
                price=regular,
                discount_price=discount,
                source=source,
                image_url=_normalize_image_url(str(card.get("image") or "")),
                stock_qty=_stock_qty_from_text(text, card.get("stockText")),
            )
        )

    return items


def _collect_favorite_from_personal(page, source: str) -> list[DiscountItem]:
    raw_cards = page.evaluate(
        """
        () => {
          const norm = (s) => (s || '').replace(/\\u00a0/g, ' ').replace(/\\s+/g, ' ').trim();
          const collectStockText = (el) => {
            if (!el) return '';
            const stockPattern = /(?:в\\s*наличии|осталось)\\s*[:\\-]?\\s*\\d{1,4}\\s*шт\\.?/i;
            const candidates = [
              '[class*="stock"]',
              '[class*="avail"]',
              '[class*="presence"]',
              '[class*="amount"]',
              '[class*="qty"]',
              '[data-testid*="stock"]',
              '[data-testid*="avail"]',
            ];
            const texts = [];
            for (const sel of candidates) {
              for (const node of el.querySelectorAll(sel)) {
                const txt = norm(node.innerText || node.textContent || '');
                if (!txt) continue;
                texts.push(txt);
              }
            }
            if (!texts.length) {
              let probe = el;
              for (let depth = 0; depth < 4 && probe; depth += 1, probe = probe.parentElement) {
                const full = norm(probe.innerText || probe.textContent || '');
                const match = full.match(stockPattern);
                if (match) {
                  texts.push(match[0]);
                  break;
                }
              }
            }
            return texts.join(' | ');
          };
          const cards = Array.from(document.querySelectorAll('.lk-specials-col__lp-with-prod[data-xmlid], .lk-specials-col__lp-with-prod'));
          return cards.map((el) => ({
            xmlid: (el.getAttribute('data-xmlid') || '').trim(),
            text: norm(el.innerText || ''),
            name: norm(
              (el.querySelector('.lk-specials-col__lp-with-prod--name-text') || {}).innerText ||
              (el.querySelector('.js-datalayer-catalog-list-name') || {}).innerText ||
              ''
            ),
            newPrice: norm((el.querySelector('.lk-specials-col__lp-with-prod--price-new') || {}).innerText || ''),
            oldPrice: norm((el.querySelector('.lk-specials-col__lp-with-prod--price-old') || {}).innerText || ''),
            image: norm(
              (el.querySelector('img') || {}).getAttribute?.('src') ||
              (el.querySelector('img') || {}).getAttribute?.('data-src') ||
              ''
            ),
            stockText: collectStockText(el),
            maxQty: norm(
              (el.querySelector('[data-max]') || {}).getAttribute?.('data-max') ||
              (el.querySelector('input[data-max]') || {}).getAttribute?.('data-max') ||
              ''
            ),
          }));
        }
        """
    )

    items: list[DiscountItem] = []
    for card in raw_cards:
        text = (card.get("text") or "").strip()
        name = (card.get("name") or "").strip()
        if not text or not name:
            continue
        if not _is_favorite_marker(text):
            continue

        price_tokens = []
        for key in ("newPrice", "oldPrice"):
            val = (card.get(key) or "").strip()
            if val:
                price_tokens.extend(RUB_RE.findall(val))
        if not price_tokens:
            price_tokens = RUB_RE.findall(text)

        prices = []
        for token in price_tokens:
            try:
                prices.append(_parse_price(token))
            except Exception:
                continue
        prices = [p for p in prices if 5 <= p <= 10000]
        if not prices:
            continue
        discount = min(prices)
        regular = max(prices)
        if regular <= discount:
            continue

        xmlid = str(card.get("xmlid") or "").strip()
        item_id = f"fav_{xmlid}" if xmlid else _item_id(name)
        items.append(
            DiscountItem(
                item_id=item_id,
                name=name,
                price=regular,
                discount_price=discount,
                source=f"{source}_favorite",
                image_url=_normalize_image_url(str(card.get("image") or "")),
                stock_qty=_stock_qty_from_text(text, card.get("stockText")),
            )
        )
        break

    return items


def _modal_fingerprint(page) -> str:
    return str(
        page.evaluate(
            """
            () => {
              const root =
                document.querySelector('#js-lk-modal-inshop-detail .VV_SegmentedControl__Segment._online._active') ||
                document.querySelector('#js-lk-modal-inshop-detail .VV_SegmentedControl__Segment._online') ||
                document.querySelector('#js-lk-modal-inshop-detail');
              if (!root) return '';
              const cards = Array.from(root.querySelectorAll('.VV22_LKSalesModal__ProdTizers .js-product-cart[data-xmlid]'));
              const rows = cards.map((el) => {
                const xmlid = (el.getAttribute('data-xmlid') || '').trim();
                const name = ((el.querySelector('.js-datalayer-catalog-list-name') || {}).innerText || '').replace(/\\u00a0/g, ' ').replace(/\\s+/g, ' ').trim();
                return `${xmlid}:${name}`;
              });
              return rows.join('|');
            }
            """
        )
        or ""
    )


def _collect_offers_ready_food(page, url: str, max_items: int) -> list[DiscountItem]:
    try:
        response = _goto_with_retry(page, url, wait_until="domcontentloaded", timeout=120_000)
    except Exception as exc:
        _log(f"[collector] ready food skipped: {exc}")
        return []
    status_code = _response_status_code(response)
    if status_code is not None and status_code >= 400:
        _log(f"[collector] ready food skipped: HTTP {status_code}")
        return []
    page.wait_for_timeout(2200)

    # Category page uses lazy loading. Scroll several times to load more cards.
    # max_items <= 0 means "no hard cap".
    target_min_count = max_items if max_items > 0 else 10_000_000
    previous_count = 0
    stable_rounds = 0
    for _ in range(10):
        visible_count = int(
            page.evaluate(
                """
                () => {
                  let cards = Array.from(
                    document.querySelectorAll('.ProductsSection .ProductCards__list .js-datalayer-catalog-list-item[data-xmlid]')
                  );
                  if (!cards.length) {
                    cards = Array.from(document.querySelectorAll('.js-datalayer-catalog-list-item[data-xmlid]'))
                      .filter((el) => !el.closest('.VV23_6ProdsAuthorizedSlider, .VV23_6ProdsAuthorized, .swiper-container'));
                  }
                  return cards.length;
                }
                """
            )
            or 0
        )
        if visible_count <= previous_count:
            stable_rounds += 1
        else:
            stable_rounds = 0
        previous_count = max(previous_count, visible_count)
        if stable_rounds >= 2 and visible_count >= target_min_count:
            break
        page.evaluate("() => window.scrollBy(0, Math.max(1000, window.innerHeight * 0.9))")
        page.wait_for_timeout(850)
    _log(f"[collector] offers ready food: visible cards after scroll={previous_count}")

    raw = page.evaluate(
        """
        () => {
          const norm = (s) => (s || '').replace(/\\u00a0/g, ' ').replace(/\\s+/g, ' ').trim();
          const collectStockText = (el) => {
            if (!el) return '';
            const stockPattern = /(?:в\\s*наличии|осталось)\\s*[:\\-]?\\s*\\d{1,4}\\s*шт\\.?/i;
            const candidates = [
              '[class*="stock"]',
              '[class*="avail"]',
              '[class*="presence"]',
              '[class*="amount"]',
              '[class*="qty"]',
              '[data-testid*="stock"]',
              '[data-testid*="avail"]',
            ];
            const texts = [];
            for (const sel of candidates) {
              for (const node of el.querySelectorAll(sel)) {
                const txt = norm(node.innerText || node.textContent || '');
                if (!txt) continue;
                texts.push(txt);
              }
            }
            if (!texts.length) {
              let probe = el;
              for (let depth = 0; depth < 4 && probe; depth += 1, probe = probe.parentElement) {
                const full = norm(probe.innerText || probe.textContent || '');
                const match = full.match(stockPattern);
                if (match) {
                  texts.push(match[0]);
                  break;
                }
              }
            }
            const buttonLabels = Array.from(el.querySelectorAll('button, [role="button"]'))
              .map((node) => norm(node.getAttribute?.('aria-label') || node.innerText || ''))
              .filter(Boolean);
            return texts.concat(buttonLabels).join(' | ');
          };
            // Prefer the main catalog grid inside the category section.
            // This excludes personalized sliders that can appear above the list
            // (e.g. "6 товаров" block) and pollute the first items.
          let cards = Array.from(
            document.querySelectorAll('.ProductsSection .ProductCards__list .js-datalayer-catalog-list-item[data-xmlid]')
          );
          if (!cards.length) {
            // Fallback for layout variants, but explicitly skip known promo sliders.
            cards = Array.from(document.querySelectorAll('.js-datalayer-catalog-list-item[data-xmlid]'))
              .filter((el) => !el.closest('.VV23_6ProdsAuthorizedSlider, .VV23_6ProdsAuthorized, .swiper-container'));
          }
          const rows = [];
          for (const el of cards) {
            const xmlid = (el.getAttribute('data-xmlid') || '').trim();
            if (!xmlid) continue;
            const name = norm((el.querySelector('.js-datalayer-catalog-list-name') || {}).innerText || '');
            const priceNew = norm((el.querySelector('.js-datalayer-catalog-list-price') || {}).innerText || '');
            const priceOld = norm((el.querySelector('.js-datalayer-catalog-list-price-old') || {}).innerText || '');
            const image = norm(
              (el.querySelector('img') || {}).getAttribute?.('src') ||
              (el.querySelector('img') || {}).getAttribute?.('data-src') ||
              ''
            );
            const addBtn =
              el.querySelector('.js-delivery__basket--add') ||
              el.querySelector('button[class*="basket"]') ||
              el.querySelector('[class*="basket"][role="button"]');
            const plusBtn =
              el.querySelector('.Q_Up') ||
              el.querySelector('.js-delivery__product__q-btn.Q_Up');
            rows.push({
              xmlid,
              name,
              priceNew,
              priceOld,
              image,
              text: norm(el.innerText || ''),
                addText: norm((addBtn || {}).innerText || (addBtn || {}).getAttribute?.('aria-label') || ''),
                addClass: norm((addBtn || {}).className || ''),
                plusClass: norm((plusBtn || {}).className || ''),
                hasAddButton: !!addBtn,
                hasPlusButton: !!plusBtn,
                stockText: collectStockText(el),
                maxQty: norm(
                  (addBtn || {}).getAttribute?.('data-max') ||
                  (el.querySelector('[data-max]') || {}).getAttribute?.('data-max') ||
                  ''
                ),
              });
            }
            return rows;
          }
          """
    )

    items: list[DiscountItem] = []
    seen: set[str] = set()
    skipped_unavailable = 0
    for row in raw:
        xmlid = str(row.get("xmlid") or "").strip()
        name = str(row.get("name") or "").strip()
        if not xmlid or not name:
            continue
        row_text = _normalize_ws(str(row.get("text") or "")).lower()
        loyalty_markers = ("по карте", "скидка по карте", "лояльности")
        if not any(marker in row_text for marker in loyalty_markers):
            continue

        add_text = _normalize_ws(str(row.get("addText") or "")).lower()
        add_class = _normalize_ws(str(row.get("addClass") or "")).lower()
        plus_class = _normalize_ws(str(row.get("plusClass") or "")).lower()
        has_add_button = bool(row.get("hasAddButton"))
        has_plus_button = bool(row.get("hasPlusButton"))
        tomorrow_markers = (
            "завтра",
            "доставить завтра",
            "only online add",
            "only-online-add",
            "tomorrow",
        )
        looks_unavailable = _looks_unavailable_text(row_text, add_text)
        requires_tomorrow = (
            any(marker in row_text for marker in tomorrow_markers)
            or any(marker in add_text for marker in tomorrow_markers)
            or any(marker in add_class for marker in tomorrow_markers)
            or any(marker in plus_class for marker in tomorrow_markers)
        )
        has_action = has_add_button or has_plus_button
        if requires_tomorrow or (not has_action and not looks_unavailable):
            skipped_unavailable += 1
            continue

        prices = RUB_RE.findall(f"{row.get('priceNew') or ''} {row.get('priceOld') or ''} {row.get('text') or ''}")
        parsed: list[float] = []
        for token in prices:
            try:
                parsed.append(_parse_price(token))
            except Exception:
                continue
        parsed = [p for p in parsed if 5 <= p <= 10000]
        if not parsed:
            continue
        discount = min(parsed)
        regular = max(parsed)
        if regular <= discount:
            continue

        item_id = f"offers_{xmlid}"
        if item_id in seen:
            continue
        seen.add(item_id)
        items.append(
            DiscountItem(
                item_id=item_id,
                name=name,
                price=regular,
                discount_price=discount,
                source="vkusvill_offers_ready_food",
                image_url=_normalize_image_url(str(row.get("image") or "")),
                stock_qty=(
                    0
                    if looks_unavailable
                    else _stock_qty_from_text(row.get("text"), row.get("stockText"))
                ),
            )
        )
        if max_items > 0 and len(items) >= max_items:
            break
    if skipped_unavailable:
        _log(f"[collector] offers ready food: skipped unavailable={skipped_unavailable}")
    return items


def _merge_items_unique(base: list[DiscountItem], extra: list[DiscountItem]) -> list[DiscountItem]:
    merged: dict[str, DiscountItem] = {}

    for item in base:
        merged[item.item_id] = item

    for item in extra:
        if item.item_id in merged:
            continue
        merged[item.item_id] = item

    return list(merged.values())


def _merge_items_latest(base: list[DiscountItem], extra: list[DiscountItem]) -> list[DiscountItem]:
    merged: dict[str, DiscountItem] = {}

    for item in base:
        merged[item.item_id] = item

    for item in extra:
        merged[item.item_id] = item

    return list(merged.values())


def _is_ready_food_source(source: str) -> bool:
    return str(source or "").lower().startswith("vkusvill_offers_ready_food")


def _load_existing_items(out_path: Path, *, run_day: str | None = None) -> list[DiscountItem]:
    if not out_path.exists():
        return []
    if run_day:
        try:
            file_day = datetime.fromtimestamp(out_path.stat().st_mtime).strftime("%Y-%m-%d")
        except OSError:
            file_day = ""
        if file_day != run_day:
            _log(f"[collector] ignoring stale existing out-file: file_day={file_day or 'n/a'} run_day={run_day}")
            return []
    try:
        raw = json.loads(out_path.read_text(encoding="utf-8"))
    except Exception:
        return []
    items: list[DiscountItem] = []
    for row in raw if isinstance(raw, list) else []:
        try:
            items.append(
                DiscountItem(
                    item_id=str(row.get("item_id") or ""),
                    name=str(row.get("name") or ""),
                    price=float(row.get("price") or 0),
                    discount_price=float(row.get("discount_price") or 0),
                    source=str(row.get("source") or ""),
                    image_url=_normalize_image_url(str(row.get("image_url") or "")),
                    stock_qty=_coalesce_stock_qty(row.get("stock_qty")),
                )
            )
        except Exception:
            continue
    return [item for item in items if item.item_id and item.name and item.discount_price > 0]


def _pool_paths(out_path: Path) -> tuple[Path, Path]:
    return out_path.with_name("today_pool.json"), out_path.with_name("today_pool_date.txt")


def _load_today_pool(out_path: Path, run_day: str) -> list[DiscountItem]:
    pool_path, date_path = _pool_paths(out_path)
    if not pool_path.exists() or not date_path.exists():
        return []
    try:
        stored_day = date_path.read_text(encoding="utf-8").strip()
    except Exception:
        return []
    if stored_day != run_day:
        return []
    return _load_existing_items(pool_path)


def _write_today_pool(out_path: Path, run_day: str, items: list[DiscountItem]) -> None:
    pool_path, date_path = _pool_paths(out_path)
    payload = [item.as_dict() for item in items]
    pool_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    date_path.write_text(run_day, encoding="utf-8")


def _open_discounts_area(page) -> None:
    _goto_with_retry(page, "https://vkusvill.ru/personal/", wait_until="domcontentloaded", timeout=120_000)
    page.wait_for_timeout(1800)

    detail_btn = page.locator(".js-lk-inshop-show-detail")
    if detail_btn.count() <= 0:
        return

    try:
        with page.expect_response(
            lambda r: "inshop_load_shop_new.php" in r.url and "USER_ID=" in (r.request.post_data or ""),
            timeout=20_000,
        ):
            detail_btn.first.click()
    except Exception:
        try:
            detail_btn.first.click()
        except Exception:
            return
    try:
        page.wait_for_selector("#js-lk-modal-inshop-detail .js-inshop-update", timeout=12_000)
    except Exception:
        page.wait_for_timeout(2200)


def _refresh_api_status(page) -> tuple[str, str]:
    """Return refresh API result status and optional message."""
    try:
        resp = page.wait_for_response(
            lambda r: "inshop_load_shop_new.php" in r.url
            and "command=updTovAbonement" in ((r.request.post_data or "")),
            timeout=16_000,
        )
    except Exception:
        return "unknown", ""

    data = {}
    try:
        data = resp.json()
    except Exception:
        try:
            data = json.loads(resp.text())
        except Exception:
            return "unknown", ""

    ok = str(data.get("success", "")).upper() == "Y"
    if ok:
        return "success", ""

    err = _normalize_ws(str(data.get("error_text", "")))
    title = _normalize_ws(str(data.get("title", "")))
    msg = err or title
    if _is_hard_limit_message(msg):
        return "limit", msg
    return "rejected", msg


def _is_hard_limit_message(message: str) -> bool:
    lowered = _normalize_ws(str(message or "")).lower()
    if not lowered:
        return False

    # Common informative hint shown in modal even when the action is available.
    # Do not treat this phrase alone as a hard limit.
    soft_hint = "если товары не подошли, обновить подборку товаров можно до 2 раз в день"
    if soft_hint in lowered:
        hard_suffixes = ("уже", "сегодня", "исчерпан", "нельзя", "недоступно", "попробуйте завтра")
        return any(marker in lowered for marker in hard_suffixes)

    hard_markers = (
        "лимит",
        "уже обновляли",
        "уже заменяли",
        "исчерпан",
        "попробуйте завтра",
        "нельзя обновить",
        "недоступно",
        "не более 2 раз",
    )
    return any(marker in lowered for marker in hard_markers)


def _click_refresh_discounts(page) -> tuple[bool, bool, bool]:
    before_fp = _modal_fingerprint(page)
    api_status = "unknown"
    api_msg = ""
    try:
        with page.expect_response(
            lambda r: "inshop_load_shop_new.php" in r.url
            and "command=updTovAbonement" in ((r.request.post_data or "")),
            timeout=18_000,
        ) as response_info:
            clicked = page.evaluate(
                """
                () => {
                  const btn = document.querySelector('#js-lk-modal-inshop-detail .js-inshop-update');
                  if (!btn) return false;
                  btn.click();
                  return true;
                }
                """
            )
            if not clicked:
                _log("[collector] refresh button not found in inshop modal")
                return False, False, False
            _log("[collector] refresh click sent (inshop modal)")
            page.wait_for_timeout(900)
            # Some flows can show a confirmation button.
            page.evaluate(
                """
                () => {
                  const norm = (s) => (s || '').replace(/\\u00a0/g, ' ').replace(/\\s+/g, ' ').trim().toLowerCase();
                  const phrases = ['подтвердить', 'да, обновить', 'обновить', 'ок'];
                  const nodes = Array.from(document.querySelectorAll('button, [role="button"]'));
                  for (const el of nodes) {
                    const txt = norm(el.innerText);
                    if (!txt || el.offsetParent === null) continue;
                    if (phrases.some((p) => txt === p || txt.includes(p))) {
                      el.click();
                      return;
                    }
                  }
                }
                """
            )
        resp = response_info.value
        data = {}
        try:
            data = resp.json()
        except Exception:
            try:
                data = json.loads(resp.text())
            except Exception:
                data = {}
        ok = str(data.get("success", "")).upper() == "Y"
        if ok:
            api_status = "success"
        else:
            err = _normalize_ws(str(data.get("error_text", "")))
            title = _normalize_ws(str(data.get("title", "")))
            api_msg = err or title
            _log(f"[collector] refresh API rejected: title='{title}' error='{err}'")
            if _is_hard_limit_message(f"{title} {err}"):
                api_status = "limit"
            else:
                api_status = "rejected"
    except Exception:
        api_status, api_msg = _refresh_api_status(page)

    if api_status == "unknown":
        _log("[collector] refresh API status unknown")

    deadline = time.time() + 18
    changed = False
    while time.time() < deadline:
        page.wait_for_timeout(1000)
        current_fp = _modal_fingerprint(page)
        if current_fp and current_fp != before_fp:
            changed = True
            break
    if changed:
        _log("[collector] refresh updated cards")
    else:
        _log("[collector] refresh did not change cards")
        if api_status == "limit":
            _log(f"[collector] refresh rejected by server (hard limit): {api_msg}")
            return False, True, False
        if api_status == "rejected":
            _log(f"[collector] refresh rejected by server: {api_msg or 'unknown reason'}")
            return False, False, True
    return changed, False, False


def _collect_waves(page, source: str, waves: int, require_distinct_waves: bool) -> list[DiscountItem]:
    merged: dict[str, DiscountItem] = {}
    wave_fps: list[str] = []
    total_waves = max(1, waves)
    refresh_exhausted = False
    for wave_idx in range(total_waves):
        _log(f"[collector] wave {wave_idx + 1}/{total_waves}: collecting")
        _open_discounts_area(page)
        current = _collect_from_inshop_modal(page, source)
        if not current:
            # Fallback when modal selectors change.
            current = [x for x in _collect_from_dom(page, source) if "_favorite" not in x.source]
        favorite = _collect_favorite_from_personal(page, source)
        if favorite:
            for fav in favorite:
                merged.setdefault(fav.item_id, fav)

        wave_ids = sorted(x.item_id for x in current)
        wave_fp = "|".join(wave_ids)
        wave_fps.append(wave_fp)

        _log(f"[collector] wave {wave_idx + 1}: found {len(current)} inshop items")
        for item in current:
            merged.setdefault(item.item_id, item)
        _log(f"[collector] merged unique items: {len(merged)}")

        if require_distinct_waves and len(current) < 6:
            _save_debug(page, f"wave_{wave_idx + 1}_less_than_6")
            raise SystemExit(
                f"Wave {wave_idx + 1}: expected 6 inshop items, found {len(current)}. "
                "Cannot guarantee 3x6 collection."
            )

        if wave_idx == total_waves - 1:
            break
        if refresh_exhausted:
            if require_distinct_waves:
                if len(merged) >= 6:
                    _log("[collector] partial day confirmed: refresh exhausted, keeping collected waves")
                    break
                raise SystemExit(
                    "Refresh exhausted before collecting all requested waves. "
                    "Cannot guarantee 3x6 collection for this day."
                )
            break
        changed, limit_reached, refresh_rejected = _click_refresh_discounts(page)
        if limit_reached:
            _save_debug(page, f"refresh_limit_reached_wave_{wave_idx + 1}")
            if require_distinct_waves:
                if len(merged) >= 6:
                    _log("[collector] partial day confirmed: replace limit reached, keeping collected waves")
                    break
                raise SystemExit(
                    "Replace limit reached before collecting all requested waves. "
                    "Cannot guarantee 3x6 collection for this day."
                )
            break
        if refresh_rejected:
            _log("[collector] refresh rejected, skipping further refresh attempts")
            refresh_exhausted = True
            if require_distinct_waves:
                if len(merged) >= 6:
                    _log("[collector] partial day confirmed: replace rejected by server, keeping collected waves")
                    break
                raise SystemExit(
                    "Replace rejected by server before collecting all requested waves. "
                    "Cannot guarantee 3x6 collection for this day."
                )
            break
        if not changed:
            # Fallback: reopen the section and try one more time.
            _log("[collector] refresh unchanged, retrying after reopen")
            try:
                _open_discounts_area(page)
            except Exception:
                pass
            changed, limit_reached, refresh_rejected = _click_refresh_discounts(page)
            if limit_reached:
                _save_debug(page, f"refresh_limit_reached_wave_{wave_idx + 1}_retry")
                if require_distinct_waves:
                    if len(merged) >= 6:
                        _log("[collector] partial day confirmed: replace limit reached on retry, keeping collected waves")
                        break
                    raise SystemExit(
                        "Replace limit reached during retry. "
                        "Cannot guarantee 3x6 collection for this day."
                    )
                break
            if refresh_rejected:
                _log("[collector] refresh rejected, skipping further refresh attempts")
                refresh_exhausted = True
                if require_distinct_waves:
                    if len(merged) >= 6:
                        _log("[collector] partial day confirmed: replace rejected on retry, keeping collected waves")
                        break
                    raise SystemExit(
                        "Replace rejected by server during retry. "
                        "Cannot guarantee 3x6 collection for this day."
                    )
                break
        if not changed:
            _save_debug(page, f"refresh_not_changed_wave_{wave_idx + 1}")
            if require_distinct_waves:
                if len(merged) >= 6:
                    _log("[collector] partial day confirmed: replace did not change cards, keeping collected waves")
                    break
                raise SystemExit(
                    f"Wave {wave_idx + 1}: replace did not change inshop cards. "
                    "Cannot guarantee 3 distinct waves."
                )
            # Continue anyway for non-strict mode.

    if require_distinct_waves and total_waves > 1:
        unique_wave_fps = len({x for x in wave_fps if x})
        if unique_wave_fps < total_waves:
            if len(merged) >= 6:
                _log(
                    f"[collector] partial day confirmed: collected {unique_wave_fps} distinct wave(s) "
                    f"from requested {total_waves}, keeping accumulated pool"
                )
            else:
                raise SystemExit(
                    f"Collected only {unique_wave_fps} distinct wave(s) from requested {total_waves}. "
                    "Cannot guarantee full 3x6 selection."
                )

    return list(merged.values())


def _is_logged_in(page) -> bool:
    # Prefer structural markers over text to avoid encoding issues.
    if page.locator("input[type='tel']").count() > 0:
        return False
    if page.locator("input[placeholder*='телефон']").count() > 0:
        return False
    if page.locator("text=Введите номер телефона").count() > 0:
        return False

    body = _normalize_ws(page.inner_text("body")).lower()
    login_markers = [
        "авторизуйтесь во вкусвилл",
        "введите номер телефона",
        "с картой выгоднее",
        "р°рір‚рѕсђрёс—сѓр№с‚рµсѓсњ рірѕ",
        "ріірірµріирёс‚ріµ рѕрѕрјрµсђ с‚рµр»рµс„рѕрѕр°",
    ]
    if any(marker in body for marker in login_markers):
        return False
    return True


def _save_debug(page, prefix: str) -> None:
    debug_dir = Path("out/debug")
    debug_dir.mkdir(parents=True, exist_ok=True)
    png = debug_dir / f"{prefix}.png"
    txt = debug_dir / f"{prefix}.txt"
    page.screenshot(path=str(png), full_page=True)
    txt.write_text(page.inner_text("body")[:8000], encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collect VkusVill personal discount items from web cabinet.")
    parser.add_argument("--source", choices=["storage_state", "system_chrome"], default="system_chrome")
    parser.add_argument("--state-file", default="data/vkusvill_storage_state.json")
    parser.add_argument("--chrome-user-data-dir", default="")
    parser.add_argument("--chrome-profile-name", default="Default")
    parser.add_argument("--out-file", default="data/today_discounts.json")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--interactive-login", action="store_true")
    parser.add_argument(
        "--max-items",
        type=int,
        default=0,
        help="Hard cap for total output items. 0 means unlimited.",
    )
    parser.add_argument("--waves", type=int, default=1, help="How many waves to collect (1..3).")
    parser.add_argument(
        "--require-distinct-waves",
        action="store_true",
        help="Fail if requested waves are not all distinct (recommended for strict 3x6 mode).",
    )
    parser.add_argument(
        "--offers-ready-food-url",
        default="",
        help="Optional URL for extra 'Ваши скидки -> Готовая еда' collection.",
    )
    parser.add_argument(
        "--offers-ready-food-max",
        type=int,
        default=0,
        help="Hard cap for ready-food items. 0 means unlimited.",
    )
    parser.add_argument(
        "--expected-delivery-hint",
        default="",
        help="Optional substring to validate current delivery location/address in UI.",
    )
    parser.add_argument(
        "--strict-delivery-check",
        action="store_true",
        help="Fail collection when delivery location does not match expected hint.",
    )
    return parser.parse_args()


def _collect_with_storage_state(args: argparse.Namespace) -> list[DiscountItem]:
    state_file = Path(args.state_file)
    if not state_file.exists():
        raise SystemExit(f"State file not found: {state_file}")
    _ensure_disk_headroom(state_file.parent)

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=args.headless)
        except OSError as exc:
            if getattr(exc, "errno", None) == errno.ENOSPC:
                raise SystemExit(f"[collector] disk full on {state_file.parent}, cannot launch browser") from exc
            raise
        context = browser.new_context(storage_state=str(state_file), locale="ru-RU")
        page = context.new_page()
        _open_discounts_area(page)
        _assert_delivery_hint(page, args.expected_delivery_hint, args.strict_delivery_check)
        if not _is_logged_in(page):
            _save_debug(page, "storage_state_not_logged_in")
            raise SystemExit(
                "VkusVill is not logged in for storage_state session. "
                "Re-auth required. Debug saved to out/debug."
            )
        items = _collect_waves(
            page,
            "vkusvill_web_storage_state",
            waves=args.waves,
            require_distinct_waves=bool(args.require_distinct_waves),
        )
        if args.offers_ready_food_url:
            extra = _collect_offers_ready_food(
                page,
                url=args.offers_ready_food_url,
                max_items=int(args.offers_ready_food_max),
            )
            if extra:
                _log(f"[collector] offers ready food: +{len(extra)} items")
                items = _merge_items_unique(items, extra)
        backfilled, corrected = _repair_item_images(page, items, max_items=14)
        if backfilled > 0 or corrected > 0:
            _log(f"[collector] image repair: backfilled=+{backfilled}, corrected=+{corrected}")
        browser.close()
    return items


def _collect_with_system_chrome(args: argparse.Namespace) -> list[DiscountItem]:
    if args.chrome_user_data_dir:
        user_data_dir = Path(args.chrome_user_data_dir)
    else:
        local_app_data = os.getenv("LOCALAPPDATA")
        if not local_app_data:
            raise SystemExit("LOCALAPPDATA is not set")
        user_data_dir = Path(local_app_data) / "Google" / "Chrome" / "User Data"

    if not user_data_dir.exists():
        raise SystemExit(f"Chrome user data dir not found: {user_data_dir}")
    _ensure_disk_headroom(user_data_dir)

    profile_name = args.chrome_profile_name
    if profile_name.lower() == "auto":
        local_state = user_data_dir / "Local State"
        if local_state.exists():
            try:
                payload = json.loads(local_state.read_text(encoding="utf-8"))
                profile_name = payload.get("profile", {}).get("last_used") or "Default"
            except Exception:
                profile_name = "Default"
        else:
            profile_name = "Default"

    with sync_playwright() as p:
        def open_context(headless_mode: bool):
            try:
                return p.chromium.launch_persistent_context(
                    channel="chrome",
                    user_data_dir=str(user_data_dir),
                    headless=headless_mode,
                    locale="ru-RU",
                    timezone_id="Europe/Moscow",
                    args=[
                        f"--profile-directory={profile_name}",
                        "--disable-blink-features=AutomationControlled",
                    ],
                )
            except OSError as exc:
                if getattr(exc, "errno", None) == errno.ENOSPC:
                    raise SystemExit(
                        f"[collector] disk full on {user_data_dir}, cannot write Chrome profile"
                    ) from exc
                raise
            except Exception as exc:
                raise SystemExit(
                    "Failed to open Chrome profile. Close all Chrome windows and retry. "
                    f"Details: {exc}"
                ) from exc

        context = open_context(args.headless)
        context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )
        page = context.new_page()
        open_discounts_error = None
        try:
            _open_discounts_area(page)
            _assert_delivery_hint(page, args.expected_delivery_hint, args.strict_delivery_check)
        except Exception as exc:
            open_discounts_error = exc

        if open_discounts_error is not None:
            if args.offers_ready_food_url:
                _log(
                    "[collector] personal area unavailable; "
                    "falling back to ready-food-only refresh from offers route"
                )
                extra = _collect_offers_ready_food(
                    page,
                    url=args.offers_ready_food_url,
                    max_items=int(args.offers_ready_food_max),
                )
                if extra:
                    backfilled, corrected = _repair_item_images(page, extra, max_items=14)
                    if backfilled > 0 or corrected > 0:
                        _log(
                            f"[collector] image repair: backfilled=+{backfilled}, corrected=+{corrected}"
                        )
                    context.close()
                    return extra
            context.close()
            raise open_discounts_error

        if not _is_logged_in(page) and args.interactive_login and args.headless:
            # Headless context cannot be used for SMS login; reopen headed once.
            context.close()
            context = open_context(False)
            context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
            )
            page = context.new_page()
            _open_discounts_area(page)
            _assert_delivery_hint(page, args.expected_delivery_hint, args.strict_delivery_check)

        if not _is_logged_in(page):
            if args.interactive_login:
                _log("VkusVill login required in automation browser.")
                _log("Sign in on the opened page. Waiting up to 10 minutes...")
                deadline = time.time() + 600
                while time.time() < deadline:
                    if _is_logged_in(page):
                        break
                    page.wait_for_timeout(1500)
                    # Re-focus account page periodically.
                    if int(time.time()) % 15 == 0:
                        try:
                            _open_discounts_area(page)
                        except Exception:
                            pass
                _open_discounts_area(page)
            if _is_logged_in(page):
                items = _collect_waves(
                    page,
                    "vkusvill_web_system_chrome",
                    waves=args.waves,
                    require_distinct_waves=bool(args.require_distinct_waves),
                )
                if args.offers_ready_food_url:
                    extra = _collect_offers_ready_food(
                        page,
                        url=args.offers_ready_food_url,
                        max_items=int(args.offers_ready_food_max),
                    )
                    if extra:
                        _log(f"[collector] offers ready food: +{len(extra)} items")
                        items = _merge_items_unique(items, extra)
                backfilled, corrected = _repair_item_images(page, items, max_items=14)
                if backfilled > 0 or corrected > 0:
                    _log(f"[collector] image repair: backfilled=+{backfilled}, corrected=+{corrected}")
                context.close()
                return items
            _save_debug(page, "system_chrome_not_logged_in")
            context.close()
            raise SystemExit(
                "VkusVill account is not logged in in selected Chrome profile. "
                "Login in Chrome first, then retry. Debug saved to out/debug."
            )
        items = _collect_waves(
            page,
            "vkusvill_web_system_chrome",
            waves=args.waves,
            require_distinct_waves=bool(args.require_distinct_waves),
        )
        if args.offers_ready_food_url:
            extra = _collect_offers_ready_food(
                page,
                url=args.offers_ready_food_url,
                max_items=int(args.offers_ready_food_max),
            )
            if extra:
                _log(f"[collector] offers ready food: +{len(extra)} items")
                items = _merge_items_unique(items, extra)
        backfilled, corrected = _repair_item_images(page, items, max_items=14)
        if backfilled > 0 or corrected > 0:
            _log(f"[collector] image repair: backfilled=+{backfilled}, corrected=+{corrected}")
        context.close()

    return items


def main() -> None:
    args = parse_args()
    args.waves = max(1, min(int(args.waves), 3))
    if args.waves > 1 and not bool(args.require_distinct_waves):
        args.require_distinct_waves = True
        _log("[collector] forcing require_distinct_waves for multi-wave collect")
    out_path = Path(args.out_file)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    run_day = datetime.now().strftime("%Y-%m-%d")
    existing_items = _load_today_pool(out_path, run_day=run_day)

    if args.source == "storage_state":
        items = _collect_with_storage_state(args)
    else:
        items = _collect_with_system_chrome(args)

    items = [x for x in items if x.name and x.discount_price > 0]
    items = _merge_items_latest(existing_items, items)
    if int(args.max_items) > 0:
        items = items[: int(args.max_items)]
    if not items:
        raise SystemExit("No discounts detected. Check login status and page selectors.")

    payload = [item.as_dict() for item in items]
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_today_pool(out_path, run_day, items)
    print(json.dumps(payload, ensure_ascii=False))


if __name__ == "__main__":
    main()

