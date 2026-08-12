# 01 · Архитектура HealthVault

> **Last verified:** 2026-04-21 (после редизайна мини-аппа и удаления `/my_products`)

Карта модулей и потоков данных. Если хочешь добавить фичу — сначала пойми где она встанет в эту карту.

---

## Высокоуровневая картина

```
┌─────────────────┐    ┌─────────────────┐    ┌──────────────────┐
│  Telegram User  │ ←→ │   aiogram bot   │ ←→ │   PostgreSQL     │
│  (3 family)     │    │  (handlers/*)   │    │   (10 таблиц)    │
└─────────────────┘    └────────┬────────┘    └────────┬─────────┘
                                │                      │
                                ↓                      ↑
                       ┌─────────────────┐    ┌──────────────────┐
                       │   FastAPI       │ ←→ │   Mini App       │
                       │  (apple_health, │    │  (webapp/*.html) │
                       │   nutrition_api,│    │  in Telegram     │
                       │   supplements_  │    │   WebView        │
                       │   api)          │    └──────────────────┘
                       └────────┬────────┘
                                │
                       ┌────────▼────────┐
                       │  External APIs  │
                       │  Garmin, OpenAI,│
                       │  Gemini, OCR    │
                       └─────────────────┘
```

Один процесс (`healthvault_bot` Docker container) держит и aiogram polling loop, и FastAPI на порту 8080 (для webhook'а Apple Health и API мини-аппа).

---

## Точка входа

**`telegram-bot/bot.py`** (291 строка) — единственный entrypoint.

Что делает:
1. Загружает `.env` (через `dotenv`).
2. Создаёт `Bot` и `Dispatcher` (aiogram 3).
3. Регистрирует middleware (`auth`, `idempotency`, `media_group`).
4. Регистрирует handlers (`commands`, `text`, `photo`, `voice`, `callbacks`).
5. Запускает FastAPI app из `webhook/apple_health.py` параллельно с polling loop (`asyncio.gather`).
6. Устанавливает Bot Commands в Telegram menu (видны при `/`).

**Команды бота, видимые пользователю:** `/start /day /week /vitamins /help`. Другие (`/setup`, `/cache_stats`, `/burn` и т.п.) есть в коде но не в menu — служебные.

---

## Слои и их роли

### `telegram-bot/handlers/` — UI-слой

| Файл | Назначение | Размер |
|---|---|---|
| `commands.py` | Команды `/start`, `/day`, `/week`, `/vitamins`, `/help`, `/setup`, `/burn`, `/cache_*`, `/status`, `/activity`, `/targets` | 521 |
| `text.py` | Любое текстовое сообщение → LLM router → диспатч в food/supplement/weight handler | 925 |
| `photo.py` | Фото: меню → vision OCR / еда → vision GPT-4o / весы → OCR weight | 1217 |
| `voice.py` | Голос → AssemblyAI транскрипт → text.py | ~70 |
| `callbacks.py` | Inline-кнопки подтверждения «Сохранить»/«Отмена» | ~10 |

⚠️ **`photo.py` — самый большой и сложный, без покрытия тестами** (см. `2026-04-21-architectural-review.md`).

### `telegram-bot/middlewares/` — кросс-cutting

| Файл | Что делает |
|---|---|
| `auth.py` | Whitelist-доступ через `config/users.py`. Прокидывает `user_id` в handler data. |
| `idempotency.py` | Дедупликация апдейтов Telegram (защита от ретраев Telegram API). |
| `media_group.py` | Сборка нескольких фото в одну группу до прихода последнего. |

### `telegram-bot/webhook/` — FastAPI слой

Routers подключаются к одному FastAPI `app` в `apple_health.py`:

| Файл | Назначение |
|---|---|
| `apple_health.py` | Главный FastAPI app. POST `/apple_health_v2` — приём данных от Health Auto Export (iOS, ежедневно автоматически). POST `/apple_health` (v1) — legacy endpoint для старых Shortcuts с плоским JSON. GET/POST `/api/settings`. Раздача статики мини-аппа `/webapp/*` с auto-versioning. |
| `nutrition_api.py` | Endpoints мини-аппа для дневника еды: GET `/api/day`, POST/PATCH/DELETE `/api/meal/item`, GET `/api/favorites`. |
| `supplements_api.py` | Endpoints для daily-log таба добавок: GET `/api/supplements/day`, POST/DELETE `/api/supplements/take`. |
| `nutrition_goals.py` | `compute_goals()` — БЖУ-цели на день из настроек+Garmin. |
| `nutrition_slots.py` | Маппинг `meal_time` → slot (breakfast/lunch/snack/dinner). |
| `tg_auth.py` | `get_tg_user()` — валидация Telegram `initData` (HMAC по bot token). Используется как `Depends()` во всех endpoint'ах. |

### `telegram-bot/webapp/` — Mini App (frontend)

| Файл | Что |
|---|---|
| `index.html` | Главный HTML с inline `<style>` и inline `<script>` для Settings + Supplements log. Вкладка Дневник идёт через `day.js`. |
| `day.js` | Логика дневника: загрузка `/api/day`, рендер карточек, переключение даты, прогресс-бары. Экспортирует `window.__nutri.state` для cross-tab синхронизации. |
| `api.js` | Тонкий типизированный fetch-wrapper (`window.API.getDay()`, `addItem()`, и т.п.). Кладёт `Authorization: tma <initData>` автоматически. |
| `day.css` | Стили вкладки Дневник + общие (tab-bar, app-header). Стили Settings/Supplements — внутри `<style>` в `index.html`. |

### `core/` — бизнес-логика, изолирована от Telegram

```
core/
├── food/                    ← парсинг еды и КБЖУ
│   ├── nutrition.py         ← главный entrypoint: process_meal_description, process_llm_food_data
│   ├── description_parser.py ← regex extraction веса/количества из текста
│   ├── menu_meal_processor.py ← обработка фото меню
│   ├── product_search.py    ← локальная база продуктов (Bombbar и т.п.)
│   └── fiber_table.py       ← lookup-таблица fiber_per_100g + estimate_fiber, enrich_items_with_fiber
│
├── llm/                     ← OpenAI/Gemini промпты
│   ├── router.py            ← главный system prompt: классифицирует сообщение (food/weight/supplement/...) и парсит
│   └── models.py            ← pydantic-схемы LLM-ответов
│
├── vision/                  ← фото-обработка
│   ├── chatgpt_vision.py    ← GPT-4o Vision: фото блюд + упаковок + весов
│   ├── gemini_vision.py     ← fallback на Gemini
│   ├── menu_parser.py       ← парсинг фото меню (отдельный flow)
│   ├── ocr_weight.py        ← скриншот весов Zepp Life → wt
│   └── weight_extraction.py ← фото-инструменты для food (выделение веса с упаковки)
│
├── health/                  ← здоровье
│   ├── garmin_data.py       ← скачать activity/sleep/HRV/body battery с garmin
│   ├── caloric_budget.py    ← расчёт дневного бюджета с учётом Garmin за 14д
│   ├── nutrition_targets.py ← БЖУ-цели через калькулятор Миффлина
│   ├── supplements.py       ← DEFAULT_SUPPLEMENTS константа
│   └── weekly_nutrition.py  ← weekly digest для команды /week
│
└── infra/
    ├── api_key_loader.py    ← Google Vision key из ~/.google_vision_api_key
    ├── storage.py           ← обёртка над Path операциями
    └── voice_service.py     ← AssemblyAI транскрипция
```

⚠️ **Proxy shims в `core/` (на уровень выше папок).** Файлы `core/llm_router.py`, `core/menu_parser.py`, `core/chatgpt_vision.py`, `core/description_parser.py`, `core/ocr_weight.py`, `core/weight_extraction.py`, `core/menu_meal_processor.py`, `core/nutrition.py`, `core/garmin_data.py`, `core/voice_service.py`, `core/weekly_nutrition.py`, `core/supplements.py`, `core/nutrition_targets.py`, `core/caloric_budget.py`, `core/storage.py`, `core/llm_models.py`, `core/api_key_loader.py`, `core/apple_health_parser.py`, `core/product_search.py`, `core/gemini_vision.py` — всё это 3-строчные `from core.<subpkg>.X import *` re-exports из рефакторинга 22.03.2026. **При новом коде импортировать напрямую из `core.food.*`, `core.vision.*`, и т.п.**

### `database/` — слой данных

| Файл | Что |
|---|---|
| `models.py` | SQLAlchemy 2 declarative models. 9 классов: User, NutritionLog, Weight, SupplementLog, ActivityLog, BloodTest, BodyMeasurement, UserSettings, и Base. |
| `crud.py` | Функции CRUD + агрегации. Принимают `db: Session` явно (никаких контекстных менеджеров внутри). |
| `__init__.py` | `SessionLocal`, `init_db`, реэкспорт CRUD-функций. |

Подробности — `03_database_schema.md`.

### `services/` — небольшие фасады

| Файл | Что |
|---|---|
| `state.py` | In-memory `state_manager` для multi-step диалогов (фото → описание → подтверждение). |
| `state_helpers.py` | `create_photo_state()` фабрика. |
| `state_models.py` | Pydantic модели для state-data. |
| `nutrition_service.py` | `get_nutrition_service()` — фасад для команды `/day`, считает дневной итог + добавки + цели. |

### `helpers/db_save.py` — write path для бота

Единственный путь записи приёмов пищи **из текстового/голосового флоу**:
- Принимает `meal_data` из `state_manager` (после подтверждения).
- Сериализует `meal_items` → `items` JSONB (см. `03_database_schema.md` про схему).
- Делает `enrich_items_with_fiber()` перед записью (write-time fiber backfill).
- Зовёт `database.crud.create_nutrition_log`.

⚠️ **Путь записи через мини-апп идёт по другой ветке** — `nutrition_api.py:add_meal_item()` пишет items напрямую в формате `{product, weight_g, ...}`. Текущая система терпит обе схемы за счёт fallback'ов в readers, но это техдолг (см. ревью).

---

## Поток данных: «owner пишет ⌨️ "ужин: курица 200г, рис 150г"»

```
1. text.py:handle_message()                 [Telegram → handler]
2. extract_date_from_text(text)              [text.py: ловим "вчера"/"19 апреля"]
3. core.llm.router.analyze_message(text)     [GPT-4o → JSON {type:"food", items:[...]}]
4. core.food.nutrition.process_llm_food_data [LLM JSON → meal_items]
5. State save → "waiting_confirmation"       [services.state]
6. Пользователь жмёт «✅ Сохранить»           [callbacks.py]
7. helpers.db_save.save_meal_to_db()          [enrich_items_with_fiber → DB]
8. database.crud.create_nutrition_log         [INSERT]
```

⚠️ **Для аудита/диагностики (важно не путать с багом):** превью с итогами БЖУ и кнопками
«✅ Сохранить»/«❌ Отмена» (шаги 5-6) — это **штатное подтверждение, не автосохранение**.
Пока юзер не нажал «Сохранить», строка в `nutrition_log` не появляется — это ожидаемо, а не
потеря данных. Состояние «waiting_confirmation» живёт в `services.state.state_manager`
**в памяти процесса**, не в БД и НЕ в `agent_conversations` — значит поиск «ответа бота» по
`agent_conversations` для этого шага ничего не найдёт, даже если бот всё показал корректно.
Прецедент: ночная смена 10.07.2026 приняла неподтверждённое превью за «молчаливую потерю
данных» (Finding F-002, впоследствии понижен после проверки по скриншоту юзера и прямому
SQL к `nutrition_log`). **Перед тем как флагать «бот не ответил» на текст еды/АД/добавок —
всегда проверяй сначала `nutrition_log`/`blood_pressure_logs`/`supplements_log` напрямую**,
а не только `agent_conversations`.

## Поток данных: «открыл мини-апп, добавил Сыр 50г в обед»

```
1. WebView → GET /webapp/                     [apple_health.py serves index.html with cache-bust hash]
2. day.js → API.getDay(today)                 [GET /api/day?date=2026-04-21]
3. nutrition_api.get_day()                    [reads nutrition_log + computes totals_day + activity_today]
4. User taps row, edits weight                [day.js: PATCH /api/meal/item]
5. nutrition_api.update_meal_item_weight()    [in-place JSONB update]
6. day.js → re-renders bars                   [client-side recompute]
```

## Поток данных: «Health Auto Export → ежедневный автоэкспорт Apple Health»

```
1. iOS-приложение Health Auto Export ($24.99 lifetime)
   → раз в сутки в фоне POST /apple_health_v2  [Bearer APPLE_HEALTH_TOKEN]
   → формат: {"data":{"metrics":[{"name":"step_count","data":[{"qty":18000,"date":"..."}]}, ...]}}
2. apple_health.py:_hae_to_daily_payloads()    [маппит 17 метрик HAE → нашу схему по дням]
3. Группировка по дате, UPSERT в:
   - activity_log (steps, distance, active_calories, HR, gait в raw_data)
   - blood_pressure_logs (systolic/diastolic от Omron)
   - weights (вес, %жира, мышцы от Mi-весов через Apple Health)
4. /sync команда подтягивает свежие данные  [scripts/sync_all_data.sh + fetch_remote_nutrition.sh]

Legacy путь: POST /apple_health (v1) — плоский JSON от старых Shortcuts.
Endpoint оставлен для обратной совместимости, но новые автоматизации — на v2.
```

---

## Что находится снаружи кода, но критически важно для понимания

| Что | Где | Зачем |
|---|---|---|
| **Файлы пользовательских данных** | Google Drive `~/HealthVault/` (отдельная папка, НЕ внутри проекта) | medical PDFs, knowledge_base.json каждого члена семьи. См. `CLAUDE.md` в корне проекта. |
| **Фото блюд (последние)** | `data/media/` | Сюда падают фото от Telegram, чтобы LLM могла к ним обращаться. |
| **Кеш Garmin токенов** | `data/cache/tokens.json` | OAuth Zepp/Garmin, истекают через 5–7 дней. |
| **Бэкапы БД** | `data/backups/healthvault_backup_*.sql` | Делаются `/cleanup` skill раз в сутки, ротация 7 файлов. |
| **Логи бота** | server: `/opt/healthvault/logs/bot.log` | `docker logs healthvault_bot --tail 50` для свежего хвоста. |

---

## Anti-patterns при работе с этим кодом

❌ **Не импортируй `core.llm_router`, `core.menu_parser`, …** — это proxy shims, держатся для обратной совместимости archived скриптов. В новом коде: `from core.llm.router import …`, `from core.vision.menu_parser import …`.

❌ **Не пиши новые поля в `users` таблицу** — все настройки/цели живут в `user_settings` (отдельная таблица, см. `03_database_schema.md`). Поле `users.target_calories` НЕ существует.

❌ **Не дублируй логику записи приёмов пищи.** Текстовый/голосовой флоу → `helpers/db_save.py`. Мини-апп → `nutrition_api.py:add_meal_item`. Обе делают `enrich_items_with_fiber` перед записью — следи чтобы любой новый путь записи делал то же.

❌ **Не делай `SELECT … FROM nutrition_log WHERE date >= …` без `user_id`.** В боте 3 пользователя — без фильтра суммируются все.

❌ **Не используй `users.id`** в FK — используется `users.telegram_id` (BigInteger). Все FK на пользователя — на `telegram_id`.

❌ **Не читай поле `totals.fat`** — поле называется `totals.fats` (множественное число). Старая дока ошибалась — могло привести к молчаливому 0 в SQL.

✅ **Сначала прочитай `database/crud.py`** прежде чем писать новый запрос — почти всё уже есть.

✅ **Всегда `enrich_items_with_fiber`** перед записью — иначе fiber поле пустое и `/api/day` молча показывает 0г клетчатки.

✅ **Канонические команды для проверки бота:**
```bash
# логи бота
ssh root@116.203.213.137 "docker logs healthvault_bot --tail 50"

# статус контейнера
ssh root@116.203.213.137 "docker ps | grep healthvault"

# рестарт после деплоя
ssh root@116.203.213.137 "docker restart healthvault_bot"

# тесты локально
./venv/bin/python3 -m pytest tests/ --ignore=tests/test_live_llm.py -q
```

---

## Неочевидные архитектурные решения и почему

**`items` хранится как JSONB, не как нормальная таблица `nutrition_items`.** Решение: схема items сильно вариативна (макросы, fiber, источник, веса с компонентами), редко join'им поэлементно. Цена — невозможность индексировать имена через b-tree (есть GIN, но не использовали).

**Один Docker container держит aiogram + FastAPI.** Решение: low traffic (3 user'а), деплой проще. Цена — Garmin sync блокирует event loop когда вызывается из `/api/day` (см. ревью).

**Mini App auth через Telegram `initData`.** Решение: пользователю не надо отдельно логиниться, при открытии в Telegram WebView токен уже есть. Цена — нельзя протестировать API из браузера без mock'а initData.

**Fiber backfill в 4 слоя.** LLM prompt → write-time enrichment → read-time fallback → migration script. Решение: исторические данные уже разнородные, нужна defense in depth. См. `AI_CHANGELOG.md` 2026-04-20.

---

[← Документация Botkin — Index](../INDEX.md)
