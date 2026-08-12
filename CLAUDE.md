# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

> Открытая платформа трекинга здоровья ([botkin.health](https://botkin.health)).
> Контакты автора на сайте.

---

## 🎯 Vision — куда движется проект

**Botkin — это мультиюзерная система с гибридной приватностью.**

```
┌─ Семейный/командный слой (Hetzner server) ─────────────────────┐
│  • Telegram-бот (общий entry point)                            │
│  • PostgreSQL: nutrition, activity, weights, BP, biomarkers    │
│  • Server-side sync: Garmin, Apple Health (HAE), Netatmo, ...  │
│  • Дашборд /mc/{share_token} — для каждого пользователя        │
│  • Tools API /api/agent/* (JWT-isolation по cohort+RLS)        │
└─────────────────────┬──────────────────────────────────────────┘
                      │ MCP
┌─────────────────────┴──────────────────────────────────────────┐
│  Личный AI-агент на компе пользователя (для тех кто хочет       │
│  приватности и готов работать со своим Claude через MCP):      │
│   • Локальные приватные потоки (психолог-дневник, Screen Time,  │
│     личные заметки, медзаписи без согласия публикации)         │
│   • Свой Claude (Claude Desktop / Code) с MCP подключением      │
│   • Видит и серверные данные (через MCP), и локальные          │
│   • Архитектура host-оркестратора см. NanoClaw                  │
│     (docs/architecture/decisions/0001-nanoclaw-*.md)            │
└────────────────────────────────────────────────────────────────┘
```

**Ключевые принципы:**

1. **Multi-user из коробки** — cohort-роли (owner / family / early_user / external), RLS-изоляция, JWT для агентов. Сделано в Sprint 1a (04.05.2026).
2. **Гибридная приватность** — пользователь сам решает: что лежит на семейном сервере (доступно через AI), что только локально на его компе.
3. **MCP — основной канал** между личным Claude пользователя и сервером Botkin. Server отдаёт tools, Claude (на компе пользователя) использует.
4. **AI-врач = BotkinClaw** — in-process handler в основном aiogram-боте (решение от 21.05.2026 после спайка NanoClaw). Прямой вызов Anthropic Messages API из `@Botkin_md_bot`, история диалога в Postgres, tools через переиспользуемый `webhook/agent_tools_api.py` (JWT+RLS, 30+ endpoints). Один бот для всех пользователей, без отдельной контейнерной инфры. История: [ADR-0001](docs/architecture/decisions/0001-nanoclaw-ephemeral-not-persistent.md) (ephemeral vs persistent — остаётся валидным архитектурным принципом *если* когда-нибудь вернёмся к контейнеризации) + [ADR-0002](docs/architecture/decisions/0002-rejecting-nanoclaw-for-simpler-agent.md) (почему отказались от NanoClaw, и почему «BotkinClaw» — игра слов NanoClaw → BotkinClaw, бот сам играет роль контейнера).
5. **Open source** — код публичный. Все приватные данные (имена, диагнозы, биомаркеры, личные планы) — только в `~/FamilyHealth/<user>/`. Правила: `docs/operations/personal-data.md`.

**Что НЕ есть Botkin:**
- Не централизованный медицинский сервис (нет SLA / врачебной ответственности)
- Не публичный SaaS — приватная семейная платформа с open-source кодом
- Не «всё в облаке» — приватный слой обязателен (см. vision выше)

---

## 📚 Где что искать в документации

См. **[docs/INDEX.md](docs/INDEX.md)** — карта-навигатор.

Ключевые точки входа:
- **[docs/ROADMAP.md](docs/ROADMAP.md)** — NOW / NEXT / LATER / VISION / DONE
- **[docs/architecture/decisions/](docs/architecture/decisions/)** — ADR (отвергнутые подходы и почему)
- **[docs/projects/](docs/projects/)** — активные / завершённые / отвергнутые проекты с метками статуса
- **[docs/operations/personal-data.md](docs/operations/personal-data.md)** — куда класть личные данные

---

## Расположение проекта

**Код проекта (эта папка):**
`~/Library/CloudStorage/GoogleDrive-lyskovsky@gmail.com/Мой диск/Projects/Vibe coding/Botkin/`

Git remote: `git@github.com:botkin-health/Botkin.git` (перенесён в орг `botkin-health` 14.06.2026; ранее `Lyskovsky/Botkin`, ещё раньше `HealthVault`)

**Медицинские данные семьи (отдельная папка, не путать!):**
`~/Library/CloudStorage/GoogleDrive-lyskovsky@gmail.com/Мой диск/FamilyHealth/`

Там лежат папки с PDF-анализами и knowledge_base.json каждого. У каждой папки свой CLAUDE.md. Это данные — не код.

Если нужно обратиться к медданным из кода/скриптов — путь:
```python
FAMILY_HEALTH = Path.home() / "Library/CloudStorage/GoogleDrive-lyskovsky@gmail.com/Мой диск/FamilyHealth"
```

## Пользователи бота (Botkin)

⚠️ **ВНИМАНИЕ — миграция бота (12.05.2026):**
- **Активный бот:** `@Botkin_md_bot` (display name «Botkin», bot_id 8739688481)
- **Прямая ссылка для пользователей:** **t.me/Botkin_md_bot**
- **Старый бот `@HealthVault_bot`** (bot_id 8500310863) — архив, webhook удалён. Истории чатов у юзеров сохраняются, но новые сообщения не обрабатываются.

`@NutriLogBot` БЕЗ префикса — это **чужой украинский бот-двойник**, не наш. Не давать пользователям ссылку `t.me/NutriLogBot`.

Telegram ID и личные данные пользователей — в `~/.claude/CLAUDE.md` (приватный, не в git).

**ВАЖНО:** При SQL-запросах к `nutrition_log` и `supplements_log` ВСЕГДА добавлять `WHERE user_id = 895655` для данных владельца. Без фильтра суммируются калории всех пользователей.

## Навигация по проекту

| Файл | Что содержит |
|---|---|
| `HEALTH.md` | Профиль здоровья: вес, анализы, добавки, давление, цели |
| `knowledge_base.json` | Структурированные данные анализов (JSON) — источник истины |
| `KNOWLEDGE_BASE.md` | Человекочитаемый каталог анализов (дублирует JSON для удобства) |
| `todo.md` | Техдолг и роадмап проекта (без личных целей здоровья — они в HEALTH.md) |
| `docs/ai_context/` | Контекст для AI. **Начни с `README.md`** — там навигация. 01 архитектура · 02 источники данных · 03 схема БД · 04 workflows · 05 помощь с едой · `AI_CHANGELOG.md` |

## Данные здоровья — источники и пайплайн

### 🩸 Анализы (KB) — 2-source pipeline + read-time канонизация (унифицировано 01.06.2026)

Источник истины — `~/FamilyHealth/<Имя>/knowledge_base.json` **на маке**. На сервере биомаркеры живут в **двух местах**, и канонизация ключей происходит **на чтении** через `core/health/kb_schema.py` (единый реестр алиасов + конверсия единиц с guard + US→метрика по признаку `_unit_system`):

В Postgres `blood_tests` импортируются KB-секции `blood_tests`/`biochemistry`/`hormones`/`vitamins` (`_extract_rows`). Записи в US-единицах (KB-поле `"units"` содержит `mg/dl`/`g/dl`) получают служебный ключ `_unit_system="US"` в JSONB `values` — `to_canonical` по нему конвертирует g/dL·mg/dL·µg/dL в метрику на чтении (таблица `US_TO_METRIC`).

| Канал на сервере | Кто читает | Формат | Как туда попадают данные |
|---|---|---|---|
| PostgreSQL `blood_tests` (сырые `values`) | **дашборд** (`dashboard_generator._load_biomarkers_from_db` → `aggregate_biomarkers`) **и агент** (`/recent_biomarkers`, `/phenoage`) | канонизируется на лету `to_canonical` | `scripts/import/kb_to_blood_tests.py` **и** `/doc` в боте (см. ниже) |
| `/app/data/kb/kb_<id>.json` (bind-mount) | агент (`/kb_value`, `/list_kb_keys`) | сырой полный KB | `scripts/sync_family_kb.py --apply` |

Дашборд **больше не читает файл** `biomarkers_<id>.json` — он берёт биомаркеры из Postgres (durable, не теряются при rebuild контейнера — раньше у 4 family-юзеров дашборды пустели после деплоя). Legacy-fallback `BOTKIN_LEGACY_BIOMARKERS_JSON` удалён 11.06.2026 (аудит): флаг нигде не включался.

**Третий писатель — `/doc` в боте (#281, 25.07.2026).** Когда пользователь сам грузит анализ через `/doc` и жмёт «Сохранить», `handlers/doc_upload._save_to_blood_tests` пишет показатели прямо в `blood_tests` (маппер `core/health/doc_to_blood_test.py` → `crud.upsert_blood_test`), минуя мак и `sync_user_health`. Правила те же: сырые ключи, канонизация на чтении. Ключ идемпотентности — `(user_id, test_date, test_type)`, где `test_type` = `«<лаборатория> · <8hex контент-хэша файла>»`. Нелабораторные документы (УЗИ, заключения) в `blood_tests` не попадают — их отсекает гейт `to_canonical`; они остаются в `documents[]`. Нет даты в документе — строки нет (дату не выдумываем).

⚠️ **Следствие:** данные от `/doc` живут только на сервере, в локальном `~/FamilyHealth/<юзер>/knowledge_base.json` их нет. `sync_user_health` их **не затрёт** (upsert без DELETE), но и не подтянет обратно на мак. Если нужно свести — переносить в локальный KB руками.

**Когда добавил новый анализ в KB → одна команда для ЛЮБОГО юзера:**
```bash
python3 scripts/sync_user_health.py --user <telegram_id> --apply   # или --all
```
Две идемпотентные стадии: KB → bind-mount `kb_<id>.json` + KB → Postgres `blood_tests`. Маппинг `telegram_id → папка` — в `config/users.py::KB_USERS` (единый, не дублировать).

⚠️ `sync_user_health` льёт из **локального** KB. Если у юзера на сервере данные богаче локального (прецедент: KB Андрея беднее его старого дашборда) — сперва дополнить локальный `knowledge_base.json`, иначе перезатрёшь.

**Прецеденты:** 24.05.2026 — забывали стадии синка (теперь одна команда). 01.06.2026 — унификация: 3 формата ключей (`LDL`/`ldl`/`ldl_mmol_l`) и битый ad-hoc файл Димы (сырые pmol/L под каноническими именами) → единый `kb_schema` с конверсией единиц. 16.06.2026 (#95) — секция `biochemistry` вообще не импортировалась → phenoage не видел альбумин/железо/ALKP; плюс панель Маккаби в US-единицах писалась без конверсии (молча неверный bio_age). Фикс: импорт `biochemistry` + признак `_unit_system` + конверсия US→метрика на чтении + алиас `ALKP`→`ALP`.

**Follow-up:** консолидировать `core/reports/biomarker_dynamics.py::MARKER_CONFIG` (4-й case-sensitive маппинг) на `kb_schema`.

### Автоматические (скрипты тянут сами)

| Метрика | Источник | Файл/таблица | Скрипт |
|---|---|---|---|
| Шаги, дистанция | Garmin API | `data/garmin/daily-summary/YYYY-MM-DD.json` → `stats.totalSteps`, `totalDistanceMeters` | `scripts/garmin/download_garmin_data.py` |
| Пульс покоя, min/max HR | Garmin API | `data/garmin/daily-summary/` → `stats.restingHeartRate` | то же |
| Сон, стресс, HRV, Body Battery | Garmin API | `data/garmin/{sleep,stress,hrv,body-battery}/` | то же |
| Тренировки | Garmin API | `data/garmin/activities/` | то же |
| Вес, жир, висцеральный жир | Zepp API (CN3) | `data/zepp_export_latest.csv` | `scripts/import/zepp_api.py` (токен ~7 дней, reauth через `--code URL`) |
| Вес + ПОЛНЫЙ состав тела (мышцы, вода, кости, висцеральный жир) | Withings API (весы Body Smart) | таблица `weights` (`source='withings'`) | `scripts/import/withings_api.py --user <tg_id>`. Нужен, потому что **в HealthKit нет типов** для мышечной массы/воды/костной массы/висцерального жира — через HAE доходят только вес, % жира и безжировая масса. Апсерт с COALESCE (канал HAE не перетирается). Креды `WITHINGS_CLIENT_ID/SECRET/REFRESH_TOKEN`; refresh **ротируется** → `data/cache/withings_tokens.json` |
| Воздух дома | Netatmo API | `data/environment/netatmo_history.json` | `scripts/import/netatmo.py` |
| Погода | Open-Meteo | `data/weather/weather_history.json` | `scripts/import/weather.py` |
| Глюкоза (CGM) | LibreLinkUp API (Abbott FreeStyle Libre 3) | таблица `glucose_readings` (mmol/L) | `scripts/import/librelinkup.py` (follower `dr@botkin.health`, регион EU; маппинг `cgm_connections`; онбординг `/connect_cgm`) |
| Питание, добавки | PostgreSQL (сервер) | таблицы `nutrition_log`, `supplements_log` | `scripts/fetch_remote_nutrition.sh` |
| iPhone Screen Time | ActivityWatch + Biome | `data/activities/iphone_screentime_perapp.json` | `aw-import-screentime` + `scripts/import/activitywatch.py` |
| Mac Screen Time | ActivityWatch | `data/activities/mac_screentime_perapp.json` | `scripts/import/mac_screentime.py` |

### Ежедневный автоэкспорт через Health Auto Export (iOS)

**С мая 2026 — основной канал для всех Apple Health метрик.** Заменяет старый Shortcut, который был ненадёжный (требовал ручного запуска и часто падал на ошибках). Ставится один раз, дальше работает в фоне без участия пользователя.

**Метрики, которые приходят через этот канал** (все с iPhone/Apple Watch/Omron/Mi-весов через Apple Health):

| Метрика | Куда пишется |
|---|---|
| Шаги, дистанция ходьбы, активные ккал, этажи | `activity_log` (steps, distance_km, active_calories) |
| Пульс (avg/min/max), пульс покоя | `activity_log` + `raw_data` |
| Давление систолическое/диастолическое (Omron) | `blood_pressure_logs` |
| Походка: скорость, длина шага, двойная опора, асимметрия | `activity_log.raw_data` |
| Вес, % жира, мышечная масса (Mi-весы → Apple Health) | `weights` |
| VO2 Max, частота дыхания, температура запястья | `activity_log.raw_data` |

**Тренировки** приходят **отдельным POST** `data.workouts[]` (своя автоматизация HAE типа Workouts, не Health Metrics) → таблица `workouts`, дедуп по `source=hae_<id>`. Парсинг в `webhook/apple_health.py` (`_hae_workouts_to_rows`/`_insert_new_workouts`). Формат, грабли и ручная настройка — [docs/researches/2026-06-18-hae-workouts.md](docs/researches/2026-06-18-hae-workouts.md) (#100). HAE не умеет шэрить конфиги автоматизаций — настройка только ручная.

**Стек:**
- **iOS-приложение:** [Health Auto Export – JSON+CSV](https://apps.apple.com/app/health-auto-export-json-csv/id1115567069) (Lybron Sobers, $24.99 lifetime)
- **Webhook:** `POST https://health.orangegate.cc/apple_health_v2` (Bearer token из `.env: APPLE_HEALTH_TOKEN`)
- **Серверный адаптер:** `telegram-bot/webhook/apple_health.py` — функция `_hae_to_daily_payloads()` парсит формат `data.metrics[]`, группирует по дням, упсертит в БД

**Настройки в HAE (важные):**
- Тип: REST API · Формат: JSON · Версия: v2 · Диапазон: «Вчера» · Суммировать: ON · Группировка: «День» · Частота: 1 / Дни
- Header: `Authorization: Bearer <APPLE_HEALTH_TOKEN>`
- 17 метрик выбрано (см. таблицу выше)

**Когда срабатывает:** iOS-планировщик решает сам (~1 раз в сутки, обычно ночью когда iPhone на зарядке). Точное время не задаётся — разброс ±1-2 часа. Требования: iPhone разблокирован, Background App Refresh для HAE включён, Low Power Mode выключен.

**Ручной экспорт:** в HAE → автоматизация Botkin (на iPhone может ещё называться «HealthVault» если не переименовали в HAE-приложении — переименовать) → внизу зелёная кнопка «Ручной экспорт» → выбрать диапазон → POST уйдёт сразу. Полезно для проверки свежей тренировки/замера на дашборде, не дожидаясь ночного автозапуска.

**Endpoint `/apple_health` (v1)** — поддерживаемый канал **бесплатного пути через iOS Shortcuts** (iCloud-шаблон из `docs/user_guide/ru/apple-health.md`, per-user токены `hvt_`). Принимает плоский JSON. Это не legacy: HAE v2 — надёжный платный путь, Shortcut v1 — официальный бесплатный (требует ручного/автоматизированного запуска Shortcut).

**Документация HAE:**
- [Help Center — REST API automation](https://help.healthyapps.dev/en/health-auto-export/automations/rest-api/)
- [GitHub: Lybron/health-auto-export](https://github.com/Lybron/health-auto-export) — спецификация JSON формата
- [Wiki: API Export JSON Format](https://github.com/Lybron/health-auto-export/wiki/API-Export---JSON-Format) — структура `data.metrics[]`

### Apple Health XML экспорт (ручной, редко)

Когда пользователь делает Health → Export All Health Data и присылает zip (`экспорт.zip`), распаковываем и запускаем парсер:

```bash
# 1. Распаковать zip в /tmp/apple_health/apple_health_export/
# 2. Запустить парсер (путь к XML захардкожен — поправить при необходимости)
python3 scripts/import/parse_apple_health_xml.py
# 3. Удалить сырой XML — он 700 МБ+
```

Это обновляет **плоские файлы, которые читают `/sync` и `/dashboard`**:
- `data/apple_health_blood_pressure.json` → `measurements[{date, time, systolic, diastolic}]` — история АД с 2018
- `data/apple_health_heart_rate.json` → `measurements[{date, avg, min, max, n}]` — дневная агрегация пульса
- `data/apple_health_steps_daily.json` → `steps_by_day[{date, steps, primary_source}]` — шаги с 2015 (см. ⚠️ ниже)
- `data/apple_health_steps_by_source.json` → `by_day[{date, primary, primary_steps, all_sources}]` — разбивка шагов по источникам (для аудита/дебага)
- `data/apple_health_gait.json` → `gait_by_day[{date, speed_km_h, step_length_cm, double_support_pct, asymmetry_pct}]` — походка с 2020
- `data/apple_health_weight_daily.json` + `apple_health_weight.json` — вес с 2015

**ВАЖНО:** эти файлы НЕ устарели. /sync читает их, /dashboard тоже. Каждый раз когда приходит новый Apple Health экспорт — перезаписываем их через `scripts/import/parse_apple_health_xml.py` и удаляем сырой XML (он 700 МБ+).

⚠️ **Текущий `apple_health_steps_daily.json` ЗАДВОЕН для 2023+** (баг старого парсера: суммировал все sourceName без дедупликации; в 2026 даёт ≈ ×2.57 от Garmin). Парсер починен 14.05.2026 — теперь выбирает один primary-источник по приоритету `Garmin → Apple Watch → iPhone → fallback-max`. Чтобы исправить flat-файлы — нужен **свежий экспорт Apple Health XML** (Health → Профиль → Экспорт всех данных) и повторный запуск `parse_apple_health_xml.py`. Сравнить можно через новый `apple_health_steps_by_source.json` (там видны все источники за день). Для аналитики **2023+ года** до этого момента — использовать **только** Garmin `data/garmin/daily-summary/`, а не AH-flat. История **до 2022** в файле корректна (тогда был фактически один источник).

### Apple Health — исторический архив (не читается автоматически)

Дополнительно из того же XML-экспорта вытащены данные, которых нет в боте/Garmin/Zepp:

- **`data/apple_health/workouts.json`** — 502 тренировки за 11 лет (2015–2026), поля: `type`, `duration`, `distance`, `energy`, `start`, `source`. Использовать когда нужно посмотреть долгосрочную динамику спорта ("сколько HIIT в 2022 vs 2026", "пробежки до 2020").
- **`data/apple_health/daily_metrics.json`** — 16 дополнительных метрик с дневной агрегацией: SpO2, активные ккал, этажи, температура тела, плавание, громкость наушников и т.д. Покрытие: см. файл.
- **`data/apple_health/types_summary.json`** — каталог всех 31 типов записей из последнего экспорта с диапазонами дат (метадата, для справки).

Эти файлы НЕ читают `/sync` и `/dashboard` — они лежат как архив. Если пользователь спросит что-то из истории ("тренировки за 2017", "плавание в 2024") — читаем напрямую через `Read`/`python3`.

## Команды разработки

### Тесты

```bash
# Запуск всех unit-тестов (integration и live LLM исключены по умолчанию)
PYTHONPATH=. pytest tests/ -v \
  --ignore=tests/integration \
  --ignore=tests/test_nutrition_parsing.py

# Запуск одного файла
PYTHONPATH=. pytest tests/test_nutrition_logic.py -v

# Запуск одного теста
PYTHONPATH=. pytest tests/test_nutrition_logic.py::test_xxx -v

# Env-переменные НЕ нужны: dummy-ключи ставит tests/conftest.py (setdefault +
# autouse-фикстура, защищающая от реальных LLM-вызовов за деньги).
# DATABASE_URL не нужна — conftest.py создаёт in-memory SQLite
```

### Линтинг

```bash
# Проверить линтером
ruff check .

# Проверить форматирование
ruff format --check .

# Автоисправление
ruff check --fix .
ruff format .
```

Конфигурация ruff — в `pyproject.toml`. Строки >120 символов игнорируются (E501 — LLM-промпты намеренно длинные).

### Деплой на сервер (Hetzner 116.203.213.137)

**Деплой — только через GitHub Actions.** Workflow «Deploy prod» (`.github/workflows/deploy-prod.yml`):

```bash
# Запуск из CLI (ветка по умолчанию main)
gh workflow run deploy-prod.yml -f branch=main

# Откат на готовый образ — сборка пропускается
gh workflow run deploy-prod.yml -f image_tag=<готовый-тег-образа>
```

Либо вручную: Actions → «Deploy prod» → Run workflow. Workflow собирает Docker-образ бота, пушит в GHCR (`ghcr.io/botkin-health/botkin-bot`), затем по SSH на сервере (каталог `/opt/botkin`) выполняет `docker compose -f docker-compose.prod.yml pull && up -d --wait` (pull-only, **без сборки на сервере**). Файл `.env` лежит на сервере (`/opt/botkin/.env`), в репозиторий не входит. Подробнее — `docs/DEPLOYMENT.md`.

### Диагностика сервера

```bash
# Логи бота (последние 50 строк)
ssh root@116.203.213.137 "docker logs healthvault_bot --tail 50"

# Статус контейнера
ssh root@116.203.213.137 "docker ps | grep healthvault"

# Рестарт бота
ssh root@116.203.213.137 "docker restart healthvault_bot"

# psql на сервере
ssh root@116.203.213.137 "docker exec healthvault_postgres psql -U healthvault -d healthvault"

# Диагностика общего состояния
./scripts/util/diagnose_server.sh
```

### Синк данных здоровья

```bash
# Синк KB конкретного пользователя (bind-mount + Postgres)
python3 scripts/sync_user_health.py --user 895655 --apply

# Все пользователи
python3 scripts/sync_user_health.py --all --apply
```

---

## Skills (Claude Code)

- `/sync` — обновить все источники данных, показать таблицу актуальности
- `/cleanup` — коммит, пуш, бэкап БД, удаление мусора

## Хранение токенов и секретов

| Что | Где | Примечание |
|---|---|---|
| API-ключи (OpenAI, Gemini, Telegram) | `.env` / `.env.production` | Постоянные, не истекают |
| Пароли (Garmin, Zepp, Netatmo) | `.env` | Для OAuth-потоков |
| OAuth-токены (Zepp, и др.) | `data/cache/tokens.json` | Истекают через 5-7 дней, требуют `--reauth` |
| Netatmo refresh_token | `.env` (`NETATMO_REFRESH_TOKEN`) | Долгоживущий |

`data/cache/` в `.gitignore` — токены не попадают в git.

### sshpass и PATH

`sshpass` установлен в `/opt/homebrew/bin/sshpass`. В subshell-скриптах (bash, Python subprocess) `/opt/homebrew/bin` может отсутствовать в PATH, поэтому **всегда использовать полный путь** во всех скриптах. Все `.sh` и `.py` в `scripts/` уже исправлены. Если добавляешь новый скрипт с `sshpass` — сразу пиши полный путь.

### Zepp reauth — правильный порядок

1. Запустить `scripts/import/zepp_api.py --reauth` → скрипт выведет URL для логина
2. Открыть URL в браузере, залогиниться в Xiaomi
3. Скопировать redirect URL вида `hm.xiaomi.com/watch.do?code=...`
4. Запустить `scripts/import/zepp_api.py --code КОД` (только сам код после `?code=`)
5. Дождаться `✅ Токен получен!` — токен сохранён в `data/cache/tokens.json`
6. **Если после этого упала ошибка** (sshpass, сеть и т.п.) — **не передавать `--code` повторно!**
   OAuth-код одноразовый. Просто запустить `scripts/import/zepp_api.py` без флагов — токен уже в кэше.

## Анти-паттерны кода (не повторять)

- ❌ Импортировать `core.llm_router`, `core.menu_parser` и другие proxy-shims из корня `core/` — это re-exports из рефакторинга 22.03.2026. Импортировать напрямую: `from core.llm.router import …`, `from core.vision.menu_parser import …`
- ❌ `SELECT … FROM nutrition_log` без `WHERE user_id = X` — суммируются все пользователи
- ❌ Писать новые поля в таблицу `users` — настройки и цели живут в `user_settings`
- ❌ Читать поле `totals->>'fat'` — поле называется `totals->>'fats'` (множественное число)
- ❌ FK на `users.id` — PK таблицы users это `telegram_id` (BigInt), не синтетический `id`
- ❌ Читать items только по одному ключу (`it["food"]`) — есть 3 схемы одновременно; использовать `_item_name()` из `core/food/fiber_table.py`
- ❌ Писать items без поля `fiber` — прогонять через `enrich_items_with_fiber()` перед INSERT
- ❌ Писать в orphan-таблицы `daily_summaries / sleep_records` — они не управляются ORM и пусты на проде. В `blood_pressure_logs / workouts` пишут только штатные raw-SQL пути (`webhook/apple_health.py`, `webhook/agent_tools_api.py`) — новые записи добавлять через них, не через ORM
- ❌ **Держать открытую транзакцию Postgres поперёк долгого сетевого вызова** (LLM, внешний API). Сессии живут с `idle_in_transaction_session_timeout` (15с, `database/__init__.py`) — Postgres обрывает такое соединение, следующий запрос падает с `OperationalError`. Перед сетью закрывать транзакцию (`_end_open_tx` в `core/agent_chat.py`). Транзакцию открывают не только записи: после `commit()` ORM-объект истекает (`expire_on_commit=True`), и **чтение его атрибута** тянет refresh-SELECT. Прецедент #347 (26.07.2026): агент терял ответы, и чем содержательнее ответ — тем вероятнее терялся
- ❌ Ронять уже полученный от LLM ответ из-за сбоя записи в БД — генерация оплачена. Логировать сбой персистентности, но ответ пользователю отдавать (`_persist_turn`)

---

## Хронолог разработки в Notion

Notion-страница **«Хронолог разработки»** (ID `37bf1efb-961b-81cd-9145-cc24bca86e96`, вложена в «Боткин») — краткая история изменений для всех участников проекта (не только разработчиков).

**Когда обновлять:** когда изменение важно для всех участников проекта, а не только для отдела разработки — сайт, база данных, новая функция, заметный для пользователя баг, изменение в работе бота. Чисто внутренний тулинг/процесс без видимого эффекта (agent-скилы, CI, рефакторинг, метки трекера) сюда НЕ пишем — для него есть `docs/ai_context/AI_CHANGELOG.md`.

**Формат одной записи** (добавлять в начало страницы, перед предыдущими):

```
## ДД месяца ГГГГ — Имя Автора

Короткий заголовок: что изменилось с точки зрения пользователя

Одно-два предложения — что теперь работает иначе или лучше. Без технических деталей.
За подробностями — на GitHub по ссылке ниже.

[PR #NN](https://github.com/botkin-health/Botkin/pull/NN)
```

**Правила:**
- ⚠️ **Имя автора — не хардкодить.** В заголовок ставить имя реального автора изменения, а НЕ того, кто запускает сессию. Проект мультиюзерный — записи разных участников подписываются разными именами.
  - **Как определить автора:** по автору PR / git-коммита (`gh pr view <N> --json author -q .author.login` или `git log -1 --format='%an'`).
  - **Что писать в заголовок:** человекочитаемое ФИО на русском (как в существующих записях), НЕ GitHub-логин и НЕ `git %an`. Соответствие логин → имя:
    - `Igor-Lysk` → **Игорь Лысковский**
    - `Lyskovsky` → **Александр Лысковский**
    - `Alegas` → **Олег Лысковский**
    - для автора не из этого списка — уточнить имя у Игоря, не угадывать.
- Писать для не-разработчиков: что изменилось для пользователя, а не как это устроено внутри
- Не дублировать `docs/ai_context/AI_CHANGELOG.md` — там технические детали, здесь — суть
- Одна запись = один PR или логически связанная группа PR (объединять фиксы одной темы)
- Обновлять через Notion MCP (`mcp__aa7ec113...notion-update-page` или `notion-create-pages` с parent `37bf1efb-961b-81cd-9145-cc24bca86e96`)

## Важные правила

- **Язык**: всегда общаться с пользователем на русском
- **AI_CHANGELOG**: после каждой задачи обновлять `docs/ai_context/AI_CHANGELOG.md`
- **Research-grounded решения — фиксировать durable**: если нетривиальное решение опирается на внешний ресёрч / опыт сообщества / реверс-инжиниринг (особенно интеграции с неофициальными API), оформить **ADR** в `docs/architecture/decisions/` (+ строка в индексе README) и, если есть повторяющиеся грабли — раздел **«Known issues / troubleshooting»** в соответствующем research-доке `docs/researches/`, **со ссылками на источники**. Цель: сторонний разработчик понимает *что делали, почему и на чём основано* без археологии по issue. Issue/PR/коммиты — не замена durable-докам. Пример: [ADR-0005](docs/architecture/decisions/0005-cgm-librelinkup-integration.md) (CGM/LibreLinkUp).
- **Notion Хронолог**: обновлять страницу `37bf1efb-961b-81cd-9145-cc24bca86e96`, когда изменение важно для всех участников проекта (не только отдела разработки) — см. раздел «Хронолог разработки в Notion»
- **Синк перед анализом**: всегда запускать `/sync` перед анализом данных здоровья
- **knowledge_base.json**: при добавлении новых анализов/УЗИ/МРТ/ЭКГ — обновлять этот файл
- **Бэкап**: БД на удалённом сервере, не на localhost. Для записи в БД нужен SSH к серверу.

## Протокол чтения медданных (КРИТИЧНО)

**При ЛЮБОМ вопросе о здоровье любого пользователя — порядок чтения строго такой:**

1. **`PROFILE.md`** в папке человека (`FamilyHealth/{Имя} — Здоровье/PROFILE.md`):
   - **Сначала «🩺 Журнал обследований»** (между маркерами `<!-- EXAM_JOURNAL_START -->...END -->`) — это автогенерируемый индекс **что и когда** обследовалось. Никогда не предлагать сделать обследование, не сверившись с журналом.
   - Затем основная часть — карта диагнозов и хронических состояний.
2. **`knowledge_base.json`** в той же папке — детали из журнала. Секции:
   - `blood_tests`, `urine_tests`, `hormones`, `vitamins`, `genetics` — лабораторные
   - `ultrasound` — УЗИ (все типы: ОБП, почки, простата, щитовидка, ЭхоКГ, БЦА, малый таз, молочные)
   - `medical_records` — **приёмы врачей часто содержат embedded summary с ЭКГ/ЭхоКГ/УЗДГ/ЭГДС/колоноскопией** (например, 2021-03-01 atlas_therapist — там весь комплекс Атласа)
   - `ecg`, `spirometry`, `sports_tests` — функциональные тесты
3. **Только после этого** — читать отдельные PDF/docx для деталей, которых нет в JSON.

**⚠️ Граница «источника истины» — по типу данных (уточнено 12.07.2026):**
- **Живые ежедневные потоки** — питание (`nutrition_log`), БАДы (`supplements_log`), вес/состав тела (`body_measurements`), глюкоза (`glucose_readings`), функциональные замеры (сила хвата и т.п.) — **первичны в БД Botkin (Postgres на сервере), локальный `knowledge_base.json` для них может быть УСТАРЕВШИМ**. По ним читать сначала БД (agent_tools / psql), KB — только бэкап.
- **Курируемые документы** — анализы крови, генетика, УЗИ/МРТ, мед.записи — первичны в `knowledge_base.json` на маке (я их парсю и завожу).
- Направление желаемой эволюции (по решению Александра 12.07): постепенно перейти на работу через Botkin как основную систему, локальную KB держать как бэкап и периодически обновлять снимок из БД (dump DB → JSON). Маркер продублирован первой строкой каждого `knowledge_base.json` (`_source_of_truth`).

**Если добавили новые анализы/УЗИ в `knowledge_base.json` — обязательно регенерировать журнал:**
```bash
python3 scripts/generate_exam_journal.py "Имя — Здоровье" --update-profile
```

**Главные ошибки прошлого** (не повторять!):
- ❌ Игнорировать `medical_records.summary` — там часто весь комплекс приёма (УЗИ + ЭКГ + ЭхоКГ).
- ❌ Цитировать прозу из HEALTH.md/PROFILE.md как «снимок состояния» — она может устаревать. JSON и журнал обследований — единственный источник истины.
- ❌ Предлагать сделать ЭКГ/УЗИ ОБП/колоноскопию, не глянув в журнал — там может быть свежее. **Прецедент 09.05.2026:** AI забыл про УЗИ ОБП в МЕДСИ от 19.04.2026 и предложил сделать его «впервые с 2021».

**Никаких параллельных md-черновиков к обследованиям.** Развёрнутая интерпретация — в `knowledge_base.json` в полях `summary`/`conclusion`/`recommendations`. JSON — единственный источник истины, PDF/docx — архив оригиналов.

## Правила аналитики и отображения данных

- **Факт vs среднее — не путать.** Когда пишешь «было X → стало Y», X и Y должны быть реальными замерами (первый и последний), а не средними за неделю. Средние за неделю — это отдельная метрика, которую нужно явно подписывать «(средняя за неделю)».
- **Не сглаживать молча.** Любая трансформация данных (усреднение, детренд, фильтрация) должна быть явно указана. Пользователь знает свои цифры — если написать 81.7 вместо реальных 82.75, он заметит и потеряет доверие к анализу.
- **Корреляция ≠ причина.** Два показателя могут коррелировать только потому что оба плавно меняются со временем (тренд). Перед выводами всегда проверять: это связь дневных колебаний (детренд) или ложная корреляция трендов? Пример: «вес падает + температура растёт» → r=−0.49, но после детренда r=+0.19 (ноль). Реальная причина потери веса — питание и тренировки, а не весна.
- **Указывать размер выборки.** Корреляция на 12 днях — предварительная гипотеза. На 70+ днях — устойчивый сигнал. Всегда писать (N дней) рядом с r.
- **Не дублировать числа.** Если число уже есть в одной колонке таблицы (например «29 до 21.03»), не повторять его в другой (например «29 тренировок» в статусе).

## Семейное хранилище медицинских данных (FamilyHealth)

**Google Drive — единственный источник истины для медицинских документов всех членов семьи.**

Локальный путь (синхронизируется автоматически):
```
/Users/alexlyskovsky/Library/CloudStorage/GoogleDrive-lyskovsky@gmail.com/Мой диск/FamilyHealth/
```

Короткая ссылка в коде: `GD_HEALTH` (задать через переменную окружения или хардкод).

### Структура

Папки по каждому члену семьи — см. `~/.claude/CLAUDE.md` (имена, возраст, диагнозы там).

### Правила работы с данными семьи

- **НИКОГДА не путать данные разных людей.** Каждый человек = отдельная папка, отдельный knowledge_base.json
- Каждая папка содержит `PROFILE.md` (диагнозы) и `knowledge_base.json` (все обследования)
- knowledge_base.json в корне проекта — данные ТОЛЬКО owner-cohort
- Данные бота (PostgreSQL) — только пользователи бота (см. `~/.claude/CLAUDE.md`)
- Именование файлов: `{тип}_{YYYY-MM-DD}_{лаборатория}_{подтип}.{ext}`

## BotkinClaw — AI-агент (in-process)

AI-врач живёт **внутри** основного aiogram-бота (`@Botkin_md_bot`), не в отдельном контейнере.

- **Точка входа:** `core/agent_chat.py:ask_agent()` — прямой вызов Anthropic Messages API (Claude)
- **История диалога:** таблица `agent_conversations` в Postgres (DDL: `database/migrations/add_agent_chat.sql`)
- **Tools:** 30+ endpoints в `telegram-bot/webhook/agent_tools_api.py` (JWT+RLS изоляция по cohort; актуальный список — `grep '@router\.'`)
- **JWT-контракт:** каждый запрос агента несёт `user_id` + `cohort` — RLS автоматически ограничивает видимость данных
- **Доступ — у всех (#165, 18.06.2026):** разговорный агент работает для **любого** зарегистрированного пользователя. `users.agent_system_prompt` — **опциональный override** (богатая семейная персона из `onboard_family_user.py`), а НЕ гейт. Если он пуст — `ask_agent` использует `build_default_agent_prompt(user)` (лёгкий промпт из `onboarding_data`). Никакого деления на «семью» для доступа к агенту.

Ключевые agent tools: `get_weight_history`, `get_body_measurements`, `get_day_summary`, `get_indoor_air`, `get_outdoor_weather`, `get_user_settings`, `recent_workouts`, `recent_biomarkers`, `phenoage`, `kb_value`, `list_kb_keys`.

Решение принято 21.05.2026 вместо NanoClaw (отдельной контейнерной инфры). Подробнее — [ADR-0002](docs/architecture/decisions/0002-rejecting-nanoclaw-for-simpler-agent.md).

### Медпрофиль пациента: аллергии, хроники, постоянные лекарства

Живёт в `users.onboarding_data` (JSONB). Единый реестр ключей и сплиттер свободного текста — **`core/health/onboarding_lists.py`** (`CONDITION_KEYS`, `ALLERGY_KEYS`, `onboarding_list`, `split_freetext`). Новые читатели/писатели подключать только через него — «куда пишем» должно совпадать с «откуда читаем».

⚠️ **Онбординг-квиз про хроники и лекарства НЕ спрашивает** — шаг убран в `f366c98` («value-first quiz»), legacy-шаг `chronic` ремапится на `artifact`. Не искать этот вопрос в онбординге и не считать, что профиль заполнится сам.

| | Кто | Что делает |
|---|---|---|
| **Пишут** | `/doc` (`telegram-bot/handlers/doc_upload.py`) | диагнозы/аллергии из разобранного документа → `merge_onboarding_lists` |
| | агент, тул `save_health_profile` | со слов пациента в диалоге; ставит флаг `health_profile_asked` |
| **Читают** | `core/agent_chat.py::_health_profile_block` | блок «Медпрофиль» в системном промпте (+ курение из `users.smoking_status`) |
| | `core/agent_chat.py::_health_profile_ask_block` | инструкция спросить один раз, если профиль пуст и флага нет |
| | `/meal_context` (`webhook/agent_tools_api.py`) | `constraints` — KB-файл приоритетнее, затем `onboarding_data`; источник в `constraints_source` |
| | `services/doctor_report.py` | «проблемы»/аллергии/лекарства в отчёте для врача |

Курение — отдельная колонка `users.smoking_status` (`never`/`former`/`current`/`occasional`), не в `onboarding_data`. Её читают промпт-блок, дашборд и `/user_profile`.

История: #340 (курение в промпт, fallback `/meal_context`, `save_health_profile`), #7/#309 (сплиттер: запятая не разделитель пунктов).

---

## Архитектура проекта (код)

```
Botkin/                          # ~/Botkin/ — ТОЛЬКО КОД И OPERATIONAL DATA
├── config/                      # Настройки, пользователи
├── core/                        # Бизнес-логика (LLM, питание, парсеры)
├── database/                    # SQLAlchemy модели, CRUD, миграции
├── domain/                      # Доменные модели
├── services/                    # Сервисный слой
├── telegram-bot/                # Aiogram бот (handlers, middlewares)
├── scripts/                     # Импорт данных, анализ, бэкфилл
├── tests/                       # Pytest (unit + LLM prompt тесты)
├── data/                        # Operational data (JSON, CSV, кэш, медиа бота)
│   ├── garmin/                  # Garmin API данные (шаги, сон, HRV)
│   ├── nutrition/               # JSON-логи питания
│   ├── media/                   # Фото еды, голосовые
│   ├── cache/                   # Токены OAuth
│   └── ...                      # НЕ медицинские документы (те на Google Drive)
├── knowledge_base.json          # KB владельца (источник истины для его анализов)
├── docs/                        # Документация + ai_context/
└── archive/                     # Архив старого кода
```

---

## Agent skills

Per-repo config consumed by the Matt Pocock engineering skills (`to-issues`, `triage`, `qa`, `review`, `tdd`, `improve-codebase-architecture`, etc.).

### Issue tracker

Issues live in the `botkin-health/Botkin` GitHub Issues, managed via the `gh` CLI. See `docs/agents/issue-tracker.md`.

### Triage labels

Canonical triage vocabulary (`needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`) — defaults, label strings equal role names. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context repo: one `CONTEXT.md` (lazy) + ADRs at `docs/architecture/decisions/`. See `docs/agents/domain.md`.

---

[← Документация Botkin — Index](docs/INDEX.md)
