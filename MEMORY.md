# Project: VkusVill Telegram Autobot
## Status: active development
## Next agent: claude
## Last updated: 2026-03-27 by codex

## What this project is
Telegram bot and Mini App for coordinated VkusVill discount ordering. It collects discounts, shows them in the Mini App, tracks user selections, adds orders to cart, and keeps debt bookkeeping.

## Project files
- MEMORY.md
- NOTES.md
- README.md
- ROADMAP.md
- ARCHITECTURE.md
- TEST_MATRIX.md
- P0_CHECKLIST.md
- SMOKE_CHECKLIST.md
- AUTOTEST_PLAN.md
- SKILL_STACK.md
- src/
- tests/
- scripts/
- deploy/
- webapp/
- log/CHANGELOG.md
- scripts/vkusvill_enrich_nutrition.py
- scripts/vkusvill_mobile_session_check.py
- scripts/vkusvill_refresh_token.py
- scripts/live_system_audit.py
- scripts/android_runtime_probe.py
- scripts/vkusvill_order_executor.py
- webapp/landing.html
- src/mobile_api.py
- src/android_runtime_probe.py
- tests/test_mobile_api.py
- tests/test_android_runtime_probe.py
- tests/test_script_entrypoints.py
- tests/test_order_executor_cart_matching.py
- TASKS.md
- tests/test_bot_backend_guards.py
- tests/test_nutrition_parser.py
- audit/AUDIT-2026-03-20.md
- audit/AUDIT-2026-03-24-server-transition.md
- audit/AUDIT-2026-04-10-triz-api-deprecation.md
- audit/AUDIT-2026-04-10-triz-100-ideas.md
- audit/AUDIT-2026-04-10-saas-scaling-triz.md

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
- [x] Task 73 full system audit landed
- [x] Product packaging public demo page landed
- [x] Sharable owner/workspace scaffold landed
- [x] Server migration hardening checkpoint landed
- [x] Linux/VPS runtime scaffold landed
- [x] Autonomy watchdog repair alert dedupe landed
- [x] Collector `out_path` recovery and live day resync landed
- [x] Group broadcast reminders for open/close/reboot/payment/delivery landed
- [x] VkusVill APK reverse engineering landed and the mobile auth surface is now mapped
- [x] Mobile auth bootstrap runner landed
- [x] Chrome dependency reduction step 1 landed (mobile-first session probe with Chrome fallback)
- [x] Linux install helper scaffold landed for the VPS service layout
- [x] Portable mobile state now round-trips on startup: `data/mobile_state.json` can seed `src/mobile_api.py` when `.env` is sparse
- [x] Standard Android adb probe helper landed for future live proof runs (`src/android_runtime_probe.py` + wrapper)
- [x] Live cart executor now accepts portable Playwright `storage_state` so the cart path can reuse the same browser snapshot pattern as read-only cart smoke
- [x] Collector service now prefers portable state first via `--source auto` and falls back to system Chrome only when needed
- [x] Favorite/loyalty items now stay as a full independent lane in the public snapshot and Mini App instead of being truncated to one card
- [x] Linux VPS install helper is now fully contract-tested and marked done
- [x] Live current-day snapshot is now rebuilt from the 2026-03-27 live popup endpoint and ready-food pages: today's `state.db`, `data/today_discounts.json`, `data/today_pool.json`, `webapp/latest.json`, and `webapp/img-cache/current/map.json` now show 18 regular cards plus 45 unique ready-food items, with the 18 regular cards ordered to match the live popup
- [ ] Android runtime live login proof
- [ ] Multi-owner architecture
- [ ] First external users
- [ ] Monetization

## Latest update
- 2026-03-27: Restored the current-day snapshot from backup `66272b872f31`, keeping 18 regular cards, 1 favorite, and 36 ready-food items in sync across `state.db`, `today_discounts.json`, `today_pool.json`, and `webapp/latest.json`, then switched local runtime config to `PROVIDER=manual_json`.
- [critical] `.env` must stay UTF-8. A non-UTF-8 rewrite breaks `load_dotenv` in the test suite and will look like a config failure even when the values are otherwise correct.
- [critical] When a selected skill does not mention a needed source or angle, widen the search into adjacent docs, tools, domains, and prior art instead of stopping at the skill text; the skill is a starting lens, not the full map.

## What changed last
Codex added a synchronous VkusVill MCP client plus a search-based `VkusvillMCPProvider`, switched `.env.example` to `PROVIDER=mcp`, and added Windows PowerShell hook equivalents in the shared workbench. The live MCP probe confirmed the public API is only `search_products` / `product_details` / `create_cart_link`, so the provider filters discounted hits from catalog search results rather than assuming a dedicated discount feed.
- Follow-up live MCP testing showed the real `initialize` handshake returns server metadata only and no session id, so the client now tolerates sessionless calls. A live probe over `скидки` / `зеленый ценник` / `акция` found 1 / 0 / 0 discounted items respectively, and `AgentWorkbenchSync` was created in Task Scheduler for the workbench report loop.
- On 2026-03-27 the day snapshot had to be repaired across layers: `state.db` was still on the 7-item MCP slice while `webapp/latest.json` and GitHub Pages had already been restored to the 55-item manual screenshot snapshot. The fix was to resync `state.db` from `data/today_discounts.json`, update today's `last_collect_*` meta, and republish Pages so all three surfaces match again.
- [critical] Full Chrome profile copies from `Default` and `Default_copu` both land on the VkusVill delivery gate (`Выберите способ и время доставки`), so the real VkusVill login is not actually present in the filesystem copy. Future recovery has to come from a live authenticated browser session, not from the copied profile alone.

Current working baseline:
- Live cart smoke is read-only and safe to run against the existing Chrome session.
- Executor reconciliation now has a file contract and a regression test.
- Full test suite passed after the patch: `96 tests OK`.
- Group notice times are now configurable via `ORDER_WINDOW_OPEN_TIME` and `ORDER_WINDOW_CLOSE_TIME`.
- Full test suite passed after the config step: `97 tests OK`.
- Next high-value step is a real live `qty=2` smoke when the cart contains a duplicate item case.
- APK 26.4.6 reverse engineering confirmed a mobile backend surface with `AuthApi`, `Login2Api`, and `UserApi`.
- `src/mobile_api.py` now has OTP auth helpers plus a `refreshToken` fallback path, while preserving the existing orders/updateToken flow.
- `scripts/vkusvill_mobile_bootstrap.py` now exercises the OTP -> confirm -> session-check flow as a standalone CLI runner.
- `scripts/vkusvill_mobile_bootstrap.py` also has an `--auth-only` diagnostic mode so we can verify the OTP request response before waiting for SMS.
- `src/mobile_api.py` now mirrors the app's mobile request shape more closely with `X-VKUSVILL-*` headers, a stable device ID, and `number=` on GET probes instead of `cardNumber=`.
- The APK also exposed `X-VKUSVILL-SCREEN`, so the mobile layer now sends a screen hint too.
- Full test suite passed after the mobile header/query contract alignment: `101 tests OK`.
- Next high-value step is a live token-backed bootstrap run, then a live `qty=2` proof in the cart.
- A reusable Android probe helper now exists as a thin adb wrapper in `src/android_runtime_probe.py` and `scripts/android_runtime_probe.py`; it standardizes `adb devices`, `wm size`, `uiautomator dump`, and screenshot capture without inventing a new runtime layer.
- Full test suite passed after adding the probe helper: `125 tests OK`.
- `guest/createAnonymousCard/` is live and returns a guest card number plus anon token.
- `POST /api/v1/user/otp/account-creating` is the real register bootstrap path, and it wants a 10-digit phone string plus app-like headers (`X-VKUSVILL-DEVICE=android`, `X-VKUSVILL-SOURCE=2`, `X-VKUSVILL-MODEL=Android`).
- That register OTP request now returns `data.statusId=1`, so the remaining live step is confirm with the SMS code.
- `scripts/vkusvill_mobile_bootstrap.py` now has a resend-friendly `--request-only` mode, so we can trigger a fresh register SMS without dropping immediately into confirm.
- `src/mobile_api.py` now also writes a portable `data/mobile_state.json` snapshot alongside `.env`, so the mobile session can move with us toward a VPS-ready runtime.
- `src/mobile_api.py` now also reads `data/mobile_state.json` back into config on startup, and the snapshot records `bootstrap_source` plus `last_bootstrap_at` so provenance survives the move.
- The bot now has an owner-only `/schedule` command that updates the order window times/messages from chat, persists overrides in meta, and reschedules the open/closed jobs without a restart.
- Full test suite passed after the schedule command patch: `107 tests OK`.
- Mini App edits now refuse to save into locked batches: `finalizing` is exposed explicitly, and `single_choice` / `all_choices` are rejected when the current day already has a locked cycle in `finalizing`, `partially_added`, `added_waiting_payment`, or `closed`.
- Full test suite passed after the locked-batch guard: `109 tests OK`.
- Current UX follow-up: rewrite user-facing statuses and bot messages into plain language so the app says `Можно редактировать` / `Сейчас только просмотр` / `Заказ собран` / `Заказ оплачен` / `Заказ доставлен` instead of leaking internal cycle jargon.
- Next roadmap priority after the UX pass is product packaging, then shared architecture for other owners, then server migration hardening.
- Portable state is now a first-class bridge from APK reverse to VPS migration, because the mobile session can survive outside `.env` and still be reloaded on start.
- We still do not have a live Android-device/emulator login proof, so that remains the next unresolved proof obligation.
- No Android runtime (`adb` / emulator) is available in this workspace, so Task 104 is blocked until a device or emulator is provisioned.
- Android Studio, `cmdline-tools`, `platform-tools`, `emulator`, `platforms;android-34`, and Google APIs x86_64 + ARM64 system images are now installed locally, but the emulator still cannot become a usable live proof path because `systeminfo` reports `Virtualization Enabled In Firmware: No`.
- The Android probe helper is the reusable glue layer we wanted: standard adb primitives only, no custom runtime manager.
- The cart executor now matches the read-only cart smoke pattern: it can boot from portable Playwright `storage_state` instead of a persistent Chrome profile when the snapshot is available.
- [critical] A selected skill is only a starting lens. If the task needs something the skill does not mention, search beyond the skill into adjacent docs, tools, domains, and prior art before inventing any new layer.
- Russian prior-art sweep found useful context but not a ready-made VkusVill client: Habr articles on VkusVill mobile engineering, Android testing, telegram bots, and offline-first/PWA thinking; plus Russian code-assistant prior art like SourceCraft Code Assistant and GigaCode materials.
- Task 107 is now the next live proof target: an end-to-end `qty=2` cart proof using the existing cart smoke / order executor path, with `productId`-first reconciliation as the matching base.
- Current blocker for Task 107: every accessible Chrome profile is still unauthenticated for VkusVill, so the executor exits with `vkusvill login required in automation profile` and there is no reusable logged-in `storage_state` snapshot yet. The next step is a single auth bootstrap to capture a logged-in snapshot, then reuse it for the live proof.
- Task 108 is now the immediate prerequisite: capture one reusable logged-in VkusVill snapshot from Chrome using the existing auth helpers, then feed that snapshot back into Task 107.
- I verified that the Default Chrome profile does contain VkusVill cookies, but a plain profile copy did not yield an authenticated portable state because Chrome now stores the cookies under app-bound encryption on this machine. So the reusable snapshot still needs to come from an actual logged-in browser session or another valid auth path, not from a raw file clone alone.
- Full clone to a local `C:` path plus `--disable-features=UseAppBoundEncryptionProviderForEncryption` still did not surface a logged-in VkusVill page; the Default profile is effectively unauthenticated for Playwright capture on this machine.
- The auth helper is now more capture-friendly: it disables app-bound encryption in the launched browser, waits for logged-in markers, and exits cleanly on timeout instead of asking for terminal input.

## What changed last (2026-03-25 chrome reduction)
Codex kept the collect/executor Chrome path intact but moved session health to mobile-first with Chrome fallback. This reduces Chrome from truth source to fallback for auth checks without touching the order execution path.

Current working baseline:
- `scheduled_sessioncheck`, `/sessioncheck`, and autonomy repair now prefer mobile auth first.
- Chrome is still needed for collect, cart smoke, and executor, but it is no longer the default truth source for session health.
- Full test suite after the Chrome-reduction step: `112 tests OK`.
- Next step is the Android/mobile runtime proof path, then the live `qty=2` cart proof.
- The Linux scaffold now has a lightweight installer helper so the service/timer layout can be reproduced on a clean VPS.

## What changed last (2026-03-26 session/fallback repair)
- `scripts/vkusvill_session_check.py` now fails closed on login prompts instead of treating footer text as an authenticated session, and it retries with `domcontentloaded` plus a cart fallback before giving up.
- `collect-vkusvill-discounts.cmd` and `collect-vkusvill-discounts-headless.cmd` no longer point at the stale cloned `data\chrome-user-data` directory; they now target the live Chrome user-data root under `%LOCALAPPDATA%`.
- `.env` and `.env.example` now make the live Chrome root explicit for `RPA_COMMAND` and `ORDER_EXECUTOR_COMMAND`, so fallback settings are aligned with the actual profile source.
- `src/providers.py` now resolves `ManualJsonProvider` paths against the project root, so the fallback JSON source works even when the bot process cwd is not the repo root.
- Full suite after the fallback hardening stayed green: `132 tests OK`.
- Live startup catchup on 2026-03-26 confirmed the fallback chain end-to-end: primary and fallback_rpa failed to open Chrome, but `fallback_json` resolved from the project root and the collect completed successfully (`last_collect_status=ok`, `last_collect_source=fallback_json`).
- The current collect pipeline now keeps the live current-day slice honest: if the current-day file is verified (`last_collect_day == today`, `last_collect_status == ok`, and the discounts snapshot is fresh), `webapp/latest.json` stays live even when the selected source was `fallback_json`.
- Only unverified reserve data should fall back to stale presentation. Stale means clearly marked stale/empty UI for unverified data, not fake product cards and not a fake live catalog.
- The collector now writes a wave-history sidecar (`today_discounts.waves.json`) and the bot archives each wave separately into `day_snapshots`, so each 6x6x6 wave can be recovered later by `snapshot_id` instead of being flattened into one merged blob.
- `last_wave_snapshot_*` meta now points at the last archived wave, and `list_day_snapshots(day)` / `get_day_snapshot(day, snapshot_id)` can recover individual waves.
- The remaining live blocker is process-level: Chrome is still running in the background, so `system_chrome` cannot open the profile until Chrome is fully exited or the process lock is released.

## What changed last (2026-03-25 chrome reduction step 2)
- `scripts/vkusvill_collect_discounts.py` now supports `--source auto`, which tries portable storage-state first and falls back to system Chrome when the snapshot is missing or not logged in.
- `scripts/vkusvill_order_executor.py` now accepts `--storage-state` and can run from a portable Playwright browser snapshot instead of a persistent Chrome profile.
- The Linux collector service now also defaults to `--source auto`, so the VPS path prefers portable state first by default.
- The old Chrome-profile fallback remains available in both places, so this is a bounded reduction rather than a hard cutover.
- `tests/test_collect_guards.py`, `tests/test_linux_deploy_artifacts.py`, and `tests/test_order_executor_cart_matching.py` now pin the new CLI contracts.
- Full test suite after the collector/executor/service step: `127 tests OK`.
- The current next step is a live `qty=2` proof using the existing cart smoke and executor path, not a new runtime layer or a new abstraction.
- The Russian prior-art pass is useful for patterns, but it still did not surface a reusable VkusVill-specific client or wrapper. The strongest reuse lanes remain the generic Android/emulator/adb stack, the Playwright storage-state pattern, and Russian code-assistant tooling if we need a local assistant workflow later.

## What changed last (2026-03-25 vps architecture)
The repo now has an explicit `ARCHITECTURE.md` VPS plan with capacity ranges, migration order, and per-owner isolation rules. The current estimate is that a small VPS can hold roughly `1` Chrome-backed account comfortably, `2` only with careful serialization, and `2-4` owners more comfortably once mobile/API-first dominates the runtime.

## What changed last (2026-03-25 linux install helper)
- Added `deploy/linux/install.sh` to copy the systemd units, create the env stub, and enable the core VPS services/timers.
- Added `tests/test_linux_install_helper.py` to pin the helper contract.
- Updated the Linux README and architecture note to mention the installer path and keep backup handling explicit.

## Open questions
None right now. See ROADMAP.md for the full plan.

## For Claude
Read ROADMAP.md for product direction. Read TEST_MATRIX.md for reliability picture. Keep the canonical-root guard in `src/runtime_guard.py`, `src/main.py`, and `scripts/ensure-bot-running.ps1`. Keep the app-level verification gate: `src/bot.py` only emits a normal Mini App payload when `last_collect_status == ok` for today, and `webapp/index.html` must respect `force_stale`. Critical invariants now: for `mode='all'`, the collector/today-pool is the source of truth for Mini App rows; do not reintroduce same-day DB preservation or "richer snapshot" selection over verified live rows; do not restore `_preserve_best_regular_set`; and do not remove `--offers-ready-food-url` from `.env` unless you intentionally want the ready-food block hidden. Also keep the new hardening guards intact: `scripts/vkusvill_collect_discounts.py` should abort early on low disk, stop refresh attempts after a server rejection, never use `data-max` as a fake stock source, and `src/main.py` must keep `data/bot.pid` in sync with the socket lock. `tests/test_bot_backend_guards.py` must stay isolated from the real repo cwd, otherwise the test suite can overwrite the live `webapp/latest.json`. Keep the new startup observability too: `src/bot.py` startup sanity should continue probing group binding / `get_chat()` / runtime root and recording `last_startup_sanity_*`, and `scripts/live_system_audit.py` should remain the one-shot operator entrypoint for runtime + Telegram + collect health.

## For Codex
Main project goal: keep the VkusVill bot reliable end-to-end as a service-like system that can collect, reconcile, and publish orders without Chrome or desktop assumptions being the core dependency; Android/mobile runtime proof is a proof step toward that, not the end goal.
Current priority: Android/mobile runtime proof, then the live `qty=2` cart proof. Keep future edits local and do not restructure the bot unless a task explicitly requires it. If you touch the mobile API path again, keep the refresh/write-back flow aligned between the bot and the standalone scripts. If you touch watchdog/runtime code, keep three invariants: canonical path must come from `C:\Users\Sasha\projects\REGISTRY.md`, old copies must delegate to X:, and the healthy child interpreter of the venv launcher must not be killed as stale.
Reuse rule v4: before new design choices, always reuse-first scan local repo + existing skills + ecosystem primitives + official docs, then TRIZ transfer and a small formalization/invariant check; build new only if the concrete gap stays real after that pass.
Hard stop: it is unacceptable to narrow the whole project into a single proof subtask and then declare the architecture solved without a complete reuse-first pass, named rejected alternatives, and an honest `hold` if live proof is still blocked.
Hard stop: do not confuse "we found a standard path" with "we proved it works in this workspace"; blocked runtime proof stays blocked until the environment actually supports it.

## Token budget
- MEMORY.md - read fully
- NOTES.md - read fully
- ROADMAP.md - read fully
- README.md - skip unless onboarding context is needed
- TEST_MATRIX.md - read when testing/reviewing
- src/ - large, read selectively by task
- webapp/ - read when working on Mini App


## Manual screenshot restore (2026-03-26)
- The 18 current-day regular cards were rebuilt from the native VkusVill screenshots.
- They now live as first-class current-day data in `today_discounts.json`, `today_pool.json`, `state.db`, and `webapp/latest.json`.
- The restored shot items use local `webapp/img-cache/current/shot_XX.webp` assets and a regular-source prefix so the Mini App shows real cards instead of placeholder emptiness.
- Full test suite after the restore: `138 tests OK`.


## Image mirror repair (2026-03-26)
The manual screenshot restore was repaired into a real local-mirror snapshot: every current-day item now points to a local image path, the 18 shot items use official VkusVill images, `webapp/latest.json` was rebuilt from the repo root, and the round status is back to an honest non-stale state. GitHub Pages was republished after the fix.
## Image cache-bust (2026-03-26)
The published Mini App now appends a snapshot/day cache-bust to image candidates so Telegram/WebView does not keep stale bytes for updated product photos on reused `img-cache/current/*` paths.
The live `latest.json` and live image bytes were verified to match the local snapshot after the republish.

## Duo handoff (2026-03-26)
The duo brief landed on the registry-path portability slice first: `src/runtime_guard.py` now honors `REGISTRY_PATH` from the environment with a safe fallback, and the behavior is pinned by a regression test.

## What changed last (2026-04-10 TRIZ & API Deprecation)
- **API Deprecation**: We definitively proved that the recent 404 errors on `/api/user/privAbonement/update` were due to VkusVill changing their Mobile API endpoints, not due to missing tokens or app version. The data now hides behind GraphQL or new deeplinks (`action://update.abonement`).
- **TRIZ Audits**: Generated `AUDIT-2026-04-10-triz-api-deprecation.md` and `AUDIT-2026-04-10-triz-100-ideas.md`. The most critical insight from TRIZ: instead of fighting the new Mobile API or Playwright on the VPS, we shift the architecture to **Web API (HttpApiProvider) + Headless Cookie Delivery**.
- **No-Chrome Engine**: The project is migrating to a "0% Chrome" architecture on the VPS. The VPS will only use pure Python HTTP via `src/providers.py` (`HttpApiProvider`). Auth cookies will be gathered using a local one-time script (`web_auth.py`) which bypasses Cloudflare using `curl_cffi` to perform pure-Python SMS authentication on the desktop, and hands off the session state to the VPS.
- **SaaS Architecture**: The plan for commercial scaling was drafted (`AUDIT-2026-04-10-saas-scaling-triz.md`), aiming at multi-tenant boundaries with charitable split routing.
