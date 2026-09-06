# 02 · Реестр источников данных

> **Last verified:** 2026-09-06 (после добавления BotkinClaw, MCP-коннектора, CGM и режима план→факт — добавлены источники CGM/LibreLinkUp, Withings, Whoop, Android Health Connect)
>
> ⚠️ Даты образцов/цифр покрытия в этом файле частично унаследованы из более ранних ревью (видны по разбросу дат «последнее измерение» ниже) — они иллюстрируют **формат** SOP, а не текущий снимок. Актуальные цифры покрытия — смотреть в `/dashboard` skill, не здесь.

В этом файле описаны все интеграции Botkin, методы получения данных, статус актуальности и SOP «как подтянуть свежие данные».

> [!IMPORTANT]
> **Канонические имена полей** (часто путаются):
> - `nutrition_log.totals` → `calories`, `protein`, `fats` (мн.ч.!), `carbs`, `fiber`, `drinks`. Поля `fat` нет.
> - `weights` → `weight`, `body_fat`, `muscle_mass`, `water`, `bmi`. Полей `weight_kg`, `fat_percent`, `muscle_kg` нет — это старая дока врала.
> - `supplements_log` → `supplement_name` (не `name`), `time`, `date`.
> - FK на пользователя — всегда `users.telegram_id` (не `users.id`).
> Подробности → `03_database_schema.md`.

---

## 🤖 Шпаргалка для ИИ: откуда брать данные при анализе

> [!WARNING]
> **Перед любым анализом данных** — сначала прочитай эту таблицу. Ошибочное чтение из локальных файлов вместо PostgreSQL (или наоборот) даёт неверные результаты.

| Метрика | Источник | Как читать |
|---|---|---|
| 🍽️ **Питание** (ккал, белки, жиры, углеводы, план vs факт) | **PostgreSQL** `nutrition_log` (`status` = `eaten`/`plan`) | SSH → psql (см. SQL ниже) или agent tool `recent_meals`/`meal_context` |
| ⚖️ **Вес** (kg, % жира, мышцы, вода, кости, висцеральный жир) | **PostgreSQL** `weights` (HAE/Zepp/**Withings**/ручные — см. §9) | SSH → psql или agent tool `weight_history` |
| 💊 **Добавки** | **PostgreSQL** `supplements_log` | SSH → psql |
| 🩺 **АД** (систола, диастола, пульс) | **PostgreSQL** `blood_pressure_logs` (Omron → HAE, ежедневно автоматически) | SSH → psql или agent tool `recent_bp`. Локальный `data/apple_health_blood_pressure.json` — только исторический архив до перехода на HAE, не читается автоматически. |
| 🩸 **Глюкоза (CGM)** | **PostgreSQL** `glucose_readings` (LibreLinkUp, ночной синк + on-demand refresh) | agent tool `recent_glucose`/`glucose_stats` — см. §16b |
| 👣 **Шаги** (ежедневные) | Локально: `data/apple_health_steps_daily.json` | `json.load()` |
| 💤 **Сон** (длительность, фазы, HRV ночи) | Локально: `data/garmin/sleep/YYYY-MM-DD.json` | glob по дням |
| ❤️ **ЧСС покоя, стресс, BB** | Локально: `data/garmin/daily-summary/YYYY-MM-DD.json` | glob по дням, поле `data['stats']` |
| 📊 **HRV** (weekly/lastNight) | Локально: `data/garmin/hrv/YYYY-MM-DD.json` | glob по дням, поле `data['hrvSummary']` |
| 🔋 **Body Battery** (charged/drained) | Локально: `data/garmin/body-battery/YYYY-MM-DD.json` | glob, `data[0]['charged']` |
| 🌬️ **CO₂, температура, влажность** | Локально: `data/environment/netatmo_history.json` | unix-ts ключи |
| 📱 **Экранное время iPhone** | Локально: `data/activities/iphone_screentime_perapp.json` | `{date: {total_minutes: N}}` |
| 💻 **Экранное время Mac** | Локально: `data/activities/mac_screentime_perapp.json` | `{date: {total_minutes: N}}` |
| 🌐 **Chrome история** | Локально: `data/activities/chrome_history.json` | `{date: {total_visits: N}}` |
| 🚶 **Характеристики ходьбы** | Локально: `data/activities/gait_metrics.json` | `{date: {walkingSpeed, stepLength, ...}}` |
| 🩸 **Анализы крови** | Локально: PDF в `data/blood-tests/` + `reports/COMPLETE_MEDICAL_DATA.md` | Читать MD-файл |

### ⚡ Быстрый SQL для питания

```python
# Питание из PostgreSQL через SSH — ЕДИНСТВЕННЫЙ правильный способ
import subprocess, json

result = subprocess.run([
    'ssh', 'root@116.203.213.137',
    'docker exec healthvault_postgres psql -U healthvault -d healthvault '
    '--no-align --field-separator=, --tuples-only -c "'
    'SELECT date, '
    'ROUND(SUM((totals->>\'\'calories\'\')::numeric),0) as kcal, '
    'ROUND(SUM((totals->>\'\'protein\'\')::numeric),1) as protein, '
    'ROUND(SUM((totals->>\'\'fats\'\')::numeric),1) as fats, '
    'ROUND(SUM((totals->>\'\'carbs\'\')::numeric),1) as carbs '
    'FROM nutrition_log '
    'WHERE date >= \'\'2026-01-06\'\' AND user_id = 895655 '
    'GROUP BY date ORDER BY date;"'
], capture_output=True, text=True)
```

Или напрямую через bash:
```bash
ssh root@116.203.213.137 "docker exec healthvault_postgres psql -U healthvault -d healthvault -c \"
SELECT date,
  ROUND(SUM((totals->>'calories')::numeric), 0) as kcal,
  ROUND(SUM((totals->>'protein')::numeric), 1) as protein,
  ROUND(SUM((totals->>'fats')::numeric), 1) as fats,
  ROUND(SUM((totals->>'carbs')::numeric), 1) as carbs,
  ROUND(SUM(COALESCE((totals->>'fiber')::numeric, 0)), 1) as fiber
FROM nutrition_log
WHERE date >= '2026-01-06' AND user_id = 895655
GROUP BY date ORDER BY date;\""
```

### ⚡ Быстрый SQL для веса

```bash
ssh root@116.203.213.137 "docker exec healthvault_postgres psql -U healthvault -d healthvault -c \"
SELECT measured_at::date as date,
       AVG(weight) as weight_kg,
       AVG(body_fat) as body_fat_pct,
       AVG(muscle_mass) as muscle_kg
FROM weights WHERE user_id = 895655
GROUP BY date ORDER BY date;\""
```

### ⚠️ Критические ошибки которые нельзя повторять

- ❌ **НЕ ищи локальные JSON-файлы с питанием** — их не существует. Питание только в PostgreSQL.
- ❌ **НЕ ищи `nutrition_log.calories`** — КБЖУ хранится в JSONB поле `totals->>'calories'`, не как отдельная колонка.
- ❌ **НЕ читай `activity_log` для HRV** — поле `hrv` там NULL. HRV только в `data/garmin/hrv/*.json`.
- ❌ **НЕ читай `sleep_records`** — таблица пустая. Сон только в `data/garmin/sleep/*.json`.
- ❌ **НЕ читай `workouts`** — таблица пустая. Тренировки только в `data/garmin/activities/*.json`.

---

> [!IMPORTANT]
> **ЕДИНАЯ ТОЧКА ВХОДА:** Чтобы гарантировать 100% актуальность всех потоков данных при начале аналитической сессии, **используй скилл `/sync`** или запусти мастер-скрипт вручную:
> `bash scripts/sync_all_data.sh`
> Скрипт: (1) синхронизирует БД с удалённого сервера (питание, добавки, вес), (2) скачивает свежие данные Гармина, (3) обновляет Netatmo климат, (4) обновляет экранное время iPhone/Mac/Chrome, (5) обновляет погоду.
>
> **⚠️ НЕ АВТОМАТИЗИРОВАННЫЕ ШАГИ (требуют ручного действия):**
> 1. **Apple Health** (вес, АД, пульс, шаги, ходьба): iPhone → Здоровье → Профиль → Экспортировать данные → AirDrop на Mac → разархивировать → `python3 scripts/import_apple_health.py`
> 2. **iPhone Screen Time актуальный**: если нужны данные за сегодня — запустить вручную `~/.local/bin/aw-import-screentime events import --device D2727389-2B2E-4E31-88FE-7BF0E925C580 && python3 scripts/import_activitywatch.py`
> 3. **Zepp состав тела**: `python3 scripts/import_zepp_api.py` (через CN3 → Hetzner). Если токен истёк → `--reauth`.

> [!NOTE]
> **Данные Гармина — два слоя:**
> - **PostgreSQL `activity_log`** — шаги, калории, ЧСС, стресс, часы сна.
>   - **Сегодняшние данные**: обновляются автоматически при каждом `/day` в боте — бот вызывает Garmin API в реальном времени. Кеш 15 минут (повторный `/day` берёт из БД без нового запроса к Garmin). Garth-токены сохраняются в `/app/data/garth/{user_id}/` — повторный логин по паролю не нужен.
>   - **Исторические данные**: пушатся с Mac через `scripts/push_garmin_to_db.py` при запуске `/sync`.
>   - При ошибке Garmin API в `/day` показывается `⚠️ Garmin недоступен` вместо 0.
> - **Локальные JSON файлы** (`data/garmin/`) — фазы сна, HRV, Body Battery, детали тренировок. Обновляются скриптом `download_garmin_data.py` (запускается через `sync_all_data.sh`).
> ⚠️ `activity_log.hrv` всегда NULL — HRV доступен только из JSON. Таблицы `sleep_records` и `workouts` в PostgreSQL пустые.

---

## 1. 🩸 Медицинские анализы
* **Данные**: Холестерин и фракции (ЛПНП, ЛПВП, ТГ), витамины (D, B12, ферритин), гормоны (тестостерон, ГСПГ, кортизол), HbA1c, общий белок.
* **Канал**: PDF-файлы лабораторий (Invitro, ЕМИАС, CMD) в `data/blood-tests/` и `data/hormones/`. Сводный отчёт — `reports/COMPLETE_MEDICAL_DATA.md`.
* **ВАЖНО**: Таблица `blood_tests` в PostgreSQL **пустая** (данные живут только в PDF + COMPLETE_MEDICAL_DATA.md).
* **Актуальность**: 14 срезов с 2014 по 2026 год. Последний анализ: **7 января 2026** (Invitro): холестерин 5.86, ЛПНП 3.79 (↑), тестостерон 12.09 (нижняя граница ISSAM), витамин D 33.5, HbA1c 5.2, ферритин 41.7 (↑).
* 💡 **Статус**: ⚠️ ТРЕБУЕТ ОБНОВЛЕНИЯ (план: раз в 3 месяца, следующий ~апрель 2026).
* 🛠 **Инструкция**: Пользователь сдаёт биохимию и загружает PDF в чат бота или кладёт в `data/blood-tests/`. ИИ парсит PDF и обновляет `COMPLETE_MEDICAL_DATA.md`.

---

## 2. 💤 Сон
* **Данные (PostgreSQL)**: Длительность сна (sleep_hours) — в `activity_log`. Покрытие: 98.2% дней с 6 января.
* **Данные (JSON)**: Фазы сна (глубокий/REM/лёгкий), дыхание во сне (avg_respiration), SpO2, время засыпания/пробуждения — в `data/garmin/sleep/`.
* **ВАЖНО**: Таблица `sleep_records` в PostgreSQL **пустая**. Фазы сна — только в локальных JSON.
* **Актуальность**: PostgreSQL — **9 марта 2026** ✅. JSON файлы — **1 марта 2026** (отстают на 8 дней, обновляются через `/sync`).
* 📊 **Базовая линия дыхания**: 12.07 ± 0.84 вд/мин (54 дня, янв–март 2026). Значение ≥14 = аномалия (болезнь/алкоголь/стресс).
* 💡 **Статус**: ✅ (длительность) / ⚠️ (фазы — JSON могут отставать).
* 🛠 **Как обновить**: `/sync` или `python3 scripts/garmin/download_garmin_data.py`.

---

## 3. 🏋️‍♂️ Тренировки и Активность
* **Данные (PostgreSQL)**: Шаги, активные калории, суммарные калории, расстояние — в `activity_log`. Покрытие: с 5 января 2025 по 9 марта 2026.
* **Данные (JSON)**: Детали тренировок (тип, ЧСС зоны, лапы, темп) — в `data/garmin/activities/`. 51 тренировка всего, 24 с 6 января, последняя **28 февраля 2026**.
* **ВАЖНО**: Таблица `workouts` в PostgreSQL **пустая**. Детали тренировок — только в локальных JSON.
* **Актуальность**: PostgreSQL — **9 марта 2026** ✅. JSON активности — **28 февраля 2026** (обновляются через `/sync`).
* 💡 **Статус**: ✅ (ежедневная активность) / ⚠️ (детали тренировок — JSON).
* 🛠 **Как обновить**: `/sync` или `python3 scripts/garmin/download_garmin_data.py`.

---

## 4. ❤️ Пульс, Стресс, ВСР (HRV), Body Battery
* **Данные**:
  - **Пульс**: Apple Health → `data/apple_health_heart_rate.json`. 2 037 измерений, последнее **9 марта 2026** ✅.
  - **Стресс**: PostgreSQL `activity_log.stress_level` — ✅ **9 марта 2026**.
  - **HRV**: Только в `data/garmin/hrv/` JSON — **1 марта 2026**. ⚠️ `activity_log.hrv` = NULL (не заполняется).
  - **Body Battery**: Только в `data/garmin/body-battery/` JSON — **1 марта 2026**.
* **Актуальность**: Пульс и стресс — ✅ актуальны. HRV и Body Battery — ⚠️ JSON могут отставать.
* 💡 **Статус**: ✅ (пульс, стресс) / ⚠️ (HRV, Body Battery — только JSON).
* 🛠 **Как обновить пульс**: Экспорт Apple Health → `workflows/apple-health-import.md`. HRV/BB — `/sync`.

---

## 5. 🩺 Артериальное давление (АД)
* **Данные**: Систолическое/диастолическое давление, пульс при измерении (Omron).
* **Канал (основной, автоматический)**: Health Auto Export (iOS) → `POST /apple_health_v2` → таблица PostgreSQL `blood_pressure_logs`, ежедневно без участия пользователя. Читает агент (`recent_bp` tool) и `/day`.
* **Канал (архивный)**: `data/apple_health_blood_pressure.json` — история из ручных XML-экспортов Apple Health до перехода на HAE; НЕ обновляется автоматически, не путать с актуальными данными.
* 💡 **Статус**: ✅ автоматизировано через HAE.
* 🛠 **Как обновить**: ничего делать не нужно — HAE шлёт раз в сутки сам. Проверить руками: agent tool `recent_bp` или `psql -c "SELECT * FROM blood_pressure_logs WHERE user_id=X ORDER BY measured_at DESC LIMIT 5"`.

---

## 5b. 👣 Шаги (ежедневные)
* **Данные**: Суммарное количество шагов в день (объединяет данные Garmin + iPhone).
* **Канал**: Apple Health → `data/apple_health_steps_daily.json`. 4063 дня истории (с 2015).
* **ВАЖНО**: Этот файл точнее, чем `activity_log.steps` в PostgreSQL, т.к. суммирует **оба источника** (Garmin Watch + iPhone). PostgreSQL содержит только данные Garmin Watch.
* **ВАЖНО**: Apple Health НЕ дедуплицирует. Garmin + iPhone + Zepp Life суммировались бы. Скрипт берёт только **Garmin (Connect)** как единственный надёжный источник, fallback на iPhone если нет данных Garmin.
* **Актуальность**: **9 марта 2026** ✅. Среднее с 6 января: **7,489 шагов/день** (63 дня, покрытие 100%). Диапазон: 1,960–13,841 шагов/день.
* 💡 **Статус**: ✅
* 🛠 **Как обновить**: Ручной экспорт Apple Health → `python3 scripts/import_apple_health.py` (автопоиск export.xml в ~/Downloads/).

---

## 5c. 🚶 Характеристики ходьбы (Gait)
* **Данные**: Скорость ходьбы (km/h), длина шага (cm), двойная опора (%), асимметрия (%). Пассивно измеряется iPhone при ходьбе.
* **Канал**: Apple Health → `data/apple_health_gait.json`. 1987 дней истории (с 2020).
* **Биохакерское значение**:
  - **Двойная опора (double_support_pct)**: норма 25-30%. Рост >30% → усталость/болезнь/недовосстановление.
  - **Асимметрия (asymmetry_pct)**: норма <4%. Рост >5% → дисбаланс, риск травмы.
  - **Скорость ходьбы**: косвенный показатель уровня энергии (снижается при болезни/стрессе).
* **Актуальность**: **9 марта 2026** ✅. 63 дня с 6 января. Текущие: 4.94 км/ч, опора 26.8%, асимм 0%.
* 💡 **Статус**: ✅
* 🛠 **Как обновить**: Ручной экспорт Apple Health → `python3 scripts/import_apple_health.py`.

---

## 6. 🥗 Питание
* **Данные**: Приёмы пищи (время), КБЖУ, названия продуктов, клетчатка.
* **Канал**: PostgreSQL `nutrition_log` на сервере Hetzner. 318 записей, **6 января — 9 марта 2026** (покрытие 98.2%).
* **Актуальность**: **9 марта 2026** ✅.
* 💡 **Статус**: ✅
* 🛠 **Как обновить**: Данные вводятся через Telegram-бот в реальном времени. `/sync` синхронизирует локальный дамп.

---

## 7. 💊 Витамины и Добавки
* **Данные**: Приёмы витаминов и добавок (Омега 3-6-9, Псиллиум, Витамин D3, Магний, Стеролы и др.).
* **Канал**: PostgreSQL `supplements_log` на сервере Hetzner. 263 записи, **11 января — 9 марта 2026** (покрытие ~80%).
* **Актуальность**: **9 марта 2026** ✅.
* 💡 **Статус**: ✅
* 🛠 **Как обновить**: Данные вводятся через Telegram-бот. `/sync` синхронизирует локальный дамп.

---

## 8. 🍷 Алкоголь
* **Данные**: Флаг употребления алкоголя (день/нет), тип напитка.
* **Канал**: Вычисляется «на лету» из `nutrition_log` — поиск по ключевым словам (вино, пиво, ром и т.д.).
* **Актуальность**: **9 марта 2026** ✅ (синхронно с питанием).
* 💡 **Статус**: ✅
* 🛠 **Как обновить**: Специальных действий не требуется — метрика извлекается при анализе.

---

## 9. ⚖️ Вес и Состав тела
* **Данные**: Вес, процент жира, мышечная масса, % воды, висцеральный жир, ИМТ, масса костей, пульс покоя (Withings), BMR по составу тела (Withings).
* **Каналы**:
  - **PostgreSQL `weights`** — единая таблица для всех каналов (Zepp/Withings/HAE/ручные), различаются полем `source`.
  - **`data/zepp_export_latest.csv`** — прямой CSV с Zepp API (`scripts/import/zepp_api.py` через CN3 → Hetzner).
  - **Withings** (весы Body Smart) — полный состав тела (мышцы/вода/кости/висцеральный жир), которых нет в HealthKit (см. §9c).
  - **`data/apple_health_weight_daily.json`** — только вес, без состава. Синхронизируется через Apple Health → ручной экспорт (историческая линия, редко обновляется).
* **Единый канал записи для внешних импортёров**: `POST /api/agent/log_body_composition` (PAT+JWT, scope `rw`) — `user_id` берётся из токена, RLS изолирует данные, доступ к прод-серверу/суперюзер Postgres не нужен. `measured_at` обязан нести UTC-офсет (ключ идемпотентности), `source` — имя канала. Заменяет старую схему «ssh + `docker exec psql`», которая требовала членства в docker-группе (= root на хосте).
* 🛠 **Как обновить**:
  - *Вес:* Экспорт Apple Health → `python3 scripts/import/apple_health.py`. Zepp → iPhone → Apple Health автоматически.
  - *Состав тела (Zepp):* `python3 scripts/import/zepp_api.py` (автоматически, в /sync). Если токен истёк → `--reauth`.
  - *Полный состав тела (Withings):* `python3 scripts/import/withings_api.py --user <tg_id> --push-api --min-weight <кг>` (пишет через `log_body_composition`).

## 9b. ⚖️ Умные весы Zepp — полная интеграция (ВОССТАНОВЛЕНА 15.03.2026)

### Архитектура
* **Устройство**: Mi Body Composition Scale 2 → Zepp Life (iPhone) → Zepp Cloud
* **Сервер данных**: `api-mifit-cn3.zepp.com` (Китай, IP 1.14.224.102)
* **ВАЖНО**: CN3 сервер НЕ доступен напрямую из-за VPN (Россия/Европа/Италия). Запросы идут через **Hetzner SSH-прокси** (116.203.213.137, Германия).
* **Скрипт**: `scripts/import_zepp_api.py` — полноценная замена scaleconnect
* **Данные**: вес, BMI, % жира, вода, кости, мышцы, метаболический возраст, висцеральный жир, базовый метаболизм, score тела, протеин

### Аутентификация (OAuth через браузер)
Токен живёт ~5-7 дней. Когда истекает — нужна ручная переавторизация:
1. Открыть в браузере: `https://account.xiaomi.com/oauth2/authorize?skip_confirm=false&client_id=2882303761517383915&pt=0&scope=1+6000+16001+20000&redirect_uri=https%3A%2F%2Fhm.xiaomi.com%2Fwatch.do&_locale=en_US&response_type=code`
2. Залогиниться Xiaomi аккаунтом (lyskovsky@gmail.com)
3. Скопировать URL после редиректа (hm.xiaomi.com/watch.do?code=XXX)
4. `python3 scripts/import_zepp_api.py --reauth` → вставить URL
5. Код обменивается на `app_token` через `account.huami.com/v2/client/login`
6. Токен кэшируется в `tools/scaleconnect/scaleconnect.json`

### Почему scaleconnect v0.4.1 сломался
1. Токен истёк (~5 дней жизни)
2. `account.xiaomi.com/pass/serviceLogin` стал возвращать HTML вместо JSON (антибот-защита Xiaomi)
3. scaleconnect не умеет OAuth через браузер → упал с `Get "": unsupported protocol scheme ""`

### API запросы (через Hetzner)
```bash
# Получить записи веса за последние 30 дней
sshpass -p 'PASSWORD' ssh root@116.203.213.137 \
  "curl -sS -H 'apptoken: TOKEN' -H 'userid: USER_ID' \
  'https://api-mifit-cn3.zepp.com/users/USER_ID/members/-1/weightRecords?from_date=YYYY-MM-DD&to_date=YYYY-MM-DD'"
```

### Использование
```bash
# Обычный синк (при /sync):
python3 scripts/import_zepp_api.py

# Полный бэкфил за год:
python3 scripts/import_zepp_api.py --days 365

# Переавторизация (когда токен истёк):
python3 scripts/import_zepp_api.py --reauth
```

---

## 9c. ⚖️ Весы Withings (Body Smart) — полный состав тела

* **Зачем нужен ещё один канал весов**: в **HealthKit нет типов** для мышечной массы, воды, костной массы, висцерального жира — через Health Auto Export доходят только вес, % жира и безжировая масса. Withings API отдаёт всё целиком напрямую, без прогонки через Apple Health.
* **Устройство**: весы Withings Body Smart → Withings API (OAuth).
* **Скрипт**: `scripts/import/withings_api.py --user <telegram_id> --push-api --min-weight <кг>` — пишет через `POST /api/agent/log_body_composition` с `BOTKIN_PAT` (не нужен SSH-доступ к серверу, см. §9 выше).
* **Дополнительные поля**: `heart_rate` (пульс стоя натощак — по сути пульс покоя; вскрыл фоновую тахикардию, которая по данным часов выглядела эпизодической), `bmr_kcal` (точнее оценки Apple по возрасту/весу), `fat_mass_kg`, `lean_mass_kg`.
* **Апсерт с COALESCE** — канал HAE не перетирается, если Withings синкнулся позже.
* **Креды**: `WITHINGS_CLIENT_ID/SECRET/REFRESH_TOKEN` в `.env`; refresh-токен **ротируется** при каждом использовании → кэш в `data/cache/withings_tokens.json`.
* 🛠 **Как обновить**: `python3 scripts/import/withings_api.py --user <tg_id> --push-api --min-weight <кг>` (обычно вручную/по cron, не часть общего `/sync`).

---

## 10. 📏 Замеры тела (сантиметром)
* **Данные**: Талия, шея, бёдра, грудь, бицепс, бедро.
* **Канал**: Ручной ввод в `data/weights/body_measurements.json`.
* **Актуальность**: 4 записи: 2026-01-08, 2026-02-01, 2026-02-10, **2026-03-01** ✅.
* 💡 **Статус**: ✅
* 🛠 **Как обновить**: Сделать замеры лентой и попросить ИИ добавить запись в `data/weights/body_measurements.json`.

---

## 11. 🌬️ Климат спальни (Netatmo)
* **Данные**: Температура, влажность, CO2, шум (dB). Только станция «Большевик».
* **Канал**: `data/environment/netatmo_history.json`. 60 суточных записей.
* **Актуальность**: **2026-01-09 → 2026-03-09** ✅. Текущие: ~21.6°C, CO2 ~1051 ppm.
* 💡 **Статус**: ✅
* 🛠 **Как обновить**: `/sync` или `python3 scripts/import_netatmo.py`.

---

## 12. 📱 Экранное время (iPhone, по приложениям)
* **Данные**: Время использования каждого приложения по категориям (соцсети, продуктивность, развлечения и т.д.).
* **Канал**: `data/activities/iphone_screentime_perapp.json`. 29 дней. Источник: Biome/ActivityWatch (`aw-import-screentime`).
* **Актуальность**: **2026-02-09 → 2026-03-09** ✅.
* 💡 **Статус**: ✅ (LaunchAgent запускается автоматически в 8:00 каждый день).
* 🛠 **Как обновить**: Автоматически или `/sync`. Скрипт: `python3 scripts/import_activitywatch.py`.

---

## 13. 💻 Экранное время (Mac, по приложениям)
* **Данные**: Время активного использования каждого приложения на MacBook.
* **Канал**: `data/activities/mac_screentime_perapp.json`. 10 дней. Источник: `macOS knowledgeC.db` (системная БД Screen Time).
* **Актуальность**: **2026-02-28 → 2026-03-09** ✅.
* 💡 **Статус**: ✅ (LaunchAgent 8:00). ⚠️ Данные хранятся в ОС ~11 дней — пропуск в синке = потеря истории.
* 🛠 **Как обновить**: Автоматически или `/sync`. Скрипт: `python3 scripts/import_mac_screentime.py`. Требует Full Disk Access у Terminal.

---

## 14. 🌐 История браузера (Chrome) — выведен из эксплуатации
* **Статус**: ⛔ канал закрыт; скрипт `chrome_history.py` убран в `archive/` при аудите 2026-06.
* **Исторические данные**: `data/activities/chrome_history.json` (2025-12-10 → 2026-03-09, 26 383 визита) — остаются читаемыми для ретро-анализа.

---

## 15. 🧬 Генетика
* **Данные**: Генетические предрасположенности (риски по здоровью, происхождение, носительство).
* **Канал**: PDF-файлы в `data/genetics/`. 2 документа: Atlas Biomed (2009), дополнительный тест (2016).
* **Актуальность**: Архивные данные. Не обновляются.
* 💡 **Статус**: ✅ (статичные данные).
* 🛠 **Как обновить**: Не требуется. При новом тестировании — загрузить PDF в `data/genetics/`.

---

## 16. 🩸 Анализы крови / мочи / гормоны / витамины (CMD, Invitro, Helix, КДЛ)
* **Данные**: Все лабораторные показатели — биохимия, липиды, гормоны, витамины, ПСА, ОАМ. По всей семье (per-user KB-файлы).
* **Источник истины**: `~/FamilyHealth/<Имя>/knowledge_base.json` **на маке** (приватные мед-документы не хранятся вне локалки в исходном виде). PDF-оригиналы лежат рядом.
* **2-source pipeline + read-time канонизация** (унифицировано 01.06.2026, см. `core/health/kb_schema.py`; заменяет прежнюю схему с 3 разными форматами ключей):
  1. **PostgreSQL `blood_tests`** (сырые `values`) — читает **дашборд** (`dashboard_generator._load_biomarkers_from_db` → `aggregate_biomarkers`) **и агент** (`/recent_biomarkers`, `/phenoage`). Канонизация ключей/единиц — на лету, через `kb_schema.to_canonical` (единый реестр алиасов + US→метрика конверсия по признаку `_unit_system`). Дашборд **не читает** отдельный flat-JSON файл — эта промежуточная стадия удалена (legacy-fallback `BOTKIN_LEGACY_BIOMARKERS_JSON` убран 11.06.2026, флаг нигде не включался).
  2. `/app/data/kb/kb_<id>.json` (bind-mount) — читает агент (`/kb_value`, `/list_kb_keys`). Целый KB как есть.
* **Третий писатель — `/doc` в боте (#281, см. §17 ниже)** — пользователь сам грузит анализ через `/doc`, пишет прямо в `blood_tests`, минуя мак. Эти данные **не попадают обратно** в локальный `knowledge_base.json` автоматически.
* 💡 **Статус**: ✅ автоматизировано.
* 🛠 **Как обновить (после правки локального KB на маке)**: одна команда для любого юзера — **`python3 scripts/sync_user_health.py --user <telegram_id> --apply`** (или `--all`). Две идемпотентные стадии: KB → bind-mount `kb_<id>.json`, KB → Postgres `blood_tests`. Маппинг `telegram_id → папка` — `config/users.py::KB_USERS`.

---

## 16b. 🩸 Глюкоза непрерывного мониторинга (CGM, LibreLinkUp, #96)

* **Данные**: Точки глюкозы каждые ~5-15 мин (ммоль/л), стрелка тренда.
* **Устройство**: Abbott FreeStyle Libre 3 → приложение FreeStyle LibreLink → LibreLinkUp (follower API, неофициальный, реверс-инжинирен сообществом).
* **Подключение (`/connect_cgm` в боте)** — два пути:
  - **Путь A (регион EU)**: пригласить в LibreLink сервисный follower-аккаунт `dr@botkin.health` — бот сам находит новый `patient_id` (поллинг 30с до 10 мин) и мапит на `telegram_id` в таблицу `cgm_connections`.
  - **Путь B (свой follower-аккаунт, #381)** — обязателен для НЕ-EU регионов (LibreLinkUp приглашения работают только внутри одного региона Abbott). Пользователь заводит собственный follower-аккаунт под свой регион, вводит креды в бота (пароль удаляется из чата немедленно, логин валидируется live ДО сохранения). Креды хранятся зашифрованными в `cgm_followers.password_enc`. Rate-limit на попытки логина (5/15 мин) — регион-специфичный анти-бан на стороне Abbott реагирует на частые неудачные попытки блокировкой Cloudflare 476 для всего региона (см. `core/health/glucose_runtime.py`).
* **Канал**: `scripts/import/librelinkup.py` — ночной синк по cron (`scripts/server/sync_all.sh`) для всех подключённых пользователей + follower-аккаунтов; хранит канонический UTC-timestamp, конвертирует mg/dL→mmol/L.
* **On-demand refresh**: agent tool `/recent_glucose` дёргает live-обновление перед ответом (10с таймаут), но не блокируется — при ошибке/кулдауне отдаёт то, что уже есть в БД.
* **Метрики**: `core/health/glucose_stats.py` — Time-in-Range (ADA/EASD пороги 3.9–10.0 ммоль/л), avg/min/max/std. Свежесть данных (`glucose_staleness`, порог 30 мин) — только для формулировки ответа, не фильтрует данные.
* **Хранилище**: PostgreSQL `glucose_readings`. **Не показывается на публичном дашборде** — доступно только через агент (BotkinClaw/MCP), см. `03_database_schema.md` §12.
* 💡 **Статус**: ✅ автоматизировано (ночной синк) + on-demand refresh.
* 🛠 **Как подключить**: `/connect_cgm` в боте. Дизайн — [ADR-0005](../architecture/decisions/0005-cgm-librelinkup-integration.md), грабли региона — `docs/researches/2026-06-14-cgm-librelinkup-integration.md`.

---

## 17. 📄 Пользовательская загрузка анализов (`/doc`, #281)

* **Данные**: Тот же тип, что и §16 (лабораторные показатели), но вводится самим пользователем через Telegram, не Александром на маке. Также принимает нелабораторные документы (УЗИ, заключения врача) — они архивируются, но не парсятся в биомаркеры.
* **Канал**: `/doc` → фото/PDF → `core/health/doc_extractor.py` (LLM-извлечение, Anthropic `claude-haiku-4-5`) → превью с подсветкой новых аллергий/диагнозов → «Сохранить» → `handlers/doc_upload.py:_save_to_blood_tests` → `core/health/doc_to_blood_test.py` → `database.crud.upsert_blood_test`.
* **Идемпотентность**: ключ `(user_id, test_date, test_type)`, где `test_type = "<лаборатория> · <8hex-хэш-файла>"` — повторная загрузка того же файла обновляет ту же строку.
* **Нелабораторные документы** (УЗИ, заключения) — не проходят гейт `to_canonical`, в `blood_tests` не попадают, остаются только в `documents[]` внутри `/app/data/kb/kb_<id>.json`.
* **Документ без даты** — строка не создаётся (дата не выдумывается).
* ⚠️ **Следствие**: данные от `/doc` живут только на сервере. `sync_user_health.py` их не затирает (upsert без DELETE), но и не подтягивает обратно в локальный `knowledge_base.json` на маке — если нужно свести, переносить руками.
* 💡 **Статус**: ✅ в проде.

---

## 18. ⌚ Whoop (носимое, мультиюзерное подключение)

* **Данные**: Восстановление, нагрузка, сон — через собственный OAuth Whoop.
* **Канал**: `/whoop` в боте → `webhook/whoop_oauth.py` (`GET /whoop/connect?uid=...`, `GET /whoop/callback`) → OAuth 2.0 обмен на токены → `data/cache/whoop_tokens.json` (ключ `whoop:<telegram_id>`), переживает рестарт (bind-mount). Импорт данных — `scripts/import/whoop_api.py`.
* **Ограничение**: один Whoop developer-app обслуживает до 10 пользователей без approval (development mode) — дальше нужна заявка Whoop на approval.
* 💡 **Статус**: подключение работает для мультиюзера; per-user данные зависят от того, подключил ли пользователь свой Whoop.

---

## 19. 🤖 Android Health Connect

* **Данные**: То же покрытие, что и Apple Health (шаги, пульс, сон и т.п.), но с Android-устройств.
* **Канал**: приложение [mcnaveen/health-connect-webhook](https://github.com/mcnaveen/health-connect-webhook) (APK) → `POST /android_health_v1` (Bearer `users.health_token`, та же таблица токенов, что у Apple Health).
* **Ключевое отличие от HAE**: Health Connect шлёт сырые записи с временными метками (`{count, start_time, end_time}`), не готовые дневные агрегаты — агрегация по дням делается на сервере, в таймзоне пользователя (`users.timezone`), иначе записи после 21:00 МСК уезжают на следующий день.
* **Источник в БД**: `activity_log.source = 'health_connect'`.
* 💡 **Статус**: ✅ в проде, для пользователей на Android.

---

[← Документация Botkin — Index](../INDEX.md)
