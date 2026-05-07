# Задача для Gemini (Мытищи): найти endpoint "Заменить товары" в VkusVill APK

## Контекст
Вкусвилл-бот собирает персональные скидки "Скидка 20% на 6 товаров" через mobile API.
Endpoint `GET mobile.vkusvill.ru/api/user/privAbonement/abonementScreen?shopNo=916` возвращает уже выбранную подборку (items.data[]).

**Проблема:** после полуночи MSK `items.data` = `[]` (пустой), хотя `items.resources.qty_tov = 6` и заголовок "Скидка 20% на 6 товаров". Сервер знает что должно быть 6 товаров, но не отдаёт их — подборка требует регенерации.

В UI приложения есть кнопка "Заменить товары" с `action_link: "action://update.abonement?channel=1&source=1&refresh=1"`. Это внутренний app-роутинг, НЕ HTTP-путь.

## Что нужно найти
Точный HTTP-запрос (метод, путь, тело, заголовки), который мобильное приложение VkusVill отправляет при нажатии "Заменить товары" / при обнулении подборки после полуночи.

## Ресурсы на машине
- Декомпилированный APK: `C:\temp\vkusvill-decompiled\` (jadx вывод)
- Версия APK: 26.5.11 (2605011)
- Grep по коду за строками:
  - `update.abonement` (action scheme)
  - `privAbonement` (известный контроллер)
  - `abonementScreen`, `refreshAbonement`, `updTovAbonement`, `generateAbonement`
  - `"refresh"` параметр в контексте абонемента

## Что перепробовано и НЕ работает
- `GET abonementScreen?refresh=1&channel=1` → 200 OK, но items.data=[]
- `GET abonementScreen?refresh=1&channel=2` → 200 OK, но items.data=[]
- `POST abonementScreen?refresh=1` → 500 "Временные проблемы"
- `GET updateAbonement`, `GET refresh`, `GET generate`, `GET getItems`, `GET updateTovar` → 404

## Что нужно вернуть
1. Точный HTTP-путь endpoint'а (напр. `POST user/privAbonement/updateScreen`)
2. HTTP-метод (GET/POST)
3. Все query-параметры и/или тело запроса (JSON)
4. Обязательные заголовки (кроме стандартных X-VKUSVILL-*)
5. Фрагмент Kotlin/Java кода из декомпиляции с этим запросом
6. Файл где найдено (имя .java в декомпиле)

## Формат ответа
```
Endpoint: <METHOD> <path>
Params: {...}
Body: {...}
Headers: {...}
Source: <filename>
Code:
<снипет>
```

## Почему это важно
Бот работает на резервном http_api, но он нестабилен (QRATOR, cookie expiry). mobile_api с refresh_token живёт год → нужен для полной автономии и мульти-юзер монетизации.
