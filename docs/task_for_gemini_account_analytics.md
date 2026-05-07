# Задача для Gemini (Мытищи): endpoints аккаунтной аналитики VkusVill

## Контекст
Вкусвилл-бот собирает скидки через mobile API (`mobile.vkusvill.ru`). Auth OTP-flow работает, токены есть (`data/mobile_tokens.json`). Endpoint `GET user/privAbonement/abonementScreen?shopNo=916` возвращает скидки для КОНКРЕТНОГО магазина.

**Что болит:**
1. `shopNo` захардкожен (`916`) — нужно брать из профиля пользователя (выбранный адрес/магазин в app).
2. У 24 товаров готовой еды в Mini App показывается "остаток уточняется" — нет per-shop availability.
3. Адрес/телефон/карту сейчас берём из `.env`, а они должны подтягиваться из аккаунта (как в app после OTP).

**Цель:** после OTP-авторизации одним/двумя запросами получить ПОЛНУЮ аналитику аккаунта:
- привязанный адрес доставки + выбранный магазин (`shopNo`)
- список всех адресов пользователя
- профиль (имя, карта, телефон, бонусы)
- per-shop stock для любого товара (particularly для готовой еды)
- историю заказов (опционально)

## Что нужно от тебя

Найти в декомпилированном APK точные HTTP endpoints со всеми параметрами.

## Ресурсы на машине (Мытищи)
- APK: проверь `~/vkusvill-decompiled/` или `/tmp/vkusvill-decompiled/`. Если нет — скачай APK версии 26.5.11 и декомпилируй через `jadx`:
  ```bash
  pip install --user jadx  # или apt
  jadx -d ~/vkusvill-decompiled ~/vkusvill-26.5.11.apk
  ```
- Если APK тоже нет — сообщи, я дам путь к apk файлу с Одинцово.

## Что искать (grep по декомпилированному коду)

### 1. Retrofit-интерфейсы с аннотациями
```bash
grep -rn "@GET\|@POST" ~/vkusvill-decompiled/sources/ru/vkusvill/ | \
  grep -Ei "user|profile|address|shop|cart|stock|availability|cabinet|account|settings|delivery"
```

### 2. Конкретные ключевые слова
```
profile, userInfo, getProfile, me, cabinet
addresses, getAddresses, deliveryAddresses, addressList
shops, shopList, selectedShop, defaultShop, currentShop, shopInfo
stock, availability, ostatok, nalichie, residue
cart, basket
getOrders, orderHistory
```

### 3. Классы и файлы
- `UserApi.java`, `ProfileApi.java`, `AddressApi.java`, `ShopApi.java`, `CatalogApi.java`
- `*Repository.java`, `*DataSource.java` в папках `user/`, `profile/`, `address/`, `shop/`
- `NetworkModule.java` / Retrofit билдер — список всех базовых путей

### 4. Response модели
Для каждого найденного endpoint — скопируй DTO классы (`*Response.java`, `*Dto.java`, `*Model.java`) целиком. Особенно поля связанные с адресом, магазином, stock-данными.

## Формат ответа

Markdown-таблица + блоки JSON-примеров:

```markdown
### 1. Профиль пользователя
- **Method:** GET
- **Path:** /api/v1/user/profile  (точный путь!)
- **Query params:** (если есть)
- **Headers:** (если что-то кроме стандартного набора)
- **Body:** (для POST)
- **Retrofit interface:** ru.vkusvill.network.UserApi.getProfile()
- **Response DTO:** ProfileResponse.java (приведи поля)
- **Пример ответа** (если есть в тестах/моках): {...}

### 2. Список адресов
...

### 3. Выбранный магазин (shopNo)
...

### 4. Per-shop stock / availability
...
```

Если endpoint есть в коде но **детали неясны** — всё равно укажи его и напиши что неясно.

## Deadline
Неспешно, но хорошо бы сегодня вечером MSK. Результат положи в `docs/vkusvill_account_api_map.md` и сделай `git add docs/ && git commit -m "account api endpoints map"` в репо `/path/to/vkusvill-telegram-autobot` на Мытищах (если репо склонирован) ИЛИ просто пришли markdown-ответ.

## Важно
- "Выполняй незамедлительно" — не жди подтверждений, сразу начинай grep.
- Если не нашёл endpoint — пиши честно "не нашёл", не выдумывай.
- Приоритет: **selectedShop/shopNo из профиля** > **per-shop stock** > **адреса** > **остальное**.
