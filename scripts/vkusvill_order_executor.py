from __future__ import annotations

import argparse
import json
import os
import re
import shutil
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


def _wait_until_logged_in(page, timeout_sec: int) -> bool:
    deadline = time.time() + max(1, int(timeout_sec))
    while time.time() < deadline:
        try:
            if _is_logged_in(page):
                return True
        except Exception:
            pass
        page.wait_for_timeout(1200)
    try:
        return _is_logged_in(page)
    except Exception:
        return False


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

          const controls = Array.from(best.card.querySelectorAll(
            'button, [role="button"], a, .js-delivery__basket--add, [class*="basket"][class*="add"], [data-product-id]'
          ));
          const scoreBtn = (el) => {
            if (!visible(el)) return -999;
            const txt = norm(el.innerText || el.getAttribute('aria-label') || '');
            const cls = norm((el.className || '') + ' ' + (el.id || ''));
            if (el.hasAttribute('disabled') || el.getAttribute('aria-disabled') === 'true') return -999;
            let s = 0;
            const hasAddClass =
              cls.includes('js delivery basket add') ||
              cls.includes('delivery basket add') ||
              cls.includes('basket add');
            if (txt.includes('в корзину') || txt.includes('добав')) s += 12;
            if (txt === '+' || txt.includes('увелич')) s += 10;
            if (cls.includes('plus') || cls.includes('inc') || cls.includes('counter') || cls.includes('qty')) s += 5;
            if (hasAddClass) s += 18;
            if (el.getAttribute('data-product-id')) s += 8;
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
            const blob2 = norm(best.card.innerText || '');
            const unavailable = blob2.includes('нет в наличии') || blob2.includes('недоступен');
            return { ok: false, reason: unavailable ? 'unavailable' : 'no_add_button', match: best.name };
          }
          const btnClass = norm(btn.className || '');
          const requiresTomorrow =
            btnClass.includes('only online add') ||
            btnClass.includes('only-online-add') ||
            btnClass.includes('tomorrow');
          if (requiresTomorrow) {
            return {
              ok: false,
              reason: 'requires_tomorrow_delivery',
              match: best.name,
              buttonClass: btnClass,
            };
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


def _open_product_page_and_add(page, target_name: str) -> dict[str, Any]:
    card = page.evaluate(
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
            const nameEl = card.querySelector('.js-datalayer-catalog-list-name, [class*="name"], a[href*="/goods/"], h2, h3');
            const name = norm((nameEl && nameEl.innerText) || card.innerText || '');
            if (!name || name.length < 3) continue;
            let score = 0;
            if (name === target) score += 10;
            for (const t of tokens) {
              if (name.includes(t)) score += 3;
            }
            if (score > bestScore) {
              const linkEl = card.querySelector('a[href*="/goods/"]');
              const href = (linkEl && linkEl.getAttribute('href')) || '';
              bestScore = score;
              best = { href, score };
            }
          }
          if (!best || bestScore <= 0) return { ok: false, reason: 'no_product_link' };
          return { ok: !!best.href, href: best.href || '', score: bestScore };
        }
        """,
        target_name,
    )
    card_data = dict(card or {})
    href = str(card_data.get("href") or "").strip()
    if not href:
        return {"ok": False, "reason": str(card_data.get("reason") or "no_product_link")}
    if href.startswith("/"):
        href = f"https://vkusvill.ru{href}"
    if not href.startswith("http"):
        return {"ok": False, "reason": "bad_product_link"}

    page.goto(href, wait_until="domcontentloaded", timeout=120_000)
    page.wait_for_timeout(900)
    clicked = page.evaluate(
        """
        () => {
          const norm = (s) => (s || '')
            .replace(/\\u00a0/g, ' ')
            .toLowerCase()
            .replace(/\\s+/g, ' ')
            .trim();
          const visible = (el) => !!(el && el.offsetParent !== null);
          const controls = Array.from(document.querySelectorAll(
            'button, [role="button"], a, .js-delivery__basket--add, [class*="basket"][class*="add"], [data-product-id]'
          ));
          let best = null;
          let bestScore = -999;
          for (const el of controls) {
            if (!visible(el)) continue;
            if (el.hasAttribute('disabled') || el.getAttribute('aria-disabled') === 'true') continue;
            const txt = norm(el.innerText || el.getAttribute('aria-label') || '');
            const cls = norm((el.className || '') + ' ' + (el.id || ''));
            let s = 0;
            if (txt.includes('в корзину') || txt.includes('добав')) s += 12;
            if (txt === '+' || txt.includes('увелич')) s += 10;
            if (cls.includes('plus') || cls.includes('inc') || cls.includes('counter') || cls.includes('qty')) s += 5;
            if (cls.includes('basket add') || cls.includes('delivery basket add')) s += 18;
            if (el.getAttribute('data-product-id')) s += 8;
            if (txt.includes('выбрать')) s -= 4;
            if (s > bestScore) {
              bestScore = s;
              best = el;
            }
          }
          if (!best || bestScore < 1) return { ok: false, reason: 'no_add_button_on_product' };
          const cls = norm((best.className || '') + ' ' + (best.id || ''));
          const requiresTomorrow =
            cls.includes('only online add') ||
            cls.includes('only-online-add') ||
            cls.includes('tomorrow');
          if (requiresTomorrow) {
            return {
              ok: false,
              reason: 'requires_tomorrow_delivery',
              buttonClass: cls,
            };
          }
          best.click();
          return {
            ok: true,
            score: bestScore,
            buttonText: norm(best.innerText || best.getAttribute('aria-label') || '')
          };
        }
        """
    )
    page.wait_for_timeout(450)
    click_data = dict(clicked or {})
    click_data["product_href"] = href
    return click_data


def _click_offers_by_xmlid(page, xmlid: str) -> dict[str, Any]:
    if not xmlid:
        return {"ok": False, "reason": "offers_xmlid_missing"}
    page.goto("https://vkusvill.ru/offers/gotovaya-eda/", wait_until="domcontentloaded", timeout=120_000)
    page.wait_for_timeout(1400)
    for _ in range(6):
        locator = page.locator(f".js-datalayer-catalog-list-item[data-xmlid='{xmlid}']")
        if locator.count() > 0:
            card = locator.first
            add_btn = card.locator(".js-delivery__basket--add:visible")
            if add_btn.count() > 0:
                try:
                    btn_class = str(add_btn.first.get_attribute("class") or "").lower()
                except Exception:
                    btn_class = ""
                if ("only-online-add" in btn_class) or ("tomorrow" in btn_class):
                    return {
                        "ok": False,
                        "reason": "requires_tomorrow_delivery",
                        "method": "offers_by_xmlid",
                        "button_class": btn_class,
                    }
                try:
                    add_btn.first.click(timeout=6_000)
                    page.wait_for_timeout(450)
                    return {"ok": True, "method": "offers_by_xmlid"}
                except Exception as exc:
                    return {
                        "ok": False,
                        "reason": "offers_click_failed",
                        "method": "offers_by_xmlid",
                        "error": str(exc),
                    }
            plus_btn = card.locator(".Q_Up:visible, .js-delivery__product__q-btn.Q_Up:visible")
            if plus_btn.count() > 0:
                try:
                    plus_btn.first.click(timeout=6_000)
                    page.wait_for_timeout(450)
                    return {"ok": True, "method": "offers_by_xmlid"}
                except Exception as exc:
                    return {
                        "ok": False,
                        "reason": "offers_plus_click_failed",
                        "method": "offers_by_xmlid",
                        "error": str(exc),
                    }
            return {"ok": False, "reason": "no_add_button", "method": "offers_by_xmlid"}
        page.evaluate("() => window.scrollBy(0, Math.max(1100, window.innerHeight * 0.9))")
        page.wait_for_timeout(700)
    return {"ok": False, "reason": "offers_card_not_found", "method": "offers_by_xmlid"}


def _ajax_add_from_best_card(page, target_name: str) -> dict[str, Any]:
    data = page.evaluate(
        """
        async (targetName) => {
          const norm = (s) => (s || '')
            .replace(/\\u00a0/g, ' ')
            .toLowerCase()
            .replace(/ё/g, 'е')
            .replace(/[\"'`“”„()\\[\\]{}:;,.!?/+\\-]/g, ' ')
            .replace(/\\s+/g, ' ')
            .trim();
          const visible = (el) => !!(el && el.offsetParent !== null);
          const getData = (el, key) => {
            if (!el) return '';
            const raw = el.getAttribute('data-' + key);
            return raw ? String(raw).trim() : '';
          };
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
            const nameEl = card.querySelector('.js-datalayer-catalog-list-name, [class*="name"], a[href*="/goods/"], h2, h3');
            const name = norm((nameEl && nameEl.innerText) || card.innerText || '');
            if (!name || name.length < 3) continue;
            let score = 0;
            if (name === target) score += 10;
            for (const t of tokens) {
              if (name.includes(t)) score += 3;
              else if (norm(card.innerText || '').includes(t)) score += 1;
            }
            if (score > bestScore) {
              bestScore = score;
              best = { card, name };
            }
          }
          if (!best || bestScore <= 0) return { ok: false, reason: 'no_card_for_ajax' };

          const controls = Array.from(best.card.querySelectorAll(
            '.js-delivery__basket--add, button, [role="button"], a, [data-product-id]'
          ));
          let btn = null;
          let btnScore = -999;
          for (const el of controls) {
            if (!visible(el)) continue;
            const txt = norm(el.innerText || el.getAttribute('aria-label') || '');
            const cls = norm((el.className || '') + ' ' + (el.id || ''));
            let s = 0;
            if (txt.includes('в корзину') || txt.includes('добав')) s += 12;
            if (txt === '+' || txt.includes('увелич')) s += 10;
            if (cls.includes('basket add') || cls.includes('delivery basket add')) s += 18;
            if (el.getAttribute('data-product-id')) s += 8;
            if (s > btnScore) {
              btnScore = s;
              btn = el;
            }
          }
          const btnClass = norm((btn && btn.className) || '');
          if (
            btnClass.includes('only online add') ||
            btnClass.includes('only-online-add') ||
            btnClass.includes('tomorrow')
          ) {
            return {
              ok: false,
              reason: 'requires_tomorrow_delivery',
              match: best.name,
              buttonClass: btnClass,
            };
          }

          const id = getData(btn, 'id') || getData(btn, 'product-id') || getData(best.card, 'id');
          const xmlid = getData(btn, 'xmlid') || getData(best.card, 'xmlid');
          if (!id) return { ok: false, reason: 'no_product_id_for_ajax', match: best.name };

          const curKoef = getData(btn, 'koef') || '1';
          const curStep = getData(btn, 'step') || '1';
          const couponInput = document.querySelector('input[name="coupon"], #coupon');
          const bonusInput = document.querySelector('input[name="bonus"], #bonus');
          const bodyIsApp = document.body && document.body.classList.contains('_app');
          const parentProduct = btn ? btn.closest('.js-delivery__product') : null;
          const isDefaultButton = !!(parentProduct && parentProduct.querySelector('.js-delivery__product__q-container'));

          const params = new URLSearchParams();
          params.set('id', id);
          if (xmlid) params.set('xmlid', xmlid);
          const max = getData(btn, 'max');
          const selectShop = getData(btn, 'select-shop');
          const isGreen = getData(btn, 'is-green') || '0';
          const isExperiment = getData(btn, 'experiment');
          const isOnlyOnline = getData(btn, 'only-online');
          const priceType = getData(btn, 'price-type');
          if (max) params.set('max', max);
          if (selectShop) params.set('delivery_no_set', selectShop);
          params.set('koef', curKoef);
          params.set('step', curStep);
          params.set('coupon', couponInput && couponInput.value ? String(couponInput.value) : '');
          params.set('bonus', bonusInput && bonusInput.value ? String(bonusInput.value) : '');
          if (isExperiment) params.set('isExperiment', isExperiment);
          if (isOnlyOnline) params.set('isOnlyOnline', isOnlyOnline);
          params.set('isGreen', isGreen);
          params.set('is_app', bodyIsApp ? 'Y' : 'N');
          params.set('is_default_button', isDefaultButton ? 'Y' : 'N');
          params.set('cssInited', 'N');
          if (priceType) params.set('price_type', priceType);
          params.set('skip_analogs', btn && btn.classList.contains('_skip_analogs') ? 'Y' : '');
          const uidEl = document.querySelector('#lk-user-id');
          if (uidEl && uidEl.value) params.set('user_id', String(uidEl.value));

          try {
            const response = await fetch('/ajax/delivery_order/basket_add.php', {
              method: 'POST',
              credentials: 'include',
              headers: {
                'X-Requested-With': 'XMLHttpRequest',
                'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
              },
              body: params.toString(),
            });
            let payload = {};
            try {
              payload = await response.json();
            } catch (e) {
              payload = {};
            }
            const ok = !!(payload && (payload.success === 'Y' || payload.success === true));
            const slim = {};
            for (const key of ['success', 'error_text', 'message', 'isFirst', 'order_params_new']) {
              if (payload && Object.prototype.hasOwnProperty.call(payload, key)) slim[key] = payload[key];
            }
            return {
              ok,
              reason: ok ? 'ok' : 'ajax_add_failed',
              match: best.name,
              params: Object.fromEntries(params.entries()),
              response: slim,
              status: response.status,
            };
          } catch (e) {
            return {
              ok: false,
              reason: 'ajax_request_error',
              match: best.name,
              error: String(e || ''),
              params: Object.fromEntries(params.entries()),
            };
          }
        }
        """,
        target_name,
    )
    page.wait_for_timeout(280)
    return dict(data or {})


def _rescue_add_via_ajax(page, target_name: str, attempts: int) -> dict[str, Any]:
    logs: list[dict[str, Any]] = []
    success_calls = 0
    for _ in range(max(1, int(attempts))):
        try:
            _search_product(page, target_name)
            call = _ajax_add_from_best_card(page, target_name)
        except Exception as exc:
            call = {"ok": False, "reason": "ajax_rescue_exception", "error": str(exc)}
        logs.append(call)
        if bool(call.get("ok")):
            success_calls += 1
        page.wait_for_timeout(320)
    return {"success_calls": success_calls, "attempts": logs}


def _measure_after_qty(
    page,
    target_name: str,
    expected_min_qty: int,
    verify_retries: int,
) -> tuple[int, str]:
    best_qty = 0
    best_match = ""
    for _ in range(max(1, int(verify_retries))):
        after_rows = _collect_cart_items(page)
        qty, match = _match_cart_qty(target_name, after_rows)
        if qty > best_qty:
            best_qty = qty
            best_match = match
        if qty >= expected_min_qty:
            return qty, match
        page.wait_for_timeout(450)
    return best_qty, best_match


def _add_target(page, target: dict[str, Any], click_delay_ms: int, per_item_retries: int) -> dict[str, Any]:
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
        "attempt_log": [],
    }
    for _ in range(qty):
        unit_done = False
        last_reason = "unknown_click_error"
        for attempt_no in range(1, max(1, int(per_item_retries)) + 1):
            result["attempted_clicks"] += 1
            clicked = False
            click: dict[str, Any] = {"ok": False, "reason": "unknown_click_error"}
            method = "search"

            if item_id.startswith("offers_"):
                method = "offers_by_xmlid"
                click = _click_offers_by_xmlid(page, item_id.split("_", 1)[1])
                clicked = bool(click.get("ok"))

            if not clicked:
                immediate_reason = str(click.get("reason") or "")
                if immediate_reason in {"requires_tomorrow_delivery", "unavailable"}:
                    pass
                else:
                    try:
                        _search_product(page, name)
                    except Exception:
                        click = {"ok": False, "reason": "search_failed", "method": "search"}
                    else:
                        click = _click_best_card_add(page, name)
                        method = "search"
                        clicked = bool(click.get("ok"))
                        if not clicked:
                            immediate_reason = str(click.get("reason") or "")
                            if immediate_reason in {"requires_tomorrow_delivery", "unavailable"}:
                                pass
                            else:
                                try:
                                    click2 = _open_product_page_and_add(page, name)
                                except Exception:
                                    click2 = {"ok": False, "reason": "product_page_fallback_failed"}
                                if bool(click2.get("ok")):
                                    click = click2
                                    method = "product_page"
                                    clicked = True
                                elif str(click.get("reason") or "") == "search_failed":
                                    click = click2

            attempt_entry = {
                "attempt": attempt_no,
                "method": method,
                "ok": bool(clicked),
                "reason": str(click.get("reason") or ""),
                "match": str(click.get("match") or ""),
            }
            result["attempt_log"].append(attempt_entry)

            if clicked:
                result["successful_clicks"] += 1
                page.wait_for_timeout(max(120, click_delay_ms))
                unit_done = True
                break

            last_reason = str(click.get("reason") or "unknown_click_error")
            if last_reason in {"requires_tomorrow_delivery", "unavailable"}:
                break
            page.wait_for_timeout(260)

        if not unit_done:
            result["errors"].append(last_reason)
            break
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Auto-add finalized VkusVill order items to cart.")
    parser.add_argument("--order-file", required=True)
    parser.add_argument("--chrome-user-data-dir", default="data/chrome-user-data")
    parser.add_argument("--chrome-profile-name", default="Default")
    parser.add_argument("--headless", dest="headless", action="store_true")
    parser.add_argument("--no-headless", dest="headless", action="store_false")
    parser.set_defaults(headless=True)
    parser.add_argument("--interactive-login", action="store_true")
    parser.add_argument("--interactive-login-wait-sec", type=int, default=240)
    parser.add_argument("--check-session-only", action="store_true")
    parser.add_argument("--click-delay-ms", type=int, default=420)
    parser.add_argument("--launch-retries", type=int, default=2)
    parser.add_argument("--per-item-retries", type=int, default=3)
    parser.add_argument("--verify-retries", type=int, default=3)
    parser.add_argument("--circuit-breaker-failures", type=int, default=3)
    parser.add_argument("--ajax-fallback-retries", type=int, default=2)
    return parser.parse_args()


def _safe_copy(src: Path, dst: Path) -> None:
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    except Exception:
        pass


def _clone_user_data(src_root: Path, profile_name: str, dst_root: Path) -> None:
    src_profile = src_root / profile_name
    dst_profile = dst_root / profile_name
    if not src_profile.exists():
        raise SystemExit(f"profile not found for clone: {src_profile}")
    if dst_root.exists():
        shutil.rmtree(dst_root, ignore_errors=True)
    dst_root.mkdir(parents=True, exist_ok=True)
    _safe_copy(src_root / "Local State", dst_root / "Local State")

    ignore = shutil.ignore_patterns(
        "Cache",
        "Code Cache",
        "GPUCache",
        "GrShaderCache",
        "DawnCache",
        "ShaderCache",
        "Crashpad",
        "BrowserMetrics",
        "Singleton*",
        "LOCK",
        "lockfile",
        "*.log",
    )

    def _copyfunc(src: str, dst: str) -> None:
        try:
            Path(dst).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        except Exception:
            return

    shutil.copytree(src_profile, dst_profile, dirs_exist_ok=True, ignore=ignore, copy_function=_copyfunc)


def main() -> None:
    args = parse_args()
    order_file = Path(args.order_file)
    if not order_file.exists():
        raise SystemExit(f"order file not found: {order_file}")

    day, targets = _load_order_targets(order_file)
    if (not targets) and (not bool(args.check_session_only)):
        print(json.dumps({"ok": True, "day": day, "message": "no_selected_items"}, ensure_ascii=False))
        return

    user_data_dir = Path(args.chrome_user_data_dir)
    if not user_data_dir.exists():
        raise SystemExit(f"chrome user data dir not found: {user_data_dir}")

    results: list[dict[str, Any]] = []
    checks: list[dict[str, Any]] = []
    all_ok = True

    with sync_playwright() as p:
        temp_user_data_dir: Path | None = None

        def open_context(user_data_path: Path, headless_mode: bool):
            return p.chromium.launch_persistent_context(
                channel="chrome",
                user_data_dir=str(user_data_path),
                headless=headless_mode,
                locale="ru-RU",
                timezone_id="Europe/Moscow",
                args=[
                    f"--profile-directory={args.chrome_profile_name}",
                    "--disable-blink-features=AutomationControlled",
                ],
            )

        last_exc: Exception | None = None
        context = None
        for attempt in range(max(1, int(args.launch_retries))):
            try:
                context = open_context(user_data_dir, bool(args.headless))
                break
            except Exception as exc:
                last_exc = exc
                time.sleep(0.6)

        if context is None:
            temp_user_data_dir = Path("out") / "tmp" / f"order-profile-{int(time.time())}"
            _clone_user_data(user_data_dir, args.chrome_profile_name, temp_user_data_dir)
            context = open_context(temp_user_data_dir, bool(args.headless))

        context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")
        page = context.new_page()
        page.goto("https://vkusvill.ru/personal/", wait_until="domcontentloaded", timeout=120_000)
        page.wait_for_timeout(1200)

        if not _is_logged_in(page):
            if bool(args.interactive_login):
                context.close()
                context = open_context(user_data_dir, False)
                page = context.new_page()
                page.goto("https://vkusvill.ru/personal/", wait_until="domcontentloaded", timeout=120_000)
                print(
                    "VkusVill login required in automation profile. "
                    f"Please sign in in opened browser (wait up to {int(args.interactive_login_wait_sec)} sec)..."
                )
                if not _wait_until_logged_in(page, int(args.interactive_login_wait_sec)):
                    context.close()
                    raise SystemExit("vkusvill login required in automation profile")
            else:
                context.close()
                raise SystemExit("vkusvill login required in automation profile")

        if bool(args.check_session_only):
            context.close()
            if temp_user_data_dir is not None:
                shutil.rmtree(temp_user_data_dir, ignore_errors=True)
            print(
                json.dumps(
                    {
                        "ok": True,
                        "day": day,
                        "message": "session_ok",
                        "profile": str(args.chrome_profile_name),
                    },
                    ensure_ascii=False,
                )
            )
            return

        consecutive_failures = 0
        breaker_threshold = max(1, int(args.circuit_breaker_failures))
        breaker_triggered = False

        for idx, target in enumerate(targets):
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
                        "reason": "already_in_cart",
                        "before_match": before_match,
                        "after_match": before_match,
                    }
                )
                consecutive_failures = 0
                continue

            result = _add_target(page, target, int(args.click_delay_ms), int(args.per_item_retries))
            results.append(result)
            after_qty, after_match = _measure_after_qty(
                page,
                target_name,
                req_qty,
                int(args.verify_retries),
            )
            delta = max(0, after_qty - before_qty)
            ok = after_qty >= req_qty
            reason = "ok"
            if not ok:
                if delta > 0:
                    reason = "partial_added"
                elif int(result.get("successful_clicks") or 0) > 0:
                    reason = "click_no_effect"
                else:
                    reason = str((result.get("errors") or ["not_added_to_cart"])[-1])

                rescue_reasons = {
                    "click_no_effect",
                    "no_add_button",
                    "no_add_button_on_product",
                    "search_failed",
                    "not_added_to_cart",
                    "unknown_click_error",
                }
                if reason in rescue_reasons:
                    rescue = _rescue_add_via_ajax(
                        page,
                        target_name,
                        int(args.ajax_fallback_retries),
                    )
                    result["ajax_rescue"] = rescue
                    after_qty2, after_match2 = _measure_after_qty(
                        page,
                        target_name,
                        req_qty,
                        int(args.verify_retries),
                    )
                    if after_qty2 > after_qty:
                        after_qty = after_qty2
                        after_match = after_match2
                        delta = max(0, after_qty - before_qty)
                    if after_qty >= req_qty:
                        ok = True
                        reason = "rescued_via_ajax"
                    elif delta > 0:
                        reason = "partial_added_ajax"
                    elif int(rescue.get("success_calls") or 0) > 0:
                        reason = "ajax_call_no_effect"
                    elif reason == "click_no_effect":
                        reason = "click_no_effect_ajax_failed"

            if not ok:
                all_ok = False
            checks.append(
                {
                    "item_id": str(target.get("item_id") or ""),
                    "name": target_name,
                    "requested_qty": req_qty,
                    "before_qty": before_qty,
                    "after_qty": after_qty,
                    "added_delta": delta,
                    "ok": ok,
                    "reason": reason,
                    "before_match": before_match,
                    "after_match": after_match,
                }
            )
            if ok:
                consecutive_failures = 0
            else:
                consecutive_failures += 1
                if consecutive_failures >= breaker_threshold:
                    breaker_triggered = True
                    all_ok = False
                    for rest in targets[idx + 1 :]:
                        rest_name = str(rest.get("name") or "")
                        rest_qty = int(rest.get("qty") or 0)
                        results.append(
                            {
                                "item_id": str(rest.get("item_id") or ""),
                                "name": rest_name,
                                "requested_qty": rest_qty,
                                "attempted_clicks": 0,
                                "successful_clicks": 0,
                                "errors": [],
                                "note": "skipped_by_circuit_breaker",
                            }
                        )
                        checks.append(
                            {
                                "item_id": str(rest.get("item_id") or ""),
                                "name": rest_name,
                                "requested_qty": rest_qty,
                                "before_qty": 0,
                                "after_qty": 0,
                                "added_delta": 0,
                                "ok": False,
                                "reason": "circuit_breaker_open",
                                "before_match": "",
                                "after_match": "",
                            }
                        )
                    break
        final_rows = _collect_cart_items(page)
        context.close()
        if temp_user_data_dir is not None:
            shutil.rmtree(temp_user_data_dir, ignore_errors=True)

    out = {
        "ok": all_ok,
        "day": day,
        "order_file": str(order_file),
        "targets": len(targets),
        "click_results": results,
        "checks": checks,
        "cart_unique_after": len(final_rows),
        "cart_total_qty_after": sum(int(row.get("qty") or 0) for row in final_rows),
        "breaker_triggered": breaker_triggered,
        "breaker_threshold": breaker_threshold,
    }
    print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        sys.exit(1)

