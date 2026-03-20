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

---

## Task 69: Incident fix — заблокировать неканионические копии runtime

**Status:** done
**Priority:** P0

**Do:** После живого инцидента проверить, что бот отвечает только из канонического `X:\vkusvill-telegram-autobot`, а любые старые копии в `D:` / `Documents` не могут тихо поднять свой `src.main`. Старые `scripts/ensure-bot-running.ps1` должны делегировать в путь из `C:\Users\Sasha\projects\REGISTRY.md`, а старые `src/main.py` — fail-closed с сообщением `Refusing to start from non-canonical workspace`.

**Files:** `src/main.py`, `scripts/ensure-bot-running.ps1`, `src/runtime_guard.py` в старых копиях (`D:` и `Documents`), `C:\Users\Sasha\projects\REGISTRY.md`

**Done when:**
- В системе жив только один бот из `X:\vkusvill-telegram-autobot`
- Пробный запуск из `C:\Users\Sasha\Documents\vkusvill-telegram-autobot` не стартует и печатает `Refusing to start from non-canonical workspace`
- Запуск старого `ensure-bot-running.ps1` делегирует в `X:` и не поднимает отдельный рантайм

---

## Task 70: Fix — вернуть `CHAT_ID` и убрать тихий провал отправки в группу

**Status:** done
**Priority:** P0

**Do:** После миграции на `X:` проверить, что групповой `CHAT_ID` не потерялся. Если он пустой, восстановить его из старого рабочего state/meta, прописать в `X:\vkusvill-telegram-autobot\.env` и в живую `state.db`, затем подтвердить прямой `send_message` в группу. В `src/bot.py` убрать тихий провал `_send()`: при пустом `CHAT_ID` или ошибке Telegram бот должен предупредить владельца, а не молча "съесть" сообщение.

**Files:** `.env`, `data/state.db`, `src/bot.py`

**Done when:**
- Прямой `send_message` в привязанную группу проходит
- В `X:` есть `CHAT_ID=-1003477471957`
- `_send()` больше не молчит при пустом chat binding или ошибке отправки

---

## Task 71: Hardening � startup sanity check + ������ live audit

**Status:** done
**Priority:** P0

**Do:** ����� ����� ����� ���������� �������� �� �������� �����, � ��������� self-check:
- � `src/bot.py` ������� startup sanity run, ������� ��������� `CHAT_ID`, owner, runtime root, `get_chat()` � Telegram � ������� ������������� ���, � ��� ��������� ��� ���� owner-alert;
- �������� ��������� ������������ ������ `scripts/live_system_audit.py`, ������� ����� �������� ��������� runtime, scheduled task, Telegram API, chat binding, collect meta � day integrity.

**Files:** `src/bot.py`, `scripts/live_system_audit.py`, `tests/test_bot_backend_guards.py`

**Done when:**
- ����� ������ ��� ��� ����� `last_startup_sanity_status=ok|warning|critical`
- ��� ������ `CHAT_ID` ��� ����� group chat �������� �������� alert ��� ������� `/health`
- `python scripts/live_system_audit.py` �������� ���� ������� JSON-����� �� �������

---

## Task 72: Fix — `cancelcycle` должен отменять текущий активный batch, а не только `open`

**Status:** done
**Priority:** P0

**Do:** После живого инцидента с кнопкой `Отменить текущий заказ` привести owner cancel-path в соответствие с реальным жизненным циклом batch. Если текущий batch уже перешёл в `added_waiting_payment` или `partially_added`, команда и callback всё равно должны его отменять, а текст ответа должен честно предупреждать, что корзину ВкусВилл, возможно, нужно дочистить вручную.

**Files:** `src/bot.py`, `tests/test_bot_backend_guards.py`

**Done when:**
- owner button `Отменить текущий заказ` работает для `open`, `partially_added` и `added_waiting_payment`
- при отсутствии batch текст говорит `Сейчас нет активного batch для отмены.`
- есть тесты на waiting-payment и partial cancel path

---

## Task 73: Полный аудит системы -- stress test + chaos + TRIZ

**Status:** pending
**Priority:** P0 -- фундамент перед любой новой разработкой

**Контекст:**
20 марта 2026 бот полностью сломался: вчерашние товары в Mini App, двойной процесс, диск D: переполнен, CHAT_ID пустой, silent failures. Кодекс починил инциденты точечно (tasks 57-72). Теперь нужен системный аудит: найти ВСЕ оставшиеся проблемы до того как они выстрелят.

**Результат:** только отчёт `audit/AUDIT-2026-03-20.md`. Без фиксов. Claude проанализирует отчёт и напишет задачи на фиксы.

**Формат отчёта:** каждый пункт -- verdict (pass / fail / warning / not_testable) + комментарий + evidence (лог, вывод команды).

---

### Блок 1: Процесс и runtime

**Do:**
1. Проверить что PID lock работает: запустить второй `python -m src.main` -- должен отказать. Записать stdout/stderr.
2. Проверить Task Scheduler: `schtasks /Query /TN "vkusvill-telegram-autobot-watchdog" /XML` -- путь указывает на X:? Интервал? Trigger?
3. Убить бот-процесс (taskkill /PID ...), засечь время -- через сколько watchdog поднимет? Записать секунды.
4. Проверить старые копии: запустить `python -m src.main` из `C:\Users\Sasha\Documents\vkusvill-telegram-autobot\` и из `D:\projects\vkusvill-telegram-autobot\` -- оба должны отказать с `Refusing to start from non-canonical workspace`.
5. Проверить runtime_guard.py -- при импорте из неканонического пути -- exception.

**Done when:** 5 verdicts в отчёте с evidence.

---

### Блок 2: Сбор данных (collector)

**Do:**
1. Запустить сбор с текущим .env -- записать exit code, количество товаров, время выполнения.
2. Проверить day-pool: запустить сбор дважды подряд -- второй запуск добавляет товары к первому (union), не заменяет?
3. Проверить cross-day guard: вручную записать вчерашнюю дату в today_pool_date.txt, запустить сбор -- пул должен сброситься.
4. Проверить graceful 404: запустить с --offers-ready-food-url "https://vkusvill.ru/nonexistent-page-12345/" -- скрипт не падает, пишет warning, exit 0.
5. Проверить low-disk guard: проверить код -- вызвать shutil.disk_usage на текущем диске и сверить с порогом.
6. Проверить refresh-limit guard: в логе при отклонённом refresh -- ровно 1 строка refresh rejected, не повторяется.
7. Замерить: сколько места занимает data/chrome-user-data/ сейчас? Есть ли Cache/, GrShaderCache/, Crashpad/ которые можно чистить?

**Done when:** 7 verdicts в отчёте.

---

### Блок 3: Данные и состояние

**Do:**
1. sqlite3 data/state.db "PRAGMA integrity_check" -- ok?
2. Сравнить data/today_discounts.json vs webapp/latest.json vs записи в state.db -- количество item_id совпадает? Даты совпадают?
3. Проверить backup: скопировать последний backup из data/backups/, открыть, сверить integrity и количество записей.
4. Проверить stock_qty: в today_discounts.json найти все товары где stock_qty != null -- значение взято из текста или из data-max?
5. Проверить JSON atomic write: найти в коде где пишется today_discounts.json -- используется ли tempfile + rename (atomic) или прямой open().write() (может обрезать при crash)?

**Done when:** 5 verdicts в отчёте.

---

### Блок 4: Telegram интеграция

**Do:**
1. Проверить CHAT_ID: getChat(CHAT_ID) через Telegram API -- ответ 200, название группы корректное?
2. Проверить owner alert: временно очистить CHAT_ID в памяти (не в .env!), вызвать _send() -- владелец получил алерт?
3. Отправить /app боту -- Mini App открывается? Данные = сегодняшние? force_stale = false?
4. Проверить startup sanity: прочитать из state.db -- last_startup_sanity_status, last_startup_sanity_detail -- что там?
5. Rate limit: отправить 10 сообщений боту за 5 секунд -- бот не крашится? Telegram не банит?

**Done when:** 5 verdicts в отчёте.

---

### Блок 5: Mini App frontend

**Do:**
1. Собрать payload с force_stale=true -- Mini App показывает "сбор не удался", не вчерашние товары?
2. Пустой payload (rows=0) -- Mini App не крашится, показывает empty state?
3. Проверить: webapp/latest.json с вчерашней датой -- Mini App НЕ показывает его как свежий?
4. Блок "Как выбрать" -- удалён из HTML?
5. Проверить XSS: в URL params подставить скрипт-тег вместо item name -- экранируется?
6. Проверить cache: заголовки latest.json на GitHub Pages -- есть ли Cache-Control, ETag? Через сколько обновляется?

**Done when:** 6 verdicts в отчёте.

---

### Блок 6: Скрытые угрозы (code review)

**Do:**
1. Найти все except: и except Exception: без логирования -- silent swallow?
2. Найти все open(path, "w") без atomic write pattern -- risk of half-write?
3. Найти все hardcoded пути (C:\Users, D:\projects, конкретные URL) -- config drift?
4. Найти все time.sleep() в основном event loop -- blocking?
5. Найти все subprocess.run() без timeout -- может зависнуть навечно?
6. Найти все обращения к datetime без timezone -- clock skew risk?
7. Проверить: есть ли requirements.txt или pyproject.toml с pinned versions? pip freeze -- записать в отчёт.
8. Проверить .gitignore: .env, data/, out/, *.db -- не утекут в git?

**Done when:** 8 verdicts в отчёте.

---

### Блок 7: Боевые сценарии (chaos testing)

**ВАЖНО:** перед каждым деструктивным тестом -- cp data/state.db data/state.db.pre-chaos. После -- восстановить. Бот должен остаться рабочим.

**Do:**

**S1 -- Phantom Bot:** записать несуществующий PID в data/bot.pid, запустить бота -- должен стартовать нормально (stale PID detection).

**S2 -- Silent Corruption:** записать невалидный JSON в data/today_discounts.json, вызвать бот-обработчик который читает этот файл -- бот не крашится? Показывает stale state? Логирует ошибку?

**S3 -- Clock Skew:** (только анализ кода, НЕ менять системные часы) -- найти все вызовы datetime.now(), date.today(), time.time() -- используют ли TIMEZONE из .env? Что если системное время UTC, а бот ожидает Europe/Moscow?

**S4 -- Zombie Chrome:** запустить Chrome с --user-data-dir=data/chrome-user-data, НЕ закрывать -- запустить сбор -- как обрабатывает locked profile? Timeout? Ошибка?

**S5 -- SQLite Lock:** открыть state.db в exclusive lock (BEGIN EXCLUSIVE в фоне), запустить сбор -- WAL busy обрабатывается? Timeout? Или crash?

**S6 -- Half-Write:** обрезать today_discounts.json до 50 байт (невалидный JSON), проверить что бот восстановится -- либо из backup, либо при следующем сборе.

**S7 -- GitHub CDN Cache:** после публикации latest.json -- сразу fetch с ?t=timestamp и без -- одинаковый контент? Cache-busting работает?

**S8 -- Token Cascade:** очистить VV_ACCESS_TOKEN в .env, запустить vkusvill_mobile_session_check.py и vkusvill_refresh_token.py -- graceful degradation? Warning? Или crash?

**S9 -- Chrome Profile Bloat:** du -sh data/chrome-user-data/ и du -sh data/chrome-user-data/*/ -- размер каждой поддиректории. Есть ли Cache/, Code Cache/, GPUCache/? Прогноз роста за 30 дней.

**S10 -- Reboot Recovery:** убить ВСЕ python и chrome процессы (taskkill /F /IM python.exe, taskkill /F /IM chrome.exe), подождать 2 минуты -- watchdog поднял бота? Проверить PID в data/bot.pid -- свежий? Бот отвечает на /app?

**Done when:** 10 verdicts в отчёте. После всех тестов бот работает (проверить /app).

---

### Блок 8: Фундаментальные проверки

**Do:**

**F1 -- Логи:** запустить бота, подождать 2 минуты, прочитать stderr/stdout -- есть timestamp? severity? source? Формат консистентный?

**F2 -- Config validation:** подставить в .env кривые значения (по одному, восстанавливая после каждого):
- ORDER_DEADLINE=invalid -- fail-fast с понятной ошибкой?
- COLLECTION_TIMES= (пустой) -- fail-fast?
- BOT_TOKEN= + BOT_TOKEN_FILE=nonexistent -- fail-fast?
- TIMEZONE=Invalid/Zone -- fail-fast?

**F3 -- Git:** что в .gitignore? Что отслеживается? Код (src/, scripts/) под версионным контролем или нет?

**F4 -- Dependencies:** pip freeze -- записать все пакеты и версии. Есть ли requirements.txt? Совпадает с freeze? Pinned или floating?

**F5 -- Concurrency:** запустить сбор (subprocess) и одновременно отправить /app -- бот обрабатывает оба без deadlock?

**F6 -- Mini App XSS:** подставить в URL params HTML-инъекцию вместо item name -- экранируется? Проверить innerHTML vs textContent в коде.

**F7 -- Timezone:** все datetime.now() в коде -- используют tz= параметр? Или naive datetime?

**F8 -- Backup restore:** скопировать backup в temp, заменить им state.db, запустить python -m src.main -- стартует? Данные корректны? После теста восстановить оригинал.

**Done when:** 8 verdicts в отчёте.

---

### Блок 9: TRIZ-анализ противоречий

**Do:** (аналитический, без тестов)

**T1 -- Автономность vs хрупкость:**
Бот должен работать без человека 24/7. Но зависит от: Chrome binary, VkusVill DOM structure, Telegram API, диска, сети, Windows Task Scheduler. Перечислить каждую зависимость -- есть ли fallback? Что будет если каждая упадёт? Какие зависимости можно убрать или заменить?

**T2 -- Свежесть vs доступность:**
Данные должны быть сегодняшние. Но если сбор упал -- показать "ничего нет" тоже плохо. Где граница? Текущая реализация force_stale -- правильный ли компромисс? Или лучше показать вчерашнее с пометкой "данные могут быть неактуальны"?

**T3 -- Chrome profile: хранить vs чистить:**
Сессия VkusVill хранится в cookies -- нужен profile. Но Cache/, GPU cache, Crashpad растут бесконтрольно. Можно ли автоматически чистить cache сохраняя только cookies + localStorage? Предложить конкретный скрипт/команду.

**Done when:** 3 аналитических секции в отчёте с конкретными рекомендациями.

---

**Files:** audit/AUDIT-2026-03-20.md (создать директорию audit/)

**Do NOT:**
- Не фиксить ничего. Только отчёт.
- Не менять .env / state.db / today_discounts.json на постоянной основе (только на время теста, потом restore).
- Не перезаписывать webapp/latest.json.
- Не коммитить аудит -- это внутренний документ.
- Не пропускать пункты со словами "не удалось проверить" -- если нельзя проверить, объяснить почему и дать verdict not_testable.
