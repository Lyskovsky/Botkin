# 03 · Database Schema

> **Last verified:** 2026-09-06 (после добавления BotkinClaw, MCP-коннектора, CGM и режима план→факт — новые таблицы `agent_conversations`, `personal_access_tokens`, `glucose_readings`, `cgm_connections`/`cgm_followers`, `nutrition_log.status`, и переход на Alembic)
> **DB:** PostgreSQL 16, Docker container `healthvault_postgres` на сервере (имя контейнера/compose-проекта — legacy `healthvault`, само приложение и образ — `botkin`), БД `healthvault`
> **ORM:** SQLAlchemy 2 declarative (Mapped style)
> **Migrations:** Alembic (`database/alembic/`, см. §«Migration / схема change процесс» ниже — заменяет старый ручной `ALTER TABLE`-процесс, см. [ADR-0003](../architecture/decisions/0003-alembic-for-db-migrations.md))
> **Source of truth:** `database/models.py`. Эта дока — человекочитаемая проекция оттуда. При расхождениях — верить коду.

---

## TL;DR — все таблицы за 1 экран

| Таблица | Что | Ключ | Главные поля |
|---|---|---|---|
| `users` | Пользователи бота (open registration, `is_active`-гейт) | `telegram_id` (BigInt, PK!) | `cohort`, `bmr`, `jwt_secret`, `agent_system_prompt`, `onboarding_data` JSONB |
| `user_settings` | Per-user настройки и список добавок | `user_id` (PK = telegram_id) | `bmr_override`, `supplements` (JSON), `meal_reminder_times` |
| `nutrition_log` | Приёмы пищи | `id` autoinc | `items` JSONB, `totals` JSONB, `meal_time`, `meal_name`, **`status`** (`eaten`/`plan`, #407) |
| `supplements_log` | Принятые добавки | `id` autoinc | `supplement_name`, `time`, `date` |
| `weights` | Взвешивания (Zepp/Withings/HAE + ручные) | `id` autoinc | `weight`, `body_fat`, `muscle_mass`, `bmi`, `heart_rate`, `bmr_kcal` (Withings-only поля) |
| `activity_log` | Активность за день (Garmin/Apple/Android Health) | `id` autoinc | `steps`, `active_calories`, `bmr_calories`, `hrv` |
| `blood_tests` | Анализы крови | `id` autoinc | `values` JSONB, `test_type`, `status` |
| `body_measurements` | Замеры тела (талия, шея, хват кисти) | `id` autoinc | `waist_cm`, `neck_cm`, `grip_right_kg`, `grip_left_kg` |
| `verified_products` | Справочник проверенных продуктов (#255) | `id` autoinc | `name_norm`, `*_per_100g`, `portion_g`, `barcode`; `user_id NULL` = общая запись |
| **`agent_conversations`** | История диалога BotkinClaw (in-process AI-агент) | `id` autoinc (BigInt) | `role` (`user`/`assistant`/`tool_use`/`tool_result`), `content` JSONB, `source` |
| **`personal_access_tokens`** | PAT для MCP-коннектора Claude Desktop (#228) | `id` autoinc (BigInt) | `token` (`pat_<telegram_id>_<hex32>`), `scope` (`ro`/`rw`), `revoked_at` |
| **`glucose_readings`** | Точки CGM-глюкозы (LibreLinkUp, #96) | `id` autoinc (BigInt) | `ts`, `value` (mmol/L), `trend`, `source` |
| **`cgm_connections`** | Маппинг LibreLinkUp `patient_id` → `telegram_id` | `id` autoinc (BigInt) | `patient_id` (unique), `telegram_id` |
| **`cgm_followers`** | Креды follower-аккаунта LibreLinkUp (per-region, #381) | `id` autoinc (BigInt) | `region`, `email`, `password_enc`, `owner_user_id` |
| **`ecg_records`** | Метаданные ЭКГ с Apple Watch | `id` autoinc (BigInt) | `classification`, `average_heart_rate`, `duration_sec` |
| **`heart_rate_events`** | Уведомления Apple Watch о пульсе вне нормы | `id` autoinc (BigInt) | `event_type`, `min_bpm`/`max_bpm`/`avg_bpm` |
| **`food_interactions`** | Аудит-след пищевого пайплайна (#193) | `id` autoinc (BigInt) | `raw_text`, `recognized` JSONB, `bot_reply`, `status` |
| **`user_feedback`** | Инбокс `/feedback` + агент (#188) | `id` autoinc (BigInt) | `kind`, `text`, `source`, `status` |
| **`health_reports`** | HTML-отчёты по публичному токену `GET /r/{token}` | `id` autoinc | `token` (unique), `html` |
| **`funnel_events`** | Onboarding/активация продуктовая воронка | `id` autoinc (BigInt) | `event`, `track`, `meta` JSONB |
| **`llm_usage_log`** | Учёт токенов/стоимости LLM-вызовов | `id` autoinc (BigInt) | `purpose`, `model`, `cost_usd` |
| **`audit_log`** | Аудит доступа (DB-триггер `audit_admin_access`) | `id` autoinc (BigInt) | `db_user`, `query_type`, `table_name` |

Кроме того в БД есть **orphan-таблицы**, управляемые ORM-моделями (зеркалят прод-схему для тестов/alembic-check), но **не читаемые бизнес-логикой бота**: `blood_pressure_logs` (пишут только raw-SQL пути `webhook/apple_health.py` и `webhook/agent_tools_api.py::log_bp`), `daily_summaries` (пуста на проде), `sleep_records` (пуста на проде), `workouts` (пишут raw-SQL пути `apple_health.py`/`android_health.py`/`agent_tools_api.py`). Из нового кода в эти таблицы — только через существующие raw-SQL функции, не через ORM напрямую.

---

## Connection

**На сервере (production):**
```bash
ssh root@116.203.213.137 \
  "docker exec healthvault_postgres psql -U healthvault -d healthvault"
```

**Из Python кода (через SQLAlchemy):**
```python
from database import SessionLocal
db = SessionLocal()
try:
    user = db.query(User).filter(User.telegram_id == 895655).first()
finally:
    db.close()
```

⚠️ Никогда не open-ть `SessionLocal()` без `try/finally db.close()`. Pool маленький.

⚠️ **`idle_in_transaction_session_timeout` = 15с** (`database/__init__.py`). Держать открытую транзакцию поперёк сетевого вызова (LLM, внешний API) нельзя — Postgres обрывает соединение. См. `_end_open_tx` в `core/agent_chat.py` и anti-pattern в корневом `CLAUDE.md`.

---

## Multi-user и RLS (важное отличие от ранних версий этой доки)

**Регистрация открытая** — любой Telegram-пользователь может написать боту, доступ регулируется `users.is_active` (не whitelist из 3 ID, как было раньше). У каждого пользователя — `cohort` (`owner`/`family`/`early_user`/`external`, CHECK-констрейнт `users_cohort_check`).

**Row-Level Security** включена на проде для основных пользовательских таблиц (`activity_log`, `blood_pressure_logs`, `nutrition_log`, `supplements_log`, `user_settings`, `weights`, `agent_conversations` — см. `database/alembic/versions/711fd5e3f1e8_baseline_schema.py`): роль `hv_app` видит только строки, где `user_id = current_setting('app.user_id')::bigint`. Сессионная переменная `app.user_id` выставляется вызовом `SET LOCAL app.user_id = :uid` (`database/crud.py::set_user_session_var`) — это делает `webhook/jwt_auth.py::get_agent_user` на каждый агентский HTTP-запрос. Обычный код бота (handlers/*) **не полагается на RLS** — там по-прежнему обязателен явный `WHERE user_id = X` в каждом запросе (RLS — вторая линия обороны для агентского пути, не замена явного фильтра).

⚠️ **Любой запрос к данным — с явным `WHERE user_id = X`.** Пользователей — не 3, их число растёт (open registration); без фильтра суммируются все.

---

## 1. `users` — пользователи

**Главное:** primary key — это `telegram_id` (BigInteger), **НЕ** synthetic `id`. Все foreign keys из других таблиц ссылаются на `users.telegram_id`.

```python
class User(Base):
    telegram_id: BigInteger      # PK
    username: str?
    first_name: str?
    last_name: str?
    email: str?
    phone: str?
    is_active: bool = True
    role: str = "user"
    registered_at: timestamp
    last_active: timestamp?
    timezone: str = "Europe/Moscow"

    # Apple Health webhook auth
    health_token: str?           # Bearer token для Apple Health webhook (Health Auto Export, ранее iPhone Shortcut). Используется в /apple_health и /apple_health_v2

    # Garmin (без шифрования сейчас — в проде нужно)
    garmin_email: str?
    garmin_password: str?

    # Manual targets для пользователей без Garmin
    bmr: float?                  # 1750 для Александра
    avg_active_calories: float?
    target_weight_kg: float?
```

⚠️ **Ловушка устаревшей доки:** старые версии этого файла указывали поля `target_calories / target_protein / target_fat / target_carbs` в users. **Их там нет.** Цели по БЖУ вычисляются динамически в `core/health/nutrition_targets.py` из `bmr` (или `user_settings.bmr_override`) + средняя активность из `activity_log`.

---

## 2. `user_settings` — настройки и список добавок

```python
class UserSettings(Base):
    user_id: BigInteger          # PK + FK → users.telegram_id
    show_calorie_budget_bar: bool = True   # шкала калорий в /day
    bmr_override: int?           # если задан, использовать вместо Garmin/users.bmr
    target_weight_kg: float?     # цель веса для мини-аппа
    target_weight_date: date?    # дедлайн цели
    supplement_reminders_enabled: bool = False
    supplement_reminder_time: time = "08:00:00"
    supplements: list = []       # JSON: [{"name": "Витамин D3", "slot": "morning_with"}, ...]
    created_at, updated_at: timestamp
```

**Поле `supplements`** — JSON-массив объектов:
```json
[
  {"name": "Псиллиум", "slot": "morning_before"},
  {"name": "Витамин D3", "slot": "morning_with"},
  {"name": "Магний", "slot": "evening"}
]
```
Slots: `morning_before` / `morning_with` / `evening`. Это **конфиг** — что планируется принимать. Факт приёма пишется в `supplements_log`.

**API мини-аппа:** GET/POST `/api/settings` (см. `webhook/apple_health.py:224-315`).

---

## 3. `nutrition_log` — приёмы пищи (главная таблица)

```python
class NutritionLog(Base):
    id: int                              # autoinc PK
    user_id: BigInteger                  # FK → users.telegram_id
    date: date                           # дата приёма (NOT NULL)
    meal_time: time?                     # время (HH:MM)
    meal_name: str?                      # "Завтрак", "Сочник с творогом", свободный текст
    items: JSONB                         # список продуктов (см. ниже)
    totals: JSONB                        # суммарные КБЖУ
    photo_paths: text[]?                 # пути к фото если из фото-флоу
    status: str = "eaten"                # 'eaten' (факт) | 'plan' (внесено авансом, #407)
    created_at: timestamp
```

**Indexes / Constraints:**
- `idx_nutrition_user_date` on `(user_id, date)` — главный индекс для всех аналитических запросов.
- `idx_nutrition_user_date_status` on `(user_id, date, status)` — для выборки открытых планов дня.
- `uq_nutrition_user_date_meal` on `(user_id, date, meal_time, meal_name)` — unique. ⚠️ **Этот constraint практически бесполезен** так как `meal_name` свободный текст; есть 30 дублей за 100 дней (см. `2026-04-21-architectural-review.md` пункт #3).

### Поле `status` — режим план→факт (#407, 06.09.2026)

Пользователь может внести еду **авансом** («План: 3 яйца, творог 200г») до того как съел — распознаётся `core/food/plan_prefix.py::strip_plan_prefix()` (regex на «план:», «планирую (съесть/поесть/на день)», «на день:» строго в начале строки), вызывается из `handlers/text.py` и `handlers/photo.py` **до** извлечения даты/LLM-роутера. Это не отдельный флоу — тот же confirm-preview, только с `status='plan'`, emoji `📋` (вместо `🍽️`) и кнопкой «✅ Сохранить план».

⚠️ **План УЖЕ входит в итог дня.** `get_nutrition_totals_by_date` не фильтрует по `status` — план считается съеденным сразу. Визуально помечается 📋 везде, где показывается (`/day`, мини-апп), но в SQL-суммах никак не отделён — если нужен именно факт, фильтровать `WHERE status = 'eaten'` явно.

**Закрытие плана (план → факт):**
- **Агент-инструмент `adjust_meal_items`** (`webhook/agent_tools_api.py` → `database/crud.py::adjust_meal_items`) — BotkinClaw правит вес/состав по диалогу с пользователем вечером, `dry_run=True` по умолчанию (сначала превью «было → станет», потом подтверждение). `close_plan=True` переключает `status` на `'eaten'`; можно оставить остаток отдельным новым `status='plan'` через `leftover_to_slot`.
- **Вечернее напоминание** (`scripts/server/send_reminders.py::dispatch_plan_close`, диспетчер вне aiogram) шлёт вопрос «план на сегодня доеден целиком?» всем юзерам с открытыми планами за вчера/сегодня/завтра (по UTC-окну). Кнопки обрабатывает `telegram-bot/handlers/plan_close.py`: «Да, всё» — массово `status='eaten'` для всех планов даты (сессия БД закрывается **до** сетевого вызова в Telegram — правило про транзакции поперёк await, инцидент #347); «Что-то осталось» — просит текст, дальше обычный confirm-flow / `adjust_meal_items`.
- Прецедент безопасности (06.09.2026, коммит `45ff925`): `AdjustChange.new_weight` теперь валидируется (`math.isfinite(v) and v >= 0`) — иначе NaN/отрицательный вес мог тихо испортить запись. И `dispatch_plan_close` коммитит **после каждого пользователя**, а не одним махом в конце — иначе исключение на одном юзере откатывало дедуп-ключ уже отправленных более ранним юзерам (дубли вечернего вопроса).

### Структура `totals` JSONB

```json
{"calories": 504, "protein": 59, "fats": 22, "carbs": 6, "fiber": 4.0, "drinks": 0}
```

⚠️ **Поле называется `fats` (множ. число), не `fat`!** Старые версии доки ошибочно писали `fat`. SQL `(totals->>'fat')::numeric` всегда вернёт NULL.

### Структура `items` JSONB

⚠️ **В проде живут 3 разные схемы одновременно** (см. ревью пункт #1). В новом коде писать в схему `(c)`:

| Схема | Где появилась | Доля 100 дней | Поля |
|---|---|---|---|
| **(a) Legacy** | `core/food/nutrition.py:505` | 5% (69 items) | `{name, weight, quantity, calories, protein, fats, carbs, ...}` |
| **(b) Telegram-bot** ⭐ | `helpers/db_save.py:60-68` | 90% (1166 items) | `{food, amount, unit, calories, protein, fats, carbs, fiber}` |
| **(c) Mini-app** | `nutrition_api.py:add_meal_item` | <1% | `{product, weight_g, calories, protein, fats, carbs, fiber}` |

**Канонический пример item (схема b — Telegram-бот):**
```json
{
  "food": "Сочник с творогом",
  "amount": 160,
  "unit": "г",
  "calories": 552,
  "protein": 18,
  "fats": 23,
  "carbs": 68,
  "fiber": 1.6
}
```

**Псиллиум и другие БАДы** идут особняком, тоже схемы `(a)`:
```json
{"name": "Псиллиум (БАД)", "weight_g": 5, "calories": 18, "protein": 0, "fats": 0, "carbs": 5, "fiber": 4.0}
```

### Чтение items безопасным способом

```python
# core/food/fiber_table.py:_item_name() — обработать все 3 схемы
def _item_name(it):
    return it.get("product") or it.get("name") or it.get("food") or ""

def _item_weight(it):
    w = it.get("weight_g") or it.get("amount") or it.get("weight")
    return float(w) if w is not None else 0.0
```

Если ты пишешь новый reader — используй эту утилиту, не изобретай свой fallback.

### Канонический SQL для дневных сумм

```sql
-- Сегодняшний день, конкретный пользователь
SELECT
  date,
  ROUND(SUM((totals->>'calories')::numeric), 0)  AS kcal,
  ROUND(SUM((totals->>'protein')::numeric), 1)   AS protein,
  ROUND(SUM((totals->>'fats')::numeric),    1)   AS fats,
  ROUND(SUM((totals->>'carbs')::numeric),   1)   AS carbs,
  ROUND(SUM(COALESCE((totals->>'fiber')::numeric, 0)), 1) AS fiber
FROM nutrition_log
WHERE user_id = 895655
  AND date >= '2026-01-01'
GROUP BY date
ORDER BY date DESC;
```

Для итогов через items (с учётом read-time fiber enrichment) — лучше использовать `nutrition_api.py` либо реплицировать логику оттуда.

---

## 4. `supplements_log` — факт приёма добавок

```python
class SupplementLog(Base):
    id: int                              # autoinc PK
    user_id: BigInteger                  # FK → users.telegram_id
    date: date                           # NOT NULL
    time: time?                          # время приёма (HH:MM)
    supplement_name: str(255)            # "Витамин D3", "Псиллиум", и т.п.
    dosage: str(100)?                    # "5000 МЕ", "5г" — обычно null, бот не уточняет
    created_at: timestamp
```

**Indexes:**
- `idx_supplements_user_date` on `(user_id, date)`

⚠️ **Поле `supplement_name`, не `name`!** Старые доки путали.

### Связь с `user_settings.supplements`

`user_settings.supplements` — это *план* (список того что принимаешь регулярно).
`supplements_log` — *факт* (что реально принял в конкретный день).

Mini-app экран Добавок берёт *план* и проверяет какие из них уже залогированы сегодня (см. `supplements_api.py:get_supplements_day`).

⚠️ **Сравнение имён через ILIKE без нормализации.** «Витамин D3» (латинская D) и «Витамин Д3» (кириллическая Д) — разные строки. Бот может писать одно, мини-апп другое — задвоится. Нужна нормализация (см. ревью пункт #6).

---

## 5. `weights` — взвешивания

```python
class Weight(Base):
    id: int                              # autoinc PK
    user_id: BigInteger                  # FK → users.telegram_id
    measured_at: timestamp(tz)           # дата+время замера (NOT NULL)
    weight: float                        # кг (NOT NULL)
    body_fat: float?                     # % жира
    muscle_mass: float?                  # масса мышц, кг
    water: float?                        # % воды
    bmi: float?
    visceral_fat: int?                   # 1-59 шкала Zepp
    bone_mass: float?                    # масса костей, кг
    source: str(50)?                     # 'apple_health' / 'zepp' / 'manual' / 'screenshot_ocr'
```

**Indexes:**
- `idx_weights_user_date` on `(user_id, measured_at)`
- `uq_weight_user_datetime` on `(user_id, measured_at)` — unique (защита от дублей при импорте)

⚠️ **Поля называются `body_fat` / `muscle_mass` / `water`** (без суффикса `_percent` или `_kg`). Старые доки путали.

---

## 6. `activity_log` — дневная активность (Garmin)

```python
class ActivityLog(Base):
    id: int                              # autoinc PK
    user_id: BigInteger                  # FK → users.telegram_id
    date: date                           # NOT NULL
    steps: int?
    active_calories: float?              # ккал на активность
    total_calories: float?               # ккал всего за день (active + bmr)
    bmr_calories: float?                 # базовый метаболизм за день
    distance_km: float?
    sleep_hours: float?
    heart_rate_avg: int?
    hrv: int?                            # ms
    stress_level: int?                   # 0-100
    source: str(50) = "apple_health"     # 'garmin' / 'apple_health'
    raw_data: JSON?                      # полный payload для анализа потом
    synced_at: timestamp(tz)
```

**Indexes:**
- `idx_activity_user_date` on `(user_id, date)`
- `uq_activity_user_date` on `(user_id, date)` — один ряд на день, новые синки апдейтят.

⚠️ **`raw_data` — нестандартизированный JSON.** Apple Health webhook складывает туда поля давления и gait. При запросах: `raw_data->'blood_pressure'->>'systolic'`.

---

## 7. `blood_tests` — анализы крови

```python
class BloodTest(Base):
    id: int                              # autoinc PK
    user_id: BigInteger                  # FK → users.telegram_id
    test_date: date                      # NOT NULL
    test_type: str(100)?                 # "Биохимия", "Гормоны", "ОАК"
    values: JSONB                        # {"cholesterol": 5.66, "LDL": 3.2, ...}
    file_path: text?                     # путь к PDF
    status: str(50) = "current"          # current / historical
    created_at: timestamp
```

**Indexes:**
- `idx_blood_tests_user_date` on `(user_id, test_date)`

Большая часть анализов **не в этой таблице**, а в Google Drive в `~/FamilyHealth/{Имя}/knowledge_base.json`. Эта таблица — для тех что попадают через бот (`/doc`-загрузка или синк с мака, см. `02_data_sources.md` §16/§17).

---

## 8. `body_measurements` — замеры тела

```python
class BodyMeasurement(Base):
    id: int                              # autoinc PK
    user_id: BigInteger                  # FK → users.telegram_id
    date: date                           # NOT NULL
    waist_cm, neck_cm, hips_cm, chest_cm, thigh_cm, biceps_cm: float?
    notes: text?
    created_at: timestamp
```

**Indexes:**
- `idx_measurements_user_date` on `(user_id, date)`

---

## 9. `verified_products` — справочник проверенных продуктов (#255)

Этикеточные КБЖУ упакованных продуктов, чтобы LLM-vision не оценивал один и
тот же батончик заново при каждом фото. Подробности дизайна — [ADR-0007](../architecture/decisions/0007-verified-products-catalog.md).

- `user_id BIGINT NULL` → `users.telegram_id`; **NULL = общая запись, видна всем**; личная приоритетнее общей при матчинге.
- `name` + `name_norm` (нормализация — `core/food/verified_products.py::normalize_product_name`, единая точка), `brand`, `aliases` JSONB, `barcode`.
- `calories/protein/fats/carbs_per_100g FLOAT NOT NULL`, `fiber_per_100g`, `portion_g` (вес штуки/порции с этикетки) — nullable.
- `source`: `user_correction | label_photo | manual | import`; `times_used` — ранжирование топ-20 в промпт-блок.
- Уникальность — два частичных индекса: `(user_id, name_norm)` для личных, `(name_norm)` для общих (обычный UNIQUE не дедуплицирует NULL user_id).
- RLS: чтение `user_id IS NULL OR user_id = app.user_id`, запись — только свои строки (сознательное отклонение от строгого `user_isolation`).
- Потребители: `core/food/verified_products.py` (post-match в `save_meal_to_db` + промпт-блок в `core/llm/router.py`), кнопка «💾 Запомнить продукт» (`telegram-bot/handlers/verified_products.py`), сид `scripts/import/seed_verified_products.py`.

---

## 10. `agent_conversations` — история диалога BotkinClaw

Хранит денормализованную по блокам историю переписки пользователя с in-process AI-агентом (`core/agent_chat.py`). Одна логическая реплика может распасться на несколько строк (`tool_use`/`tool_result` идут отдельными записями).

```python
class AgentConversation(Base):
    id: BigInteger                       # autoinc PK
    user_id: BigInteger                  # NOT NULL, без FK (аудит-след переживает удаление юзера)
    role: str                            # 'user' | 'assistant' | 'tool_use' | 'tool_result' (CHECK)
    content: JSONB                       # содержимое блока (текст или tool-payload)
    tool_use_id: str?                    # связка tool_use ↔ tool_result
    source: str?                         # NULL/'botkinclaw' = обычный диалог; 'e2e_test'; 'router_*'/'llm_text' = события быстрого парсера (НЕ история чата)
    created_at: timestamp                # NOT NULL
```

**Indexes:** `idx_agent_conv_user_created` on `(user_id, created_at DESC)`; частичный `idx_agent_conv_source` on `source WHERE source IS NOT NULL`.

⚠️ **Чтение истории агента** — только `WHERE source IS NULL OR source = 'botkinclaw'` (см. `_load_history` в `core/agent_chat.py`), иначе в контекст попадут события быстрого парсера еды/веса/АД, которые агент никогда не «видел». RLS: `user_isolation` (см. раздел про RLS выше).

Не путать со **штатным подтверждением еды** (превью «Сохранить»/«Отмена» перед записью в `nutrition_log`) — это состояние живёт в памяти процесса (`services.state.state_manager`), не в `agent_conversations`. См. anti-pattern в `01_architecture.md`.

## 11. `personal_access_tokens` — PAT для MCP-коннектора (#228)

Долгоживущий токен, который пользователь сам выпускает через `/connect_mcp`, чтобы подключить личный Claude Desktop к серверу Botkin по MCP.

```python
class PersonalAccessToken(Base):
    id: BigInteger                       # autoinc PK
    user_id: BigInteger                  # FK → users.telegram_id — чьи данные открывает токен
    token: str(128)                      # 'pat_<telegram_id>_<hex32>'
    name: str?                           # метка от пользователя ("Мой ноут")
    scope: str = "rw"                    # 'ro' (read-only, для врача/близкого) | 'rw' (CHECK)
    created_at, last_used_at, revoked_at: timestamp?
    created_by_user: BigInteger           # обычно == user_id (self-service)
```

**Indexes:** уникальный `ix_personal_access_tokens_token`, `ix_personal_access_tokens_user_id`. **Без RLS** — обмен PAT→JWT (`POST /api/agent/exchange_pat_for_jwt`) происходит ДО того, как известен `app.user_id`.

`is_active` — python-property (`revoked_at IS NULL`), не колонка. Отзыв — soft delete через `revoked_at`, без удаления строки (уже выданные JWT остаются валидны до истечения своего TTL ~5 мин). Подробности потока — [ADR-0006](../architecture/decisions/0006-mcp-connector-pat-jwt.md) и раздел workflow в `04_workflows.md`.

## 12. CGM / глюкоза (#96) — три таблицы

**`glucose_readings`** — точки глюкозы с LibreLinkUp:
```python
class GlucoseReading(Base):
    id: BigInteger; user_id: BigInteger   # FK → users.telegram_id
    ts: timestamp(tz)                     # NOT NULL, канонический UTC (не naive local!)
    value: Numeric(5,2)                   # ммоль/л
    trend: int?                           # 0-5, стрелка тренда LibreLinkUp
    source: str = "librelinkup"
    raw: JSONB?
```
Unique `(user_id, ts)`, index `(user_id, ts DESC)`.

**`cgm_connections`** — маппинг LibreLinkUp `patient_id` (сенсор) → `telegram_id`:
```python
class CgmConnection(Base):
    id: BigInteger; patient_id: str(36) unique; telegram_id: BigInteger  # FK
    connected_at: timestamp?
```

**`cgm_followers`** — креды follower-аккаунта LibreLinkUp, который пользователь заводит сам под свой регион (#381, региональное ограничение Abbott — один follower видит пациентов только своего региона):
```python
class CgmFollower(Base):
    id: BigInteger; owner_user_id: BigInteger  # FK
    region: str(8); email: str(255)
    password_enc: text                    # ТОЛЬКО зашифровано (core.infra.secrets), ключ в env SECRETS_KEY
    label: str?
    last_ok_at: timestamp?; last_error: text?; revoked_at: timestamp?
```
Unique `(region, email)`. `revoked_at` — тот же паттерн soft-delete, что у `PersonalAccessToken`.

Подробности потока — раздел про CGM в `02_data_sources.md` и `04_workflows.md`.

## Удалённые таблицы (для исторического контекста)

- **`user_products`** + **`user_product_variants`** — фича `/my_products`, удалена 2026-04-21 после 0 рядов за всё время. См. `AI_CHANGELOG.md` и `archive/2026-02-01/scripts/`.

---

## Часто используемые SQL-сниппеты

### Сколько калорий ел Александр за последние 30 дней
```sql
SELECT date,
       ROUND(SUM((totals->>'calories')::numeric)) AS kcal
FROM nutrition_log
WHERE user_id = 895655 AND date >= CURRENT_DATE - INTERVAL '30 days'
GROUP BY date ORDER BY date DESC;
```

### Какие добавки принял сегодня
```sql
SELECT supplement_name, time
FROM supplements_log
WHERE user_id = 895655 AND date = CURRENT_DATE
ORDER BY time;
```

### Дни без записей еды (gap detection)
```sql
WITH days AS (
  SELECT generate_series(CURRENT_DATE - INTERVAL '30 days', CURRENT_DATE, '1 day'::interval)::date AS d
)
SELECT d FROM days
WHERE d NOT IN (SELECT date FROM nutrition_log WHERE user_id = 895655)
ORDER BY d;
```

### Топ-частых блюд (грубо, с учётом 3 схем items)
```sql
SELECT
  COALESCE(it->>'food', it->>'product', it->>'name') AS name,
  COUNT(*) AS times
FROM nutrition_log n, LATERAL jsonb_array_elements(n.items) it
WHERE n.user_id = 895655 AND n.date >= CURRENT_DATE - INTERVAL '90 days'
GROUP BY name ORDER BY times DESC LIMIT 20;
```

### Найти item'ы без указанного веса (data quality probe)
```sql
SELECT n.id, n.date, n.meal_name, it
FROM nutrition_log n, LATERAL jsonb_array_elements(n.items) it
WHERE n.user_id = 895655
  AND COALESCE(it->>'amount', it->>'weight_g', it->>'weight') IS NULL
  AND COALESCE((it->>'calories')::numeric, 0) > 0;
```

---

## Migration / схема change процесс

**С Alembic** (см. [ADR-0003](../architecture/decisions/0003-alembic-for-db-migrations.md), заменил ручной `ALTER TABLE`-процесс из ранних версий этой доки). Миграции — `database/alembic/versions/*.py`, короткие slug-имена (`nlplan01_add_nutrition_log_status.py`, `pat0token01_add_personal_access_tokens.py` и т.п.), не auto-generated хэши.

1. Изменить `database/models.py` (новое поле / новая таблица).
2. Сгенерировать/написать миграцию в `database/alembic/versions/`.
3. Прогнать alembic-check (сверяет ORM ↔ фактическую схему — ловит расхождения типа лишнего `UniqueConstraint`, см. комментарии у `PersonalAccessToken.token` в `database/models.py`).
4. Применить на сервере (обычно как часть деплоя — **строго с согласия пользователя** для прод-БД).
5. Обновить эту доку (`03_database_schema.md`) в том же коммите.
6. Добавить запись в `AI_CHANGELOG.md`.
7. Если новая таблица — обновить и `01_architecture.md`.

---

## Anti-patterns

❌ Запрос без `user_id`:
```sql
SELECT SUM((totals->>'calories')::numeric) FROM nutrition_log;  -- пользователей много (open registration), не 3!
```

❌ Поле `fat` (в единственном числе) — нет такого. Использовать `fats`.

❌ FK на `users.id` — нет такого PK. Использовать `users.telegram_id`.

❌ `users.target_calories / target_protein / target_fat / target_carbs` — нет таких полей. Цели вычисляются.

❌ Запись в `blood_pressure_logs / daily_summaries / sleep_records / workouts` — это orphan-таблицы, не управляются ORM. Не использовать в новом коде.

❌ Чтение items только по одному ключу (`it["food"]` или `it["product"]`) — пропустишь legacy-схему. Использовать `_item_name()` хелпер.

❌ Запись items без `fiber` — будет 0 в дневнике. Прогонять через `enrich_items_with_fiber()` перед `INSERT`.

✅ Все CRUD-функции уже есть в `database/crud.py`. Сначала grep, потом писать новое.

---

[← Документация Botkin — Index](../INDEX.md)
