from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright


RUB_RE = re.compile(r"(\d[\d\s]*[.,]?\d*)\s*(?:₽|руб|р\b|RUB)", re.IGNORECASE)


def _normalize_ws(value: str) -> str:
    return " ".join((value or "").replace("\xa0", " ").split())


def _parse_price(token: str) -> float:
    normalized = token.replace(" ", "").replace(",", ".")
    return float(normalized)


def _name_key(value: str) -> str:
    txt = _normalize_ws(value).lower().replace("ё", "е")
    txt = re.sub(r"[\"'`“”„()\\[\\]{}:;,.!?/+\\-]", " ", txt)
    return re.sub(r"\s+", " ", txt).strip()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build cart discount report from VkusVill web cart.")
    parser.add_argument("--discounts-json", default="data/today_discounts.json")
    parser.add_argument("--chrome-user-data-dir", default="data/chrome-user-data")
    parser.add_argument("--chrome-profile-name", default="Default")
    parser.add_argument("--headless", action="store_true", default=True)
    return parser.parse_args()


def _collect_cart_items(page) -> list[dict]:
    page.goto("https://vkusvill.ru/cart/", wait_until="domcontentloaded", timeout=120_000)
    page.wait_for_timeout(3500)

    raw = page.evaluate(
        """
        () => {
          const norm = (s) => (s || '').replace(/\\u00a0/g, ' ').replace(/\\s+/g, ' ').trim();
          const body = norm(document.body ? document.body.innerText : '');
          if (body.includes('Корзина ждёт, пока её наполнят')) {
            return { empty: true, rows: [] };
          }

          const selectors = [
            '.js-delivery__basket--item',
            '.js-delivery__basket--product',
            '.DeliveryBasket__Item',
            '.Delivery__Order__OrderBodyItem',
            '[class*="BasketItem"]',
            '[class*="CartItem"]',
            '[class*="OrderProduct"]',
          ];

          const nodes = [];
          for (const sel of selectors) {
            for (const el of document.querySelectorAll(sel)) nodes.push(el);
          }
          const uniq = Array.from(new Set(nodes));

          const rows = [];
          for (const el of uniq) {
            if (!el || el.offsetParent === null) continue;
            const text = norm(el.innerText || '');
            if (!text || text.length < 5) continue;

            let name = '';
            const nameSelectors = [
              '.js-datalayer-catalog-list-name',
              '[class*="name"]',
              'a[href*="/goods/"]',
            ];
            for (const s of nameSelectors) {
              const n = el.querySelector(s);
              if (!n) continue;
              const t = norm(n.innerText || '');
              if (t && t.length >= 3) {
                name = t;
                break;
              }
            }

            if (!name) {
              const a = el.querySelector('a[href*="/goods/"]');
              if (a) name = norm(a.innerText || '');
            }
            if (!name || name.length < 3) continue;

            const qtyInput = el.querySelector('input[type="number"], input[name*="quantity"], input[name*="count"]');
            let qty = 1;
            if (qtyInput && qtyInput.value) {
              const q = parseInt(qtyInput.value, 10);
              if (Number.isFinite(q) && q > 0) qty = q;
            }

            rows.push({
              name,
              qty,
              text,
            });
          }

          // Fallback: visible product links that are not from recommendation blocks.
          if (!rows.length) {
            const anchors = Array.from(document.querySelectorAll('a[href*="/goods/"]'));
            for (const a of anchors) {
              if (!a || a.offsetParent === null) continue;
              const name = norm(a.innerText || '');
              if (!name || name.length < 3) continue;
              const root = a.closest('article, li, div');
              const text = norm(root ? root.innerText : '');
              if (text.includes('Добавьте в заказ')) continue;
              rows.push({ name, qty: 1, text });
            }
          }

          return { empty: false, rows };
        }
        """
    )
    if raw.get("empty"):
        return []

    unique: dict[str, dict] = {}
    for row in raw.get("rows", []):
        name = _normalize_ws(str(row.get("name") or ""))
        if not name:
            continue
        key = _name_key(name)
        if key in unique:
            unique[key]["qty"] += int(row.get("qty") or 1)
            continue
        unique[key] = {
            "name": name,
            "qty": max(1, int(row.get("qty") or 1)),
            "text": str(row.get("text") or ""),
        }
    return list(unique.values())


def _load_discounts(path: Path) -> list[dict]:
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows: list[dict] = []
    for x in payload:
        name = _normalize_ws(str(x.get("name") or ""))
        if not name:
            continue
        try:
            price = float(x.get("price") or 0)
            disc = float(x.get("discount_price") or 0)
        except Exception:
            continue
        rows.append(
            {
                "item_id": str(x.get("item_id") or ""),
                "name": name,
                "price": price,
                "discount_price": disc,
                "source": str(x.get("source") or ""),
                "key": _name_key(name),
            }
        )
    return rows


def _best_discount_match(cart_name: str, discounts: list[dict]) -> dict | None:
    key = _name_key(cart_name)
    exact = [x for x in discounts if x["key"] == key]
    if exact:
        return exact[0]

    # Fuzzy fallback for minor naming differences.
    candidates = []
    for row in discounts:
        a = key
        b = row["key"]
        if not a or not b:
            continue
        if a in b or b in a:
            common = min(len(a), len(b))
            if common >= 10:
                candidates.append((common, row))
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1]


def build_report(discounts: list[dict], cart_items: list[dict]) -> dict:
    matches: list[dict] = []
    missed: list[dict] = []

    for cart in cart_items:
        hit = _best_discount_match(cart["name"], discounts)
        if hit is None:
            missed.append({"name": cart["name"], "qty": int(cart["qty"])})
            continue

        saving = max(0.0, float(hit["price"]) - float(hit["discount_price"]))
        qty = max(1, int(cart["qty"]))
        saving_total = round(saving * qty, 2)
        saving_pct = round((saving / hit["price"] * 100.0), 2) if hit["price"] > 0 else 0.0
        matches.append(
            {
                "name": hit["name"],
                "qty": qty,
                "price": round(float(hit["price"]), 2),
                "discount_price": round(float(hit["discount_price"]), 2),
                "saving_per_item": round(saving, 2),
                "saving_total": saving_total,
                "saving_percent": saving_pct,
                "source": hit["source"],
            }
        )

    matches.sort(key=lambda x: (x["saving_total"], x["saving_per_item"], x["name"]), reverse=True)
    return {
        "cart_count": len(cart_items),
        "matched_count": len(matches),
        "unmatched_count": len(missed),
        "matches": matches,
        "unmatched": missed,
    }


def main() -> None:
    args = parse_args()
    discounts = _load_discounts(Path(args.discounts_json))
    if not discounts:
        print(json.dumps({"error": "discounts_empty"}, ensure_ascii=False))
        return

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            channel="chrome",
            user_data_dir=str(Path(args.chrome_user_data_dir)),
            headless=bool(args.headless),
            locale="ru-RU",
            timezone_id="Europe/Moscow",
            args=[
                f"--profile-directory={args.chrome_profile_name}",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        page = context.new_page()
        cart_items = _collect_cart_items(page)
        context.close()

    report = build_report(discounts, cart_items)
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        sys.exit(1)
