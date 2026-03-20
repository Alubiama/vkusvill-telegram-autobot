from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path

from dotenv import load_dotenv
from telegram import Bot


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.bot import VkusvillGroupBot  # noqa: E402
from src.config import load_settings  # noqa: E402
from src.providers import ManualJsonProvider  # noqa: E402
from src.store import StateStore  # noqa: E402


def _run_powershell(script: str) -> tuple[int, str, str]:
    proc = subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command", script],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=20,
    )
    return proc.returncode, (proc.stdout or "").strip(), (proc.stderr or "").strip()


def _ps_json(script: str) -> object:
    code, stdout, stderr = _run_powershell(script)
    if code != 0:
        return {"error": stderr or stdout or f"powershell exit {code}"}
    if not stdout:
        return None
    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        return {"raw": stdout}


def _collect_processes() -> list[dict[str, object]]:
    data = _ps_json(
        "Get-CimInstance Win32_Process -Filter \"name='python.exe'\" | "
        "Where-Object { $_.CommandLine -like '*src.main*' } | "
        "Select-Object ProcessId,ParentProcessId,ExecutablePath,CommandLine | ConvertTo-Json -Depth 4"
    )
    if data is None:
        return []
    if isinstance(data, dict) and ("error" in data or "raw" in data):
        return [data]
    if isinstance(data, dict):
        return [data]
    return list(data)


def _collect_scheduled_task() -> dict[str, object]:
    data = _ps_json(
        "$task = Get-ScheduledTask -TaskName 'vkusvill-telegram-autobot-watchdog'; "
        "$info = Get-ScheduledTaskInfo -TaskName 'vkusvill-telegram-autobot-watchdog'; "
        "[PSCustomObject]@{ "
        "Execute=$task.Actions[0].Execute; "
        "Arguments=$task.Actions[0].Arguments; "
        "LastTaskResult=$info.LastTaskResult; "
        "LastRunTime=$info.LastRunTime; "
        "NextRunTime=$info.NextRunTime } | ConvertTo-Json -Depth 4"
    )
    if isinstance(data, dict):
        return data
    return {"raw": data}


async def _telegram_probe(bot_token: str, chat_id: int | None, owner_id: int | None) -> dict[str, object]:
    result: dict[str, object] = {
        "me_ok": False,
        "chat_ok": False,
        "owner_ok": owner_id is not None,
    }
    bot = Bot(token=bot_token)
    try:
        me = await bot.get_me()
        result["me_ok"] = True
        result["bot_username"] = me.username
    except Exception as exc:
        result["me_error"] = f"{type(exc).__name__}: {exc}"
    if chat_id is None:
        result["chat_error"] = "CHAT_ID missing"
        return result
    try:
        chat = await bot.get_chat(chat_id)
        result["chat_ok"] = True
        result["chat_type"] = getattr(chat, "type", None)
        result["chat_title"] = getattr(chat, "title", None)
    except Exception as exc:
        result["chat_error"] = f"{type(exc).__name__}: {exc}"
    return result


def main() -> int:
    load_dotenv(PROJECT_ROOT / ".env", override=True)
    settings = load_settings()
    store = StateStore(settings.db_path)
    bot = VkusvillGroupBot(settings, store, ManualJsonProvider(settings.discounts_json_path))
    day = bot._today()
    integrity = bot._assess_day_integrity(day)
    runtime = bot._runtime_root_payload()
    processes = _collect_processes()
    task = _collect_scheduled_task()
    telegram = asyncio.run(_telegram_probe(settings.bot_token, settings.chat_id, settings.owner_user_id))

    issues: list[str] = []
    warnings: list[str] = []
    observations: list[str] = []

    if settings.chat_id is None:
        issues.append("CHAT_ID missing")
    if settings.owner_user_id is None:
        issues.append("OWNER_USER_ID missing")
    if runtime["state"] == "error":
        issues.append(f"runtime root error: {runtime['detail']}")
    elif runtime["state"] == "warning":
        warnings.append(f"runtime root warning: {runtime['detail']}")

    task_args = str(task.get("Arguments") or "")
    if "X:\\vkusvill-telegram-autobot\\scripts\\ensure-bot-running.ps1" not in task_args:
        issues.append("scheduled task not pointing to canonical X: watchdog script")
    if int(task.get("LastTaskResult") or 0) != 0:
        warnings.append(f"scheduled task last result={task.get('LastTaskResult')}")

    process_paths = [str(row.get("ExecutablePath") or "") for row in processes if isinstance(row, dict)]
    noncanonical = [
        path
        for path in process_paths
        if path and (
            "C:\\Users\\Sasha\\Documents\\vkusvill-telegram-autobot" in path
            or "D:\\projects\\vkusvill-telegram-autobot" in path
        )
    ]
    if noncanonical:
        issues.append(f"non-canonical src.main process detected: {noncanonical[0]}")
    if not telegram.get("me_ok"):
        issues.append(f"telegram getMe failed: {telegram.get('me_error')}")
    if not telegram.get("chat_ok"):
        issues.append(f"telegram getChat failed: {telegram.get('chat_error')}")

    if str(store.get_meta("last_collect_day") or "") != day:
        warnings.append("last_collect_day is not today")
    if str(store.get_meta("last_collect_status") or "") != "ok":
        warnings.append(f"last_collect_status={store.get_meta('last_collect_status') or 'n/a'}")

    if list(integrity.get("missing_in_latest") or []):
        issues.append(f"latest.json missing {len(list(integrity.get('missing_in_latest') or []))} DB item(s)")
    if str(integrity.get("latest_day") or "") != day:
        issues.append(f"latest.json day mismatch: {integrity.get('latest_day') or 'n/a'} vs {day}")
    if not list(integrity.get("items") or []):
        issues.append("DB has no today items")
    observations.extend(list(integrity.get("warnings") or [])[:6])

    payload = {
        "day": day,
        "runtime": runtime,
        "processes": processes,
        "scheduled_task": task,
        "telegram": telegram,
        "settings_chat_id": settings.chat_id,
        "meta_chat_id": store.get_meta("chat_id"),
        "settings_owner_user_id": settings.owner_user_id,
        "meta_owner_user_id": store.get_meta("owner_user_id"),
        "last_collect_day": store.get_meta("last_collect_day"),
        "last_collect_status": store.get_meta("last_collect_status"),
        "last_startup_sanity_status": store.get_meta("last_startup_sanity_status"),
        "integrity_state": integrity.get("state"),
        "integrity_counts": {
            "total": len(list(integrity.get("items") or [])),
            "regular": len(list(integrity.get("regular") or [])),
            "favorite": len(list(integrity.get("favorites") or [])),
            "ready_food": len(list(integrity.get("ready_food") or [])),
            "latest_rows": len(list(integrity.get("latest_rows") or [])),
        },
        "issues": issues,
        "warnings": warnings,
        "observations": observations,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    if issues:
        return 2
    if warnings:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
