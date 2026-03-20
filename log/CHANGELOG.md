# Changelog

## 2026-03-19
- Codex: Mini App UX pass in `webapp/index.html`.
- Added a short in-app usage guide.
- Humanized round status copy.
- Clarified selection labels for sections and ready-food pages.

## 2026-03-19 - Hardening batch
- Added stale `out/tmp/order-profile-*` cleanup for temporary Chrome profiles.
- Added SQLite backup support with startup backup and a daily scheduled backup.
- Added a morning session-health gate before the audit flow.
- Added `scripts/vkusvill_enrich_nutrition.py` for nightly nutrition enrichment via `httpx`.
- Added tests for backup, session gating, and nutrition parsing.

## 2026-03-20 - Mobile API session probe
- Added `VV_MOBILE_*` and mobile token env vars to `.env.example`.
- Added `src/mobile_api.py` plus standalone refresh and session-check scripts for the VkusVill mobile API.
- Added `/mobilecheck` and switched the morning audit gate to the non-Playwright mobile probe.
- Added tests for token refresh write-back and mobile session refresh/retry behavior.

## 2026-03-20 - Stale snapshot guard
- Mini App now refuses to render yesterday's `latest.json` as if it were today's menu.
- The bot now emits a deliberate stale/empty payload in the `/app` URL when today's collect is missing, so the remote Pages fallback cannot quietly reopen old discounts.
- Added a stale empty-state message instead of silently failing open to old discounts.
- Updated bot autonomy copy to say the fresh slice is unconfirmed instead of implying a confirmed fallback slice.

## 2026-03-20 - Audit follow-up
- Added a repo-root import bootstrap to the standalone mobile and nutrition scripts so they run correctly when launched as `scripts\*.py`.
- Pointed the watchdog scheduled task at `X:\vkusvill-telegram-autobot` instead of the old `Documents` copy.
- Resynced `data/today_discounts.json` and `webapp/latest.json` to remove ready-food rows while the public ready-food source is disabled.
- Added `tests/test_script_entrypoints.py` to guard the script entrypoint contract.

## 2026-03-20 - Runtime audit hardening
- Added `src/runtime_guard.py` so the bot can read the canonical workspace path from `C:\Users\Sasha\projects\REGISTRY.md`.
- `src/main.py` now refuses to start from a non-canonical workspace, preventing stale old repo copies from silently serving outdated menus.
- `scripts/ensure-bot-running.ps1` now delegates old copies into the canonical X: workspace and preserves the healthy Windows `venv launcher -> child python` process pair.
- Added `tests/test_runtime_guard.py` and restored the watchdog regression coverage in `tests/test_operational_guards.py`.

## 2026-03-20 - Carry-over guard for unverified discounts
- `scripts/vkusvill_collect_discounts.py` now ignores yesterday's `data/today_discounts.json` when deciding whether to preserve an existing regular set.
- Multi-wave collection now enforces distinct waves, so "replace did not change cards" becomes a failed collect instead of a silently reused 18/18 menu.
- `src/bot.py` now emits a `force_stale` Mini App payload whenever today's collect is not a clean `ok`.
- `webapp/index.html` now respects `force_stale` and does not override it with `latest.json`.
- Added regression coverage in `tests/test_collect_guards.py`, `tests/test_bot_backend_guards.py`, and `tests/test_operational_guards.py`.

## 2026-03-20 - Today-pool truth and live Mini App restore
- `src/bot.py` no longer preserves stale same-day DB rows during `collect all`; the fetched today-pool now replaces the live set for full collects.
- Snapshot fallback now yields to verified live rows when `last_collect_status == ok` for today, so an older richer snapshot cannot override the current-day Mini App.
- The live `state.db`, `day_snapshots`, and `webapp/latest.json` were resynced from the correct `data/today_discounts.json` pool (6 current discounts + 1 favorite) and the bot was restarted on the fixed code.
- `tests/test_collect_guards.py`, `tests/test_bot_backend_guards.py`, and `tests/test_snapshot_contracts.py` now cover the today-pool contract instead of the old fixed-18 assumption.
- `tests/test_bot_backend_guards.py` now runs from a temp cwd so the test suite can no longer overwrite the real `webapp/latest.json`.

## 2026-03-20 - Ready-food block restored + Mini App cleanup
- Re-enabled `--offers-ready-food-url "https://vkusvill.ru/offers/gotovaya-eda/"` in `.env`, so the bot again exposes the ready-food block in the Mini App.
- Hardened `scripts/vkusvill_collect_discounts.py`: ready-food collection now logs a warning and returns `[]` on HTTP/network failure instead of crashing the whole collect.
- Ran a live collect and resynced `data/state.db` + `webapp/latest.json` to `41` items total: `6` regular discounts, `1` favorite, `34` ready-food items.
- Removed the `Как выбрать` guide card and its dead `guideBadge` JS hook from `webapp/index.html`.
- Republished GitHub Pages and restarted the live bot from `X:\vkusvill-telegram-autobot`.

## 2026-03-20 - Full audit hardening + cleanup
- Removed the dead collector branch `_preserve_best_regular_set` and kept `today_pool.json` / `today_pool_date.txt` as the only same-day accumulator for collect output.
- Added collector hardening in `scripts/vkusvill_collect_discounts.py`: low-disk preflight (`ABORT: disk space low`, exit code 2), readable `disk full on ...` Chrome-profile failure text, and `refresh_exhausted` behavior so rejected replace attempts stop after one warning.
- Added bot/runtime hardening: `src/bot.py` now schedules a startup catch-up collect when `today_discounts.json` is stale and alerts the owner when a collect succeeds with fewer than `COLLECT_MIN_ITEMS` items; `src/main.py` now manages `data/bot.pid` and rejects live duplicate starts.
- Updated `.env` and `.env.example` with `COLLECT_MIN_ITEMS=10`.
- Extended regression coverage in `tests/test_collect_guards.py`, `tests/test_bot_backend_guards.py`, and `tests/test_operational_guards.py`.
- Cleaned workspace junk from the canonical repo root: `.pytest_cache`, repo `__pycache__`, and `out.legacy`.

## 2026-03-20 - Stock quantity truth fix
- Removed the false `data-max` fallback from `stock_qty` extraction in `scripts/vkusvill_collect_discounts.py`; stock badges now appear only when the page exposes an explicit `В наличии/Осталось N шт` text.
- Added regression coverage in `tests/test_collect_guards.py` so silent fallback to `data-max` cannot return.
- Ran a fresh live collect, resynced `data/state.db` + `webapp/latest.json`, and republished Pages so Mini App no longer shows invented stock counts for inshop cards.

## 2026-03-20 - Non-canonical runtime incident fix
- Diagnosed the empty `batch #1 пока пуст` response as a runtime split: Telegram was being served by `C:\Users\Sasha\Documents\vkusvill-telegram-autobot`, while the canonical `X:\vkusvill-telegram-autobot` store still had the real votes.
- Killed the stale `Documents` bot and confirmed the live runtime is again only the healthy `X:` launcher+child pair.
- Hardened the stale `D:` and `Documents` repo copies so their `scripts/ensure-bot-running.ps1` delegate into the canonical workspace from `REGISTRY.md` and their `src/main.py` fail-closed with `Refusing to start from non-canonical workspace`.
