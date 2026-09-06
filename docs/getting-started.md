# Онбординг нового члена команды

Что сделать на старте, чтобы заработали и Claude Code (агентский тулинг), и сам бот Botkin.

---

## 1. Claude Code — агентский тулинг

### 1.1. Клонировать и встать на рабочую ветку

```bash
git clone git@github.com:botkin-health/Botkin.git
cd Botkin
git checkout dev
```

### 1.2. Открыть в Claude Code и доверить папку

Открой репозиторий в Claude Code и подтверди **Trust this folder**. Claude Code прочитает
закоммиченный `.claude/settings.json` и предложит установить объявленные плагины — согласись.

Если плагины не подтянулись автоматически — поставь руками:

```bash
claude plugin marketplace add affaan-m/everything-claude-code
claude plugin marketplace add clamp-sh/analytics-skills

claude plugin install everything-claude-code@everything-claude-code
claude plugin install posthog@claude-plugins-official
claude plugin install chrome-devtools-mcp@claude-plugins-official
claude plugin install analytics-skills@clamp-sh

claude plugin list   # проверка: все 4 enabled
```

> Плагины ставятся на **user-scope** (`~/.claude`), не в репозиторий. Это нормально:
> `.claude/settings.json` лишь декларирует, *что* включить, чтобы конфиг был
> воспроизводимым из git. Сам код плагинов в репу не коммитится.

### 1.3. Что уже приедет с git и заработает без действий

- проектные скиллы `.claude/skills/**` (architecture-patterns, tdd, to-issues, handoff, …);
- конфиг engineering-скиллов: `docs/agents/*` + блок `## Agent skills` в `CLAUDE.md`;
- `CLAUDE.md`, ADR в `docs/architecture/decisions/`.

### 1.4. Внешние инструменты, от которых зависят скиллы

| Инструмент | Зачем | Установка |
|---|---|---|
| **`gh` CLI** + `gh auth login` | скиллы `to-issues`/`triage`/`to-prd`/`qa` пишут в GitHub Issues (`botkin-health/Botkin`) | `brew install gh` |
| **Google Chrome** | `chrome-devtools-mcp` (браузерная отладка, скриншоты) | — |
| **Node.js** | часть плагинов/MCP запускается через npx | `brew install node` |

---

## 2. Botkin — запуск самого бота

> Это про приложение, не про Claude Code. Нужно, только если будешь запускать/деплоить бот.

### 2.1. Секреты

Создай `.env` из шаблона и заполни ключи (в git **не коммитится**, бери у владельца / из секрет-хранилища):

```bash
cp .env.example .env
# заполнить: TELEGRAM_BOT_TOKEN, ANTHROPIC_API_KEY, OPENAI_API_KEY, БД,
# а также (для деплоя) TELEGRAM_WEBHOOK_SECRET, WHOOP_STATE_SECRET — openssl rand -hex 32
```

### 2.2. Зависимости и тесты

```bash
# Python 3.13 (как в CI)
pip install -r requirements.txt

# Тесты (in-memory SQLite, dummy-ключи ставит conftest.py — env не нужны):
PYTHONPATH=. pytest tests/ \
  --ignore=tests/integration \
  --ignore=tests/test_nutrition_parsing.py

ruff check . && ruff format --check .
```

### 2.3. Деплой

Деплой — **только через GitHub Actions** (workflow «Deploy prod»: Actions → «Deploy prod» →
Run workflow, или `gh workflow run deploy-prod.yml -f branch=main`). Workflow собирает образ,
пушит в GHCR и по SSH тянет его на сервере (pull-only). Подробности — в [docs/DEPLOYMENT.md](DEPLOYMENT.md).

> ⚠️ Перед первым деплоем убедись, что на сервере в `.env` заданы `TELEGRAM_WEBHOOK_SECRET`
> и `WHOOP_STATE_SECRET` — без них webhook останется без аутентификации, а WHOOP-привязка упадёт.

---

## Куда дальше

- [docs/INDEX.md](INDEX.md) — карта документации
- [CLAUDE.md](../CLAUDE.md) — архитектура, источники данных, команды
- [docs/ROADMAP.md](ROADMAP.md) — NOW / NEXT / LATER / DONE

---

[← Документация Botkin — Index](INDEX.md)
