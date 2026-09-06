# 🏗 Архитектура Botkin

> Как устроена система на верхнем уровне. Без кода — для понимания где что живёт и кто кого видит.

## Общая картинка

```mermaid
graph TB
    subgraph "СЕРВЕР (Hetzner berlin)"
        BOT[🤖 Telegram-бот<br/>@Botkin_md_bot]
        DB[(🗄 Postgres<br/>shared-данные<br/>всех юзеров)]
        MA[📱 Mini-app]
        DSH[📊 Dashboard<br/>botkin.health/mc/...]
        MCP[🔌 MCP server<br/>mcp.botkin.health]
        ADM[⚙️ Admin dashboard<br/>botkin.health/admin/]
        HAE[🍎 Apple Health webhook]
        KB[(📂 Shared KB<br/>Google Drive)]
        LKB[(📚 Longevity KB<br/>в репо GitHub)]
    end

    subgraph "Юзер"
        TG[📱 Telegram]
        IP[📱 iPhone + Apple Health<br/>Auto Export]
        AW[⌚ Apple Watch]
        GA[⌚ Garmin]
        OM[🩺 Omron тонометр]
        WE[⚖️ Mi-весы]
    end

    subgraph "Коллабораторы (только Олег/Ника/Андрей)"
        CD[💻 Claude Desktop Code]
        LF[📂 Локальные<br/>приватные файлы<br/>(КПТ-дневник)]
    end

    TG <--> BOT
    IP --> HAE
    AW --> IP
    OM --> IP
    WE --> IP
    GA -.периодический синк.-> BOT

    BOT --> DB
    BOT --> KB
    BOT --> LKB
    MA --> DB
    DSH --> DB
    DSH --> KB
    HAE --> DB

    CD <--> MCP
    MCP --> DB
    MCP --> KB
    MCP --> LKB
    CD <--> LF
```

## Кто что хранит

### 🗄 На сервере (общая БД для всех юзеров)

| Что | Где |
|---|---|
| Питание и КБЖУ — каждый твой приём пищи | `nutrition_log` |
| Добавки и лекарства — что и когда принял | `supplements_log` |
| Вес, % жира, мышцы | `weights` |
| Артериальное давление | `blood_pressure_logs` |
| Шаги, активность, пульс, сон, HRV | `activity_log` |
| Профиль (имя, возраст, рост, цель, диагнозы, лекарства) | `users` + `user_settings` |
| Биомаркеры из knowledge_base.json | синкаются туда же в БД |

**Это всё юзер сам положил** — через бот, через Apple Health, через Garmin, через мини-апп. Юзер знает что отдал.

### 📂 В общей Google Drive (для тех, у кого `kb_status='shared'`)

```
~/Library/CloudStorage/Google Drive/Botkin/
  └── {Имя} — Здоровье/
       ├── CLAUDE.md          ← инструкция для AI-агента, что читать
       ├── PROFILE.md         ← живая карта здоровья: диагнозы, терапия, риски
       ├── TODO.md            ← что сдать/уточнить в ближайшие недели
       ├── knowledge_base.json← структурированная база (числа, даты, файлы)
       └── *.pdf, *.jpeg      ← оригиналы анализов, выписок, УЗИ, МРТ
```

Эту папку видит **owner проекта (Александр)** и AI-агент юзера. Не видят другие юзеры.

### 💻 На маке юзера локально (для тех, у кого `kb_status='private'`)

Приватные файлы лежат **только на маке юзера**:
```
~/CBT-journal/         ← КПТ-дневник (Олег)
~/Private-Health/      ← intimate notes (Ника)
~/somewhere/           ← у каждого своё место
```

Эти файлы **никогда не попадают на сервер**. AI-агент юзера видит их только в личной сессии в Claude Desktop через локальный filesystem MCP. Owner не видит, бот не видит, другие юзеры не видят.

### 📚 Общий longevity-KB (в репо, для всех)

```
docs/longevity_kb/
  biomarkers/    ← ApoB, LDL, vitamin D, ferritin, ... — нормы и протоколы
  protocols/     ← Attia Medicine 3.0, Sinclair, Levine PhenoAge, ...
  medications/   ← Метформин, статины, GLP-1 agonists, ...
  topics/        ← Сон, силовые, Zone 2, голодание, сауна, ...
```

Это **открытая база знаний** (нет персональных данных). Доступна в [GitHub репо](https://github.com/Lyskovsky/Botkin). AI-агент любого юзера может ссылаться на эти материалы в ответах.

## Три способа общения с системой

### 1. Через Telegram-бот (просто, для всех)

Юзер пишет в бот фотку еды/голосом/текстом. Бот:
- **Структурированное** («съел овсянку», фото тарелки, «принял креатин 5г», «АД 120/80») → парсит, пишет в БД, отвечает «записал ✓»
- **Открытый вопрос** («как мне снизить ApoB?») → conversational agent: загружает твой профиль + последние данные + общий longevity KB → отвечает в чате

### 2. Через Mini-app (визуал, для всех)

Кнопка в углу чата с ботом → открывается мини-приложение прямо в Telegram. Три вкладки:
- **Дневник** — что съел за день, графики БЖУ
- **Добавки** — чек-лист утро/обед/вечер/ночь
- **Настройки** — твой профиль, цели, список добавок

### 3. Через Claude Desktop (продвинуто, для коллабораторов)

У Олега/Ники/Андрея есть Claude Desktop Code. Они прописывают в его конфиге Botkin MCP с personal токеном. После этого в чате с Claude можно:
- «какие у меня тренды веса за месяц»
- «что я ел вчера»
- «дай советы по моим последним анализам»

И параллельно — Claude видит **локальные приватные файлы** (КПТ-дневник Олега, заметки Ники) через filesystem MCP. Сервер их **не видит**, но Claude юзера видит **и сервер, и локалку** — и может дать полноценный ответ.

## Privacy boundary — кто видит что

| Источник | Видит owner (Alex) | Видит AI юзера в боте | Видит AI юзера в Claude Desktop |
|---|---|---|---|
| Питание/добавки/вес/АД из бота | ✅ через админ-дашборд | ✅ | ✅ через MCP |
| Apple Health / Garmin / Zepp | ✅ | ✅ | ✅ |
| Shared Google Drive папка | ✅ если расшарено | ✅ | ✅ |
| **Приватные файлы на маке юзера** | ❌ никогда | ❌ | ✅ через локальный MCP |
| Longevity KB в репо | ✅ публично | ✅ | ✅ |

Подробнее — в [Безопасность](./security.md).

## Технологии под капотом (для разработчиков)

| Слой | Стек |
|---|---|
| Telegram-бот | Python · aiogram · FastAPI |
| Mini-app | Vanilla HTML/CSS/JS, Telegram WebApp SDK |
| Dashboard | Python jinja → HTML, Chart.js |
| MCP server | Python FastMCP |
| База | PostgreSQL 15 в Docker |
| LLM | Anthropic Claude Sonnet 4 (parsing + conversational) + OpenAI Whisper (голос) |
| Сервер | Hetzner CCX13 (Berlin), Docker Compose, Caddy reverse proxy |
| Auth | HTTP Basic для admin, Bearer-токены для API/MCP/HAE |
| KB pipeline | Google Drive Watch API + Claude Vision (OCR PDF/JPEG → JSON) |

Полный код — открытый: [github.com/Lyskovsky/Botkin](https://github.com/Lyskovsky/Botkin).

## Связанные разделы

- [Telegram-бот](./telegram-bot.md) — детали взаимодействия
- [Mini-app](./mini-app.md) — UI
- [Dashboard](./dashboard.md) — большая страница
- [Безопасность](./security.md) — токены и privacy
