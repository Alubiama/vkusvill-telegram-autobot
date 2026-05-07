from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.mobile_api import DEFAULT_ENV_FILE, check_mobile_session, load_mobile_config


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check VkusVill mobile API session without Playwright.")
    parser.add_argument(
        "--env-file",
        default=str(DEFAULT_ENV_FILE),
        help="Path to .env with VV_* mobile tokens.",
    )
    parser.add_argument(
        "--no-write-back",
        action="store_true",
        help="Do not persist refreshed tokens back to the env file.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_mobile_config(Path(args.env_file))
    result = check_mobile_session(config, persist=not args.no_write_back)
    print(json.dumps(result.to_payload(), ensure_ascii=False))
    raise SystemExit(0 if result.ok else 2 if result.status == "auth_failed" else 1)


if __name__ == "__main__":
    main()
