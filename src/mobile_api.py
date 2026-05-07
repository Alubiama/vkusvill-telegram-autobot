from __future__ import annotations

import json
import os
import platform
import uuid
from datetime import datetime, timezone
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import httpx
try:
    from curl_cffi import requests as curl_requests
except ImportError:
    curl_requests = None

class _CompatHttpx:
    Client = Any
    if curl_requests:
        RequestError = curl_requests.exceptions.RequestException
    else:
        RequestError = httpx.RequestError

compat_httpx = _CompatHttpx()

from dotenv import dotenv_values, set_key


DEFAULT_BASE_URL = "https://mobile.vkusvill.ru"
DEFAULT_GUEST_CREATE_ANONYMOUS_CARD_PATH = "/api/guest/createAnonymousCard/"
DEFAULT_ORDERS_PATH = "/api/v1/orders"
DEFAULT_UPDATE_TOKEN_PATH = "/api/user/v1/updateToken"
DEFAULT_REFRESH_TOKEN_PATH = "/api/user/v1/refreshToken"
DEFAULT_OTP_ACCOUNT_CREATING_PATH = "/api/v1/user/otp/account-creating"
DEFAULT_OTP_AUTH_PATH = "/api/v1/user/otp/auth"
DEFAULT_OTP_CONFIRM_ACCOUNT_CREATING_PATH = "/api/v1/user/otp/confirm/account-creating"
DEFAULT_OTP_CONFIRM_PATH = "/api/v1/user/otp/confirm/auth"
DEFAULT_CHECK_SMS_PATH = "/api/user/checkSms/"
DEFAULT_APP_VERSION = "26.5.11"
DEFAULT_APP_VERSION_CODE = "2605011"
DEFAULT_APP_SOURCE = "2"
DEFAULT_ANDROID_SDK = "33"
DEFAULT_APP_MODEL = "Android Android"
DEFAULT_APP_B2B = "0"
DEFAULT_ENV_FILE = Path(__file__).resolve().parents[1] / ".env"
DEFAULT_STATE_RELATIVE_PATH = Path("data") / "mobile_state.json"


def _nonempty(value: object) -> str:
    return str(value or "").strip()


def _merge_env_values(env_path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        raw = env_path.read_text(encoding="utf-8")
    except OSError:
        raw = ""
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'").strip('"')
        if key and value:
            values[key] = value
    return values


def _merge_state_values(state_path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return values
    if not isinstance(payload, dict):
        return values
    key_map = {
        "base_url": "VV_MOBILE_BASE_URL",
        "orders_path": "VV_MOBILE_ORDERS_PATH",
        "update_token_path": "VV_MOBILE_UPDATE_TOKEN_PATH",
        "anon_token": "VV_ANON_TOKEN",
        "access_token": "VV_ACCESS_TOKEN",
        "refresh_token": "VV_REFRESH_TOKEN",
        "card_number": "VV_CARD_NUMBER",
        "device_id": "VV_DEVICE_ID",
        "model": "VV_MODEL",
        "source": "VV_SOURCE",
        "screen": "VV_SCREEN",
        "app_version": "VV_APP_VERSION",
        "version_code": "VV_VERSION_CODE",
        "android_sdk": "VV_ANDROID_SDK",
        "b2b": "VV_B2B",
    }
    for key, value in payload.items():
        if value is None:
            continue
        text = str(value).strip()
        if text:
            key_text = str(key)
            values[key_text] = text
            mapped = key_map.get(key_text)
            if mapped:
                values[mapped] = text
    return values


def _pick(values: dict[str, str], name: str, *aliases: str, default: str = "") -> str:
    for key in (name, *aliases):
        value = values.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
        value = os.getenv(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return default


def _token_map_name(name: str) -> str:
    return {
        "anon_token": "VV_ANON_TOKEN",
        "access_token": "VV_ACCESS_TOKEN",
        "refresh_token": "VV_REFRESH_TOKEN",
        "card_number": "VV_CARD_NUMBER",
        "base_url": "VV_MOBILE_BASE_URL",
        "orders_path": "VV_MOBILE_ORDERS_PATH",
        "update_token_path": "VV_MOBILE_UPDATE_TOKEN_PATH",
    }[name]


def _default_device_id() -> str:
    seed = "|".join(
        value
        for value in (
            os.getenv("COMPUTERNAME"),
            platform.node(),
            platform.machine(),
            platform.processor(),
        )
        if value and str(value).strip()
    )
    if not seed:
        seed = "vkusvill-mobile"
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, seed))


def _extract_tokens(payload: Any) -> dict[str, str]:
    found: dict[str, str] = {}

    def visit(obj: Any) -> None:
        if isinstance(obj, dict):
            for key, value in obj.items():
                key_norm = str(key).strip().lower().replace("-", "_").replace(" ", "_")
                if isinstance(value, str) and value.strip():
                    if "access" in key_norm and "token" in key_norm:
                        found.setdefault("access_token", value.strip())
                    elif "refresh" in key_norm and "token" in key_norm:
                        found.setdefault("refresh_token", value.strip())
                    elif ("anon" in key_norm or "anonymous" in key_norm) and "token" in key_norm:
                        found.setdefault("anon_token", value.strip())
                    elif "card" in key_norm and ("number" in key_norm or key_norm == "card"):
                        found.setdefault("card_number", value.strip())
                    elif key_norm == "token" and "access_token" not in found:
                        found.setdefault("access_token", value.strip())
                visit(value)
        elif isinstance(obj, list):
            for value in obj:
                visit(value)

    visit(payload)
    return found


def _extract_string(payload: Any, *candidate_keys: str) -> str:
    wanted = {str(key).strip().lower().replace("-", "_").replace(" ", "_") for key in candidate_keys}
    found = ""

    def visit(obj: Any) -> None:
        nonlocal found
        if found:
            return
        if isinstance(obj, dict):
            for key, value in obj.items():
                key_norm = str(key).strip().lower().replace("-", "_").replace(" ", "_")
                if key_norm in wanted and value is not None:
                    text = str(value).strip()
                    if text:
                        found = text
                        return
                if found:
                    return
                visit(value)
        elif isinstance(obj, list):
            for value in obj:
                visit(value)

    visit(payload)
    return found


def _normalize_phone_digits(phone: str) -> str:
    digits = "".join(ch for ch in str(phone) if ch.isdigit())
    if len(digits) == 11 and digits.startswith(("7", "8")):
        return digits[1:]
    return digits


@dataclass(frozen=True)
class MobileConfig:
    env_file: Path
    state_file: Path
    base_url: str
    orders_path: str
    update_token_path: str
    anon_token: str
    access_token: str
    refresh_token: str
    card_number: str
    device_id: str
    model: str
    source: str
    screen: str
    app_version: str
    version_code: str
    android_sdk: str
    b2b: str

    @property
    def orders_url(self) -> str:
        return f"{self.base_url.rstrip('/')}/{self.orders_path.lstrip('/')}"

    @property
    def update_token_url(self) -> str:
        return f"{self.base_url.rstrip('/')}/{self.update_token_path.lstrip('/')}"


@dataclass(frozen=True)
class MobileRefreshResult:
    ok: bool | None
    status: str
    detail: str
    refresh_status: int | None = None
    tokens_written: bool = False
    access_token: str = ""
    refresh_token: str = ""
    anon_token: str = ""
    card_number: str = ""

    def to_payload(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class MobileGuestCardResult:
    ok: bool | None
    status: str
    detail: str
    guest_status: int | None = None
    tokens_written: bool = False
    number: str = ""
    token: str = ""

    def to_payload(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class MobileOtpAuthResult:
    ok: bool | None
    status: str
    detail: str
    auth_status: int | None = None
    status_id: str = ""
    number: str = ""
    phone: str = ""

    def to_payload(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class MobileOtpConfirmResult:
    ok: bool | None
    status: str
    detail: str
    confirm_status: int | None = None
    tokens_written: bool = False
    access_token: str = ""
    refresh_token: str = ""
    token: str = ""
    number: str = ""
    phone: str = ""
    name: str = ""
    fl_pinning: bool | None = None

    def to_payload(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class MobileBootstrapResult:
    ok: bool | None
    status: str
    detail: str
    auth_status: int | None = None
    confirm_status: int | None = None
    orders_status: int | None = None
    refresh_status: int | None = None
    tokens_written: bool = False
    status_id: str = ""
    access_token: str = ""
    refresh_token: str = ""
    token: str = ""
    number: str = ""
    phone: str = ""
    name: str = ""

    def to_payload(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class MobileSessionResult:
    ok: bool | None
    status: str
    detail: str
    orders_status: int | None = None
    refresh_status: int | None = None
    refreshed: bool = False
    tokens_written: bool = False
    access_token: str = ""
    refresh_token: str = ""
    anon_token: str = ""
    card_number: str = ""

    def to_payload(self) -> dict[str, Any]:
        return asdict(self)


def load_mobile_config(env_file: str | Path | None = None) -> MobileConfig:
    env_path = Path(env_file) if env_file is not None else DEFAULT_ENV_FILE
    env_values = _merge_env_values(env_path)
    state_file = _pick(
        env_values,
        "VV_MOBILE_STATE_FILE",
        "VV_MOBILE_SESSION_FILE",
        default=str(env_path.parent / DEFAULT_STATE_RELATIVE_PATH),
    )
    state_values = _merge_state_values(Path(state_file))
    values = {**state_values, **env_values}
    return MobileConfig(
        env_file=env_path,
        state_file=Path(state_file),
        base_url=_pick(values, "VV_MOBILE_BASE_URL", default=DEFAULT_BASE_URL).rstrip("/"),
        orders_path=_pick(values, "VV_MOBILE_ORDERS_PATH", default=DEFAULT_ORDERS_PATH),
        update_token_path=_pick(values, "VV_MOBILE_UPDATE_TOKEN_PATH", default=DEFAULT_UPDATE_TOKEN_PATH),
        anon_token=_pick(values, "VV_ANON_TOKEN", "VKUSVILL_ANON_TOKEN"),
        access_token=_pick(values, "VV_ACCESS_TOKEN", "VKUSVILL_ACCESS_TOKEN"),
        refresh_token=_pick(values, "VV_REFRESH_TOKEN", "VKUSVILL_REFRESH_TOKEN"),
        card_number=_pick(values, "VV_CARD_NUMBER", "VKUSVILL_CARD_NUMBER"),
        device_id=_pick(values, "VV_DEVICE_ID", "VV_MOBILE_DEVICE_ID", default=_default_device_id()),
        model=_pick(values, "VV_MODEL", "VV_MOBILE_MODEL", default=DEFAULT_APP_MODEL),
        source=_pick(values, "VV_SOURCE", "VV_MOBILE_SOURCE", default=DEFAULT_APP_SOURCE),
        screen=_pick(values, "VV_SCREEN", "VV_MOBILE_SCREEN", default="main"),
        app_version=_pick(values, "VV_APP_VERSION", "VV_MOBILE_APP_VERSION", default=DEFAULT_APP_VERSION),
        version_code=_pick(values, "VV_VERSION_CODE", "VV_MOBILE_VERSION_CODE", default=DEFAULT_APP_VERSION_CODE),
        android_sdk=_pick(values, "VV_ANDROID_SDK", "VV_MOBILE_ANDROID_SDK", default=DEFAULT_ANDROID_SDK),
        b2b=_pick(values, "VV_B2B", "VV_MOBILE_B2B", default=DEFAULT_APP_B2B),
    )


def _clone_config(config: MobileConfig, **updates: Any) -> MobileConfig:
    data = asdict(config)
    data.update(updates)
    return MobileConfig(**data)


def _register_bootstrap_config(config: MobileConfig, *, anon_token: str, card_number: str) -> MobileConfig:
    return _clone_config(
        config,
        anon_token=anon_token,
        card_number=card_number,
        device_id="android",
        source="2",
        model="Android",
    )


def _write_back_state(
    config: MobileConfig,
    *,
    access_token: str,
    refresh_token: str,
    anon_token: str,
    card_number: str,
    bootstrap_source: str = "mobile_api",
) -> bool:
    state_path = config.state_file
    state_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "base_url": config.base_url,
        "orders_path": config.orders_path,
        "update_token_path": config.update_token_path,
        "anon_token": anon_token,
        "access_token": access_token,
        "refresh_token": refresh_token,
        "card_number": card_number,
        "device_id": config.device_id,
        "model": config.model,
        "source": config.source,
        "screen": config.screen,
        "app_version": config.app_version,
        "version_code": config.version_code,
        "android_sdk": config.android_sdk,
        "b2b": config.b2b,
        "bootstrap_source": bootstrap_source,
        "last_bootstrap_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        state_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        return False
    return True


def _number_params(config: MobileConfig) -> dict[str, str]:
    return {k: v for k, v in {"number": config.card_number}.items() if v}


def _headers_for(config: MobileConfig, *, use_refresh: bool = False) -> dict[str, str]:
    token = config.refresh_token if use_refresh and config.refresh_token else config.access_token
    headers = {
        "Accept": "application/json, text/plain, */*",
        "User-Agent": f"vkusvill/{config.app_version} (Android; {config.android_sdk})",
        "X-VKUSVILL-DEVICE": config.device_id,
        "X-VKUSVILL-SOURCE": config.source,
        "X-VKUSVILL-SCREEN": config.screen,
        "X-VKUSVILL-VERSION": f"{config.app_version} ({config.version_code})",
        "X-VKUSVILL-MODEL": config.model,
        "X-VKUSVILL-VPN-ENABLED": "0",
        "X-VKUSVILL-B2B": config.b2b,
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
        headers["X-VKUSVILL-TOKEN"] = token
    # Only send access_token header for normal requests, not refresh
    if config.access_token and not use_refresh:
        headers["X-VKUSVILL-TOKEN-ACCESS"] = config.access_token
    if config.refresh_token:
        headers["X-VKUSVILL-TOKEN-REFRESH"] = config.refresh_token
    if config.anon_token:
        headers["X-Anonymous-Token"] = config.anon_token
    import urllib.parse
    if config.card_number:
        headers["X-Card-Number"] = urllib.parse.quote(config.card_number)
    return headers


def _headers_for_auth(config: MobileConfig, *, use_refresh: bool = False) -> dict[str, str]:
    headers = _headers_for(config, use_refresh=use_refresh)
    headers.pop("Authorization", None)
    # Auth/refresh endpoints must NOT receive expired access_token
    headers.pop("X-VKUSVILL-TOKEN-ACCESS", None)
    headers["X-VKUSVILL-TOKEN"] = config.anon_token or config.access_token or config.refresh_token or headers.get("X-VKUSVILL-TOKEN", "")
    return headers


def _build_client(client: Any | None, timeout_sec: float) -> tuple[Any, bool]:
    if client is not None:
        return client, False
    
    proxy_dict = None
    try:
        env_vars = _merge_env_values(DEFAULT_ENV_FILE)
        raw = env_vars.get("MOBILE_API_PROXY") or env_vars.get("HTTP_API_PROXY") or ""
        if raw.strip():
            proxy_dict = {"http": raw.strip(), "https": raw.strip()}
    except Exception:
        pass

    if curl_requests:
        return curl_requests.Session(timeout=timeout_sec, allow_redirects=True, impersonate="chrome131_android", proxies=proxy_dict), True
    return httpx.Client(timeout=timeout_sec, follow_redirects=True), True


def create_mobile_anonymous_card(
    config: MobileConfig,
    *,
    client: Any | None = None,
    timeout_sec: float = 20.0,
    persist: bool = True,
) -> MobileGuestCardResult:
    http_client, should_close = _build_client(client, timeout_sec)
    try:
        response = http_client.post(
            f"{config.base_url.rstrip('/')}/{DEFAULT_GUEST_CREATE_ANONYMOUS_CARD_PATH.lstrip('/')}",
            headers=_headers_for(config),
            json={"device_id": config.device_id},
        )
        payload: Any = None
        try:
            payload = response.json()
        except ValueError:
            payload = None
        number = _extract_string(payload, "number")
        token = _extract_string(payload, "token")
        if response.status_code not in {200, 201, 204}:
            detail = ""
            if isinstance(payload, dict):
                detail = str(payload.get("error") or payload.get("message") or payload.get("detail") or "").strip()
            if not detail:
                detail = f"HTTP {response.status_code}"
            return MobileGuestCardResult(
                ok=False,
                status="auth_failed",
                detail=detail,
                guest_status=response.status_code,
                number=number,
                token=token,
            )
        if not number or not token:
            return MobileGuestCardResult(
                ok=None,
                status="no_tokens",
                detail="anonymous card response did not include number/token",
                guest_status=response.status_code,
                number=number,
                token=token,
            )
        written = False
        if persist:
            written = _write_back_tokens(
                config,
                access_token=config.access_token,
                refresh_token=config.refresh_token,
                anon_token=token,
                card_number=number,
                bootstrap_source="mobile_api",
            )
        return MobileGuestCardResult(
            ok=True,
            status="ok",
            detail="anonymous_card_created",
            guest_status=response.status_code,
            tokens_written=written,
            number=number,
            token=token,
        )
    except compat_httpx.RequestError as exc:
        return MobileGuestCardResult(
            ok=None,
            status="unavailable",
            detail=str(exc),
        )
    finally:
        if should_close:
            http_client.close()


def _write_back_tokens(
    config: MobileConfig,
    *,
    access_token: str,
    refresh_token: str,
    anon_token: str,
    card_number: str,
    bootstrap_source: str = "mobile_api",
) -> bool:
    env_path = config.env_file
    env_path.parent.mkdir(parents=True, exist_ok=True)
    changed = False
    for key, value in (
        (_token_map_name("anon_token"), anon_token),
        (_token_map_name("access_token"), access_token),
        (_token_map_name("refresh_token"), refresh_token),
        (_token_map_name("card_number"), card_number),
        (_token_map_name("base_url"), config.base_url),
        (_token_map_name("orders_path"), config.orders_path),
        (_token_map_name("update_token_path"), config.update_token_path),
    ):
        if not value:
            continue
        set_key(str(env_path), key, value)
        changed = True
    _write_back_state(
        config,
        access_token=access_token,
        refresh_token=refresh_token,
        anon_token=anon_token,
        card_number=card_number,
        bootstrap_source=bootstrap_source,
    )
    return changed


def _write_back_config(config: MobileConfig, updated: MobileRefreshResult) -> bool:
    return _write_back_tokens(
        config,
        access_token=updated.access_token,
        refresh_token=updated.refresh_token,
        anon_token=updated.anon_token,
        card_number=updated.card_number,
        bootstrap_source="legacy_update_token",
    )


def _request_mobile_otp(
    config: MobileConfig,
    *,
    path: str,
    number: str,
    phone: str,
    client: Any | None = None,
    timeout_sec: float = 20.0,
) -> MobileOtpAuthResult:
    http_client, should_close = _build_client(client, timeout_sec)
    try:
        request_config = _clone_config(config, card_number=number)
        response = http_client.post(
            f"{config.base_url.rstrip('/')}/{path.lstrip('/')}",
            headers=_headers_for_auth(request_config, use_refresh=False),
            json={"number": number, "phone": phone},
        )
        payload: Any = None
        try:
            payload = response.json()
        except ValueError:
            payload = None
        status_id = _extract_string(payload, "statusId", "status_id")
        if response.status_code not in {200, 201, 204}:
            detail = ""
            if isinstance(payload, dict):
                detail = str(payload.get("error") or payload.get("message") or payload.get("detail") or "").strip()
            if not detail:
                detail = f"HTTP {response.status_code}"
            return MobileOtpAuthResult(
                ok=False,
                status="auth_failed",
                detail=detail,
                auth_status=response.status_code,
                status_id=status_id,
                number=number,
                phone=phone,
            )
        if not status_id:
            return MobileOtpAuthResult(
                ok=None,
                status="no_status_id",
                detail="OTP auth succeeded but no statusId was returned",
                auth_status=response.status_code,
                number=number,
                phone=phone,
            )
        return MobileOtpAuthResult(
            ok=True,
            status="ok",
            detail="otp_auth_requested",
            auth_status=response.status_code,
            status_id=status_id,
            number=number,
            phone=phone,
        )
    except compat_httpx.RequestError as exc:
        return MobileOtpAuthResult(
            ok=None,
            status="unavailable",
            detail=str(exc),
            number=number,
            phone=phone,
        )
    finally:
        if should_close:
            http_client.close()


def _confirm_mobile_otp(
    config: MobileConfig,
    *,
    path: str,
    number: str,
    phone: str,
    otp: str,
    name: str = "",
    is_news: int | None = None,
    appsflyer_parameters: str = "",
    client: Any | None = None,
    timeout_sec: float = 20.0,
    persist: bool = True,
) -> MobileOtpConfirmResult:
    http_client, should_close = _build_client(client, timeout_sec)
    try:
        confirm_config = _clone_config(config, card_number=number)
        payload = {
            "phone": phone,
            "number": number,
            "name": name,
            "isNews": is_news,
            "otp": otp,
            "appsFlyerParameters": appsflyer_parameters,
        }
        response = http_client.post(
            f"{config.base_url.rstrip('/')}/{path.lstrip('/')}",
            headers=_headers_for_auth(confirm_config, use_refresh=False),
            json={key: value for key, value in payload.items() if value is not None and value != ""},
        )
        response_payload: Any = None
        try:
            response_payload = response.json()
        except ValueError:
            response_payload = None
        updates = _extract_tokens(response_payload)
        token = _extract_string(response_payload, "token")
        if response.status_code not in {200, 201, 204}:
            detail = ""
            if isinstance(response_payload, dict):
                detail = str(response_payload.get("error") or response_payload.get("message") or response_payload.get("detail") or "").strip()
            if not detail:
                detail = f"HTTP {response.status_code}"
            return MobileOtpConfirmResult(
                ok=False,
                status="auth_failed",
                detail=detail,
                confirm_status=response.status_code,
                access_token=updates.get("access_token", ""),
                refresh_token=updates.get("refresh_token", ""),
                token=token,
                number=number,
                phone=phone,
                name=name,
            )
        if not (updates.get("access_token") or token):
            return MobileOtpConfirmResult(
                ok=None,
                status="no_tokens",
                detail="OTP confirm succeeded but no token was returned",
                confirm_status=response.status_code,
                token=token,
                number=number,
                phone=phone,
                name=name,
            )
        access_token = updates.get("access_token", token)
        refresh_token = updates.get("refresh_token", "")
        tokens_written = False
        if persist and access_token:
            tokens_written = _write_back_tokens(
                config,
                access_token=access_token,
                refresh_token=refresh_token,
                anon_token=updates.get("anon_token", config.anon_token),
                card_number=updates.get("card_number", config.card_number),
                bootstrap_source="mobile_api",
            )
        return MobileOtpConfirmResult(
            ok=True,
            status="ok",
            detail="otp_confirmed",
            confirm_status=response.status_code,
            tokens_written=tokens_written,
            access_token=access_token,
            refresh_token=refresh_token,
            token=token,
            number=number,
            phone=phone,
            name=name,
            fl_pinning=None,
        )
    except compat_httpx.RequestError as exc:
        return MobileOtpConfirmResult(
            ok=None,
            status="unavailable",
            detail=str(exc),
            number=number,
            phone=phone,
            name=name,
        )
    finally:
        if should_close:
            http_client.close()


def refresh_mobile_tokens(
    config: MobileConfig,
    *,
    client: Any | None = None,
    timeout_sec: float = 20.0,
    persist: bool = True,
) -> MobileRefreshResult:
    if not config.refresh_token:
        return MobileRefreshResult(
            ok=None,
            status="missing_config",
            detail="VV_REFRESH_TOKEN is not configured",
            access_token=config.access_token,
            refresh_token=config.refresh_token,
            anon_token=config.anon_token,
            card_number=config.card_number,
        )
    if not config.anon_token:
        return MobileRefreshResult(
            ok=None,
            status="missing_config",
            detail="VV_ANON_TOKEN is not configured",
            access_token=config.access_token,
            refresh_token=config.refresh_token,
            anon_token=config.anon_token,
            card_number=config.card_number,
        )

    http_client, should_close = _build_client(client, timeout_sec)
    try:
        response = http_client.get(
            config.update_token_url,
            headers=_headers_for(config, use_refresh=True),
            params=_number_params(config),
        )
        payload: Any = None
        try:
            payload = response.json()
        except ValueError:
            payload = None
        updates = _extract_tokens(payload)
        next_config = _clone_config(
            config,
            anon_token=updates.get("anon_token", config.anon_token),
            access_token=updates.get("access_token", config.access_token),
            refresh_token=updates.get("refresh_token", config.refresh_token),
            card_number=updates.get("card_number", config.card_number),
        )
        if response.status_code not in {200, 201, 204}:
            detail = ""
            if isinstance(payload, dict):
                detail = str(payload.get("error") or payload.get("message") or payload.get("detail") or "").strip()
            if not detail:
                detail = f"HTTP {response.status_code}"
            fallback = refresh_mobile_tokens_via_login2(config, client=http_client, timeout_sec=timeout_sec, persist=persist)
            if fallback.status != "missing_config":
                return fallback
            return MobileRefreshResult(
                ok=False,
                status="auth_failed",
                detail=detail,
                refresh_status=response.status_code,
                access_token=next_config.access_token,
                refresh_token=next_config.refresh_token,
                anon_token=next_config.anon_token,
                card_number=next_config.card_number,
            )
        if not next_config.access_token:
            fallback = refresh_mobile_tokens_via_login2(config, client=http_client, timeout_sec=timeout_sec, persist=persist)
            if fallback.status != "missing_config":
                return fallback
            return MobileRefreshResult(
                ok=None,
                status="no_access_token",
                detail="refresh succeeded but no access token was returned",
                refresh_status=response.status_code,
                access_token=next_config.access_token,
                refresh_token=next_config.refresh_token,
                anon_token=next_config.anon_token,
                card_number=next_config.card_number,
            )
        written = False
        if persist:
            written = _write_back_tokens(
                config,
                access_token=next_config.access_token,
                refresh_token=next_config.refresh_token,
                anon_token=next_config.anon_token,
                card_number=next_config.card_number,
                bootstrap_source="legacy_update_token",
            )
        return MobileRefreshResult(
            ok=True,
            status="ok",
            detail="refresh_ok",
            refresh_status=response.status_code,
            tokens_written=written,
            access_token=next_config.access_token,
            refresh_token=next_config.refresh_token,
            anon_token=next_config.anon_token,
            card_number=next_config.card_number,
        )
    except compat_httpx.RequestError as exc:
        return MobileRefreshResult(
            ok=None,
            status="unavailable",
            detail=str(exc),
            access_token=config.access_token,
            refresh_token=config.refresh_token,
            anon_token=config.anon_token,
            card_number=config.card_number,
        )
    finally:
        if should_close:
            http_client.close()


def refresh_mobile_tokens_via_login2(
    config: MobileConfig,
    *,
    client: Any | None = None,
    timeout_sec: float = 20.0,
    persist: bool = True,
) -> MobileRefreshResult:
    if not config.refresh_token:
        return MobileRefreshResult(
            ok=None,
            status="missing_config",
            detail="VV_REFRESH_TOKEN is not configured",
            access_token=config.access_token,
            refresh_token=config.refresh_token,
            anon_token=config.anon_token,
            card_number=config.card_number,
        )
    if not config.anon_token:
        return MobileRefreshResult(
            ok=None,
            status="missing_config",
            detail="VV_ANON_TOKEN is not configured",
            access_token=config.access_token,
            refresh_token=config.refresh_token,
            anon_token=config.anon_token,
            card_number=config.card_number,
        )

    http_client, should_close = _build_client(client, timeout_sec)
    try:
        response = http_client.post(
            f"{config.base_url.rstrip('/')}/{DEFAULT_REFRESH_TOKEN_PATH.lstrip('/')}",
            headers=_headers_for_auth(config, use_refresh=True),
            json={"refreshToken": config.refresh_token},
        )
        payload: Any = None
        try:
            payload = response.json()
        except ValueError:
            payload = None
        updates = _extract_tokens(payload)
        next_config = _clone_config(
            config,
            anon_token=updates.get("anon_token", config.anon_token),
            access_token=updates.get("access_token", config.access_token),
            refresh_token=updates.get("refresh_token", config.refresh_token),
            card_number=updates.get("card_number", config.card_number),
        )
        if response.status_code not in {200, 201, 204}:
            detail = ""
            if isinstance(payload, dict):
                detail = str(payload.get("error") or payload.get("message") or payload.get("detail") or "").strip()
            if not detail:
                detail = f"HTTP {response.status_code}"
            return MobileRefreshResult(
                ok=False,
                status="auth_failed",
                detail=detail,
                refresh_status=response.status_code,
                access_token=next_config.access_token,
                refresh_token=next_config.refresh_token,
                anon_token=next_config.anon_token,
                card_number=next_config.card_number,
            )
        if not next_config.access_token:
            return MobileRefreshResult(
                ok=None,
                status="no_access_token",
                detail="refreshToken succeeded but no access token was returned",
                refresh_status=response.status_code,
                access_token=next_config.access_token,
                refresh_token=next_config.refresh_token,
                anon_token=next_config.anon_token,
                card_number=next_config.card_number,
            )
        written = False
        if persist:
            written = _write_back_tokens(
                config,
                access_token=next_config.access_token,
                refresh_token=next_config.refresh_token,
                anon_token=next_config.anon_token,
                card_number=next_config.card_number,
                bootstrap_source="legacy_update_token",
            )
        return MobileRefreshResult(
            ok=True,
            status="ok",
            detail="refresh_ok_login2",
            refresh_status=response.status_code,
            tokens_written=written,
            access_token=next_config.access_token,
            refresh_token=next_config.refresh_token,
            anon_token=next_config.anon_token,
            card_number=next_config.card_number,
        )
    except compat_httpx.RequestError as exc:
        return MobileRefreshResult(
            ok=None,
            status="unavailable",
            detail=str(exc),
            access_token=config.access_token,
            refresh_token=config.refresh_token,
            anon_token=config.anon_token,
            card_number=config.card_number,
        )
    finally:
        if should_close:
            http_client.close()


def request_mobile_otp_auth(
    config: MobileConfig,
    *,
    number: str,
    phone: str,
    client: Any | None = None,
    timeout_sec: float = 20.0,
) -> MobileOtpAuthResult:
    return _request_mobile_otp(
        config,
        path=DEFAULT_OTP_AUTH_PATH,
        number=number,
        phone=phone,
        client=client,
        timeout_sec=timeout_sec,
    )


def request_mobile_otp_account_creating(
    config: MobileConfig,
    *,
    number: str,
    phone: str,
    client: Any | None = None,
    timeout_sec: float = 20.0,
) -> MobileOtpAuthResult:
    phone_digits = _normalize_phone_digits(phone)
    return _request_mobile_otp(
        config,
        path=DEFAULT_OTP_ACCOUNT_CREATING_PATH,
        number=number,
        phone=phone_digits,
        client=client,
        timeout_sec=timeout_sec,
    )


def confirm_mobile_otp_auth(
    config: MobileConfig,
    *,
    number: str,
    phone: str,
    otp: str,
    name: str = "",
    is_news: int | None = None,
    appsflyer_parameters: str = "",
    client: Any | None = None,
    timeout_sec: float = 20.0,
    persist: bool = True,
) -> MobileOtpConfirmResult:
    return _confirm_mobile_otp(
        config,
        path=DEFAULT_OTP_CONFIRM_PATH,
        number=number,
        phone=phone,
        otp=otp,
        name=name,
        is_news=is_news,
        appsflyer_parameters=appsflyer_parameters,
        client=client,
        timeout_sec=timeout_sec,
        persist=persist,
    )


def confirm_mobile_otp_register(
    config: MobileConfig,
    *,
    number: str,
    phone: str,
    otp: str,
    name: str = "",
    is_news: int | None = None,
    appsflyer_parameters: str = "",
    client: Any | None = None,
    timeout_sec: float = 20.0,
    persist: bool = True,
) -> MobileOtpConfirmResult:
    phone_digits = _normalize_phone_digits(phone)
    return _confirm_mobile_otp(
        config,
        path=DEFAULT_OTP_CONFIRM_ACCOUNT_CREATING_PATH,
        number=number,
        phone=phone_digits,
        otp=otp,
        name=name,
        is_news=is_news,
        appsflyer_parameters=appsflyer_parameters,
        client=client,
        timeout_sec=timeout_sec,
        persist=persist,
    )


def bootstrap_mobile_session(
    config: MobileConfig,
    *,
    number: str = "",
    phone: str,
    otp: str,
    name: str = "",
    is_news: int | None = None,
    appsflyer_parameters: str = "",
    client: Any | None = None,
    timeout_sec: float = 20.0,
    persist: bool = True,
    mode: str = "register",
) -> MobileBootstrapResult:
    http_client, should_close = _build_client(client, timeout_sec)
    try:
        mode_norm = mode.strip().lower() or "register"
        if mode_norm == "register":
            guest = create_mobile_anonymous_card(
                config,
                client=http_client,
                timeout_sec=timeout_sec,
                persist=persist,
            )
            if guest.ok is not True:
                return MobileBootstrapResult(
                    ok=guest.ok,
                    status=guest.status,
                    detail=guest.detail,
                    number=guest.number or number,
                    phone=phone,
                    name=name,
                )

            bootstrap_number = guest.number or number
            guest_config = _register_bootstrap_config(
                config,
                anon_token=guest.token or config.anon_token,
                card_number=bootstrap_number,
            )
            phone_digits = _normalize_phone_digits(phone)
            auth = request_mobile_otp_account_creating(
                guest_config,
                number=bootstrap_number,
                phone=phone_digits,
                client=http_client,
                timeout_sec=timeout_sec,
            )
            if auth.ok is not True:
                return MobileBootstrapResult(
                    ok=auth.ok,
                    status=auth.status,
                    detail=auth.detail,
                    auth_status=auth.auth_status,
                    status_id=auth.status_id,
                    number=bootstrap_number,
                    phone=phone,
                    name=name,
                )

            confirm = confirm_mobile_otp_register(
                guest_config,
                number=bootstrap_number,
                phone=phone_digits,
                otp=otp,
                name=name,
                is_news=is_news,
                appsflyer_parameters=appsflyer_parameters,
                client=http_client,
                timeout_sec=timeout_sec,
                persist=persist,
            )
            if confirm.ok is not True:
                return MobileBootstrapResult(
                    ok=confirm.ok,
                    status=confirm.status,
                    detail=confirm.detail,
                    auth_status=auth.auth_status,
                    confirm_status=confirm.confirm_status,
                    tokens_written=confirm.tokens_written,
                    access_token=confirm.access_token,
                    refresh_token=confirm.refresh_token,
                    token=confirm.token,
                    status_id=auth.status_id,
                    number=bootstrap_number,
                    phone=phone,
                    name=name,
                )

            verified_config = _clone_config(
                guest_config,
                access_token=confirm.access_token or confirm.token or config.access_token,
                refresh_token=confirm.refresh_token or config.refresh_token,
            )
        elif mode_norm == "auth":
            auth = request_mobile_otp_auth(
                config,
                number=number,
                phone=phone,
                client=http_client,
                timeout_sec=timeout_sec,
            )
            if auth.ok is not True:
                return MobileBootstrapResult(
                    ok=auth.ok,
                    status=auth.status,
                    detail=auth.detail,
                    auth_status=auth.auth_status,
                    status_id=auth.status_id,
                    number=number,
                    phone=phone,
                    name=name,
                )

            confirm = confirm_mobile_otp_auth(
                config,
                number=number,
                phone=phone,
                otp=otp,
                name=name,
                is_news=is_news,
                appsflyer_parameters=appsflyer_parameters,
                client=http_client,
                timeout_sec=timeout_sec,
                persist=persist,
            )
            if confirm.ok is not True:
                return MobileBootstrapResult(
                    ok=confirm.ok,
                    status=confirm.status,
                    detail=confirm.detail,
                    auth_status=auth.auth_status,
                    confirm_status=confirm.confirm_status,
                    tokens_written=confirm.tokens_written,
                    access_token=confirm.access_token,
                    refresh_token=confirm.refresh_token,
                    token=confirm.token,
                    status_id=auth.status_id,
                    number=number,
                    phone=phone,
                    name=name,
                )

            verified_config = _clone_config(
                config,
                anon_token=config.anon_token,
                access_token=confirm.access_token or confirm.token or config.access_token,
                refresh_token=confirm.refresh_token or config.refresh_token,
                card_number=number,
            )
        else:
            return MobileBootstrapResult(
                ok=None,
                status="missing_config",
                detail=f"unknown bootstrap mode: {mode}",
                number=number,
                phone=phone,
                name=name,
            )

        session = check_mobile_session(
            verified_config,
            client=http_client,
            timeout_sec=timeout_sec,
            persist=persist,
        )
        return MobileBootstrapResult(
            ok=session.ok,
            status=session.status,
            detail=session.detail,
            auth_status=auth.auth_status,
            confirm_status=confirm.confirm_status,
            orders_status=session.orders_status,
            refresh_status=session.refresh_status,
            tokens_written=confirm.tokens_written or session.tokens_written,
            status_id=auth.status_id,
            access_token=session.access_token or verified_config.access_token,
            refresh_token=session.refresh_token or verified_config.refresh_token,
            token=confirm.token,
            number=number,
            phone=phone,
            name=name,
        )
    except compat_httpx.RequestError as exc:
        return MobileBootstrapResult(
            ok=None,
            status="unavailable",
            detail=str(exc),
            number=number,
            phone=phone,
            name=name,
        )
    finally:
        if should_close:
            http_client.close()


def check_mobile_session(
    config: MobileConfig,
    *,
    client: Any | None = None,
    timeout_sec: float = 20.0,
    persist: bool = True,
) -> MobileSessionResult:
    if not config.access_token and not config.refresh_token:
        return MobileSessionResult(
            ok=None,
            status="missing_config",
            detail="VV_ACCESS_TOKEN/VV_REFRESH_TOKEN are not configured",
            access_token=config.access_token,
            refresh_token=config.refresh_token,
            anon_token=config.anon_token,
            card_number=config.card_number,
        )

    http_client, should_close = _build_client(client, timeout_sec)
    try:
        response = http_client.get(
            config.orders_url,
            headers=_headers_for(config, use_refresh=False),
            params=_number_params(config),
        )
        if response.status_code == 200:
            return MobileSessionResult(
                ok=True,
                status="ok",
                detail="orders_ok",
                orders_status=response.status_code,
                access_token=config.access_token,
                refresh_token=config.refresh_token,
                anon_token=config.anon_token,
                card_number=config.card_number,
            )

        auth_failure = response.status_code in {401, 403, 498} or not config.access_token
        if not auth_failure:
            detail = ""
            try:
                payload = response.json()
            except ValueError:
                payload = None
            if isinstance(payload, dict):
                detail = str(payload.get("error") or payload.get("message") or payload.get("detail") or "").strip()
            if not detail:
                detail = f"HTTP {response.status_code}"
            return MobileSessionResult(
                ok=None,
                status="unavailable",
                detail=detail,
                orders_status=response.status_code,
                access_token=config.access_token,
                refresh_token=config.refresh_token,
                anon_token=config.anon_token,
                card_number=config.card_number,
            )

        refresh = refresh_mobile_tokens(config, client=http_client, timeout_sec=timeout_sec, persist=persist)
        if refresh.ok is not True:
            return MobileSessionResult(
                ok=False if refresh.ok is False else None,
                status=refresh.status,
                detail=refresh.detail,
                orders_status=response.status_code,
                refresh_status=refresh.refresh_status,
                refreshed=False,
                tokens_written=refresh.tokens_written,
                access_token=refresh.access_token,
                refresh_token=refresh.refresh_token,
                anon_token=refresh.anon_token,
                card_number=refresh.card_number,
            )

        refreshed_config = _clone_config(
            config,
            anon_token=refresh.anon_token or config.anon_token,
            access_token=refresh.access_token or config.access_token,
            refresh_token=refresh.refresh_token or config.refresh_token,
            card_number=refresh.card_number or config.card_number,
        )
        retry = http_client.get(
            refreshed_config.orders_url,
            headers=_headers_for(refreshed_config, use_refresh=False),
            params=_number_params(refreshed_config),
        )
        if retry.status_code == 200:
            return MobileSessionResult(
                ok=True,
                status="refreshed",
                detail="orders_ok_after_refresh",
                orders_status=retry.status_code,
                refresh_status=refresh.refresh_status,
                refreshed=True,
                tokens_written=refresh.tokens_written,
                access_token=refreshed_config.access_token,
                refresh_token=refreshed_config.refresh_token,
                anon_token=refreshed_config.anon_token,
                card_number=refreshed_config.card_number,
            )

        try:
            payload = retry.json()
        except ValueError:
            payload = None
        detail = ""
        if isinstance(payload, dict):
            detail = str(payload.get("error") or payload.get("message") or payload.get("detail") or "").strip()
        if not detail:
            detail = f"HTTP {retry.status_code}"
        return MobileSessionResult(
            ok=False,
            status="auth_failed",
            detail=detail,
            orders_status=retry.status_code,
            refresh_status=refresh.refresh_status,
            refreshed=True,
            tokens_written=refresh.tokens_written,
            access_token=refreshed_config.access_token,
            refresh_token=refreshed_config.refresh_token,
            anon_token=refreshed_config.anon_token,
            card_number=refreshed_config.card_number,
        )
    except compat_httpx.RequestError as exc:
        return MobileSessionResult(
            ok=None,
            status="unavailable",
            detail=str(exc),
            access_token=config.access_token,
            refresh_token=config.refresh_token,
            anon_token=config.anon_token,
            card_number=config.card_number,
        )
    finally:
        if should_close:
            http_client.close()
