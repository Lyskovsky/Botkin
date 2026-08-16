# 03 · Database Schema

> **Last verified:** 2026-04-21 (после удаления `user_products` / `user_product_variants`)
> **DB:** PostgreSQL 16, Docker container `healthvault_postgres` на сервере, БД `healthvault`
> **ORM:** SQLAlchemy 2 declarative (Mapped style)
> **Source of truth:** `database/models.py`. Эта дока — человекочитаемая проекция оттуда. При расхождениях — верить коду.

---

## TL;DR — все таблицы за 1 экран

| Таблица | Что | Ключ | Главные поля |
|---|---|---|---|
| `users` | 3 пользователя бота (whitelist) | `telegram_id` (BigInt, PK!) | `bmr`, `target_weight_kg` |
| `user_settings` | Per-user настройки и список добавок | `user_id` (PK = telegram_id) | `bmr_override`, `supplements` (JSON) |
| `nutrition_log` | Приёмы пищи | `id` autoinc | `items` JSONB, `totals` JSONB, `meal_time`, `meal_name` |
| `supplements_log` | Принятые добавки | `id` autoinc | `supplement_name`, `time`, `date` |
| `weights` | Взвешивания (Zepp + ручные) | `id` autoinc | `weight`, `body_fat`, `muscle_mass`, `bmi` |
| `activity_log` | Активность за день (Garmin) | `id` autoinc | `steps`, `active_calories`, `bmr_calories`, `hrv` |
| `blood_tests` | Анализы крови | `id` autoinc | `values` JSONB, `test_type`, `status` |
| `body_measurements` | Замеры тела (талия, шея, …) | `id` autoinc | `waist_cm`, `neck_cm`, и т.п. |
| `verified_products` | Справочник проверенных продуктов (#255) | `id` autoinc | `name_norm`, `*_per_100g`, `portion_g`, `barcode`; `user_id NULL` = общая запись |

Кроме того в БД есть **legacy/orphan таблицы** не управляемые SQLAlchemy: `blood_pressure_logs`, `daily_summaries`, `sleep_records`, `workouts`. Заполняются Apple Health webhook'ом для давления и оставлены ради старых скриптов аналитики. Из бот-кода **не читать**.

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

---

## Главные пользователи (для SQL и тестов)

| `telegram_id` | Кто | Роль |
|---|---|---|
| **895655** | Александр (владелец) | основной user, на нём проверять всё |
| REDACTED_ID | user_2 | второй пользователь |
| <telegram_id> | early_user | реальные данные |

⚠️ **Любой запрос к данным — с явным `WHERE user_id = X`.** Без фильтра суммируются все 3.

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
    created_at: timestamp
```

**Indexes / Constraints:**
- `idx_nutrition_user_date` on `(user_id, date)` — главный индекс для всех аналитических запросов.
- `uq_nutrition_user_date_meal` on `(user_id, date, meal_time, meal_name)` — unique. ⚠️ **Этот constraint практически бесполезен** так как `meal_name` свободный текст; есть 30 дублей за 100 дней (см. `2026-04-21-architectural-review.md` пункт #3).

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

Большая часть анализов **не в этой таблице**, а в Google Drive в `~/HealthVault/{Имя}/knowledge_base.json`. Эта таблица — для тех что попадают через бот.

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

В проекте **нет Alembic**. Migrations делаются руками:

1. Изменить `database/models.py` (новое поле / новая таблица).
2. На сервере выполнить `ALTER TABLE` через psql — **строго с согласия пользователя**:
   ```bash
   ssh root@116.203.213.137 "docker exec healthvault_postgres psql -U healthvault -d healthvault -c \"ALTER TABLE … \""
   ```
3. Обновить эту доку (`03_database_schema.md`) в том же коммите.
4. Добавить запись в `AI_CHANGELOG.md`.
5. Если новая таблица — обновить и `01_architecture.md`.

**TODO (см. `2026-04-21-architectural-review.md`):** ввести Alembic для версионирования миграций. Сейчас изменения схемы — устные договорённости.

---

## Anti-patterns

❌ Запрос без `user_id`:
```sql
SELECT SUM((totals->>'calories')::numeric) FROM nutrition_log;  -- 3 пользователя суммируются!
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
