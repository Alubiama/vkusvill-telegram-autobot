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
Codex traced the latest "batch #1 пока пуст" incident to a real runtime split: Telegram was being served by an old `C:\Users\Sasha\Documents\vkusvill-telegram-autobot` bot process even though `REGISTRY.md` already points to `X:\vkusvill-telegram-autobot`. The canonical `X:` DB was healthy and `_format_who_chose_text()` returned a non-empty batch, so the empty answer was coming from the wrong workspace, not from missing votes. Codex killed the stale `Documents` bot, confirmed the live process pair is now only `X:\vkusvill-telegram-autobot\.venv\Scripts\python.exe -> Python312\python.exe`, and hardened both stale copies (`D:` and `Documents`) so they can no longer start silently: their `scripts/ensure-bot-running.ps1` now delegates into the canonical path from `C:\Users\Sasha\projects\REGISTRY.md`, and their `src/main.py` now fail-closes with `Refusing to start from non-canonical workspace`. Direct validation after the fix: old watchdog scripts delegate to `X:`, a manual start from `Documents` refuses to run, and the canonical bot still sees a non-empty `batch #1`.

## Open questions
None right now. See ROADMAP.md for the full plan.

## For Claude
Read ROADMAP.md for product direction. Read TEST_MATRIX.md for reliability picture. Keep the canonical-root guard in `src/runtime_guard.py`, `src/main.py`, and `scripts/ensure-bot-running.ps1`. Keep the app-level verification gate: `src/bot.py` only emits a normal Mini App payload when `last_collect_status == ok` for today, and `webapp/index.html` must respect `force_stale`. Critical invariants now: for `mode='all'`, the collector/today-pool is the source of truth for Mini App rows; do not reintroduce same-day DB preservation or "richer snapshot" selection over verified live rows; do not restore `_preserve_best_regular_set`; and do not remove `--offers-ready-food-url` from `.env` unless you intentionally want the ready-food block hidden. Also keep the new hardening guards intact: `scripts/vkusvill_collect_discounts.py` should abort early on low disk, stop refresh attempts after a server rejection, never use `data-max` as a fake stock source, and `src/main.py` must keep `data/bot.pid` in sync with the socket lock. `tests/test_bot_backend_guards.py` must stay isolated from the real repo cwd, otherwise the test suite can overwrite the live `webapp/latest.json`. The latest incident showed one more invariant: if a stale repo copy still exists on disk, its `src/main.py` and `scripts/ensure-bot-running.ps1` must fail closed or delegate into `X:`; otherwise Telegram can answer from the wrong workspace while the canonical DB remains correct.

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
