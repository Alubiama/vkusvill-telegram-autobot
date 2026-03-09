# Vkusvill Telegram Autobot (Stage 1)

Автоматизация сбора заявок в Telegram:
- 3 сбора скидок в день
- карточки товаров с кнопками `+1/+2/Сброс`
- дедлайн и автоматический итог в JSON
- опциональный executor для автооформления

## 0) Безопасность

Токен бота уже был отправлен в чат. Рекомендуется:
1. В `@BotFather` выполнить `/revoke` для старого токена.
2. Сгенерировать новый и использовать только его.

## 1) Установка

```powershell
cd C:\Users\Sasha\Documents\vkusvill-telegram-autobot
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
python -m playwright install chromium
```

## 2) Настройка

```powershell
Copy-Item .env.example .env
```

Заполни `.env`:
- `BOT_TOKEN=...`
- `CHAT_ID=` можно оставить пустым и привязать через `/bind`
- `TIMEZONE=Europe/Moscow`
- `COLLECTION_TIMES=09:00,13:00,17:00`
- `ORDER_DEADLINE=19:30`
- `PROVIDER=manual_json` (для теста) или `rpa_command` (боевой режим)
- `DRY_RUN=true`

Для теста скопируй пример:

```powershell
Copy-Item data\today_discounts.example.json data\today_discounts.json
```

## 3) Запуск

```powershell
cd C:\Users\Sasha\Documents\vkusvill-telegram-autobot
.\.venv\Scripts\activate
python -m src.main
```

Или одной командой:

```powershell
cd C:\Users\Sasha\Documents\vkusvill-telegram-autobot
.\start.ps1
```

## 4) Привязка чата

1. Добавь бота в нужный Telegram-чат.
2. Отправь команду `/bind` в этом чате.
3. Выполни `/collect` чтобы сразу опубликовать текущие скидки.

## 5) Команды

- `/bind` - привязать текущий чат
- `/collect` - принудительно обновить скидки
- `/status` - текущие выборы
- `/finalize` - собрать итог вручную
- `/help` - помощь

## 6) Боевой режим без участия

Перевод в полный автопилот:
1. `PROVIDER=rpa_command`
2. `RPA_COMMAND=<команда, которая возвращает JSON со скидками>`
3. `DRY_RUN=false`
4. `ORDER_EXECUTOR_COMMAND=<команда добавления в корзину/оформления>`

`RPA_COMMAND` должен вернуть JSON-массив:

```json
[
  {
    "item_id": "abc123",
    "name": "Йогурт греческий",
    "price": 129,
    "discount_price": 99,
    "source": "vkusvill_rpa"
  }
]
```

## 7) Авторизация VkusVill (Web Session)

Одноразовая авторизация по SMS, затем сессия сохраняется в файл.

```powershell
cd C:\Users\Sasha\Documents\vkusvill-telegram-autobot
.\.venv\Scripts\activate
python scripts\vkusvill_auth_playwright.py
```

Быстрый вариант одной командой:

```powershell
cd C:\Users\Sasha\Documents\vkusvill-telegram-autobot
.\auth-vkusvill.ps1
```

Если PowerShell блокирует `.ps1`, запусти CMD-вариант:

```powershell
cd C:\Users\Sasha\Documents\vkusvill-telegram-autobot
.\auth-vkusvill.cmd
```

После логина проверь сессию:

```powershell
python scripts\vkusvill_session_check.py
```

Или через CMD-файл:

```powershell
.\check-vkusvill-session.cmd
```

Если вывод содержит `"ok": true`, сессия готова для подключения реального сборщика скидок.

Синхронизация уже авторизованного системного Chrome-профиля:

```powershell
cd C:\Users\Sasha\Documents\vkusvill-telegram-autobot
.\sync-vkusvill-session.cmd
.\check-vkusvill-session.cmd
```

Пробный сбор реальных скидок в `data/today_discounts.json`:

```powershell
.\collect-vkusvill-discounts.cmd
```

Упрощенный путь (рекомендуется): если ты уже залогинен в обычном Chrome, можно сразу запускать
`collect-vkusvill-discounts.cmd` без `sync`/`state` шагов.

## 8) New UX: Scrollable Showcase

Use the new in-chat cards with paging:

- `/shop` or `/browse` - open one-card showcase
- `Prev/Next` - switch products
- `-1/+1/+2/Reset` - set your quantity
- `Totals` - quick selected summary

`/collect` now also posts an `Open Showcase` button.

## 9) Mini App Window (Telegram Web App)

1. Set `MINI_APP_URL` in `.env` (HTTPS public URL).
2. Restart bot.
3. Run `/app` and click `Open App Window`.

Starter web app template is in:

- `webapp/index.html`

Mini App sends user choices back with `Telegram.WebApp.sendData(...)`.
The bot saves those choices automatically.
