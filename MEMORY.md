# Project: VkusVill Telegram Autobot
## Status: active development
## Next agent: claude
## Last updated: 2026-03-20 by codex

## What this project is
Telegram bot and Mini App for coordinated VkusVill discount ordering. It collects discounts, shows them in the Mini App, tracks user selections, adds orders to cart, and keeps debt bookkeeping.

## Project files
- MEMORY.md
- NOTES.md
- README.md
- ROADMAP.md
- TEST_MATRIX.md
- P0_CHECKLIST.md
- SMOKE_CHECKLIST.md
- AUTOTEST_PLAN.md
- SKILL_STACK.md
- src/
- tests/
- scripts/
- webapp/
- log/CHANGELOG.md
- scripts/vkusvill_enrich_nutrition.py
- scripts/vkusvill_mobile_session_check.py
- scripts/vkusvill_refresh_token.py
- scripts/live_system_audit.py
- src/mobile_api.py
- tests/test_mobile_api.py
- tests/test_script_entrypoints.py
- TASKS.md
- tests/test_bot_backend_guards.py
- tests/test_nutrition_parser.py

## Key decisions
- Stack: Python + Playwright + Telegram Bot API
- Mini App: web-based via Telegram WebApp
- Session: RPA-based auth with VkusVill
- Existing docs (ROADMAP, TEST_MATRIX) serve as PRD and tests

## Current state
- [x] Canonical workspace root moved to X:\vkusvill-telegram-autobot
- [x] Core bot working (collect, order, debts)
- [x] Mini App exists
- [x] Mini App UX pass
- [x] Operational hardening batch landed (cleanup, DB backup, session audit, nutrition enricher)
- [x] Mobile API token refresh and non-Playwright session health check landed
- [x] Startup sanity check and live system audit landed
- [ ] Product packaging
- [ ] Multi-owner architecture
- [ ] First external users
- [ ] Monetization

## What changed last
Codex fixed another owner-flow bug in `src/bot.py`: `collectnow` was still `open`-only, so when a live batch moved to `partially_added` the owner button `Собрать заказ в корзину` lied with `Сейчас нет open batch для сборки.` even though there were missing positions to retry. The new behavior is `active`-aware: `collectnow` now retries the current active batch, uses full payload for `open`, uses missing-only payload for `partially_added`, and only refuses when the batch is already fully `added_waiting_payment` or when there is no active batch at all. Regression coverage was added in `tests/test_bot_backend_guards.py`. Live confirmation on 2026-03-20: `batch #3` was in `partially_added`, first retry reduced the missing set from 10 to 1, and the second retry completed the last missing item and moved the batch to `added_waiting_payment`.

## Open questions
None right now. See ROADMAP.md for the full plan.

## For Claude
Read ROADMAP.md for product direction. Read TEST_MATRIX.md for reliability picture. Keep the canonical-root guard in `src/runtime_guard.py`, `src/main.py`, and `scripts/ensure-bot-running.ps1`. Keep the app-level verification gate: `src/bot.py` only emits a normal Mini App payload when `last_collect_status == ok` for today, and `webapp/index.html` must respect `force_stale`. Critical invariants now: for `mode='all'`, the collector/today-pool is the source of truth for Mini App rows; do not reintroduce same-day DB preservation or "richer snapshot" selection over verified live rows; do not restore `_preserve_best_regular_set`; and do not remove `--offers-ready-food-url` from `.env` unless you intentionally want the ready-food block hidden. Also keep the new hardening guards intact: `scripts/vkusvill_collect_discounts.py` should abort early on low disk, stop refresh attempts after a server rejection, never use `data-max` as a fake stock source, and `src/main.py` must keep `data/bot.pid` in sync with the socket lock. `tests/test_bot_backend_guards.py` must stay isolated from the real repo cwd, otherwise the test suite can overwrite the live `webapp/latest.json`. Keep the new startup observability too: `src/bot.py` startup sanity should continue probing group binding / `get_chat()` / runtime root and recording `last_startup_sanity_*`, and `scripts/live_system_audit.py` should remain the one-shot operator entrypoint for runtime + Telegram + collect health.

## For Codex
Current priority: Task 73 -- full system audit. Write audit/AUDIT-2026-03-20.md with verdicts. NO FIXES. After audit is done, set Next agent: claude. Keep future edits local and do not restructure the bot unless a task explicitly requires it. If you touch the mobile API path again, keep the refresh/write-back flow aligned between the bot and the standalone scripts. If you touch watchdog/runtime code, keep three invariants: canonical path must come from `C:\Users\Sasha\projects\REGISTRY.md`, old copies must delegate to X:, and the healthy child interpreter of the venv launcher must not be killed as stale.

## Token budget
- MEMORY.md - read fully
- NOTES.md - read fully
- ROADMAP.md - read fully
- README.md - skip unless onboarding context is needed
- TEST_MATRIX.md - read when testing/reviewing
- src/ - large, read selectively by task
- webapp/ - read when working on Mini App
