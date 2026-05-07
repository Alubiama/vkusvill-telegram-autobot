# Вкусвилл Telegram Autobot

## Статус: Mobile API РАБОТАЕТ! (09.04.2026)
Полный auth chain пройден. Токены получены. abonementScreen возвращает реальные скидки.

## Что СДЕЛАНО

1. **API найден** — APK декомпиляция (jadx), endpoint: `GET user/privAbonement/abonementScreen`
2. **Контракт обновлён**: `docs/mobile_api_contract.md` (VERIFIED)
3. **Новый OTP auth flow найден** — `v1/user/otp/auth` + `v1/user/otp/confirm/auth`
4. **SMS онбординг пройден** — телефон 9104350933, карта 9592053, АЛЕКСАНДР
5. **Токены получены и сохранены**: `data/mobile_tokens.json`
6. **abonementScreen протестирован** — реальные скидки возвращаются (shopNo=916)
7. **MobileApiProvider написан** — `src/providers.py` (нужно обновить под новый auth flow)
8. **Config обновлён** — `src/config.py`

## Что ДЕЛАТЬ ДАЛЬШЕ

### Шаг 1 (ПРИОРИТЕТ): Обновить MobileApiProvider
- Заменить старый auth flow на OTP
- Исправить заголовки: X-VKUSVILL-DEVICE="android", X-VKUSVILL-VERSION="26.5.11 (2605011)"
- Парсить реальный ответ abonementScreen (формат известен)
- Добавить shopNo в запрос (обязательный!)
- Добавить refresh_token логику

### Шаг 2: /onboard команда в боте
- В bot.py: телефон (10 цифр) → OTP → код → токены в data/mobile_tokens.json
- PROVIDER=mobile_api в .env

### Шаг 3: Тест на Одинцово
- Переключить PROVIDER=mobile_api
- Убедиться что бот собирает скидки через API

### Шаг 4: QRATOR / VPS миграция
- VPS IP заблокирован. Мытищи работают.
- Варианты: cooldown, другой IP, curl_cffi

### Шаг 5: Мульти-группы + монетизация
- groups/group_N/{tokens.json, state.db, config.env}
- 200-500₽/мес, цель 10 групп

## Ключевые технические находки (09.04.2026)

**ПРАВИЛЬНЫЕ заголовки** (из VkusVillHeadersInterceptor.java):
```
User-Agent: vkusvill/26.5.11 (Android; 34)
X-VKUSVILL-DEVICE: android              ← НЕ UUID! Буквально "android"
X-VKUSVILL-SOURCE: 2
X-VKUSVILL-VERSION: 26.5.11 (2605011)   ← формат "display (code)"
X-VKUSVILL-MODEL: Google Pixel 7        ← обязательный
X-VKUSVILL-TOKEN: <JWT>
X-VKUSVILL-TOKEN-ACCESS: <access_token>
```

**НОВЫЙ auth chain** (AuthApi.java, OTP):
1. `POST v1/user/otp/auth` — JSON: `{phone: "9104350933"}` → SMS
2. `POST v1/user/otp/confirm/auth` — JSON: `{phone, otp, number, is_news: 0}` → ВСЕ токены сразу

**Старый auth chain (POST user/ → checkSms → updateToken) — DEPRECATED**, возвращает 401.

**Телефон**: 10 цифр без +7 (9104350933, НЕ +79104350933)

**abonementScreen**: требует shopNo (номер магазина, напр. "916")

**Токены**:
- access_token: ~24 часа
- refresh_token: ~1 год
- Обновление: `POST user/v1/refreshToken` с `{refresh_token}`

## Данные авторизации
- Телефон: +79104350933 (Саша)
- Карта ВВ: 9592053
- Имя: АЛЕКСАНДР
- Email: me@klymik.ru
- Токены: `data/mobile_tokens.json`

## Текущий провайдер (рабочий, на Одинцово)
```
PROVIDER=rpa_command
```

## Архитектура
- `src/bot.py` — основная логика
- `src/providers.py` — ManualJsonProvider, RPACommandProvider, HttpJsonProvider, **MobileApiProvider**
- `src/config.py` — Settings
- `src/store.py` — SQLite хранилище (WAL mode)
- `webapp/latest.json` — данные для Mini App
- `data/mobile_tokens.json` — токены Mobile API (НОВОЕ)

## Запуск бота на Одинцово
```cmd
del bot.pid & schtasks /Run /TN "VkusvillBotNow"
```

## SSH
```bash
ssh odintsovo   # через Tailscale
ssh mytishchi   # user the_202
```

## Правила
- Перед фиксом — ЧИТАТЬ src/bot.py
- Данные от пользователя → СРАЗУ в память
- Не трогать данные вслепую
- Декомпилированный APK: `C:\temp\vkusvill-decompiled\` на Одинцово
