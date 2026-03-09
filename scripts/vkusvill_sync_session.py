from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from playwright.sync_api import sync_playwright


DEFAULT_LOGIN_URL = "https://vkusvill.ru/personal/"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Sync VkusVill session from local Chrome profile to Playwright storage_state."
    )
    parser.add_argument(
        "--state-file",
        default="data/vkusvill_storage_state.json",
        help="Path to write Playwright storage state JSON.",
    )
    parser.add_argument(
        "--chrome-profile-name",
        default="Default",
        help="Chrome profile directory name (Default, Profile 1, etc.).",
    )
    parser.add_argument(
        "--url",
        default=DEFAULT_LOGIN_URL,
        help="URL to open before saving state.",
    )
    parser.add_argument(
        "--skip-nav",
        action="store_true",
        help="Skip navigation and only export current profile storage state.",
    )
    parser.add_argument(
        "--nav-timeout-ms",
        type=int,
        default=30000,
        help="Navigation timeout in milliseconds.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    local_app_data = os.getenv("LOCALAPPDATA")
    if not local_app_data:
        raise SystemExit("LOCALAPPDATA is not set")

    state_path = Path(args.state_file)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    chrome_user_data = Path(local_app_data) / "Google" / "Chrome" / "User Data"
    if not chrome_user_data.exists():
        raise SystemExit(f"Chrome profile root not found: {chrome_user_data}")

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            channel="chrome",
            user_data_dir=str(chrome_user_data),
            headless=False,
            locale="ru-RU",
            timezone_id="Europe/Moscow",
            args=[f"--profile-directory={args.chrome_profile_name}"],
        )
        hints = ["выйти", "личный кабинет", "мои заказы", "моя карта"]
        ok = False
        if not args.skip_nav:
            page = context.new_page()
            try:
                page.goto(args.url, wait_until="domcontentloaded", timeout=args.nav_timeout_ms)
                page.wait_for_timeout(1500)
                html = page.content().lower()
                ok = any(h in html for h in hints)
            except Exception:
                ok = False
        context.storage_state(path=str(state_path))
        context.close()

    payload = {
        "ok": ok,
        "state_file": str(state_path),
        "profile": args.chrome_profile_name,
        "skip_nav": args.skip_nav,
    }
    print(json.dumps(payload, ensure_ascii=False))
    if (not ok) and (not args.skip_nav):
        raise SystemExit(2)


if __name__ == "__main__":
    main()
