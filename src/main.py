from __future__ import annotations

import logging

from dotenv import load_dotenv

from .bot import VkusvillGroupBot
from .config import load_settings
from .providers import create_provider
from .store import StateStore


def main() -> None:
    load_dotenv()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    settings = load_settings()
    store = StateStore(settings.db_path)
    provider = create_provider(settings)
    service = VkusvillGroupBot(settings=settings, store=store, provider=provider)
    app = service.build_app()
    app.run_polling(close_loop=False)


if __name__ == "__main__":
    main()
