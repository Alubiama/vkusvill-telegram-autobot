# Skill Stack For Vkusvill Telegram Autobot

This set is the minimum useful stack for the current project.

## Active Skills

1. `ask-questions-if-underspecified`
- Use only for critical missing inputs (for example: chat_id, deadline, payment mode).

2. `app-builder`
- Use for project structure and module split (bot/store/providers/config).

3. `async-python-patterns`
- Use for robust async bot logic, timeouts, retries, and stable I/O behavior.

4. `android_ui_verification`
- Use while integrating RPA discount collection from the mobile app (adb/uiautomator/screencap).

5. `007`
- Use for security hardening:
  - keep tokens/secrets only in `.env`,
  - rotate tokens after exposure,
  - least privilege, rate-limit, and incident playbook.

6. `bash-defensive-patterns`
- Use for production run scripts and cron jobs (strict mode, fail-fast, safe defaults).

7. `api-security-best-practices`
- Use for all bot webhooks and external API integrations (validation, auth, abuse protection).

## Notes

- No Telegram-only skill was found in the current catalog.
- For Telegram best practices, use `007` + `api-security-best-practices`.
