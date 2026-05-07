"""HTTP cart executor for VkusVill — replays basket_add.php AJAX.

Usage:
    python scripts/vkusvill_add_to_cart.py <order_file.json> [--check-session-only]

Contract (see bot.py _run_executor_if_needed):
    stdin: none
    stdout (JSON):
      {
        "ok": bool,
        "status": "success"|"partial"|"failed"|"session_ok",
        "targets": int,
        "checks": [{
            "item_id": str, "name": str,
            "requested_qty": int, "before_qty": int, "after_qty": int,
            "added_delta": int, "ok": bool, "reason": str,
            # L1 post-verify fields (from basket_add.php response):
            "actual_price": float, "base_price": float,
            "expected_price": float, "is_discounted": bool,
            "max_available": int
        }],
        "cart_unique_before": int, "cart_total_qty_before": int,
        "cart_unique_after": int, "cart_total_qty_after": int,
        "cart_total_price_after": float,   # L1
        "cart_base_price_after": float,    # L1
        "cart_discount_total": float,      # L1
        "error": str (optional),
        "message": str (optional)
      }
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
STATE_FILE = ROOT / "data/vkusvill_storage_state.json"

PERSONAL_URL = "https://vkusvill.ru/personal/"
BASKET_ADD_URL = "https://vkusvill.ru/ajax/delivery_order/basket_add.php"
BASKET_LIST_URL = "https://vkusvill.ru/personal/cart/"

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
PROXY = os.getenv("HTTP_API_PROXY") or os.getenv("MOBILE_API_PROXY") or "socks5h://127.0.0.1:1080"

RE_USER_ID = re.compile(r'"USER_ID"\s*:\s*"(\d+)"|"user_id"\s*:\s*"(\d+)"|data-user-id="(\d+)"')
RE_SESSID = re.compile(r'"bitrix_sessid"\s*:\s*"([a-f0-9]+)"')


def _emit(payload: dict) -> None:
    """Single JSON line on stdout — bot parses last JSON block."""
    sys.stdout.write(json.dumps(payload, ensure_ascii=False))
    sys.stdout.write("\n")
    sys.stdout.flush()


def _load_cookies() -> list[dict]:
    data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return data.get("cookies", [])


def _make_client() -> httpx.Client:
    client = httpx.Client(follow_redirects=True, timeout=25, proxy=PROXY)
    for c in _load_cookies():
        client.cookies.set(c["name"], c["value"], domain="vkusvill.ru")
    return client


def _fetch_user_id(client: httpx.Client) -> tuple[str | None, str | None, str]:
    """Return (user_id, bitrix_sessid, personal_html). Raises if personal page not accessible."""
    r = client.get(PERSONAL_URL, headers={"User-Agent": UA})
    r.raise_for_status()
    html = r.text
    uid_m = RE_USER_ID.search(html)
    sid_m = RE_SESSID.search(html)
    user_id = None
    if uid_m:
        user_id = next((g for g in uid_m.groups() if g), None)
    return (user_id, sid_m.group(1) if sid_m else None, html)


def _add_one(client: httpx.Client, xmlid: str, qty: int, user_id: str) -> dict:
    """POST basket_add.php for one item; return parsed JSON or error dict."""
    body = {
        "id": xmlid,
        "xmlid": xmlid,
        "max": "10",
        "delivery_no_set": "N",
        "koef": str(max(1, qty)),
        "step": "1",
        "coupon": "",
        "isExperiment": "N",
        "isOnlyOnline": "",
        "isGreen": "0",
        "user_id": user_id,
        "skip_analogs": "",
        "is_app": "",
        "is_default_button": "Y",
        "cssInited": "N",
        "price_type": "6",
    }
    headers = {
        "User-Agent": UA,
        "Referer": PERSONAL_URL,
        "X-Requested-With": "XMLHttpRequest",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    }
    r = client.post(BASKET_ADD_URL, data=body, headers=headers)
    try:
        return r.json()
    except Exception:
        return {"success": "N", "error": f"non-json response: {r.text[:200]}"}


def _extract_xmlid(item_id: str) -> str | None:
    if not item_id:
        return None
    s = str(item_id)
    if s.startswith("inshop_"):
        s = s[len("inshop_"):]
    if s.isdigit():
        return s
    return None


def _session_check() -> int:
    try:
        with _make_client() as client:
            uid, sid, _ = _fetch_user_id(client)
            if not uid:
                _emit({"ok": False, "status": "session_invalid", "error": "user_id missing on /personal/"})
                return 1
            _emit({"ok": True, "status": "session_ok", "message": f"user_id={uid}"})
            return 0
    except httpx.HTTPError as exc:
        _emit({"ok": False, "status": "session_invalid", "error": f"http: {exc}"})
        return 1
    except Exception as exc:  # noqa: BLE001
        _emit({"ok": False, "status": "session_invalid", "error": str(exc)})
        return 1


def _run(order_file: Path) -> int:
    try:
        payload = json.loads(order_file.read_text(encoding="utf-8"))
    except Exception as exc:
        _emit({"ok": False, "status": "failed", "error": f"read order file: {exc}"})
        return 1

    items = payload.get("items") or []
    # Normalize: keep only rows with positive qty.
    targets = []
    for row in items:
        qty = int(row.get("qty") or 0)
        if qty <= 0:
            continue
        targets.append(row)

    if not targets:
        _emit({
            "ok": True, "status": "success", "targets": 0, "checks": [],
            "cart_unique_before": 0, "cart_total_qty_before": 0,
            "cart_unique_after": 0, "cart_total_qty_after": 0,
            "message": "no_selected_items",
        })
        return 0

    try:
        client = _make_client()
    except Exception as exc:
        _emit({"ok": False, "status": "failed", "error": f"client init: {exc}"})
        return 1

    with client:
        try:
            user_id, _sid, _html = _fetch_user_id(client)
        except httpx.HTTPError as exc:
            _emit({"ok": False, "status": "session_invalid", "error": f"personal fetch: {exc}"})
            return 1
        if not user_id:
            _emit({"ok": False, "status": "session_invalid", "error": "user_id missing (session expired?)"})
            return 1

        checks: list[dict] = []
        ok_count = 0
        last_totals: dict = {}
        for row in targets:
            item_id = str(row.get("item_id") or "")
            name = str(row.get("name") or "")
            req_qty = int(row.get("qty") or 0)
            # Expected price hints from order_file (optional — bot sends both)
            expected_price = 0.0
            for key in ("discount_price", "price"):
                v = row.get(key)
                if v in (None, ""):
                    continue
                try:
                    expected_price = float(v)
                    break
                except Exception:
                    continue

            xmlid = _extract_xmlid(item_id)
            if not xmlid:
                checks.append({
                    "item_id": item_id, "name": name,
                    "requested_qty": req_qty, "before_qty": 0, "after_qty": 0,
                    "added_delta": 0, "ok": False, "reason": "no_xmlid",
                    "expected_price": expected_price,
                })
                continue

            try:
                resp = _add_one(client, xmlid, req_qty, user_id)
            except httpx.HTTPError as exc:
                checks.append({
                    "item_id": item_id, "name": name,
                    "requested_qty": req_qty, "before_qty": 0, "after_qty": 0,
                    "added_delta": 0, "ok": False, "reason": f"http_error: {exc}",
                    "expected_price": expected_price,
                })
                continue

            success_flag = str(resp.get("success") or "").upper() == "Y"
            err = str(resp.get("error") or "").strip()
            # Post-verification: inspect basket{} snapshot from this response.
            basket = resp.get("basket") or {}
            last_totals = resp.get("totals") or last_totals
            b_entry = basket.get(f"{xmlid}_0") or _find_basket_entry(basket, xmlid)

            actual_qty = int(b_entry.get("Q") or 0) if b_entry else 0
            actual_price = float(b_entry.get("PRICE") or 0) if b_entry else 0.0
            base_price = float(b_entry.get("BASE_PRICE") or 0) if b_entry else 0.0
            diff_price = float(b_entry.get("DIFF_PRICE") or 0) if b_entry else 0.0
            max_q = int(b_entry.get("MAX_Q") or 0) if b_entry else 0
            can_buy = str((b_entry or {}).get("CAN_BUY") or "").upper() == "Y"
            is_discounted = bool(b_entry) and base_price > 0 and actual_price < base_price - 0.5

            # Authoritative source of truth — actual basket snapshot, not success flag.
            # success=N + basket echo = "уже в корзине" / "лимит достигнут" — item still physically there.
            reason = ""
            ok_item = False
            low_err = err.lower()
            tomorrow_hint = "tomorrow" in low_err or "завтра" in low_err

            if tomorrow_hint:
                reason = "requires_tomorrow_delivery"
            elif b_entry and actual_qty > 0:
                # Item физически в корзине — проверяем qty, потом цену.
                if actual_qty < req_qty:
                    reason = f"partial_qty_{actual_qty}_of_{req_qty}"
                elif expected_price > 0 and expected_price < base_price - 0.5 and not is_discounted:
                    reason = "no_discount"
                elif expected_price > 0 and abs(expected_price - actual_price) > 1.0:
                    reason = "price_mismatch"
                else:
                    reason = "added"
                    ok_item = True
            elif success_flag and not b_entry:
                # success=Y, но basket не эхоит item — считаем добавленным как просили.
                reason = "added"
                ok_item = True
                actual_qty = req_qty
            else:
                # success=N и в корзине ничего не видно — не добавилось.
                if "already" in low_err or "уже" in low_err:
                    reason = "already_in_cart"
                elif not can_buy or max_q == 0:
                    reason = "sold_out"
                elif err:
                    reason = err[:160]
                else:
                    reason = "not_added_to_cart"

            if ok_item:
                ok_count += 1
            checks.append({
                "item_id": item_id, "name": name,
                "requested_qty": req_qty, "before_qty": 0, "after_qty": actual_qty,
                "added_delta": actual_qty, "ok": ok_item, "reason": reason,
                "actual_price": actual_price,
                "base_price": base_price,
                "expected_price": expected_price,
                "discount_amount": diff_price,
                "is_discounted": is_discounted,
                "max_available": max_q,
            })

    total = len(checks)
    # Cart state = всё что физически добавилось (after_qty>0), независимо от reason.
    # Q_ITEMS из totals — число уникальных позиций в корзине (включая прошлые items, если были).
    cart_unique_after = int(last_totals.get("Q_ITEMS") or 0) or sum(
        1 for c in checks if int(c.get("after_qty") or 0) > 0
    )
    cart_total_qty_after = sum(int(c.get("after_qty") or 0) for c in checks)
    cart_total_price_after = float(last_totals.get("PRICE") or 0)
    cart_base_price_after = float(last_totals.get("BASE_PRICE") or 0)
    cart_discount_total = float(last_totals.get("DISCOUNT") or 0)
    status = "success" if ok_count == total else ("partial" if ok_count > 0 else "failed")
    _emit({
        "ok": ok_count == total,
        "status": status,
        "targets": total,
        "checks": checks,
        "cart_unique_before": 0,
        "cart_total_qty_before": 0,
        "cart_unique_after": cart_unique_after,
        "cart_total_qty_after": cart_total_qty_after,
        "cart_total_price_after": cart_total_price_after,
        "cart_base_price_after": cart_base_price_after,
        "cart_discount_total": cart_discount_total,
    })
    return 0 if status == "success" else 1


def _find_basket_entry(basket: dict, xmlid: str) -> dict | None:
    """Basket keys are usually '<xmlid>_0' but fall back to XML_ID scan."""
    try:
        xid = int(xmlid)
    except Exception:
        xid = -1
    for v in basket.values():
        if not isinstance(v, dict):
            continue
        if str(v.get("XML_ID")) == xmlid or v.get("XML_ID") == xid:
            return v
    return None


def main(argv: list[str]) -> int:
    args = [a for a in argv[1:] if a not in ("--no-headless",)]
    if "--check-session-only" in args:
        return _session_check()
    # Ignore interactive-login flags in this HTTP-only executor.
    if "--interactive-login" in args:
        idx = args.index("--interactive-login")
        # strip flag and its value pairs (wait-sec etc.)
        drop = {idx}
        for probe in ("--interactive-login-wait-sec",):
            if probe in args:
                j = args.index(probe)
                drop.update({j, j + 1})
        args = [a for i, a in enumerate(args) if i not in drop]

    if not args:
        _emit({"ok": False, "status": "failed", "error": "order_file path required"})
        return 1
    order_file = Path(args[0])
    if not order_file.exists():
        _emit({"ok": False, "status": "failed", "error": f"order_file not found: {order_file}"})
        return 1
    return _run(order_file)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
