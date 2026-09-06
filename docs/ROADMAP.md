# Botkin Roadmap

> **Главная цель на сейчас:** пилотный запуск с трафиком Павла/Ани (Трек A).
> **Сегодня:** 16.06.2026 — Фаза 0, подготовка продукта к пилоту.
>
> Этот файл — высокоуровневая карта движения. Низкоуровневые таски — в `todo.md`.
> Обновлять при каждой смене направления.

---

## 🎯 NOW — Фаза 0: продукт под пилот (июнь 2026)

Что обязательно успеть, чтобы был осмысленный демо на конференции.

### Продукт под пилот (главное)
- [ ] **Бесшовный онбординг** — объяснения базовых команд, «можно писать боту напрямую», мастер `/profile` доходит до конца. Барьер №1 против конкурентов. [2-3 дня]
- [ ] **Health Reports для всех юзеров** — `/report` в боте, JWT-ссылка, публичная/приватная, кнопка «обновить с diff» к предыдущему. [3-5 дней]
- [ ] **Ссылки на дашборд и отчёт в Telegram mini-app** [полдня]

### BotkinClaw
- [ ] **Per-user system prompts** — персональные промпты для papa/mama/Nika (сейчас только Alex) [1 день]
- [x] ~~**BotkinClaw MVP**~~ — задеплоен 21.05, 30+ tools, история в Postgres
- [x] ~~**PhenoAge на дашборде и в агенте**~~ — реализовано (биологический возраст, 9 маркеров Levine 2018)
- [x] ~~**Индикатор свежести биомаркеров**~~ — бейджи на дашборде + агент упоминает дату (PR #77-79)
- [x] ~~**add_agent_correction**~~ — агент сохраняет поправки пользователя в KB (PR #104)

---

## ⏭ NEXT — после пилота (июль)

- [ ] **Per-user credentials в БД** — `garmin_email/password`, `apple_health_token` per user, OAuth для Fitbit/Whoop. [2-3 дня]
- [ ] **Мини-app Apple Health (grandma-proof)** — SwiftUI читает HealthKit и шлёт данные без HAE/Shortcuts при открытии. Референс: baccula/health-dashboard-export. «Тест бабушки» — рядовой юзер не настроит Shortcut. [3-5 дней]
- [x] ~~**Google Health Connect**~~ — задеплоен (PR #93), папа на Samsung подключён.
- [ ] **Локальные приватные потоки** — дневники family-cohort, Screen Time. Личный Claude через MCP + серверные данные. [сессия]
- [ ] **Login-форма в админке** — вместо HTTP Basic. [полдня]
- [ ] **Broadcast в админке** — массовая рассылка active-юзерам. [полдня]
- [ ] **Bot health metrics** — uptime, webhook latency, последние ошибки. [1 день] (расходы API уже есть в `/admin`)
- [ ] **Sync в Claude Code + PDF→KB pipeline** — интерфейс для IDE. [1 день]

---

## 🌅 LATER — лето 2026

Архитектурные апгрейды, требующие отдельной сессии.

- [ ] **Local-first вычисления на устройстве** — тяжёлые пайплайны (PDF-парсинг, корреляционный анализ, генерация инсайтов) выполняются локально на iPhone/Mac юзера, а не на сервере. Сервер — «координатор данных + API», устройство — «AI-runtime» (личный Claude через MCP). Снижает стоимость серверной части, повышает приватность. [по итогам Granola 16.05]
- [ ] **Cohort-aware BotkinClaw** — пользователи в когортах (early_users + family, посторонние = public). Per-cohort системные промпты и доступные tools. [Sprint 1a инфра уже есть]
- [ ] **Self-serve onboarding** — публичная регистрация через сайт, не только через приглашение в Telegram.
- [ ] **Audit log** — кто что менял (для прозрачности при работе вдвоём с Андреем).
- [ ] **Centenarian Decathlon** — функциональные тесты долголетия (Аттиа), квартальный лог в боте.

---

## 🚀 VISION — осень 2026 и дальше

Цели верхнего уровня, без сроков.

- **Открытый продукт** — paid tier (BYOK или подписка) когда расходы превысят $100/мес или станет 50+ юзеров. Сейчас open-source без коммерции.
- **Доклад / выступление** — после FFF Tbilisi, потенциально другие конференции (Welltory-style aud).
- **Семейный mesh** — у каждого свой бот в семье, общая Knowledge Base, MCP-агент видит всю семью при разрешении.
- **API for клиник / wellness-программ** — managed deployment для B2B клиентов (упоминается на лендинге как business audience).
- **Биологический возраст по семейному паттерну** — Genealogy-aware pipeline (главное конкурентное преимущество, обсуждалось с экспертом female-health).

---

## ✅ DONE — прошлые недели

Хронологически. Чтобы было видно скорость и не повторять.

### Сентябрь 2026
- [x] **Режим «план → факт»** — записи-планы (префикс «план:»), сведение к факту через агента (adjust_meal_items, dry-run превью), вечерний вопрос, бейдж в мини-аппе; фиксы веса из текста (#408) и этикетки «на 100 г» (#409) — 06.09.2026, #407

### Июнь 2026 (пост-FFF: стабилизация и новые интеграции)
- CGM (FreeStyle Libre 3) через LibreLinkUp — импорт глюкозы, on-demand refresh, /sync, токен на диске + User-Agent + backoff при 476, дашборд-блок (TIR/24h/спарклайн). Осталась живая проверка рендера после снятия Cloudflare-бана (#153). PR #129/#135/#139/#152, ADR-0005
- Alembic миграции — baseline схема прода + CI workflow + ADR-0003 (PR #83, #99)
- Android Health Connect — эндпоинт + деплой, папа подключён (PR #90, #93)
- Индикатор свежести биомаркеров — бейджи на дашборде + логика в агенте (PR #77-79)
- Фиксы по итогам первого теста бота (PR #50-51, #57-59, #71-74): вес из чата, мастер /profile, /health_token, /start для non-admin, сохранение роста
- Apple Health бесплатный путь через iOS Shortcuts — гайд + фикс дробных значений (PR #61, #63)
- `add_agent_correction` — агент сохраняет поправки пользователя в KB (PR #102-104)
- Рефактор webapp: settings.js + settings.css вынесены отдельно (PR #105-106)
- Dashboard URL в API сводки здоровья; исправлена запись нескольких приёмов пищи (PR #77)

### 21–28 мая (FFF Tbilisi + BotkinClaw)
- 🟢 BotkinClaw задеплоен: in-process агент в @Botkin_md_bot, 30+ tools, история в Postgres
- 🔴 NanoClaw свёрнут — инфра снесена с Hetzner (3.8 GB освобождено), ADR-0002
- PhenoAge реализован: формула Levine 2018, дашборд, агент-тул, тесты
- get_menstrual_data — новый тул агента

### 12–13 мая (ребрендинг + базовая инфраструктура)
- Ребрендинг HealthVault → Botkin (бот, README, CLAUDE.md, лендинг botkin.health)
- Webhook auto-register hotfix
- Удалён старый репо NutriLogBot
- SemVer (v0.4.0, v0.5.0) + bump-скрипт + RELEASING.md
- Сообщения о миграции отправлены 4 active-юзерам
- MCP-конфиг почищен (healthvault удалён, instagram токен зашит, garmin реавторизован)

### 17–19 мая (server-side sync)
- 4 источника на сервере: weather, netatmo, garmin, zepp (direct API mode)
- Объединены в `sync_all.sh`, один cron в 04:05 UTC, единый лог
- `/sync` команда в боте: `/sync`, `/sync <source>`, `/sync status`
- Bind-mount всех .py-папок (deploy = scp + restart, без docker cp)
- `lnetatmo` в requirements.txt (hotfix после force-recreate)
- 5 PR merged (#6, #7, #8, #9, #10 ROADMAP, #11 NanoClaw roadmap update)

### 19 мая (NanoClaw попытка → откат → research)
- 🔴 Построен `botkin-agent:v0.1` (Python FastAPI + Claude SDK, persistent per user) — повторил obsolete-подход из 11.05
- 🛠 Заметили проблему когда Alex дважды не получил ответа от бота на `привет, кто ты?`
- 🔧 Webhook chain repair: добавлен port 8081 в `docker-compose.prod.yml` (потерян при `--force-recreate` 18.05)
- ↩️ Полный откат: контейнер `botkin-agent-alex` удалён, image удалён, сервис закомментирован, ALEX_JWT_SECRET убран
- 📄 Создан [ADR-0001](architecture/decisions/0001-nanoclaw-ephemeral-not-persistent.md) — полная история решений (04.05 → 11.05 → 19.05) + правила «как не повторить»
- 📌 Ветка `feat/nanoclaw-agent-v0.1` НЕ мержится в main — оставлена как experimental/historical

### 19–20 мая (NanoClaw правильный deploy → Phase 1-3 ✅)
- 🟢 Поднят NanoClaw v2.0.64 на Hetzner (`/opt/nanoclaw/`, systemd)
- 🟢 OneCLI vault (Anthropic key + per-agent token gateway)
- 🟢 Telegram long-polling adapter, agent group "Alex"
- 🟢 MCP server "botkin" с 7 tools → реальные данные Alex из Postgres через JWT
- 🟢 Health context CLAUDE.local.md (семейный анамнез, текущие цифры, цели)
- ~~🟢 Auto-chown systemd timer (фикс readonly-db после restart)~~ → **закрыто 16.06.2026**: systemd-таймер ушёл вместе с откатом NanoClaw (`systemctl list-timers` пуст). Права на bind-mount данных теперь чинит chown pre-step в host-cron перед `sync_all.sh` — см. [DEPLOYMENT.md → «Права на bind-mount данных и ночной sync»](DEPLOYMENT.md)
- 📄 Документация в [`docs/projects/2026-05_nanoclaw-agent-bot/`](projects/2026-05_nanoclaw-agent-bot/): STATUS, SPEC, PLAN, QUESTIONS_FOR_ALEX

---

## 📌 Принципы поддержки этого файла

- **NOW** — то что в активной работе **на этой неделе**. Максимум 5-7 пунктов.
- **NEXT** — то что начнём **в течение месяца**. Должно быть честно достижимо.
- **LATER** — идеи без deadline, проверенные на ценность.
- **VISION** — куда движемся, без сроков.
- **DONE** — хронологически, для скорости видна.

При завершении пункта в NOW — перенести в DONE (с датой). Если NOW пустеет — взять из NEXT.

---

[← Документация Botkin — Index](INDEX.md)
