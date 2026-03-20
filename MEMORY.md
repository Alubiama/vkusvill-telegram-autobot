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
- [ ] Product packaging
- [ ] Multi-owner architecture
- [ ] First external users
- [ ] Monetization

## What changed last
Codex completed a full hardening/cleanup pass on 2026-03-20 after the stale-menu incident. `scripts/vkusvill_collect_discounts.py` now has a low-disk preflight (`ABORT: disk space low` exit code 2), readable `disk full` handling for Chrome profile launch, and `refresh_exhausted` protection so rejected replace attempts stop after one warning instead of burning the daily limit. The dead `preserving existing regular set` path was removed completely; the day-pool (`today_pool.json` + `today_pool_date.txt`) remains the only same-day accumulator. `src/bot.py` now schedules a startup catch-up collect when `today_discounts.json` is stale for the current day, and it alerts the owner if a collect succeeds with fewer than `COLLECT_MIN_ITEMS` items. `src/main.py` now writes `data/bot.pid`, rejects a live second instance, and cleans the PID lock on exit. Repo junk was cleaned (`.pytest_cache`, repo `__pycache__`, `out.legacy`), and the test suite is green (`77 OK`).

## Open questions
None right now. See ROADMAP.md for the full plan.

## For Claude
Read ROADMAP.md for product direction. Read TEST_MATRIX.md for reliability picture. Keep the canonical-root guard in `src/runtime_guard.py`, `src/main.py`, and `scripts/ensure-bot-running.ps1`. Keep the app-level verification gate: `src/bot.py` only emits a normal Mini App payload when `last_collect_status == ok` for today, and `webapp/index.html` must respect `force_stale`. Critical invariants now: for `mode='all'`, the collector/today-pool is the source of truth for Mini App rows; do not reintroduce same-day DB preservation or "richer snapshot" selection over verified live rows; do not restore `_preserve_best_regular_set`; and do not remove `--offers-ready-food-url` from `.env` unless you intentionally want the ready-food block hidden. Also keep the new hardening guards intact: `scripts/vkusvill_collect_discounts.py` should abort early on low disk, stop refresh attempts after a server rejection, and `src/main.py` must keep `data/bot.pid` in sync with the socket lock. `tests/test_bot_backend_guards.py` must stay isolated from the real repo cwd, otherwise the test suite can overwrite the live `webapp/latest.json`.

## For Codex
Current priority can move back to product packaging unless Sasha adds more operational tasks. Keep future edits local and do not restructure the bot unless a task explicitly requires it. If you touch the mobile API path again, keep the refresh/write-back flow aligned between the bot and the standalone scripts. If you touch watchdog/runtime code, keep three invariants: canonical path must come from `C:\Users\Sasha\projects\REGISTRY.md`, old copies must delegate to X:, and the healthy child interpreter of the venv launcher must not be killed as stale.

## Token budget
- MEMORY.md - read fully
- NOTES.md - read fully
- ROADMAP.md - read fully
- README.md - skip unless onboarding context is needed
- TEST_MATRIX.md - read when testing/reviewing
- src/ - large, read selectively by task
- webapp/ - read when working on Mini App
