from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

from playwright.sync_api import sync_playwright


RUB_RE = re.compile(r"(\d[\d\s]*[.,]?\d*)\s*(?:в‚Ѕ|СЂСѓР±)", re.IGNORECASE)


@dataclass
class DiscountItem:
    item_id: str
    name: str
    price: float
    discount_price: float
    source: str

    def as_dict(self) -> dict:
        return {
            "item_id": self.item_id,
            "name": self.name,
            "price": self.price,
            "discount_price": self.discount_price,
            "source": self.source,
        }


def _normalize_ws(value: str) -> str:
    # Normalize NBSP and repeated spaces to make text checks robust.
    return " ".join(value.replace("\xa0", " ").split())


def _parse_price(token: str) -> float:
    normalized = token.replace(" ", "").replace(",", ".")
    return float(normalized)


def _item_id(name: str) -> str:
    return hashlib.sha1(name.strip().lower().encode("utf-8")).hexdigest()[:16]


def _is_favorite_marker(text: str) -> bool:
    lowered = _normalize_ws(text).lower()
    markers = [
        "РїРѕРґРѕР±СЂР°Р»Рё РґР»СЏ РІР°СЃ",
        "РЅР°Р·РЅР°С‡РёС‚СЊ РЅРѕРІС‹Р№",
        "Р»СЋР±РёРјС‹Р№ РїСЂРѕРґСѓРєС‚",
        "Р»СЋР±РёРјС‹Р№ С‚РѕРІР°СЂ",
        "СЂС—СЂС•СЂТ‘СЂС•СЂВ±СЃС’СЂВ°СЂВ»СЂС‘ СЂТ‘СЂВ»СЃСџ СЂС–СЂВ°СЃСЃ",
        "СЂС•СЂВ°СЂВ·СЂС•СЂВ°СЃвЂЎСЂС‘СЃвЂљСЃСљ СЂС•СЂС•СЂС–СЃвЂ№СЂв„–",
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
            )
        )

    return items


def _open_discounts_area(page) -> None:
    page.goto("https://vkusvill.ru/personal/", wait_until="domcontentloaded", timeout=120_000)
    page.wait_for_timeout(1800)

    # Open "6 discounts" details to reveal product cards.
    detail_btn = page.locator(".js-lk-inshop-show-detail")
    if detail_btn.count() > 0:
        try:
            detail_btn.first.click()
            page.wait_for_timeout(2200)
            if page.locator(".js-product-cart").count() > 0:
                return
        except Exception:
            pass

    # Try navigation to discount-related section by text.
    discount_sub = "\u0441\u043a\u0438\u0434"  # "СЃРєРёРґ"
    candidates = page.locator("a")
    count = candidates.count()
    for idx in range(min(count, 80)):
        link = candidates.nth(idx)
        txt = (link.inner_text() or "").strip().lower()
        if discount_sub not in txt:
            continue
        href = link.get_attribute("href")
        if not href:
            continue
        if href.startswith("/"):
            href = "https://vkusvill.ru" + href
        try:
            page.goto(href, wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_timeout(1800)
            return
        except Exception:
            continue


def _click_refresh_discounts(page) -> bool:
    # Try to click any visible refresh control for personal "6 discounts".
    clicked = page.evaluate(
        """
        () => {
          const norm = (s) => (s || '').replace(/\\u00a0/g, ' ').replace(/\\s+/g, ' ').trim().toLowerCase();
          const phrases = [
            'обновить 6 скидок',
            'обновить скидки',
            'обновить подборку',
            'сменить 6 скидок',
            'поменять 6 скидок',
            'обновить',
          ];
          const nodes = Array.from(document.querySelectorAll('button, a, [role="button"], div, span'));
          for (const el of nodes) {
            const txt = norm(el.innerText);
            if (!txt || el.offsetParent === null) continue;
            if (phrases.some((p) => txt.includes(p))) {
              el.click();
              return true;
            }
          }
          return false;
        }
        """
    )
    if not clicked:
        return False

    page.wait_for_timeout(1600)
    # Some flows show a confirmation button.
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
    page.wait_for_timeout(2600)
    return True


def _collect_waves(page, source: str, waves: int) -> list[DiscountItem]:
    merged: dict[str, DiscountItem] = {}
    total_waves = max(1, waves)
    for wave_idx in range(total_waves):
        _open_discounts_area(page)
        current = _collect_from_dom(page, source)
        for item in current:
            merged.setdefault(item.item_id, item)
        if wave_idx == total_waves - 1:
            break
        if not _click_refresh_discounts(page):
            break
    return list(merged.values())


def _is_logged_in(page) -> bool:
    text = page.inner_text("body")
    lowered = text.lower()
    # Strong login wall markers.
    if "Р°РІС‚РѕСЂРёР·СѓР№С‚РµСЃСЊ РїРѕ" in lowered:
        return False
    if "РІРІРµРґРёС‚Рµ РЅРѕРјРµСЂ С‚РµР»РµС„РѕРЅР°" in lowered:
        return False
    if "СЃ РєР°СЂС‚РѕР№ РІС‹РіРѕРґРЅРµРµ" in lowered and "РІРѕР№С‚Рё" in lowered:
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
    parser.add_argument("--chrome-profile-name", default="auto")
    parser.add_argument("--out-file", default="data/today_discounts.json")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--interactive-login", action="store_true")
    parser.add_argument("--max-items", type=int, default=24)
    parser.add_argument("--waves", type=int, default=1, help="How many waves to collect (1..3).")
    return parser.parse_args()


def _collect_with_storage_state(args: argparse.Namespace) -> list[DiscountItem]:
    state_file = Path(args.state_file)
    if not state_file.exists():
        raise SystemExit(f"State file not found: {state_file}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=args.headless)
        context = browser.new_context(storage_state=str(state_file), locale="ru-RU")
        page = context.new_page()
        _open_discounts_area(page)
        if not _is_logged_in(page):
            _save_debug(page, "storage_state_not_logged_in")
            raise SystemExit(
                "VkusVill is not logged in for storage_state session. "
                "Re-auth required. Debug saved to out/debug."
            )
        items = _collect_waves(page, "vkusvill_web_storage_state", waves=args.waves)
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
        try:
            context = p.chromium.launch_persistent_context(
                channel="chrome",
                user_data_dir=str(user_data_dir),
                headless=args.headless,
                locale="ru-RU",
                timezone_id="Europe/Moscow",
                args=[f"--profile-directory={profile_name}", "--disable-blink-features=AutomationControlled"],
            )
        except Exception as exc:
            raise SystemExit(
                "Failed to open Chrome profile. Close all Chrome windows and retry. "
                f"Details: {exc}"
            ) from exc

        context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )
        page = context.new_page()
        _open_discounts_area(page)
        if not _is_logged_in(page):
            if args.interactive_login:
                print("VkusVill login required in automation browser.")
                print("Sign in on the opened page. Waiting up to 10 minutes...")
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
                items = _collect_waves(page, "vkusvill_web_system_chrome", waves=args.waves)
                context.close()
                return items
            _save_debug(page, "system_chrome_not_logged_in")
            context.close()
            raise SystemExit(
                "VkusVill account is not logged in in selected Chrome profile. "
                "Login in Chrome first, then retry. Debug saved to out/debug."
            )
        items = _collect_waves(page, "vkusvill_web_system_chrome", waves=args.waves)
        context.close()

    return items


def main() -> None:
    args = parse_args()
    args.waves = max(1, min(int(args.waves), 3))
    out_path = Path(args.out_file)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    if args.source == "storage_state":
        items = _collect_with_storage_state(args)
    else:
        items = _collect_with_system_chrome(args)

    items = [x for x in items if x.name and x.discount_price > 0][: args.max_items]
    if not items:
        raise SystemExit("No discounts detected. Check login status and page selectors.")

    payload = [item.as_dict() for item in items]
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False))


if __name__ == "__main__":
    main()

