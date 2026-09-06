# 01 · Архитектура Botkin

> **Last verified:** 2026-09-06 (после добавления BotkinClaw, MCP-коннектора, CGM и режима план→факт — полностью переписан: бот больше не 3-user whitelist, а open-registration multi-user с cohort/RLS, работает через Telegram webhook, не polling)

Карта модулей и потоков данных. Если хочешь добавить фичу — сначала пойми где она встанет в эту карту.

⚠️ Проект переименован из HealthVault в **Botkin** (12.05.2026). Docker container/compose-проект и БД на сервере по историческим причинам всё ещё называются `healthvault_*` — это не баг, а сознательно не переименованный legacy (см. `docker-compose.prod.yml`).

---

## Высокоуровневая картина

```
┌──────────────────┐                    ┌──────────────────┐
│  Telegram User   │ ←→ webhook ──────→ │   aiogram bot     │
│  (open registr.  │   /telegram/webhook│  (handlers/*)     │
│   is_active гейт)│                    └────────┬──────────┘
└──────────────────┘                             │
                                                  ↓
┌──────────────────┐   HTTP (localhost)  ┌────────────────────┐    ┌──────────────────┐
│  BotkinClaw       │ ←──────────────── →│   FastAPI          │ ←→ │   PostgreSQL 16   │
│  (in-process AI-  │  /api/agent/*       │  (webhook/*.py —   │    │  (RLS + Alembic) │
│  агент, core/      │  JWT+RLS           │  ~10 роутеров)     │    └────────┬─────────┘
│  agent_chat.py)    │                    └─────────┬──────────┘             │
└──────────┬─────────┘                              │                       │
           │ Anthropic Messages API         ┌────────▼─────────┐             │
           ↓                                │   Mini App       │             │
   claude-sonnet-5 (+ fallback)             │  (webapp/*.html) │             │
                                            │  в Telegram       │             │
┌──────────────────┐                       │  WebView, 4 таба  │             │
│  Claude Desktop /  │  MCP (stdio) ───────→│                   │             │
│  Code пользователя │  botkin_pat_mcp.py   └──────────────────┘             │
│  (личный AI-агент) │  PAT → JWT exchange                                    │
└──────────────────┘                                                        │
                                            ┌──────────────────┐             │
                                            │  External APIs   │ ────────────┘
                                            │  Garmin, Withings,│
                                            │  LibreLinkUp(CGM),│
                                            │  Whoop, OpenAI,   │
                                            │  Gemini, Anthropic│
                                            └──────────────────┘
```

Один процесс (Docker container `healthvault_bot`, образ `ghcr.io/botkin-health/botkin-bot`) держит и aiogram (Telegram обновления через **webhook**, не polling — см. ниже), и FastAPI на порту 8081 — единая точка входа и для webhook'ов внешних источников (Apple/Android Health, Withings), и для API мини-аппа, и для агентских tools, и для публичного дашборда/отчётов.

---

## Точка входа

**`telegram-bot/bot.py`** (~500 строк) — единственный entrypoint.

Что делает:
1. Загружает `.env` (через `dotenv`).
2. Создаёт `Bot` и `Dispatcher` (aiogram 3).
3. Регистрирует middleware (`auth`, `idempotency`, `media_group`, `typing_indicator`).
4. Регистрирует ~15 handler-роутеров (`commands`, `text`, `photo`, `voice`, `callbacks`, `doc_upload`, `connect_cgm`, `connect_claude`, `verified_products`, `feedback`, `food_audit`, `persona_cmd`, `plan_close`, `apple_health_connect`, `setup`, `sync_cmd`).
5. Регистрирует Telegram **webhook** (`POST /telegram/webhook`, обрабатывается через `webhook/telegram_router.py` на том же FastAPI app) — не polling. Polling остаётся только на дев-стенде через `BOTKIN_FORCE_POLLING=1` (нет публичного TLS-эндпойнта).
6. Запускает единый FastAPI app из `webhook/apple_health.py` (`start_webhook_server()`).
7. Устанавливает Bot Commands в Telegram menu и Menu Button (WebApp кнопка «Дневник»).

**Команды бота, видимые пользователю:** `/start /day /week /vitamins /doc /share /profile /connect_mcp /agent_reset /feedback /help`. Другие (`/setup`, `/cache_stats`, `/burn`, `/connect_cgm`, `/status`, `/targets`, `/block /unblock /users` (админ), `/whoop`, `/meal_reminders`, `/report`, `/doctor_report`, `/health_token`) есть в коде, но не в menu — служебные/нишевые.

⚠️ **Регистрация открытая** (не whitelist из 3 семейных ID, как было раньше) — доступ регулирует `users.is_active` через `AuthMiddleware`, роль — `users.cohort` (`owner`/`family`/`early_user`/`external`).

---

## Слои и их роли

### `telegram-bot/handlers/` — UI-слой

| Файл | Назначение |
|---|---|
| `commands.py` | Команды `/start /day /week /vitamins /help /setup /activity /burn /targets /status /share /report /doctor_report /health_token /whoop /meal_reminders` + админские `/block /unblock /users /cache_*` |
| `text.py` | Любое текстовое сообщение → LLM router → диспатч в food/supplement/weight handler ИЛИ в BotkinClaw (`core.agent_chat.ask_agent`), если сообщение не распознано как трекинг. Также распознаёт префикс «план:»/«планирую» (`core/food/plan_prefix.py`) для режима план→факт (#407) |
| `photo.py` | Фото: меню → vision OCR / еда → vision GPT-4o / весы → OCR weight / follow-up вопросы → BotkinClaw |
| `voice.py` | Голос → AssemblyAI транскрипт → text.py ИЛИ BotkinClaw |
| `callbacks.py` | Inline-кнопки подтверждения «Сохранить»/«Отмена» |
| `doc_upload.py` | `/doc` — загрузка своего анализа/заключения (фото/PDF) → LLM-извлечение → превью → запись в `blood_tests` + KB-архив |
| `connect_cgm.py` | `/connect_cgm` — подключение LibreLinkUp (CGM-глюкоза, #96/#381) |
| `connect_claude.py` | `/connect_mcp`, `/my_connections` — выпуск/отзыв PAT-токенов для MCP-коннектора Claude Desktop (#228) |
| `verified_products.py` | Кнопка «💾 Запомнить продукт» → запись в `verified_products` |
| `feedback.py` | `/feedback` — инбокс обратной связи (#188) |
| `food_audit.py` | `/food_audit` — аудит пищевого пайплайна |
| `persona_cmd.py` | `/persona` — управление персоной BotkinClaw |
| `plan_close.py` | Кнопки вечернего вопроса «план доеден целиком?» (режим план→факт, #407) |
| `apple_health_connect.py` | Онбординг подключения Apple Health / Health Auto Export |
| `setup.py` | `/setup` — профиль (рост/возраст/цель/BMR) |
| `sync_cmd.py` | `/sync`-подобные операции внутри бота (не путать с mac-скиллом `/sync`) |

⚠️ **`photo.py` — самый большой и сложный файл в handlers**, историческая точка минимального покрытия тестами (см. `2026-04-21-architectural-review.md`; с тех пор частично закрыт тестами plan-prefix/caption-merge).

### `telegram-bot/middlewares/` — кросс-cutting

| Файл | Что делает |
|---|---|
| `auth.py` | Гейт по `users.is_active` (open registration, не whitelist). Прокидывает `user_id`/`cohort` в handler data. |
| `idempotency.py` | Дедупликация апдейтов Telegram (защита от ретраев Telegram API). |
| `media_group.py` | Сборка нескольких фото в одну группу до прихода последнего. |
| `typing_indicator.py` | Индикатор «печатает…» в Telegram, пока handler (в т.ч. BotkinClaw) работает. |

### `telegram-bot/webhook/` — FastAPI слой

Единый FastAPI `app` собирается в `apple_health.py`, остальные роутеры подключаются через `app.include_router(...)`:

| Файл | Назначение |
|---|---|
| `apple_health.py` | Главный FastAPI app + сборка всех роутеров. POST `/apple_health_v2` — Health Auto Export (iOS, ежедневно автоматически). POST `/apple_health` (v1) — legacy Shortcuts. GET/POST `/api/settings`. Раздача статики мини-аппа `/webapp/*` (auto-versioning). |
| `telegram_router.py` | `POST /telegram/webhook` — принимает апдейты Telegram (webhook-режим, не polling) и диспатчит в aiogram `Dispatcher`. |
| `android_health.py` | `POST /android_health_v1` — приём сырых записей от Android Health Connect (агрегация по дням делается на сервере, в отличие от HAE). |
| `agent_tools_api.py` | **~39 endpoints** `/api/agent/*` для BotkinClaw и MCP-коннектора — JWT+RLS изоляция по `user_id`. Категории: nutrition (`log_meal_text`, `edit_meal`, `adjust_meal_items`, `recent_meals`, `meal_context`), supplements, BP, body composition (`log_body_composition`), CGM/глюкоза (`recent_glucose`, `glucose_stats`), биомаркеры/KB (`recent_biomarkers`, `phenoage`, `kb_value`), профиль/настройки, отчёты (`doctor_report`, `render_report`, `render_chart`), фидбек, и единственный public endpoint `POST /exchange_pat_for_jwt` (PAT → JWT для MCP). Подробности — `04_workflows.md` и ADR-0006. |
| `jwt_auth.py` | `get_agent_user()` / `require_agent_scope("rw"/"ro")` — валидация агентского JWT (per-user `jwt_secret`), выставление RLS-переменной `app.user_id` (`SET LOCAL`). |
| `rate_limit.py` | `SlidingWindowRateLimiter` (in-process, per-IP) — используется на `/exchange_pat_for_jwt` (10 req/мин). |
| `nutrition_api.py` | Endpoints мини-аппа для дневника еды: GET `/api/day`, POST/PATCH/DELETE `/api/meal/item`, PATCH/DELETE `/api/meal`, GET `/api/favorites`. |
| `supplements_api.py` | Endpoints для daily-log таба добавок: GET `/api/supplements/day`, POST/DELETE `/api/supplements/take`. |
| `nutrition_goals.py` | `compute_goals()` — БЖУ-цели на день из настроек+Garmin. |
| `nutrition_slots.py` | Маппинг `meal_time` → slot (breakfast/lunch/snack/dinner). |
| `profile_api.py` | GET/POST `/api/profile/bmr` (auto/manual BMR), PATCH `/api/profile/timezone`. |
| `dashboard.py` | `GET /mc/{token}` — публичный персональный дашборд (botkin.health/mc/…), встраивается в мини-апп (таб «Здоровье») и шарится через `/share`. |
| `report.py` | Публичные HTML-отчёты `GET /r/{token}` (таблица `health_reports`). |
| `doctor_report_api.py` | Endpoint для генерации PDF-отчёта врачу (ISO 27269 IPS, ADR-0008) — тонкая обёртка над `services/doctor_report.py`. |
| `whoop_oauth.py` | OAuth 2.0 подключение носимого Whoop (`/whoop/connect`, `/whoop/callback`); токены в `data/cache/whoop_tokens.json`. |
| `feedback_api.py` | API мини-аппа для отправки фидбека (кнопка в webapp). |
| `admin.py` | Админ-эндпоинты (`/block`, `/users` подложка и т.п.). |
| `tg_auth.py` | `get_tg_user()` — валидация Telegram `initData` (HMAC по bot token). Используется как `Depends()` во всех мини-апп endpoint'ах. |

### `telegram-bot/webapp/` — Mini App (frontend)

**4 таба** (было 3): Дневник / Добавки / **Здоровье** (новый, добавлен вместе с публичным дашбордом) / Настройки.

| Файл | Что |
|---|---|
| `index.html` | Главный HTML, tab-bar на 4 таба. Inline `<style>`/`<script>` для Settings + Supplements log. Дневник — через `day.js`, Здоровье — через `dashboard.js`. |
| `day.js` | Логика дневника: загрузка `/api/day`, рендер карточек (включая 📋-маркер для план→факт), переключение даты, прогресс-бары. Экспортирует `window.__nutri.state` для cross-tab синхронизации. |
| `dashboard.js` | Таб «Здоровье» — встраивает персональный дашборд (`GET /mc/{token}`) в iframe, лениво (строится при первом открытии таба, дальше переиспользуется без перезагрузки). |
| `api.js` | Тонкий типизированный fetch-wrapper (`window.API.getDay()`, `addItem()`, и т.п.). Кладёт `Authorization: tma <initData>` автоматически. |
| `day.css` / `settings.css` | Стили вкладок. |
| `settings.js` | Логика таба Настройки + переключение табов (`switchTab()`). |

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
│   ├── weekly_nutrition.py  ← weekly digest для команды /week
│   ├── kb_schema.py         ← единый реестр алиасов биомаркеров + конверсия единиц (read-time канонизация)
│   ├── phenoage.py          ← расчёт биологического возраста (PhenoAge) по биомаркерам
│   ├── biomarkers.py        ← агрегация биомаркеров из Postgres blood_tests
│   ├── doc_extractor.py     ← LLM-извлечение (Anthropic, claude-haiku) данных из /doc-загрузки (лаборатория/значения/аллергии)
│   ├── doc_to_blood_test.py ← маппинг извлечённых значений → строка blood_tests (идемпотентность по хэшу файла)
│   ├── glucose_stats.py     ← чистые функции: Time-in-Range, avg/min/max для CGM (без обращения к БД)
│   ├── glucose_runtime.py   ← кэшированный LibreLinkUp-клиент для on-demand refresh глюкозы из agent tools
│   ├── onboarding_lists.py  ← единый реестр ключей + сплиттер свободного текста для аллергий/хроник
│   └── kb_writer.py         ← запись/мердж knowledge_base.json
│
├── reminders/               ← напоминания вне aiogram (диспетчер scripts/server/send_reminders.py)
│   ├── meal_reminders.py    ← напоминания о приёме пищи по расписанию
│   └── plan_close.py        ← текст/логика вечернего вопроса «план доеден?» (режим план→факт, #407)
│
├── reports/
│   └── biomarker_dynamics.py ← динамика биомаркеров для отчётов/дашборда
│
└── infra/
    ├── api_key_loader.py    ← Google Vision key из ~/.google_vision_api_key
    ├── storage.py           ← обёртка над Path операциями
    ├── voice_service.py     ← AssemblyAI транскрипция
    ├── tz.py                ← MSK/пользовательские таймзоны (`get_user_tz`)
    └── secrets.py           ← encrypt_secret/decrypt_secret (шифрование, напр. cgm_followers.password_enc)
```

**`core/agent_chat.py`** (~3080 строк) — намеренно **не** внутри подпапки: это BotkinClaw, живёт на уровень выше `core/health/*` и т.п., т.к. это отдельный подсистемный слой (агент), а не парсер данных. См. секцию «BotkinClaw» ниже.

⚠️ **Proxy shims в `core/` (на уровень выше папок).** Файлы `core/llm_router.py`, `core/menu_parser.py`, `core/chatgpt_vision.py`, `core/description_parser.py`, `core/ocr_weight.py`, `core/weight_extraction.py`, `core/menu_meal_processor.py`, `core/nutrition.py`, `core/garmin_data.py`, `core/voice_service.py`, `core/weekly_nutrition.py`, `core/supplements.py`, `core/nutrition_targets.py`, `core/caloric_budget.py`, `core/storage.py`, `core/llm_models.py`, `core/api_key_loader.py`, `core/apple_health_parser.py`, `core/product_search.py`, `core/gemini_vision.py` — всё это 3-строчные `from core.<subpkg>.X import *` re-exports из рефакторинга 22.03.2026. **При новом коде импортировать напрямую из `core.food.*`, `core.vision.*`, и т.п.**

### `database/` — слой данных

| Файл | Что |
|---|---|
| `models.py` | SQLAlchemy 2 declarative models. ~27 классов (User, NutritionLog, Weight, SupplementLog, ActivityLog, BloodTest, BodyMeasurement, UserSettings, AgentConversation, PersonalAccessToken, GlucoseReading, CgmConnection, CgmFollower, EcgRecord, HeartRateEvent, FoodInteraction, UserFeedback, VerifiedProduct, HealthReport, FunnelEvent, и др., см. `03_database_schema.md`). |
| `crud.py` | Функции CRUD + агрегации. Принимают `db: Session` явно (никаких контекстных менеджеров внутри). |
| `__init__.py` | `SessionLocal`, `init_db`, реэкспорт CRUD-функций. |
| `alembic/` | Alembic-миграции (`versions/*.py`, короткие slug-имена, не auto-hash). Заменил ручной `ALTER TABLE`-процесс — см. [ADR-0003](../architecture/decisions/0003-alembic-for-db-migrations.md). |

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

## BotkinClaw — in-process AI-агент

**Что это:** «AI-врач», разговорный ассистент внутри основного бота. Живёт **в том же процессе**, что и aiogram/FastAPI — не отдельный контейнер (решение принято 21.05.2026 после спайка NanoClaw, см. [ADR-0001](../architecture/decisions/0001-nanoclaw-ephemeral-not-persistent.md) и [ADR-0002](../architecture/decisions/0002-rejecting-nanoclaw-for-simpler-agent.md)).

**Точка входа:** `core/agent_chat.py:ask_agent(user_id, user_text, progress_cb=None, is_e2e=False) -> str` — синхронная функция (вызывается через `loop.run_in_executor`). Вызывается из `handlers/text.py`, `handlers/voice.py`, `handlers/photo.py` когда сообщение не распознано как трекинг еды/веса/АД/добавок.

**Модель:** `claude-sonnet-5` (env `BOTKIN_AGENT_MODEL`), fallback на `claude-sonnet-4-6` при 429/503/529 (одна быстрая ретрая на том же модели, потом одна попытка на fallback-модели). `effort="medium"`, `max_tokens=4000`.

**Tool-loop:** стандартный Messages API tool-use цикл (до `MAX_TOOL_ITERATIONS=6`). Схемы ~34 инструментов заданы константой `TOOLS` прямо в `agent_chat.py` (НЕ импортируются из `agent_tools_api.py` — это раздельные структуры, которые надо поддерживать в синхронизации руками при любом изменении эндпоинтов). Каждый вызов инструмента — синхронный HTTP-запрос в `webhook/agent_tools_api.py` (тот же контейнер, `http://localhost:8081/api/agent/*`), авторизованный короткоживущим JWT (см. ниже).

**История диалога:** таблица `agent_conversations` (не путать со state автосохранения — см. anti-pattern ниже). Читается с окном `HISTORY_WINDOW=20` последних сообщений, старые `tool_result` усекаются до 1500 символов для экономии токенов. Запись всей реплики (assistant + tool_result) — одной транзакцией, с проглатыванием ошибок персистентности (см. `CLAUDE.md` — оплаченный ответ LLM никогда не теряется из-за сбоя записи в БД).

**Системный промпт:** `users.agent_system_prompt` (богатый override для семьи, задаётся `onboard_family_user.py`) ИЛИ `build_default_agent_prompt(user)` из `onboarding_data` — это **не гейт доступа**, разговорный агент работает для любого зарегистрированного пользователя (#165). Плюс общий `UNIVERSAL_META_PROMPT` (~500 строк накопленных anti-hallucination правил с датированными прецедентами) + блок медпрофиля (аллергии/хроники/курение) + админ-контекст (только для админов — тулы `list_feedback`/`triage_feedback`).

**JWT-авторизация тулов:** per-user `users.jwt_secret` (HS256), claim'ы `{user_id, container_id, scope, exp, iat}`. `container_id` = `agent_id_for(user)` (обычно `botkinclaw-<telegram_id>`) — защита от подмены между пользователями. Валидация — `webhook/jwt_auth.py::get_agent_user`, выставляет RLS-переменную `SET LOCAL app.user_id`.

⚠️ **Операционное правило (транзакции поперёк сети):** перед каждым вызовом Anthropic API (до 60с) `_end_open_tx(db)` закрывает любую открытую транзакцию — иначе Postgres `idle_in_transaction_session_timeout=15с` рвёт соединение (инцидент #347, 26.07.2026). См. anti-pattern в корневом `CLAUDE.md`.

⚠️ **Не путать с превью подтверждения еды** (`services.state.state_manager`) — это разные механизмы хранения состояния, см. anti-pattern ниже.

Подробности инструментов и endpoint'ов — `04_workflows.md` (workflow «изменить/добавить agent tool») и `telegram-bot/webhook/agent_tools_api.py`.

---

## MCP-коннектор — личный AI-агент пользователя (Claude Desktop/Code)

Второй потребитель того же `agent_tools_api.py` — не BotkinClaw, а **личный Claude пользователя** на его компьютере, согласно vision-схеме в корневом `CLAUDE.md` («гибридная приватность»).

**Поток:** `/connect_mcp` в боте (`handlers/connect_claude.py`) → пользователь выбирает scope (`rw`/`ro`) → выпускается PAT-токен (`pat_<telegram_id>_<hex32>`, таблица `personal_access_tokens`) → пользователь один раз копирует его в конфиг Claude Desktop (MCP-бандл `scripts/mcp/manifest.json`, entry point `scripts/mcp/botkin_pat_mcp.py`).

**`botkin_pat_mcp.py`** — stdio MCP-сервер (`FastMCP("Botkin")`), сам не хранит и не читает локальные файлы пользователя (KB, дневники) — это оставлено файловому коннектору Claude Desktop. Обменивает PAT на короткоживущий JWT через `POST /api/agent/exchange_pat_for_jwt` (единственный публичный, не-JWT endpoint в `agent_tools_api.py`, rate-limit 10 req/мин/IP), кэширует JWT до истечения. Даёт набор read-tools (`get_day_summary`, `get_recent_meals`, `get_recent_biomarkers`, …) + write-tools (`log_meal_text`, `log_bp`) под `rw`-скоупом, плюс generic `botkin_api(method, path, params)` escape-hatch.

**Отзыв:** `/my_connections` в боте показывает список PAT с кнопкой «❌ Отозвать» (soft-delete через `revoked_at`; уже выданные JWT доживают до истечения своего TTL ~5 мин).

Дизайн и отклонённые альтернативы — [ADR-0006](../architecture/decisions/0006-mcp-connector-pat-jwt.md) и `docs/researches/2026-06-28-mcp-connector.md`.

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
2. day.js → API.getDay(today)                 [GET /api/day?date=2026-09-06]
3. nutrition_api.get_day()                    [reads nutrition_log + computes totals_day + activity_today]
4. User taps row, edits weight                [day.js: PATCH /api/meal/item]
5. nutrition_api.update_meal_item_weight()    [in-place JSONB update]
6. day.js → re-renders bars                   [client-side recompute]
```

## Поток данных: «пишет боту вопрос не про еду» (BotkinClaw)

```
1. text.py:handle_message()                       [Telegram → handler]
2. core.llm.router.analyze_message(text)          [классификатор понимает: не food/weight/supplement/BP]
3. loop.run_in_executor(None, ask_agent, ...)      [text.py → core.agent_chat.ask_agent, синхронный вызов]
4. _load_history() читает agent_conversations       [окно HISTORY_WINDOW=20]
5. Anthropic Messages API (claude-sonnet-5)         [tool-use loop, до 6 итераций]
6. Инструмент нужен → _call_tool()                  [HTTP → localhost:8081/api/agent/*, JWT per-user]
7. jwt_auth.get_agent_user()                        [SET LOCAL app.user_id → RLS]
8. Ответ агента → _persist_turns() → agent_conversations
9. text.py отправляет ответ пользователю в Telegram
```

⚠️ **Тот же путь** (шаги 3-9), только вход другой, работает и для MCP-коннектора Claude Desktop пользователя — только вместо `_generate_jwt(user)` внутри процесса, JWT получен обменом PAT через `POST /exchange_pat_for_jwt` заранее, снаружи процесса бота.

## Поток данных: «план: ужин курица 200г» → вечером «съела всё» (план→факт, #407)

```
1. text.py: strip_plan_prefix(text)               [core/food/plan_prefix.py — «план:»/«планирую» строго в начале]
2. Обычный food-флоу, но status='plan', эмодзи 📋  [та же confirm-preview, кнопка «✅ Сохранить план»]
3. Запись уже входит в итог дня (totals не фильтруют по status)
   ...
4. Вечером: scripts/server/send_reminders.py::dispatch_plan_close  [диспетчер вне aiogram, cron]
   → кнопки «Да, всё» / «Что-то осталось»           [handlers/plan_close.py]
5a. «Да, всё» → status='eaten' для всех планов дня  [БД-сессия закрывается ДО сетевого вызова в Telegram]
5b. «Что-то осталось» → текст в чат → BotkinClaw     [tool adjust_meal_items, dry_run сначала]
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
| **Файлы пользовательских данных** | Google Drive `~/FamilyHealth/` (отдельная папка, НЕ внутри проекта) | medical PDFs, knowledge_base.json каждого члена семьи. См. `CLAUDE.md` в корне проекта. |
| **Фото блюд (последние)** | `data/media/` | Сюда падают фото от Telegram, чтобы LLM могла к ним обращаться. |
| **PAT-токены / MCP** | таблица `personal_access_tokens` (не файл) | Долгоживущий доступ MCP-коннектора Claude Desktop, self-service через `/connect_mcp`. |
| **Кеш Garmin/Zepp/Whoop токенов** | `data/cache/tokens.json`, `data/cache/whoop_tokens.json`, `data/cache/withings_tokens.json` | OAuth, часть истекает через 5–7 дней (Zepp). |
| **Бэкапы БД** | `data/backups/healthvault_backup_*.sql` (локально) / `/opt/backups` (сервер, read-only mount в контейнер) | Nightly `pg_dump` через cron сервера, показываются в админ-панели. |
| **Логи бота** | server: `/opt/botkin/logs/bot.log` (volume-mount, контейнер видит как `/app/logs`) | `docker logs healthvault_bot --tail 50` для свежего хвоста. |

---

## Anti-patterns при работе с этим кодом

❌ **Не импортируй `core.llm_router`, `core.menu_parser`, …** — это proxy shims, держатся для обратной совместимости archived скриптов. В новом коде: `from core.llm.router import …`, `from core.vision.menu_parser import …`.

❌ **Не пиши новые поля в `users` таблицу** для пользовательских настроек — цели/бюджет живут в `user_settings`. НО: у `users` теперь много «системных» полей, которых раньше не было (`cohort`, `jwt_secret`, `agent_system_prompt`, `onboarding_data`, `kb_status`, `smoking_status`) — это не то же самое, что «настройки», их добавлять можно, просто не дублируй `user_settings`.

❌ **Не дублируй логику записи приёмов пищи.** Текстовый/голосовой флоу → `helpers/db_save.py`. Мини-апп → `nutrition_api.py:add_meal_item`. BotkinClaw/MCP → `agent_tools_api.py:log_meal_text`/`adjust_meal_items` → `database/crud.py`. Все делают `enrich_items_with_fiber` перед записью — следи чтобы любой новый путь записи делал то же.

❌ **Не делай `SELECT … FROM nutrition_log WHERE date >= …` без `user_id`.** Регистрация открытая, пользователей много и их число растёт — без фильтра суммируются все.

❌ **Не путай `agent_conversations` со state автосохранения еды/АД.** Превью «Сохранить»/«Отмена» до подтверждения живёт в памяти процесса (`services.state.state_manager`), НЕ в БД. `agent_conversations` — только для реплик BotkinClaw. Отсутствие записи в `agent_conversations` ≠ потеря данных еды/веса/АД — проверяй `nutrition_log`/`weights`/`blood_pressure_logs` напрямую.

❌ **Не держи транзакцию Postgres открытой поперёк сетевого вызова** (LLM, внешний API) — `idle_in_transaction_session_timeout=15с` рвёт соединение. См. `_end_open_tx` в `core/agent_chat.py`.

❌ **Не используй `users.id`** в FK — используется `users.telegram_id` (BigInteger). Все FK на пользователя — на `telegram_id`.

❌ **Не читай поле `totals.fat`** — поле называется `totals.fats` (множественное число). Старая дока ошибалась — могло привести к молчаливому 0 в SQL.

✅ **Сначала прочитай `database/crud.py`** прежде чем писать новый запрос — почти всё уже есть.

✅ **Всегда `enrich_items_with_fiber`** перед записью — иначе fiber поле пустое и `/api/day` молча показывает 0г клетчатки.

✅ **Добавляя agent tool** — обнови СРАЗУ ДВА места в `core/agent_chat.py`: константу `TOOLS` (схема для Claude) и dispatch в `_call_tool` (какой HTTP-роут дёргать). Между собой они не связаны схема-генерацией, компилятор не поймает рассинхрон.

✅ **Канонические команды для проверки бота:**
```bash
# логи бота
ssh root@116.203.213.137 "docker logs healthvault_bot --tail 50"

# статус контейнера
ssh root@116.203.213.137 "docker ps | grep healthvault"

# деплой — ТОЛЬКО через GitHub Actions (не docker restart вручную, образ собирается в CI)
gh workflow run deploy-prod.yml -f branch=main

# тесты локально (dummy-ключи из tests/conftest.py, DATABASE_URL не нужна — in-memory SQLite)
PYTHONPATH=. pytest tests/ -v --ignore=tests/integration --ignore=tests/test_nutrition_parsing.py
```

Подробнее про деплой (GHCR-образ, `docker-compose.prod.yml` pull-only) — `04_workflows.md` §1 и `docs/DEPLOYMENT.md`.

---

## Неочевидные архитектурные решения и почему

**`items` хранится как JSONB, не как нормальная таблица `nutrition_items`.** Решение: схема items сильно вариативна (макросы, fiber, источник, веса с компонентами), редко join'им поэлементно. Цена — невозможность индексировать имена через b-tree (есть GIN, но не использовали).

**Один Docker container держит aiogram + FastAPI + BotkinClaw.** Решение: изначально low traffic, деплой проще; при добавлении агента (21.05.2026) решили НЕ выносить его в отдельный контейнер (NanoClaw), а звать напрямую в процессе — тот же аргумент простоты, теперь уже осознанный, а не только исторический (ADR-0001/0002). Цена — долгий LLM-вызов из agent tool делит event loop с обычными handler'ами; см. правило про `_end_open_tx` и транзакции поперёк сети.

**Tool-схемы агента заданы прямо в `agent_chat.py`, не сгенерированы из FastAPI-роутов `agent_tools_api.py`.** Решение: проще написать руками 34 JSON-схемы, чем городить codegen. Цена — два места (`TOOLS` + `_call_tool`) держать в синхронизации вручную при любом изменении эндпоинта.

**MCP-коннектор через долгоживущий PAT + короткоживущий JWT, а не прямой OAuth.** Решение: пользователь не хочет заводить отдельный OAuth-провайдер ради одного personal-инструмента; PAT — простой self-service токен, JWT обеспечивает то же самое короткое окно риска, что и у BotkinClaw. Цена — PAT нужно хранить у себя (keychain Claude Desktop), у него нет автоматической ротации, только ручной отзыв.

**Mini App auth через Telegram `initData`.** Решение: пользователю не надо отдельно логиниться, при открытии в Telegram WebView токен уже есть. Цена — нельзя протестировать API из браузера без mock'а initData.

**Fiber backfill в 4 слоя.** LLM prompt → write-time enrichment → read-time fallback → migration script. Решение: исторические данные уже разнородные, нужна defense in depth. См. `AI_CHANGELOG.md` 2026-04-20.

---

[← Документация Botkin — Index](../INDEX.md)
