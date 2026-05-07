#!/usr/bin/env python3
"""Proactively refresh mobile_api access_token. Run via cron every 12h."""
import sys, json, pathlib, logging, os

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("refresh")

from src.providers import MobileApiProvider

proxy = "socks5h://127.0.0.1:1080"
token_file = ROOT / "data" / "mobile_tokens.json"

p = MobileApiProvider(token_file=str(token_file), timeout_sec=30, proxy=proxy)
tokens = p._load_tokens()
try:
    p._refresh_access_token(tokens)
    log.info("refresh ok")
except Exception as e:
    log.error("refresh failed: %s %s", type(e).__name__, e)
    sys.exit(1)
