from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PUBLISH_SCRIPT = ROOT / "publish-github-pages.cmd"
CONFIG_FILE = ROOT / "src" / "config.py"
BOT_FILE = ROOT / "src" / "bot.py"
WEBAPP_FILE = ROOT / "webapp" / "index.html"
MAIN_FILE = ROOT / "src" / "main.py"
EXECUTOR_FILE = ROOT / "scripts" / "vkusvill_order_executor.py"
ENSURE_BOT_SCRIPT = ROOT / "scripts" / "ensure-bot-running.ps1"
COLLECTOR_FILE = ROOT / "scripts" / "vkusvill_collect_discounts.py"


class OperationalGuardsTest(unittest.TestCase):
    def test_publish_script_does_not_stage_entire_repo(self) -> None:
        text = PUBLISH_SCRIPT.read_text(encoding="utf-8")
        self.assertNotIn('git add .', text.lower())
        self.assertIn('add -- webapp .github/workflows/pages.yml', text)

    def test_publish_script_refuses_silent_branch_switch(self) -> None:
        text = PUBLISH_SCRIPT.read_text(encoding="utf-8")
        lowered = text.lower()
        self.assertNotIn('checkout main', lowered)
        self.assertIn('refusing to publish from branch', lowered)

    def test_watchdog_delegates_to_registry_canonical_workspace_and_keeps_child_interpreter(self) -> None:
        text = ENSURE_BOT_SCRIPT.read_text(encoding="utf-8")
        self.assertIn("Get-RegistryProjectPath", text)
        self.assertIn("delegating watchdog to canonical workspace", text)
        self.assertIn("$healthyLauncherPids", text)
        self.assertIn("$childrenOfHealthyLauncher", text)
        self.assertIn("$isHealthyChild", text)
        self.assertIn("$launcherPids", text)
        self.assertIn("$stale", text)
        self.assertIn("stopped stale bot", text)
        self.assertIn("(-not $isLauncher) -and (-not $isHealthyChild)", text)

    def test_config_supports_explicit_telegram_proxy(self) -> None:
        text = CONFIG_FILE.read_text(encoding="utf-8")
        self.assertIn("telegram_proxy_url", text)
        self.assertIn('os.getenv("TELEGRAM_PROXY_URL")', text)
        self.assertIn("collect_timeout_sec", text)
        self.assertIn('COLLECT_TIMEOUT_SEC', text)
        self.assertIn("order_executor_timeout_sec", text)
        self.assertIn('ORDER_EXECUTOR_TIMEOUT_SEC', text)
        self.assertIn("collect_min_items", text)
        self.assertIn('COLLECT_MIN_ITEMS', text)

    def test_bot_builds_explicit_httpx_requests_for_telegram(self) -> None:
        text = BOT_FILE.read_text(encoding="utf-8")
        self.assertIn("HTTPXRequest", text)
        self.assertIn(".get_updates_request(", text)
        self.assertIn(".request(", text)

    def test_finalize_does_not_refresh_discounts_before_executor(self) -> None:
        text = BOT_FILE.read_text(encoding="utf-8")
        self.assertNotIn("_refresh_before_finalize(", text)
        self.assertIn("Беру текущий срез и сразу добавляю в корзину.", text)

    def test_executor_timeout_is_handled_explicitly(self) -> None:
        text = BOT_FILE.read_text(encoding="utf-8")
        self.assertIn("subprocess.TimeoutExpired", text)
        self.assertIn("Автодобавление зависло и было остановлено по таймауту.", text)

    def test_main_uses_human_readable_network_hints(self) -> None:
        text = MAIN_FILE.read_text(encoding="utf-8")
        self.assertIn("DNS/сеть до Telegram API недоступны", text)
        self.assertIn("Telegram API не ответил вовремя", text)
        self.assertIn("Refusing to start from non-canonical workspace", text)
        self.assertIn("bot.pid", text)
        self.assertIn("[bot] already running, pid=%s", text)

    def test_bot_has_autonomy_state_and_alerts(self) -> None:
        text = BOT_FILE.read_text(encoding="utf-8")
        self.assertIn("def _autonomy_status_payload", text)
        self.assertIn("def _attempt_autonomy_repair", text)
        self.assertIn("def _run_autonomy_cycle", text)
        self.assertIn("scheduled_autonomy_watchdog", text)
        self.assertIn("def _notify_autonomy_if_needed", text)
        self.assertIn('restore=', text)
        self.assertIn("rewrite_latest", text)
        self.assertIn("publish=", text)
        self.assertIn("Работаем по аварийному режиму", text)
        self.assertIn("Свежий срез за сегодня не подтвержден.", text)
        self.assertIn("_discounts_snapshot_is_fresh_for_today", text)
        self.assertIn("startup catchup scheduled:", text)
        self.assertIn("⚠️ Сбор завершен, но найдено только", text)

    def test_bot_has_mobile_session_probe_without_playwright(self) -> None:
        text = BOT_FILE.read_text(encoding="utf-8")
        self.assertIn("def _check_mobile_vkusvill_session", text)
        self.assertIn("def mobilecheck", text)
        self.assertIn("last_mobile_sessioncheck_status", text)
        self.assertIn("/mobilecheck - проверка mobile API session без Playwright", text)
        self.assertIn("vkusvill_mobile_session_check.py", text)

    def test_env_example_exposes_mobile_api_tokens(self) -> None:
        env_text = (ROOT / ".env.example").read_text(encoding="utf-8")
        self.assertIn("VV_MOBILE_BASE_URL", env_text)
        self.assertIn("VV_ANON_TOKEN", env_text)
        self.assertIn("VV_ACCESS_TOKEN", env_text)
        self.assertIn("VV_REFRESH_TOKEN", env_text)
        self.assertIn("VV_CARD_NUMBER", env_text)

    def test_all_inline_owner_callbacks_have_handlers(self) -> None:
        text = BOT_FILE.read_text(encoding="utf-8")
        actions = {
            match.group(1)
            for match in re.finditer(r'callback_data="ctl\|([^"|]+)', text)
            if match.group(1) not in {"paiduser"}
        }
        handled = set(re.findall(r'action == "([^"]+)"', text))
        missing = sorted(action for action in actions if action not in handled)
        self.assertEqual(missing, [], f"Missing ctl handlers: {missing}")

    def test_executor_imports_datetime_for_session_preflight(self) -> None:
        text = EXECUTOR_FILE.read_text(encoding="utf-8")
        self.assertIn("from datetime import datetime", text)
        self.assertIn('day = datetime.now().strftime("%Y-%m-%d")', text)

    def test_bot_has_image_health_and_public_asset_smoke_guards(self) -> None:
        text = BOT_FILE.read_text(encoding="utf-8")
        self.assertIn("def _assess_image_health", text)
        self.assertIn("def _fetch_public_asset_head", text)
        self.assertIn("public_image_unreachable", text)
        self.assertIn("last_image_health_status", text)
        self.assertIn("Проверка картинок после сбора: есть проблемы.", text)

    def test_health_separates_historical_tails_from_current_warnings(self) -> None:
        text = BOT_FILE.read_text(encoding="utf-8")
        self.assertIn("def _iso_is_today", text)
        self.assertIn("исторические хвосты:", text)
        self.assertIn("старый executor был в ошибке", text)

    def test_bot_autonomy_messages_do_not_promote_last_confirmed_slice_as_normal(self) -> None:
        text = BOT_FILE.read_text(encoding="utf-8")
        self.assertIn("Свежий срез за сегодня не подтвержден.", text)
        self.assertNotIn("Работаем по последнему подтвержденному срезу.", text)

    def test_webapp_refuses_to_render_stale_latest_snapshot_as_current(self) -> None:
        text = WEBAPP_FILE.read_text(encoding="utf-8")
        self.assertIn("payloadIsFresh", text)
        self.assertIn("payloadIsForcedStale", text)
        self.assertIn("latestIsForcedStale", text)
        self.assertIn("Свежие скидки за сегодня еще не готовы. Вчерашний срез не показываем.", text)

    def test_collector_has_disk_guard_and_refresh_exhaustion_protection(self) -> None:
        text = COLLECTOR_FILE.read_text(encoding="utf-8")
        self.assertIn("_ensure_disk_headroom", text)
        self.assertIn("ABORT: disk space low", text)
        self.assertIn("disk full on", text)
        self.assertIn("refresh rejected, skipping further refresh attempts", text)
        self.assertNotIn("preserving existing regular set", text)

