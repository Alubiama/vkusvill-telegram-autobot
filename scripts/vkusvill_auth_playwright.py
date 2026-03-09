from __future__ import annotations

import argparse
import os
from pathlib import Path

from playwright.sync_api import sync_playwright


DEFAULT_LOGIN_URL = "https://vkusvill.ru/personal/"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Interactive VkusVill login and session save via Playwright."
    )
    parser.add_argument(
        "--state-file",
        default="data/vkusvill_storage_state.json",
        help="Path to write Playwright storage state JSON.",
    )
    parser.add_argument(
        "--profile-dir",
        default="data/chromium-profile",
        help="Persistent Chromium profile directory.",
    )
    parser.add_argument(
        "--use-system-chrome-profile",
        action="store_true",
        help="Use local Google Chrome user-data profile instead of isolated test profile.",
    )
    parser.add_argument(
        "--chrome-profile-name",
        default="Default",
        help="Chrome profile directory name (Default, Profile 1, etc.).",
    )
    parser.add_argument(
        "--login-url",
        default=DEFAULT_LOGIN_URL,
        help="VkusVill login/account URL.",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run headless (not recommended for first login).",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    state_path = Path(args.state_file)
    if args.use_system_chrome_profile:
        local_app_data = os.getenv("LOCALAPPDATA")
        if not local_app_data:
            raise SystemExit("LOCALAPPDATA is not set")
        profile_dir = Path(local_app_data) / "Google" / "Chrome" / "User Data"
    else:
        profile_dir = Path(args.profile_dir)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    profile_dir.mkdir(parents=True, exist_ok=True)

    print("Opening browser for VkusVill authorization...")
    print("1) In browser: log in with your phone and SMS code.")
    print("2) Make sure account page is visible.")
    print("3) Return to terminal and press Enter.")
    print("If 'Continue' is disabled, complete captcha/challenge first.")

    with sync_playwright() as p:
        launch_args = [
            "--disable-blink-features=AutomationControlled",
            "--disable-features=IsolateOrigins,site-per-process",
            f"--profile-directory={args.chrome_profile_name}",
        ]
        common_kwargs = dict(
            user_data_dir=str(profile_dir),
            headless=args.headless,
            viewport={"width": 1440, "height": 960},
            locale="ru-RU",
            timezone_id="Europe/Moscow",
        )
        try:
            # Prefer real Chrome for better anti-bot compatibility.
            context = p.chromium.launch_persistent_context(
                channel="chrome",
                args=launch_args,
                **common_kwargs,
            )
        except Exception:
            context = p.chromium.launch_persistent_context(
                args=launch_args,
                **common_kwargs,
            )

        context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )
        page = context.new_page()
        page.goto(args.login_url, wait_until="domcontentloaded", timeout=120_000)

        input("Press Enter after successful login...")

        context.storage_state(path=str(state_path))
        screenshot = state_path.with_suffix(".png")
        page.screenshot(path=str(screenshot), full_page=True)
        context.close()

    print(f"Saved storage state: {state_path}")
    print(f"Saved screenshot:    {screenshot}")


if __name__ == "__main__":
    main()
