from __future__ import annotations

import argparse
import json
from pathlib import Path

from playwright.sync_api import sync_playwright


DEFAULT_URL = "https://vkusvill.ru/personal/"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check saved VkusVill web session.")
    parser.add_argument(
        "--state-file",
        default="data/vkusvill_storage_state.json",
        help="Path to Playwright storage state JSON.",
    )
    parser.add_argument(
        "--url",
        default=DEFAULT_URL,
        help="Account URL to verify login state.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    state_path = Path(args.state_file)
    if not state_path.exists():
        raise SystemExit(f"State file not found: {state_path}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(storage_state=str(state_path), locale="ru-RU")
        page = context.new_page()
        page.goto(args.url, wait_until="networkidle", timeout=120_000)
        html = page.content().lower()
        url = page.url
        browser.close()

    # Heuristic check; adjust later if site markup changes.
    hints = ["выйти", "профиль", "личный кабинет", "мои заказы"]
    authenticated = any(h in html for h in hints)
    payload = {"ok": authenticated, "url": url, "hints": hints}
    print(json.dumps(payload, ensure_ascii=False))
    if not authenticated:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
