from __future__ import annotations

import argparse
import sqlite3
from datetime import datetime
from urllib.parse import quote_plus

from playwright.sync_api import sync_playwright


def _norm(s: str) -> str:
    return " ".join((s or "").replace("\xa0", " ").lower().replace("ё", "е").split())


def _tokens(s: str) -> list[str]:
    return [x for x in _norm(s).split(" ") if len(x) >= 3]


def _best_image_from_search(page, name: str) -> str:
    page.goto(
        f"https://vkusvill.ru/search/?q={quote_plus(name)}",
        wait_until="domcontentloaded",
        timeout=120_000,
    )
    page.wait_for_timeout(1100)
    query_tokens = set(_tokens(name))
    rows = page.evaluate(
        """
        () => {
          const norm = (s) => (s || '').replace(/\\u00a0/g, ' ').replace(/\\s+/g, ' ').trim().toLowerCase().replace(/ё/g, 'е');
          const cards = Array.from(document.querySelectorAll('.js-datalayer-catalog-list-item[data-xmlid], .js-product-cart'));
          const out = [];
          for (const el of cards) {
            if (!el || el.offsetParent === null) continue;
            const name =
              norm((el.querySelector('.js-datalayer-catalog-list-name') || {}).innerText || '') ||
              norm((el.querySelector('[class*="name"]') || {}).innerText || '');
            if (!name) continue;
            const image =
              (el.querySelector('img') || {}).getAttribute?.('src') ||
              (el.querySelector('img') || {}).getAttribute?.('data-src') ||
              '';
            if (!image) continue;
            out.push({ name, image });
          }
          return out;
        }
        """
    )
    if not rows:
        return ""

    best_score = -1
    best_image = ""
    for row in rows:
        rn = _norm(str(row.get("name") or ""))
        if not rn:
            continue
        rt = set(_tokens(rn))
        overlap = len(query_tokens.intersection(rt))
        score = overlap * 2
        if _norm(name) in rn or rn in _norm(name):
            score += 3
        if score > best_score:
            best_score = score
            best_image = str(row.get("image") or "")
    if best_image.startswith("//"):
        return f"https:{best_image}"
    if best_image.startswith("/"):
        return f"https://vkusvill.ru{best_image}"
    return best_image


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Backfill missing image_url in local state.db for current day items.")
    p.add_argument("--db-path", default="data/state.db")
    p.add_argument("--day", default="")
    p.add_argument("--chrome-user-data-dir", default="data/chrome-user-data")
    p.add_argument("--chrome-profile-name", default="Default")
    p.add_argument("--headless", action="store_true", default=True)
    return p.parse_args()


def main() -> None:
    args = parse_args()
    day = args.day or datetime.now().strftime("%Y-%m-%d")

    conn = sqlite3.connect(args.db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT item_id, name
        FROM items
        WHERE day = ? AND COALESCE(image_url, '') = ''
        ORDER BY rowid
        """,
        (day,),
    ).fetchall()
    if not rows:
        print('{"ok": true, "updated": 0, "day": "%s"}' % day)
        return

    updated = 0
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            channel="chrome",
            user_data_dir=args.chrome_user_data_dir,
            headless=bool(args.headless),
            locale="ru-RU",
            timezone_id="Europe/Moscow",
            args=[
                f"--profile-directory={args.chrome_profile_name}",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")
        page = context.new_page()

        for row in rows:
            item_id = str(row["item_id"])
            name = str(row["name"])
            image = _best_image_from_search(page, name)
            if not image:
                continue
            conn.execute(
                "UPDATE items SET image_url = ? WHERE day = ? AND item_id = ?",
                (image, day, item_id),
            )
            updated += 1

        context.close()
    conn.commit()
    conn.close()
    print('{"ok": true, "updated": %d, "day": "%s"}' % (updated, day))


if __name__ == "__main__":
    main()
