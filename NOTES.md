# Project Notes - VkusVill Autobot

## Agent Learnings

### Claude - 2026-03-19
- Project bootstrapped into Duo system.
- ROADMAP.md serves as PRD - no need to create a separate one.
- TEST_MATRIX.md is comprehensive and serves as the test spec.
- Existing docs (P0_CHECKLIST, SMOKE_CHECKLIST, AUTOTEST_PLAN) are valuable, do not duplicate them.
- [critical] Bot uses RPA (Playwright) for VkusVill auth - fragile, depends on site structure.

### Codex - 2026-03-19
- The stale `out/tmp/order-profile-*` Chrome profile dirs can balloon quickly; cleanup should target those temp dirs, not just generic out files.
- `sqlite3.backup()` works fine for the WAL-enabled state DB, so daily and startup backups can stay simple and reliable.
- The morning audit should treat session health as a gate: explicit `ok=false` blocks collection, but missing state / transport failures should stay soft warnings.
- The nutrition enricher can be a plain `httpx` scraper with direct goods URLs and regex parsing; no Playwright is needed for that nightly pass.

### Codex - 2026-03-20
- Mobile API session health can live alongside the older Playwright check: use the mobile probe for morning gating and keep the Chrome-profile check as a manual fallback.
- [critical] Token refresh should write back to `.env` immediately so the next bot or script invocation reads the same refreshed state.
- When a mobile probe cannot prove auth because config is missing or the endpoint is unavailable, treat it as a warning, not a hard failure.

### Codex - 2026-03-20 (stale snapshot guard)
- Mini App must not treat yesterday's `latest.json` as a valid current menu when today's collect fails.
- When today's collect is empty, the bot should emit a deliberate stale/empty payload in the `/app` URL instead of falling through to the remote Pages fallback.
- On stale payloads, render an explicit empty-state / stale notice instead of silently showing old discounts.
- Keep autonomy status copy aligned with this rule: "fresh slice unconfirmed" is better than "last confirmed slice" because it doesn't imply the fallback is current.

### Codex - 2026-03-20 (audit follow-up)
- [critical] `scripts/vkusvill_mobile_session_check.py`, `scripts/vkusvill_refresh_token.py`, and `scripts/vkusvill_enrich_nutrition.py` need a repo-root `sys.path` bootstrap so they work when launched as `scripts\*.py`.
- The watchdog scheduled task was still pointing at the old `C:\Users\Sasha\Documents\vkusvill-telegram-autobot` copy; keep the canonical action on `X:\vkusvill-telegram-autobot`.
- Keep `data/today_discounts.json` and `webapp/latest.json` aligned with the current ready-food toggle in `.env`; stale ready-food rows will trip snapshot contracts even when the live bot is healthy.

### Codex - 2026-03-20 (runtime audit hardening)
- [critical] The stale-menu incident had two real causes: an old scheduled task path and the absence of any canonical-root guard in runtime startup.
- Windows may launch the bot as `X:\...\venv\Scripts\python.exe -m src.main` with a child `C:\Users\Sasha\AppData\Local\Programs\Python\Python312\python.exe -m src.main`; that child is healthy and must not be killed by the watchdog.
- `scripts/ensure-bot-running.ps1` now reads `C:\Users\Sasha\projects\REGISTRY.md` and delegates old copies into the canonical `X:\vkusvill-telegram-autobot` workspace before any local startup logic runs.
- `src/main.py` now refuses to start from a non-canonical workspace, so even if an old copy is launched manually or by a stale task, it fails closed instead of serving yesterday's menu.
- Validation after the hardening pass: full `unittest` green (`68 OK, 1 skipped`), scheduled task action points to `X:\vkusvill-telegram-autobot\scripts\ensure-bot-running.ps1`, live bot process is the healthy launcher+child pair, and both `latest.json` / DB meta are on `2026-03-20`.

### Codex - 2026-03-20 (carry-over fix for unverified menu)
- [critical] The collector previously loaded `data/today_discounts.json` before a new run and could preserve yesterday's full 18/18 set when today's waves were partial or unchanged.
- `scripts/vkusvill_collect_discounts.py` now ignores the existing out-file unless its mtime is from the same local day, so cross-day carry-over from yesterday's file is blocked.
- Multi-wave collect now forces `require_distinct_waves`; if wave replacement does not actually change the inshop cards, the run fails instead of pretending a fresh 18/18 set exists.
- `src/bot.py` now treats Mini App data as verified only when `last_collect_day == today` and `last_collect_status == ok`. Any `error` / guard status now yields a deliberate stale payload with `force_stale=true`.
- `webapp/index.html` now respects `force_stale` and refuses to prefer fresh-looking `latest.json` over an explicit stale payload from the bot.
- Live validation on 2026-03-20: strict collect failed with `Wave 1: replace did not change inshop cards. Cannot guarantee 3 distinct waves.` The bot was restarted, and `_build_mini_app_url()` now produces `force_stale=True`, `rows=0`.

### Codex - 2026-03-20 (today-pool resync + Mini App source-of-truth fix)
- [critical] `data/today_discounts.json` already had the correct 6 live discounts plus the favorite tuna item, but `src/bot.py` was still re-contaminating the DB and `webapp/latest.json` by preserving richer same-day DB rows during `mode='all'`.
- `src/bot.py` now trusts fetched rows for `collect all` and limits downgrade-preserve guards to `mode='regular'` only. This stops old DB rows from leaking back into the Mini App after a clean same-day collect.
- `_best_available_items()` now prefers live DB rows whenever today's `last_collect_status` is `ok`, so an older "richer" snapshot can no longer override a verified current-day pool.
- We manually resynced `data/state.db`, `day_snapshots`, and `webapp/latest.json` from the already-correct `data/today_discounts.json`; the live set is now 6 current discounts + 1 favorite.
- [critical] `tests/test_bot_backend_guards.py` used to write stale data into the real `webapp/latest.json` because it ran in the repo cwd. The test now chdirs into a temp workspace first, so running the suite no longer wipes the live Mini App snapshot.

### Codex - 2026-03-20 (ready-food block restored)
- The Mini App hides the ready-food block whenever `RPA_COMMAND` / `FALLBACK_RPA_COMMAND` do not include `--offers-ready-food-url`; the missing block was config drift, not a frontend rendering bug.
- `scripts/vkusvill_collect_discounts.py` now safe-skips the ready-food source on HTTP/network failure and logs `[collector] ready food skipped: ...` instead of crashing the whole collect.
- Re-enabled `--offers-ready-food-url "https://vkusvill.ru/offers/gotovaya-eda/"` in `.env`, ran a live collect, and got `34` ready-food items plus the current 6 regular discounts + 1 favorite.
- Removed the `Как выбрать` guide block from `webapp/index.html`; only the compact selection UI remains.

## Patterns

- Keep operational cleanup close to the code that creates the artifact.
- Favor soft warnings for transport or environment failures when the data source is optional.

## Mistakes Not To Repeat

- Do not let the bot overwrite public snapshot schemas when only internal payloads need extra fields.
- Do not assume a live browser session check should block collection on every failure mode; distinguish explicit auth failure from missing tooling or transient transport errors.
- Do not treat the child interpreter of a healthy Windows venv launcher as a stale duplicate `src.main`.
- Do not rely on "we moved the repo" as an operational fix; enforce the canonical workspace at runtime via `REGISTRY.md`.
- Do not let `data/today_discounts.json` act as a cross-day source of truth; if today's collect is unverified, fail closed in `/app`.

### Codex - 2026-03-20 (full audit + anti-repeat hardening)
- [critical] The stale-menu problem is a system failure, not a single bug. The stable fix is layered: day-pool as the only same-day accumulator, fail-closed Mini App payloads, startup catch-up when `today_discounts.json` is stale, singleton bot runtime, and low-disk aborts before Chrome launch.
- `scripts/vkusvill_collect_discounts.py` no longer contains the dead `preserving existing regular set` branch. Keep it that way; it reanimated stale rows and blurred the real source of truth.
- Replace-limit failures now have a cheaper failure mode: one `refresh rejected, skipping further refresh attempts` log, then stop. Do not go back to repeated refresh attempts inside one run.
- Low disk should fail early and readably. We now have both preflight (`ABORT: disk space low`, exit 2) and runtime `disk full on {path}` handling so the bot can explain the failure instead of dumping a traceback.
- `src/main.py` now has `data/bot.pid` on top of the socket lock. Keep both: socket protects the port, PID lock protects the process model and makes stale duplicate starts visible.
- Repo hygiene matters operationally here. `.pytest_cache`, repo `__pycache__`, and `out.legacy` were noise only; delete this kind of leftover aggressively when it stops being useful.

### Codex - 2026-03-20 (stock quantity truth fix)
- [critical] `data-max` on VkusVill cards is not a trustworthy stock source. It can look like a quantity but diverge badly from the real `В наличии N шт` count shown in the app.
- `scripts/vkusvill_collect_discounts.py` now fills `stock_qty` only from explicit text markers like `В наличии` / `Осталось`. If the page does not expose a clear stock string, we keep `stock_qty = null` instead of inventing a number.
- This is intentionally fail-closed UX: no badge is better than a false badge. Mini App should only show a stock chip when we actually know the number.
- Live recollect on 2026-03-20 confirmed the effect: several inshop items that previously showed false counts (`Пломбир`, `Котлеты`, `Филе грудки индейки`, `Лесные ягоды`, `PROTEIN`, `Оладьи`) now carry `null` stock instead of wrong integers, while items with explicit text still keep real counts.

### Codex - 2026-03-20 (non-canonical runtime incident fix)
- [critical] The empty `batch #1 пока пуст` answer was not a DB or Mini App write bug. The canonical `X:` store still had votes and `_format_who_chose_text()` returned a valid non-empty summary.
- Root cause: a stale bot process from `C:\Users\Sasha\Documents\vkusvill-telegram-autobot` was still answering Telegram while `REGISTRY.md` and the scheduled task already pointed to `X:\vkusvill-telegram-autobot`.
- Fix in place: stale `src.main` processes from `Documents` were killed; the only live runtime is now the healthy `X:` launcher+child pair.
- Anti-repeat hardening: both stale copies (`D:` and `Documents`) now fail closed in `src/main.py` when the registry points elsewhere, and their `scripts/ensure-bot-running.ps1` delegate into the canonical `X:` workspace instead of starting a second bot.
- Validation: direct start from `Documents` now prints `Refusing to start from non-canonical workspace`, old watchdog scripts print `delegating watchdog to canonical workspace: X:\vkusvill-telegram-autobot`, and the canonical bot still formats a non-empty `batch #1`.
