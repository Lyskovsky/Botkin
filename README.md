# Botkin 🩺

> **Open-source family health hub** — your medical archive, your wearables, and your private AI assistant, in one self-hostable platform.
> 🌐 [botkin.health](https://botkin.health) · 🤖 [@Botkin_md_bot](https://t.me/Botkin_md_bot)

[![License: AGPL v3](https://img.shields.io/badge/License-AGPL_v3-blue.svg)](LICENSE)
[![Status: Alpha](https://img.shields.io/badge/status-alpha-orange.svg)](#status)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-yellow.svg)](https://www.python.org/downloads/)
[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](CONTRIBUTING.md)

**English** · [Русский](#по-русски)

---

Botkin is a self-hostable platform that brings together everything you actually need
to make sense of your body — and your family's bodies — over time:

- 📊 **20+ data sources** — Garmin, Apple Health (via Health Auto Export), Zepp/Mi
  scales, Omron blood pressure, Netatmo, Open-Meteo weather, ActivityWatch screen
  time, plus manual food/supplement logging via Telegram bot (photo + voice + text)
- 📋 **Medical archive in EHR style** — structured per-family-member JSON of all
  your blood tests, ultrasounds, ECGs, MRIs, doctor visits, genetics — with
  AI-generated interpretation, not just storage
- 👨‍👩‍👧 **Family vault with row-level security** — each member has their own
  profile, diagnoses, exam journal; share what you want, keep the rest private
- 🤖 **AI chat with full context** — your personal Claude (via MCP) sees
  everything you let it see; no walled garden, no per-feature paywall
- 🔐 **Hybrid privacy by design** — shared family server holds the medical
  archive and wearable data; sensitive streams (therapy notes, screen time,
  personal documents) stay on your own machine via the MCP bridge
- 🌐 **Multilingual** — Russian-first UI, but parsers and bot handle English
  too; PDF parsing of lab reports works across major Russian and international labs

### Status

**Alpha.** Started in January 2026, used daily by the founder's family and
a growing pilot cohort — 31 active accounts as of September 2026. Codebase is
open under AGPL-3.0 for tech-biohackers who want to self-host. A hosted SaaS
tier is on the roadmap once costs or user count justify it — no fixed date yet.

This is **not medical advice software**. Botkin is a personal data tool that
shows you your own information; for clinical decisions, talk to a real doctor.

### Who Botkin is for

- **Now (alpha self-host):** technically competent biohackers who already
  wrangle their own health stack — Apple Health + Grafana + ChatGPT + scattered
  PDFs — and want one coherent open-source hub instead of seven duct-taped services.
- **Soon (Cloud beta):** Russian-speaking adults 35–55 with 5–10 years of
  accumulated lab results, who want a turnkey hosted version without running
  Hetzner themselves.
- **Not yet:** non-technical caregivers with no engineering help, patients
  seeking diagnoses, clinical providers (concierge longevity clinics — talk
  to us, this is on the roadmap).

### Support this project

Botkin is built by [Aleksandr Lyskovsky](https://github.com/Lyskovsky) in evenings and
weekends, together with his sons Oleg and Igor — they write code, review PRs, and fix
bugs right alongside him. If it's useful to you, please consider sponsoring — donations
fund infrastructure (server, AI tokens) and, eventually, part-time contributors:

- 💖 **GitHub Sponsors** — *coming soon*
- 🌍 **Open Collective** — *coming soon*
- 🏢 **Commercial licensing** (SaaS providers, white-label deployments without AGPL obligations) — **lyskovsky@gmail.com**

---

## По-русски

**Botkin** — open-source платформа здоровья для всей семьи: единый медицинский
архив + данные из носимых устройств + личный AI-ассистент. Self-host под AGPL-3.0
или будущая платная hosted-версия на [botkin.health](https://botkin.health).

**Что внутри:**

- 📊 **20+ источников данных** — Garmin, Apple Health (через Health Auto Export),
  Zepp/Mi-весы, Omron АД, Netatmo (воздух дома), погода, ActivityWatch (Screen
  Time), плюс ручное логгирование еды и добавок через Telegram-бот (фото, голос,
  текст)
- 📋 **Медицинский архив в стиле EHR** — структурированный JSON по каждому члену
  семьи: все анализы крови, мочи, гормоны, УЗИ, ЭКГ, МРТ, приёмы врачей,
  генетика — с AI-интерпретацией, не просто хранилище
- 👨‍👩‍👧 **Семейный vault с row-level security** — у каждого члена свой профиль,
  диагнозы, журнал обследований; делитесь чем хотите, остальное приватно
- 🤖 **AI-чат с полным контекстом** — ваш личный Claude (через MCP) видит всё что
  вы ему разрешите; без вендорских ограничений
- 🔐 **Гибридная приватность** — серверный слой для семьи + локальный приват-слой
  на вашем компьютере (дневник терапии, Screen Time, личные документы) через MCP-мост
- 🌐 **Двуязычность** — русский интерфейс, но парсеры PDF и бот понимают
  английский тоже; работает с лабораториями ИНВИТРО, Атлас, Гемотест и зарубежными

**Статус:** alpha — проект стартовал в январе 2026, ежедневно используется автором
и семьёй, а вокруг набирается пилотная группа — на сентябрь 2026 это 31 активный
аккаунт. Пишу его вместе с сыновьями, Олегом и Игорем — они правят код и разбирают
баги наравне со мной. Сейчас открываем код для tech-биохакеров под AGPL-3.0.
Hosted-версия — в планах, но без фиксированной даты: запустим, когда расходы или
число пользователей это оправдают.

> ⚠️ Botkin — это **инструмент для работы с вашими собственными данными**, а не
> медицинский советник. Для клинических решений — врач.

**Поддержать проект:** GitHub Sponsors и Open Collective — настраиваются.
Для коммерческой лицензии (SaaS-провайдеры, white-label без обязательств AGPL):
[lyskovsky@gmail.com](mailto:lyskovsky@gmail.com)

**Связаться:** [@lyskovsky](https://t.me/lyskovsky) · [lyskovsky@gmail.com](mailto:lyskovsky@gmail.com)

---

## Телеграм-бот

**Активный бот:** [@Botkin_md_bot](https://t.me/Botkin_md_bot) (display name «Botkin», bot_id 8739688481) — фото/голос/текст логгирование еды, добавки, мини-приложение «Дневник».

Старый `@HealthVault_bot` (bot_id 8500310863) — архив, webhook удалён, новые сообщения не обрабатываются.

Код бота — в `telegram-bot/`; пишет в PostgreSQL (`nutrition_log`, `supplements_log`, `weights`). Распознавание еды: LLM (фото/текст) → приоритет веса из текста над стандартной порцией, время по Москве (MSK), напитки 0 ккал (Cola Zero и т.п.).

---

## Основные файлы

- **`HEALTH.md`** — профиль здоровья, спорт, питание, добавки
- **`KNOWLEDGE_BASE.md`** — база знаний по медицинским анализам
- **`ROADMAP.md`** — план развития проекта

## Использование

Проект работает на выделенном сервере Hetzner. Локальный запуск не поддерживается.

**Деплой — только через GitHub Actions.** Workflow «Deploy prod»: Actions → «Deploy prod» →
Run workflow, или из CLI:
```bash
gh workflow run deploy-prod.yml -f branch=main
```

Workflow собирает Docker-образ бота, пушит в GHCR и по SSH тянет его на сервере
(pull-only, без сборки на сервере). Подробнее: `docs/DEPLOYMENT.md`.

**Диагностика:**
```bash
./scripts/util/diagnose_server.sh
```

---

## Источники данных

### Из бота Botkin (PostgreSQL)
| Тип данных | Таблица БД | Описание |
|------------|------------|----------|
| **Питание** | `nutrition_log` | Ежедневные записи еды + фото блюд. Подсчет КБЖУ. |
| **Витамины** | `supplements_log` | Лог приема добавок. |
| **Вес и состав тела** | `weights` | Данные с умных весов (Zepp Life) и Apple Health. |

### Локальные данные (файлы)
| Папка / Файл | Содержимое |
|--------------|------------|
| `KNOWLEDGE_BASE.md` | Сводная таблица всех анализов с 2009 года. |
| `HEALTH.md` | Профиль здоровья. Цели, привычки, диагнозы. |
| `data/blood-tests/` | PDF анализов крови. |
| `data/medical-records/` | Заключения врачей, ЭКГ. |
| `data/garmin/` | Garmin Connect: пульс, тренировки, сон, HRV, Body Battery. |
| `data/apple-health/` | Экспорты из Apple Health. |
| `data/activities/` | Экранное время, Chrome история, шаги. |

### Где искать ответ на вопрос?

| Вопрос | Где искать |
|--------|-----------|
| *"Как изменился вес за год?"* | Таблица `weights` (БД), затем `HEALTH.md`. |
| *"Какой у меня висцеральный жир?"* | `weights.visceral_fat` (БД). |
| *"Пил ли я вчера витамин Д?"* | `supplements_log` (БД). |
| *"Результаты анализа тестостерона"* | `KNOWLEDGE_BASE.md`, затем `data/hormones/`. |
| *"Мои цели по здоровью"* | `HEALTH.md` (раздел "Цели и приоритеты"). |

---

## Лицензия

Botkin распространяется под лицензией **[GNU Affero General Public License v3.0 или новее](LICENSE)** (AGPL-3.0-or-later).

Это значит:
- **Self-hosting для себя или семьи** — без ограничений
- **Модификация и распространение кода** — разрешены при условии открытия модификаций под той же лицензией
- **Запуск как сетевого сервиса (SaaS)** — обязывает опубликовать исходный код всех модификаций, включая серверную часть

Для **коммерческой лицензии** (использование без обязательств AGPL) — пишите: lyskovsky@gmail.com

© 2026 Aleksandr Lyskovsky
