from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export VkusVill session state from system Chrome via CDP."
    )
    parser.add_argument("--state-file", default="data/vkusvill_storage_state.json")
    parser.add_argument(
        "--profile-name",
        default="auto",
        help='Chrome profile directory name. Use "auto" to pick last used profile.',
    )
    parser.add_argument("--port", type=int, default=9222)
    parser.add_argument("--url", default="https://vkusvill.ru/personal/")
    parser.add_argument("--timeout-sec", type=int, default=45)
    return parser.parse_args()


def _detect_last_profile(user_data_dir: Path) -> str:
    local_state = user_data_dir / "Local State"
    if not local_state.exists():
        return "Default"
    try:
        payload = json.loads(local_state.read_text(encoding="utf-8"))
        last_used = payload.get("profile", {}).get("last_used")
        if isinstance(last_used, str) and last_used.strip():
            return last_used.strip()
    except Exception:
        pass
    return "Default"


def _chrome_path() -> Path:
    candidates = [
        Path("C:/Program Files/Google/Chrome/Application/chrome.exe"),
        Path("C:/Program Files (x86)/Google/Chrome/Application/chrome.exe"),
    ]
    for path in candidates:
        if path.exists():
            return path
    raise SystemExit("Chrome executable not found")


def _wait_cdp(port: int, timeout_sec: int) -> None:
    endpoint = f"http://127.0.0.1:{port}/json/version"
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(endpoint, timeout=2) as resp:
                if resp.status == 200:
                    return
        except (urllib.error.URLError, TimeoutError):
            time.sleep(0.4)
    raise SystemExit(f"CDP endpoint not available: {endpoint}")


def main() -> None:
    args = parse_args()
    state_path = Path(args.state_file)
    state_path.parent.mkdir(parents=True, exist_ok=True)

    local_app_data = os.getenv("LOCALAPPDATA")
    if not local_app_data:
        raise SystemExit("LOCALAPPDATA is not set")
    user_data_dir = Path(local_app_data) / "Google" / "Chrome" / "User Data"
    profile_name = args.profile_name or _detect_last_profile(user_data_dir)
    if profile_name.lower() == "auto":
        profile_name = _detect_last_profile(user_data_dir)

    # Ensure profile lock is free.
    subprocess.run(
        ["taskkill", "/F", "/IM", "chrome.exe"],
        check=False,
        capture_output=True,
        text=True,
    )
    time.sleep(1)

    chrome = _chrome_path()
    launch_cmd = [
        str(chrome),
        f"--remote-debugging-port={args.port}",
        f"--user-data-dir={user_data_dir}",
        f"--profile-directory={profile_name}",
        "--new-window",
        "about:blank",
    ]
    proc = subprocess.Popen(launch_cmd)

    try:
        _wait_cdp(args.port, args.timeout_sec)
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{args.port}")
            if browser.contexts:
                context = browser.contexts[0]
            else:
                context = browser.new_context()

            page = context.new_page()
            ok = False
            try:
                page.goto(args.url, wait_until="domcontentloaded", timeout=35_000)
                page.wait_for_timeout(2000)
                html = page.content().lower()
                hints = ["выйти", "личный кабинет", "мои заказы", "моя карта"]
                ok = any(h in html for h in hints)
            except Exception:
                ok = False

            context.storage_state(path=str(state_path))
            browser.close()

        payload = {
            "ok": ok,
            "state_file": str(state_path),
            "profile": profile_name,
            "port": args.port,
        }
        print(json.dumps(payload, ensure_ascii=False))
        if not ok:
            raise SystemExit(2)
    finally:
        if proc.poll() is None:
            proc.terminate()
            time.sleep(0.5)
        if proc.poll() is None:
            proc.kill()

if __name__ == "__main__":
    main()

