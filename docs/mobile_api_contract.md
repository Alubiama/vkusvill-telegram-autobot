# VkusVill Mobile API Contract
> Source: APK decompilation (jadx) + live testing, 2026-04-09
> VERIFIED: all endpoints tested successfully from Mytishchi

## Base URL
`https://mobile.vkusvill.ru/api/`

## Required Headers (VkusVillHeadersInterceptor)

```
User-Agent: vkusvill/26.5.11 (Android; 34)
X-VKUSVILL-DEVICE: android              ← NOT a UUID! Literal "android"
X-VKUSVILL-SOURCE: 2
X-VKUSVILL-VERSION: 26.5.11 (2605011)   ← "display (code)" format
X-VKUSVILL-MODEL: Google Pixel 7        ← required, any realistic model
X-VKUSVILL-TOKEN: <JWT from auth>
X-VKUSVILL-TOKEN-ACCESS: <access_token>  ← for authenticated endpoints
```

**CRITICAL**: X-VKUSVILL-DEVICE must be "android", not device UUID.
**CRITICAL**: X-VKUSVILL-VERSION must include display version in format "X.Y.Z (code)".

## Request Signing (str_par)

Every request includes `str_par` query parameter:
```
{[device_id]}{[<uuid>]}{[version]}{[<ver_code>]}{[source]}{[2]}{[ts]}{[<unix_ts>]}{[user_number]}{[<card_number>]}
```

## Auth Chain — NEW OTP Flow (AuthApi.java)

### 1. Create anonymous card (optional, for new users)
```
POST guest/createAnonymousCard/
Content-Type: application/x-www-form-urlencoded
Body: device_id=<UUID>
Response: { number, phone, fullname, email, token }
```

### 2. Request SMS code (NEW — replaces old POST user/)
```
POST v1/user/otp/auth
Content-Type: application/json
Body: {
  "phone": "9104350933",     ← 10 digits, NO +7 prefix!
  "number": "<card_number>"  ← nullable, from step 1 or existing card
}
Response: {
  "status": "success",
  "data": { "statusId": 1 },
  "errors": []
}
```

### 3. Confirm SMS code (NEW — replaces old checkSms + updateToken)
```
POST v1/user/otp/confirm/auth
Content-Type: application/json
Body: {
  "phone": "9104350933",
  "otp": "324315",           ← SMS code
  "number": "<card_number>",
  "is_news": 0               ← REQUIRED!
}
Response: {
  "status": "success",
  "data": {
    "phone": "9104350933",
    "number": "9592053",     ← real card number
    "name": "АЛЕКСАНДР",
    "email": "me@klymik.ru",
    "token": "<JWT>",
    "access_token": "<JWT>",  ← use as X-VKUSVILL-TOKEN-ACCESS
    "refresh_token": "<JWT>",
    "fl_pinning": false
  }
}
```

**This single step returns ALL tokens at once** — no need for separate updateToken call.

### 4. Refresh tokens (on 401)
```
POST user/v1/refreshToken
Content-Type: application/json
Body: { "refresh_token": "<refresh_token>" }
Response: BaseResponse<GetAccessTokenResponse>
```

### Other OTP endpoints (AuthApi.java)
- `POST v1/user/otp/account-creating` — new account via OTP
- `POST v1/user/otp/confirm/account-creating` — confirm new account
- `POST v1/user/otp/change-phone` — phone change request
- `POST v1/user/otp/confirm/change-phone` — confirm phone change
- `POST v1/user/otp/account-deleting` — account deletion request
- `POST v1/user/otp/confirm/account-deleting` — confirm deletion

## Personal Discounts (VERIFIED ✅)

### Main screen
```
GET user/privAbonement/abonementScreen
Query:
  number: "9592053"       ← card number, REQUIRED
  source: "2"
  shopNo: "916"           ← REQUIRED! Store number
  str_par: <signed>
Headers: all standard + X-VKUSVILL-TOKEN-ACCESS

Response: items.resources with:
  - title: "Скидка 20% на 6 товаров"
  - addressInfo: { icon, address, shopNo }
  - checkbox: [online/offline toggle]
  - buttons: [replace, add all to cart]
  - includeCategories
  - (product items with prices and discounts)
```

### Card status
```
GET user/privAbonement/getCardStatus
Query: number, str_par
Response: { generated: true, sources: ["1"] }
```

## Token Lifetime (from JWT)
- access_token: exp - iat = ~86400 sec (24 hours)
- refresh_token: exp - iat = ~31536000 sec (~1 year)
- VkusVill token (X-VKUSVILL-TOKEN): no exp in JWT, likely indefinite

## Legacy Auth (DEPRECATED — returns 401 on all endpoints)
The old flow (POST user/ → checkSms → updateToken) no longer works.
Server returns "Пожалуйста, авторизуйтесь в приложении заново" for all
non-OTP auth attempts.

## QRATOR/Network Notes
- Mytishchi: works perfectly (clean IP)
- VPS: QRATOR blocks (timeout on SSL after API scan 06.04)
- Odintsovo: intermittently blocked
- No special TLS fingerprint needed from clean IP — plain httpx works
