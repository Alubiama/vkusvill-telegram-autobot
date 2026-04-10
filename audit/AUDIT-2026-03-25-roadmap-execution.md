# Audit - Roadmap Execution Checkpoint

Date: 2026-03-25

## What changed

### 1) Mini App UX pass
- Mini App status copy now uses plain-language labels for the main flow.
- Locked rounds now read as read-only instead of leaking internal batch jargon.
- Send-state copy now says "Сейчас только просмотр" / "Выбор отправлен" instead of "open batch".

### 2) Product packaging
- README now starts with a sharper one-sentence product pitch.
- Public landing meta copy now names the product as a shared ordering flow with live cart handoff.
- The public demo continues to live in `webapp/landing.html`, so Pages deployment stays unchanged.

### 3) Sharable architecture for other owners
- Added scoped meta helpers in `src/store.py`:
  - `scoped_meta_key()`
  - `set_scoped_meta()`
  - `get_scoped_meta()`
- These helpers give us a low-risk namespace foundation for owner-specific settings without changing the current one-user behavior.
- The intended future shape is still: one owner = one workspace, with separate group, account, and cycle state.

### 4) Server migration hardening
- The current runtime already has the main hardening layers:
  - canonical workspace guard
  - PID lock
  - startup sanity check
  - live system audit
  - mobile state snapshot path for portable auth state
- The current server-readiness bar is:
  1. canonical path resolves through `REGISTRY.md`
  2. `src.main` refuses non-canonical copies
  3. `scripts/ensure-bot-running.ps1` delegates old workspaces to `X:`
  4. `scripts/live_system_audit.py` stays green
  5. mobile session state can move with the project instead of hiding in a desktop-only Chrome profile

## Current conclusion

The project is now in a better shape for one-user operation and future VPS migration:
- human-readable UI copy is cleaner,
- public positioning is sharper,
- multi-owner separation has a namespace scaffold,
- and the runtime hardening checklist is still intact.

## Next checkpoint

- Confirm the updated UI strings on a live pass.
- Keep the owner/workspace namespace idea minimal until we actually split workspaces.
- Treat server migration as a packaging/runtime shift, not a rewrite.
