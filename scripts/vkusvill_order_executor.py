from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

from playwright.sync_api import sync_playwright


def _normalize_ws(value: str) -> str:
    return " ".join((value or "").replace("\xa0", " ").split())


def _name_key(value: str) -> str:
    text = _normalize_ws(value).lower().replace("ё", "е")
    text = re.sub(r"[\"'`“”„()\[\]{}:;,.!?/+\\-]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _tokens(value: str) -> list[str]:
    return [x for x in _name_key(value).split(" ") if len(x) >= 3]


def _is_logged_in(page) -> bool:
    if page.locator("input[type='tel']").count() > 0:
        return False
    if page.locator("input[placeholder*='телефон']").count() > 0:
        return False
    body = _normalize_ws(page.inner_text("body")).lower()
    markers = [
        "авторизуйтесь во вкусвилл",
        "введите номер телефона",
        "войти",
    ]
    return not any(x in body for x in markers)


def _load_order_targets(path: Path) -> tuple[str, list[dict[str, Any]]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    day = str(payload.get("day") or "")
    items = payload.get("items") or []
    targets: list[dict[str, Any]] = []
    for raw in items:
        name = _normalize_ws(str(raw.get("name") or ""))
        qty = int(raw.get("qty") or 0)
        if not name or qty <= 0:
            continue
        targets.append(
            {
                "item_id": str(raw.get("item_id") or ""),
                "name": name,
                "qty": qty,
            }
        )
    return day, targets


def _collect_cart_items(page) -> list[dict[str, Any]]:
    page.goto("https://vkusvill.ru/cart/", wait_until="domcontentloaded", timeout=120_000)
    page.wait_for_timeout(2800)
    raw = page.evaluate(
        """
        () => {
          const norm = (s) => (s || '').replace(/\\u00a0/g, ' ').replace(/\\s+/g, ' ').trim();
          const rows = [];
          for (const el of document.querySelectorAll('.js-delivery__basket--row')) {
            if (!el || el.offsetParent === null) continue;
            const text = norm(el.innerText || '');
            if (!text || text.length < 5) continue;
            const name = norm(
              (el.querySelector('.js-datalayer-catalog-list-name') || {}).innerText ||
              (el.querySelector('a[href*="/goods/"]') || {}).innerText ||
              ''
            );
            if (!name || name.length < 3) continue;
            let qty = 1;
            const qtyEl =
              el.querySelector('.js-delivery__product__q-selector-val-fake') ||
              el.querySelector('.js-delivery__product__q-selector-val') ||
              el.querySelector('input[type="number"]');
            const qtyRaw = qtyEl ? (qtyEl.value || qtyEl.innerText || qtyEl.textContent || '') : '';
            const q = parseInt((qtyRaw || '').toString().replace(/[^0-9]/g, ''), 10);
            if (Number.isFinite(q) && q > 0) {
              qty = q;
            } else {
              const m = text.match(/\\b(\\d+)\\s*шт\\b/i);
              const q2 = m ? parseInt(m[1], 10) : NaN;
              if (Number.isFinite(q2) && q2 > 0) qty = q2;
            }
            const idRaw =
              (el.querySelector('.js-delivery__basket--del') || {}).getAttribute?.('data-product-id') ||
              (el.querySelector('[data-product-id]') || {}).getAttribute?.('data-product-id') ||
              '';
            rows.push({
              name,
              qty,
              productId: String(idRaw || '').trim(),
            });
          }

          // Fallback for layouts where rows are wrapped by cards.
          if (!rows.length) {
            for (const card of document.querySelectorAll('.js-delivery__basket--container, .HProductCard__ItemMain')) {
              if (!card || card.offsetParent === null) continue;
              const name = norm(
                (card.querySelector('.js-datalayer-catalog-list-name') || {}).innerText ||
                (card.querySelector('a[href*="/goods/"]') || {}).innerText ||
                ''
              );
              if (!name || name.length < 3) continue;
              const text = norm(card.innerText || '');
              let qty = 1;
              const m = text.match(/\\b(\\d+)\\s*шт\\b/i);
              const q = m ? parseInt(m[1], 10) : NaN;
              if (Number.isFinite(q) && q > 0) qty = q;
              rows.push({ name, qty, productId: '' });
            }
          }
          return rows;
        }
        """
    )
    merged: dict[str, dict[str, Any]] = {}
    for row in raw:
        name = _normalize_ws(str(row.get("name") or ""))
        if not name:
            continue
        key = _name_key(name)
        if key in merged:
            merged[key]["qty"] += int(row.get("qty") or 1)
            continue
        merged[key] = {
            "name": name,
            "key": key,
            "qty": max(1, int(row.get("qty") or 1)),
            "product_id": str(row.get("productId") or ""),
        }
    return list(merged.values())


def _match_cart_qty(target_name: str, rows: list[dict[str, Any]]) -> tuple[int, str]:
    target_key = _name_key(target_name)
    for row in rows:
        if row.get("key") == target_key:
            return int(row.get("qty") or 0), str(row.get("name") or "")

    tt = set(_tokens(target_name))
    best_qty = 0
    best_name = ""
    best_score = 0.0
    for row in rows:
        rk = _name_key(str(row.get("name") or ""))
        if not rk:
            continue
        rr = set(_tokens(rk))
        if not rr:
            continue
        overlap = len(tt.intersection(rr))
        if overlap <= 0:
            if target_key in rk or rk in target_key:
                overlap = 1
            else:
                continue
        score = overlap / max(1, len(tt))
        if score > best_score:
            best_score = score
            best_qty = int(row.get("qty") or 0)
            best_name = str(row.get("name") or "")
    if best_score < 0.34:
        return 0, ""
    return best_qty, best_name


def _search_product(page, query: str) -> bool:
    page.goto("https://vkusvill.ru/", wait_until="domcontentloaded", timeout=120_000)
    page.wait_for_timeout(700)
    selectors = [
        "input[placeholder*='По товарам']",
        "input[placeholder*='Найти товары']",
        "input[placeholder*='Поиск']",
        "input[type='search']",
    ]
    for selector in selectors:
        loc = page.locator(selector)
        if loc.count() <= 0:
            continue
        box = loc.first
        try:
            box.click(timeout=3_000)
            box.fill("")
            box.type(query, delay=20)
            box.press("Enter")
            page.wait_for_timeout(1300)
            return True
        except Exception:
            continue
    page.goto(
        f"https://vkusvill.ru/search/?q={quote_plus(query)}",
        wait_until="domcontentloaded",
        timeout=120_000,
    )
    page.wait_for_timeout(1200)
    return True


def _click_best_card_add(page, target_name: str) -> dict[str, Any]:
    data = page.evaluate(
        """
        (targetName) => {
          const norm = (s) => (s || '')
            .replace(/\\u00a0/g, ' ')
            .toLowerCase()
            .replace(/ё/g, 'е')
            .replace(/[\"'`“”„()\\[\\]{}:;,.!?/+\\-]/g, ' ')
            .replace(/\\s+/g, ' ')
            .trim();
          const visible = (el) => !!(el && el.offsetParent !== null);
          const target = norm(targetName);
          const tokens = target.split(' ').filter((x) => x.length >= 3).slice(0, 9);
          const cardSelectors = [
            '.js-product-cart',
            '[class*="ProductCard"]',
            '[class*="product-card"]',
            '[class*="CatalogItem"]',
            'article',
            'li',
          ];
          const nodes = [];
          for (const s of cardSelectors) {
            for (const el of document.querySelectorAll(s)) nodes.push(el);
          }
          const cards = Array.from(new Set(nodes));
          let best = null;
          let bestScore = -1;
          for (const card of cards) {
            if (!visible(card)) continue;
            const nameSelectors = [
              '.js-datalayer-catalog-list-name',
              '[class*="name"]',
              'a[href*="/goods/"]',
              'h2',
              'h3',
            ];
            let name = '';
            for (const ns of nameSelectors) {
              const n = card.querySelector(ns);
              if (!n) continue;
              const txt = norm(n.innerText || '');
              if (txt && txt.length >= 3) {
                name = txt;
                break;
              }
            }
            if (!name) {
              name = norm(card.innerText || '').split('\\n')[0] || '';
            }
            if (!name || name.length < 3) continue;
            const blob = norm(card.innerText || '');
            let score = 0;
            if (name === target) score += 10;
            for (const t of tokens) {
              if (name.includes(t)) score += 3;
              else if (blob.includes(t)) score += 1;
            }
            if (score > bestScore) {
              bestScore = score;
              best = { card, name, score };
            }
          }
          if (!best || bestScore <= 0) {
            return { ok: false, reason: 'no_card' };
          }

          const controls = Array.from(best.card.querySelectorAll('button, [role="button"], a'));
          const scoreBtn = (el) => {
            if (!visible(el)) return -999;
            const txt = norm(el.innerText || el.getAttribute('aria-label') || '');
            const cls = norm((el.className || '') + ' ' + (el.id || ''));
            if (el.hasAttribute('disabled') || el.getAttribute('aria-disabled') === 'true') return -999;
            let s = 0;
            if (txt.includes('в корзину') || txt.includes('добав')) s += 12;
            if (txt === '+' || txt.includes('увелич')) s += 10;
            if (cls.includes('plus') || cls.includes('inc') || cls.includes('counter') || cls.includes('qty')) s += 5;
            if (txt.includes('выбрать')) s -= 4;
            return s;
          };

          let btn = null;
          let btnScore = -999;
          for (const candidate of controls) {
            const score = scoreBtn(candidate);
            if (score > btnScore) {
              btnScore = score;
              btn = candidate;
            }
          }
          if (!btn || btnScore < 1) {
            return { ok: false, reason: 'no_add_button', match: best.name };
          }
          btn.click();
          return {
            ok: true,
            match: best.name,
            cardScore: best.score,
            buttonText: norm(btn.innerText || btn.getAttribute('aria-label') || ''),
          };
        }
        """,
        target_name,
    )
    page.wait_for_timeout(450)
    return dict(data or {})


def _click_offers_by_xmlid(page, xmlid: str) -> bool:
    if not xmlid:
        return False
    page.goto("https://vkusvill.ru/offers/gotovaya-eda/", wait_until="domcontentloaded", timeout=120_000)
    page.wait_for_timeout(1400)
    for _ in range(6):
        locator = page.locator(f".js-datalayer-catalog-list-item[data-xmlid='{xmlid}']")
        if locator.count() > 0:
            card = locator.first
            add_btn = card.locator(".js-delivery__basket--add:visible")
            if add_btn.count() > 0:
                add_btn.first.click(timeout=6_000)
                page.wait_for_timeout(450)
                return True
            plus_btn = card.locator(".Q_Up:visible, .js-delivery__product__q-btn.Q_Up:visible")
            if plus_btn.count() > 0:
                plus_btn.first.click(timeout=6_000)
                page.wait_for_timeout(450)
                return True
            return False
        page.evaluate("() => window.scrollBy(0, Math.max(1100, window.innerHeight * 0.9))")
        page.wait_for_timeout(700)
    return False


def _add_target(page, target: dict[str, Any], click_delay_ms: int) -> dict[str, Any]:
    name = str(target["name"])
    item_id = str(target.get("item_id") or "")
    qty = int(target["qty"])
    result: dict[str, Any] = {
        "item_id": item_id,
        "name": name,
        "requested_qty": qty,
        "attempted_clicks": 0,
        "successful_clicks": 0,
        "errors": [],
    }
    for _ in range(qty):
        result["attempted_clicks"] += 1
        clicked = False
        if item_id.startswith("offers_"):
            clicked = _click_offers_by_xmlid(page, item_id.split("_", 1)[1])
        if not clicked:
            _search_product(page, name)
            click = _click_best_card_add(page, name)
            clicked = bool(click.get("ok"))
        if clicked:
            result["successful_clicks"] += 1
            page.wait_for_timeout(max(120, click_delay_ms))
            continue
        # One quick retry after refresh search.
        click = _click_best_card_add(page, name)
        if click.get("ok"):
            result["successful_clicks"] += 1
            page.wait_for_timeout(max(120, click_delay_ms))
            continue
        result["errors"].append(str(click.get("reason") or "unknown_click_error"))
        break
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Auto-add finalized VkusVill order items to cart.")
    parser.add_argument("--order-file", required=True)
    parser.add_argument("--chrome-user-data-dir", default="data/chrome-user-data")
    parser.add_argument("--chrome-profile-name", default="Default")
    parser.add_argument("--headless", action="store_true", default=True)
    parser.add_argument("--interactive-login", action="store_true")
    parser.add_argument("--click-delay-ms", type=int, default=420)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    order_file = Path(args.order_file)
    if not order_file.exists():
        raise SystemExit(f"order file not found: {order_file}")

    day, targets = _load_order_targets(order_file)
    if not targets:
        print(json.dumps({"ok": True, "day": day, "message": "no_selected_items"}, ensure_ascii=False))
        return

    user_data_dir = Path(args.chrome_user_data_dir)
    if not user_data_dir.exists():
        raise SystemExit(f"chrome user data dir not found: {user_data_dir}")

    results: list[dict[str, Any]] = []
    checks: list[dict[str, Any]] = []
    all_ok = True

    with sync_playwright() as p:
        def open_context(headless_mode: bool):
            return p.chromium.launch_persistent_context(
                channel="chrome",
                user_data_dir=str(user_data_dir),
                headless=headless_mode,
                locale="ru-RU",
                timezone_id="Europe/Moscow",
                args=[
                    f"--profile-directory={args.chrome_profile_name}",
                    "--disable-blink-features=AutomationControlled",
                ],
            )

        context = open_context(bool(args.headless))
        context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")
        page = context.new_page()
        page.goto("https://vkusvill.ru/personal/", wait_until="domcontentloaded", timeout=120_000)
        page.wait_for_timeout(1200)

        if not _is_logged_in(page):
            if bool(args.interactive_login) and bool(args.headless):
                context.close()
                context = open_context(False)
                page = context.new_page()
                page.goto("https://vkusvill.ru/personal/", wait_until="domcontentloaded", timeout=120_000)
                page.wait_for_timeout(1200)
            if not _is_logged_in(page):
                context.close()
                raise SystemExit("vkusvill login required in automation profile")

        for target in targets:
            target_name = str(target["name"])
            req_qty = int(target["qty"])
            before_rows = _collect_cart_items(page)
            before_qty, before_match = _match_cart_qty(target_name, before_rows)
            if before_qty >= req_qty:
                results.append(
                    {
                        "item_id": str(target.get("item_id") or ""),
                        "name": target_name,
                        "requested_qty": req_qty,
                        "attempted_clicks": 0,
                        "successful_clicks": 0,
                        "errors": [],
                        "note": "already_in_cart",
                    }
                )
                checks.append(
                    {
                        "name": target_name,
                        "requested_qty": req_qty,
                        "before_qty": before_qty,
                        "after_qty": before_qty,
                        "added_delta": 0,
                        "ok": True,
                        "before_match": before_match,
                        "after_match": before_match,
                    }
                )
                continue

            result = _add_target(page, target, int(args.click_delay_ms))
            results.append(result)
            after_rows = _collect_cart_items(page)
            after_qty, after_match = _match_cart_qty(target_name, after_rows)
            delta = max(0, after_qty - before_qty)
            ok = after_qty >= req_qty
            if not ok:
                all_ok = False
            checks.append(
                {
                    "name": target_name,
                    "requested_qty": req_qty,
                    "before_qty": before_qty,
                    "after_qty": after_qty,
                    "added_delta": delta,
                    "ok": ok,
                    "before_match": before_match,
                    "after_match": after_match,
                }
            )
        context.close()

    out = {
        "ok": all_ok,
        "day": day,
        "order_file": str(order_file),
        "targets": len(targets),
        "click_results": results,
        "checks": checks,
    }
    print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        sys.exit(1)
