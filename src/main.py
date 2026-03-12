from __future__ import annotations

import logging
import re
import socket
from pathlib import Path

from dotenv import load_dotenv

from .bot import VkusvillGroupBot
from .config import load_settings
from .providers import create_provider
from .store import StateStore


_BOT_TOKEN_RE = re.compile(r"bot\d{6,}:[A-Za-z0-9_-]{20,}")


def _redact_secret_text(value: str) -> str:
    return _BOT_TOKEN_RE.sub("bot<redacted>", value)


class _RedactSecretsFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = _redact_secret_text(record.msg)

        args = record.args
        if isinstance(args, tuple):
            record.args = tuple(_redact_secret_text(x) if isinstance(x, str) else x for x in args)
        elif isinstance(args, dict):
            record.args = {k: _redact_secret_text(v) if isinstance(v, str) else v for k, v in args.items()}
        return True


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    redactor = _RedactSecretsFilter()
    root_logger = logging.getLogger()
    for handler in root_logger.handlers:
        handler.addFilter(redactor)

    # Reduce noisy 3rd-party HTTP logs; they may include sensitive URLs.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.WARNING)


def main() -> None:
    # Prefer project .env values over inherited shell/user variables.
    env_path = Path(__file__).resolve().parents[1] / ".env"
    load_dotenv(dotenv_path=env_path, override=True)
    _configure_logging()

    settings = load_settings()
    lock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        lock.bind(("127.0.0.1", 45731))
    except OSError:
        logging.error("Another bot instance is already running. Exit.")
        return

    store = StateStore(settings.db_path)
    provider = create_provider(settings)
    service = VkusvillGroupBot(settings=settings, store=store, provider=provider)
    app = service.build_app()
    app.run_polling(close_loop=False)


if __name__ == "__main__":
    main()
