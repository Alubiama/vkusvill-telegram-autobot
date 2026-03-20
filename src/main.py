from __future__ import annotations

import atexit
import logging
import os
import re
import socket
import time
from pathlib import Path

from dotenv import load_dotenv
from telegram.error import NetworkError, RetryAfter, TimedOut

from .bot import VkusvillGroupBot
from .config import load_settings
from .providers import create_provider
from .runtime_guard import current_project_root, describe_runtime_root
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


def _backoff_sleep_seconds(attempt: int) -> int:
    # Gentle capped backoff so temporary DNS/VPN issues don't kill the bot.
    if attempt <= 1:
        return 3
    if attempt == 2:
        return 8
    if attempt == 3:
        return 15
    return min(60, 15 + (attempt - 3) * 10)


def _network_error_hint(exc: Exception) -> str:
    text = str(exc or "")
    lowered = text.lower()
    if "getaddrinfo failed" in lowered or "connecterror" in lowered:
        return " DNS/сеть до Telegram API недоступны"
    if "timed out" in lowered:
        return " Telegram API не ответил вовремя"
    return ""


def _pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _install_pid_lock(project_root: Path) -> Path | None:
    pid_path = project_root / "data" / "bot.pid"
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    current_pid = os.getpid()
    if pid_path.exists():
        try:
            existing_pid = int(pid_path.read_text(encoding="utf-8").strip())
        except Exception:
            existing_pid = 0
        if existing_pid and existing_pid != current_pid and _pid_is_alive(existing_pid):
            logging.error("[bot] already running, pid=%s", existing_pid)
            return None
        if existing_pid:
            logging.warning("[bot] replacing stale pid lock: old_pid=%s", existing_pid)
    pid_path.write_text(str(current_pid), encoding="utf-8")

    def _cleanup_pid() -> None:
        try:
            if pid_path.exists() and pid_path.read_text(encoding="utf-8").strip() == str(current_pid):
                pid_path.unlink()
        except Exception:
            logging.warning("Failed to remove pid lock: %s", pid_path)

    atexit.register(_cleanup_pid)
    return pid_path


def main() -> None:
    # Prefer project .env values over inherited shell/user variables.
    project_root = current_project_root()
    env_path = project_root / ".env"
    load_dotenv(dotenv_path=env_path, override=True)
    _configure_logging()

    runtime_state, runtime_detail, _registered_root = describe_runtime_root(project_root=project_root)
    if runtime_state == "error":
        logging.error("Refusing to start from non-canonical workspace: %s", runtime_detail)
        return
    if runtime_state == "warning":
        logging.warning("Runtime root check: %s", runtime_detail)
    else:
        logging.info("Runtime root check: %s", runtime_detail)

    pid_lock_path = _install_pid_lock(project_root)
    if pid_lock_path is None:
        return

    settings = load_settings()
    lock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        lock.bind(("127.0.0.1", 45731))
    except OSError:
        logging.error("Another bot instance is already running. Exit. pid_lock=%s", pid_lock_path)
        return

    store = StateStore(settings.db_path)
    provider = create_provider(settings)
    attempt = 0
    while True:
        attempt += 1
        service = VkusvillGroupBot(settings=settings, store=store, provider=provider)
        app = service.build_app()
        try:
            backup_path = service.backup_state_db("state_startup.db")
            logging.info("Startup DB backup created: %s", backup_path)
        except Exception as exc:
            logging.warning("Startup DB backup failed: %s", exc)
        try:
            logging.info("Starting bot polling (attempt=%s)", attempt)
            app.run_polling(close_loop=False)
            logging.warning("Bot polling stopped unexpectedly without exception. Restarting.")
        except RetryAfter as exc:
            wait_sec = max(3, int(getattr(exc, "retry_after", 10)))
            logging.warning("Telegram asked to retry later. Sleeping %s sec.", wait_sec)
            time.sleep(wait_sec)
        except (NetworkError, TimedOut, OSError) as exc:
            wait_sec = _backoff_sleep_seconds(attempt)
            logging.warning(
                "Bot polling crashed on network issue:%s. Restart in %s sec. Error: %s",
                _network_error_hint(exc),
                wait_sec,
                exc,
            )
            time.sleep(wait_sec)
        except Exception as exc:
            wait_sec = _backoff_sleep_seconds(attempt)
            logging.exception("Bot polling crashed. Restart in %s sec.", wait_sec)
            time.sleep(wait_sec)


if __name__ == "__main__":
    main()
