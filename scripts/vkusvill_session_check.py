from __future__ import annotations

import argparse
import json
from pathlib import Path

from playwright.sync_api import sync_playwright


DEFAULT_URL = "https://vkusvill.ru/personal/"
FALLBACK_URL = "https://vkusvill.ru/cart/"
LOGIN_PROMPT_SELECTORS = (
    "input[type='tel']",
    "input[placeholder*='телефон']",
    "button:has-text('Продолжить')",
)
LOGIN_PROMPT_HINTS = (
    "авторизуйтесь во вкусвилл",
    "введите номер телефона",
    "подтвердите номер",
    "получить код",
)


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


def _has_login_prompt(page) -> bool:
    for selector in LOGIN_PROMPT_SELECTORS:
        try:
            if page.locator(selector).count() > 0:
                return True
        except Exception:
            continue
    body = " ".join((page.inner_text("body") or "").replace("\xa0", " ").split()).lower()
    return any(hint in body for hint in LOGIN_PROMPT_HINTS)


def _goto_with_retry(page, url: str, *, timeout: int = 90_000, attempts: int = 2) -> None:
    last_exc: Exception | None = None
    for idx in range(max(1, attempts)):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=timeout)
            return
        except Exception as exc:
            last_exc = exc
            if idx >= attempts - 1:
                break
            page.wait_for_timeout(1500 * (idx + 1))
    if last_exc is not None:
        raise last_exc


def main() -> None:
    args = parse_args()
    state_path = Path(args.state_file)
    if not state_path.exists():
        raise SystemExit(f"State file not found: {state_path}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(storage_state=str(state_path), locale="ru-RU")
        page = context.new_page()
        last_exc: Exception | None = None
        for candidate in (args.url, FALLBACK_URL):
            try:
                _goto_with_retry(page, candidate)
                break
            except Exception as exc:
                last_exc = exc
        else:
            assert last_exc is not None
            raise last_exc
        html = page.content().lower()
        url = page.url
        has_login_prompt = _has_login_prompt(page)
        browser.close()

    # Fail closed: the presence of a phone form means this snapshot is not a valid login.
    hints = ["выйти", "профиль", "личный кабинет", "мои заказы"]
    authenticated = bool(not has_login_prompt and any(h in html for h in hints))
    payload = {"ok": authenticated, "url": url, "hints": hints, "login_prompt": has_login_prompt}
    print(json.dumps(payload, ensure_ascii=False))
    if not authenticated:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
