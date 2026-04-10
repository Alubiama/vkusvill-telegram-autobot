# 2026-03-24 - Mobile API reverse engineering

## Scope

Reverse the VkusVill APK bundle to understand how the app reaches the backend and what is needed to move the bot away from a Chrome-dependent runtime.

## Findings

- The APK is `ru.vkusvill` `26.4.6` and exposes a mobile backend surface at `https://mobile.vkusvill.ru/api/`.
- SSL pinning is enabled in the build config, so passive/static analysis is the safest baseline.
- The app clearly uses Retrofit/OkHttp style services for auth and user flows.

### Auth surface

- `AuthApi`
  - `POST /api/v1/user/otp/auth`
  - `POST /api/v1/user/otp/confirm/auth`
  - `POST /api/v1/user/otp/account-creating`
  - `POST /api/v1/user/otp/confirm/account-creating`
  - `POST /api/v1/user/otp/account-deleting`
  - `POST /api/v1/user/otp/confirm/account-deleting`
  - `POST /api/v1/user/otp/change-phone`
  - `POST /api/v1/user/otp/confirm/change-phone`
- `Login2Api`
  - `POST /api/user/v1/refreshToken`
- `UserApi`
  - `POST /api/user/v1/updateToken`
  - `POST /api/user/checkSms/`
  - `GET /api/v1/customer/phoneChangeCheck`

### DTOs

- `OtpAuthBody(number, phone)`
- `OtpConfirmBody(phone, number, name, isNews, otp, appsFlyerParameters)`
- `GetAccessTokenBody(refreshToken)`
- `GetAccessTokenResponse(accessToken, refreshToken)`
- `UpdateTokenBody(accountNumber, phone)`
- `UpdateTokenResponse(accessToken, refreshToken)`
- `OtpAuthResponse(statusId)`
- `OtpConfirmResponse(accessToken, refreshToken, token, phone, number, name, email, flPinning)`

## Impact on the bot

- The bot already had an orders/updateToken mobile layer.
- The new APK map shows the missing auth bootstrap path is not Chrome-specific; it is a mobile OTP/refresh-token flow.
- The next Chrome-free step is to wire the mobile auth helpers into a non-Playwright session bootstrap path.

## Status

- Static reverse engineering complete for the auth/token slice.
- `src/mobile_api.py` now includes refreshToken fallback plus OTP auth/confirm helpers.
- Remaining work: connect those helpers to a real bootstrap flow and decide whether the legacy `checkSms` path is still needed as fallback.
