# Tasks

## Task 55
- Status: done
- Mobile API tokens in `.env` + auto-refresh

## Task 56
- Status: done
- Mobile API session health check without Playwright


---

## Task 57: Fix — 404 на offers/gotovaya-eda/ роняет весь сбор

**Status:** done  
**Priority:** P0 — блокирует сбор каждый день

**Проблема:**
`_collect_offers_ready_food()` в `scripts/vkusvill_collect_discounts.py` вызывает `response.raise_for_status()` без try/except. URL `https://vkusvill.ru/offers/gotovaya-eda/` вернул 404 — весь скрипт упал с exit code 1. Бот не записал данные, показывал вчерашние.

**Временный воркэраунд:** флаг `--offers-ready-food-url` убран из `.env`.

**Постоянный фикс:**
В `scripts/vkusvill_collect_discounts.py` функции `_fetch_ready_food_html()` и `_collect_offers_ready_food()` — обернуть в `try/except`:
- Любой HTTP-статус ≥ 400 → log warning `[collector] ready food skipped: HTTP {status}`, вернуть `[]`
- Сетевая ошибка (timeout, ConnectionError) → log warning, вернуть `[]`
- Не прерывать весь сбор из-за дополнительного источника

**Done when:** запустить с несуществующим URL → exit code 0, в stderr warning, в stdout валидный JSON с основными товарами

---

## Task 58: Fix — скрипт тратит лимит "смены товаров" при каждом запуске

**Status:** done
**Priority:** P0 — каждый запуск сборщика тратит суточный лимит "смены товаров"

**Проблема:**
У VkusVill есть лимит на количество смен товаров в сутки. Скрипт при каждой волне делает refresh — и если запускается 3 раза (00:00, 00:05, 00:15), тратит лимит впустую до основного сбора в 10:00. В итоге к 10:00 лимит исчерпан — товаров 6 вместо 18+.

Ошибка API: `"Карта не найдена"` / `"заблокирована более 2 лет"` — это НЕ блокировка карты, это ответ когда лимит смен исчерпан или сессия не имеет права на смену.

**Фикс в `scripts/vkusvill_collect_discounts.py`:**
1. Если refresh API вернул ошибку → НЕ пытаться refresh на следующих волнах этого же запуска (флаг `refresh_exhausted = True`)
2. Логировать `[collector] refresh rejected, skipping further refresh attempts` один раз
3. Между волнами НЕ делать refresh если предыдущий был отклонён

**Done when:** при запуске с исчерпанным лимитом в логе ровно 1 строка "refresh rejected", не 6. Товары всё равно собираются.

---

## Task 59: Hardening — защита от двойного запуска бота

**Status:** done  
**Priority:** P1 — была причиной нестабильности

**Проблема:**
Было одновременно 2 экземпляра бота (C: venv + системный Python). Это даёт Telegram 409 Conflict, двойные попытки сбора, хаос.

**Фикс в `src/bot.py` или новом `scripts/ensure-single-instance.py`:**
1. При старте `src/main` — проверить через PID-файл (`data/bot.pid`) что другой экземпляр не запущен
2. Если PID-файл существует и процесс жив → exit с логом `[bot] another instance running (PID=X), exiting`
3. Если процесс мёртв → перезаписать PID-файл и продолжить
4. При штатном завершении бота → удалить PID-файл

**Done when:** запустить два `python -m src.main` одновременно → второй завершается через 3 секунды с exit 1 и логом

---

## Task 60: Fix — авто-обновление today_discounts.json после перезапуска

**Status:** done  
**Priority:** P1

**Проблема:**
Если бот перезапустился в 01:00 ночи, но сбор за сегодня был в 00:00 до перезапуска — новый экземпляр бота читает `today_discounts.json` от предыдущего дня. При открытии `/app` пользователь видит вчерашние данные.

**Фикс:**
В `src/bot.py` при запуске (до начала polling):
1. Проверить дату модификации `DISCOUNTS_JSON_PATH`
2. Если файл от вчера → запустить внеочередной сбор сразу (не ждать COLLECTION_TIMES)
3. Если сбор провалился → оставить вчерашние данные, но пометить `stale=true` в метаданных

**Done when:** перезапустить бота при вчерашнем `today_discounts.json` → в течение 5 минут файл обновлён


---

## Task 61: Fix — OSError [Errno 28] No space left при сборе

**Status:** done

**Do:** В `scripts/vkusvill_collect_discounts.py` при запуске Chrome через Playwright — поймать `OSError` с `errno == 28` (No space left on device) и выдать внятное сообщение вместо крэша: `[collector] disk full on {path}, cannot write Chrome profile`. Не пытаться продолжать. Exit code 1 с этим текстом. Так бот получит читаемую причину сбоя и не будет пытаться retry.

**Files:** `scripts/vkusvill_collect_discounts.py`

**Done when:** при полном диске ошибка в Telegram содержит "disk full" а не питонячий traceback

---

## Task 62: UX — убрать "Как выбрать" из Mini App

**Status:** done

**Do:** В `webapp/index.html` — удалить секцию `<section class="card quick-guide" aria-label="Как выбрать товары">` и все связанные стили `.guide-step`, `.guide-step-title`, `.guide-step-copy`. Место должно освободиться, товары подняться выше.

**Files:** `webapp/index.html`

**Done when:** в открытом Mini App нет блока "Как выбрать" с тремя шагами

---

## Task 63: Fix — выкинуть логику "preserving existing" из сборщика

**Status:** done

**Do:** В `scripts/vkusvill_collect_discounts.py` найти и удалить логику "preserving existing regular set" — когда скрипт при малом количестве свежих товаров добивает вывод вчерашними из существующего `today_discounts.json`. Вчерашних в списке быть не должно. Собрал 7 — пиши 7.

**Files:** `scripts/vkusvill_collect_discounts.py`

**Done when:** запустить сбор, убедиться что в `today_discounts.json` только товары текущего запуска, ни одного из старого файла


---

## Task 64: Feature — накопительный дневной пул товаров

**Status:** done
**Priority:** P0 — решает проблему исчерпания лимита смены товаров

**Идея:**
Сейчас каждый запуск сборщика перезаписывает `today_discounts.json`. Если в волне 1 нашли 6 товаров, сделали смену → нашли 6 новых → лимит исчерпан — итого видно только последние 6.

Нужно: каждый запуск **добавляет** товары в дневной пул, не заменяет. Итоговый файл = union всех запусков за сегодня (без дублей по `item_id`).

**Реализация в `scripts/vkusvill_collect_discounts.py`:**

1. Рядом с `today_discounts.json` хранить `today_pool.json` и `today_pool_date.txt`
2. При запуске: прочитать `today_pool_date.txt`. Если дата = сегодня → загрузить `today_pool.json`. Если вчера → начать пустой пул.
3. После каждой волны: добавить новые `item_id` в пул (set union по `item_id`). Более новые данные по тому же `item_id` перезаписывают старые.
4. В конце: записать пул в `today_discounts.json` и `today_pool.json`, обновить `today_pool_date.txt`.

**Files:** `scripts/vkusvill_collect_discounts.py`, `data/today_pool.json` (новый), `data/today_pool_date.txt` (новый)

**Done when:**
- Запустить сбор дважды с разными товарами → в `today_discounts.json` товары из обоих запусков
- При запуске на следующий день → пул сброшен, только свежие товары

---

## Task 65: Защита — проверка диска перед сбором

**Status:** done
**Priority:** P0

В начале `vkusvill_collect_discounts.py` перед запуском Chrome:
- Проверить свободное место на диске где лежит `chrome-user-data`
- Если < 500MB свободно → завершить с exit code 2 и сообщением `[collector] ABORT: disk space low: Xmb free on Y:`
- Бот должен поймать exit code 2 и послать алерт в Telegram владельцу: "⚠️ Сбор отменён: мало места на диске"

**Files:** `scripts/vkusvill_collect_discounts.py`, `src/bot.py`

---

## Task 66: Защита — graceful обработка 404/5xx для любых URL в сборщике

**Status:** done
**Priority:** P0

Сейчас если `--offers-ready-food-url` возвращает 404 — скрипт падает с исключением и не пишет ничего в `today_discounts.json`.

Нужно: любой HTTP-запрос к внешним URL оборачивать в try/except. При 4xx/5xx — логировать `[collector] WARNING: url X returned Y, skipping` и продолжать без этого источника. Не падать.

**Files:** `scripts/vkusvill_collect_discounts.py`

---

## Task 67: Защита — алерт боту при малом количестве товаров

**Status:** done
**Priority:** P1

После сбора: если в `today_discounts.json` меньше 10 уникальных `item_id` — бот шлёт алерт владельцу в личку:
`"⚠️ Сбор завершён, но найдено только N товаров. Проверь сессию VkusVill."`

Порог 10 — конфигурируется через `.env`: `COLLECT_MIN_ITEMS=10`

**Files:** `src/bot.py`, `.env.example`

---

## Task 68: Защита — один экземпляр бота (PID lock)

**Status:** done
**Priority:** P1

При старте `src/main.py` — писать PID в `data/bot.pid`. При повторном запуске: если `bot.pid` существует и процесс с этим PID живёт — завершиться с `[bot] already running, pid=X`. При нормальном завершении — удалять `bot.pid`.

Это убирает ситуацию когда два бота одновременно отвечают на сообщения.

**Files:** `src/main.py`

---

## Task 63 + 64 (объединить в один PR): Выкинуть "preserving existing", заменить накопительным пулом

**Status:** done
**Priority:** P0

**63:** Удалить логику "preserving existing regular set" из `vkusvill_collect_discounts.py` полностью.

**64:** Вместо неё — накопительный дневной пул:
- `data/today_pool.json` + `data/today_pool_date.txt`
- Каждый запуск: если дата совпадает с сегодня → merge (union по `item_id`, свежие данные перезаписывают старые) → записать в `today_discounts.json`
- Если дата другая → начать пустой пул

Делать в одном PR — иначе 63 без 64 оставит бота с 6 товарами.

**Files:** `scripts/vkusvill_collect_discounts.py`
