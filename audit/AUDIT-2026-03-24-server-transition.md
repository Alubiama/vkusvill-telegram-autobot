# Server Transition Audit - 2026-03-24

Scope: live runtime, watchdog/autostart, collector, Telegram integration, test gate, and readiness for a non-interactive server move.

## Verdict

The current desktop runtime is healthy and the bot is running. The failure we chased earlier was environmental, not a logic regression:

- the machine was not always available;
- the collector can still be blocked by a locked Chrome profile;
- the current autostart model still assumes a local Windows desktop session.

So the system is operational now, but it is not yet fully server-ready.

## Evidence

- `scripts/live_system_audit.py` returned `issues: []` and `warnings: []`.
- `last_collect_status=ok`.
- `last_startup_sanity_status=ok`.
- `last_publish_public_check_status=ok`.
- Task Scheduler reports `vkusvill-telegram-autobot-watchdog` as enabled with `LastTaskResult=0`.
- `cmd /c run-tests.cmd` passed `88` tests.
- `cmd /c publish-github-pages.cmd` completed successfully.

## What Is Already Good

- The bot has a stable canonical-runtime guard via `src/runtime_guard.py`.
- The bot can restart polling on transient network failures.
- The collector already has headless modes and session-check scripts.
- The repo has a live audit script that can be reused as a deployment gate.
- The system can publish the public Pages snapshot independently of the Telegram runtime.

## Main Server-Readiness Gaps

1. Autostart is still desktop-oriented.
   - `scripts/ensure-bot-running.ps1` and the Startup-folder helper assume a Windows user session.
   - This is fine on a personal PC, but it is not the same as a boot-time service.

2. Collector still depends on Chrome profile state.
   - `system_chrome` works only if the profile is available and unlocked.
   - If Chrome is open, the collector can fail with the profile-lock error we saw earlier.

3. Session bootstrap is still profile-based and manual.
   - We have `scripts/clone_chrome_profile.py`, `scripts/vkusvill_sync_session.py`, and `scripts/vkusvill_session_check.py`.
   - That is enough to migrate, but the migration is not yet automated end to end.

4. The canonical workspace is pinned to the current machine layout.
   - `src/runtime_guard.py` validates the repo against `REGISTRY.md`.
   - For a server, that registry entry must be updated to the server path before the bot can be treated as canonical there.

5. Runtime artifacts still have some local-machine assumptions.
   - This is not blocking for today, but it is worth tightening before moving the system off the desktop.

## Recommended Migration Path

1. Decide the target server shape first.
   - Windows server with Task Scheduler.
   - Windows service wrapper.
   - Or a different host that only runs the headless collector and bot backend.

2. Move the collector to a dedicated server-owned browser profile.
   - Do not point it at the personal desktop Chrome profile.
   - Prefer a cloned or dedicated automation profile under the server workspace.

3. Keep a session refresh flow separate from the runtime.
   - Use the existing sync/check scripts to refresh cookies or `storage_state`.
   - Treat login refresh as a maintenance step, not as part of every bot start.

4. Replace desktop autostart with a server boot strategy.
   - The server should start the bot after reboot without requiring an interactive login.
   - The current Startup-folder approach should be treated as a desktop fallback only.

5. Keep `scripts/live_system_audit.py` as the post-deploy gate.
   - Run it after every migration step.
   - Pair it with `cmd /c run-tests.cmd` before calling the server ready.

## Practical Conclusion

The bot is healthy enough to continue operating now, but the move to a server should be treated as a hardening project, not a simple copy-paste deploy.

The next high-value work is to make startup and collection non-interactive by default and to detach the runtime from the local Chrome profile.
