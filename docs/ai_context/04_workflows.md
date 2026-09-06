# 04 · Workflows (SOP для ИИ-ассистентов)

> **Last verified:** 2026-09-06 (после добавления BotkinClaw, MCP-коннектора, CGM и режима план→факт — переписан деплой на GitHub Actions/Alembic, добавлены workflow'ы agent tool / MCP-коннектор / CGM / план→факт)

Стандартные операционные процедуры. Когда тебя просят сделать «X» — найди X в этой доке и следуй шагам. Если процедуры нет — добавь её сюда после первого выполнения.

---

## Глобальные правила

1. **Сначала grep, потом код.** Почти всё уже написано — `grep -rn 'thing' .` экономит часы.
2. **Один коммит = одна логическая задача.** Не миксовать рефакторинг и фичу.
3. **AI_CHANGELOG.md обновлять при завершении** любой непустой задачи (файл локальный, не коммитится — см. `.gitignore`).
4. **Тесты перед commit:** `PYTHONPATH=. pytest tests/ -v --ignore=tests/integration --ignore=tests/test_nutrition_parsing.py`. Должно быть 0 failed.
5. **Pre-commit hook сам форматирует Python через ruff** — если коммит «упал» из-за форматирования, просто `git add -A && git commit` ещё раз.
6. **Никогда `git add -A`** — рядом с кодом живут сознательно незакоммиченные файлы; стейджить только свои пути.

---

## 1. Деплой кода в продакшен

**Только через GitHub Actions**, workflow «Deploy prod» (`.github/workflows/deploy-prod.yml`). Ручной `docker cp`/`docker restart` в прод-контейнер — **анти-паттерн**, изменения не переживут следующий деплой.

```bash
# ВАЖНО: сначала push в remote — деплой берёт код из GitHub, не с локальной машины
git push origin dev   # или main, в зависимости от того что деплоим

# Запуск деплоя (ветка по умолчанию main)
gh workflow run deploy-prod.yml -f branch=main

# Откат на готовый образ — сборка пропускается
gh workflow run deploy-prod.yml -f image_tag=<готовый-тег-образа>
```

Workflow: собирает Docker-образ бота (job `build`, reusable `build-images.yml`), пушит в GHCR (`ghcr.io/botkin-health/botkin-bot`), затем по SSH на сервере (`/opt/botkin`) выполняет `docker compose -f docker-compose.prod.yml pull && up -d --wait` (pull-only, **без сборки на сервере**). `.env` лежит на сервере, в репозиторий не входит. Подробнее — `docs/DEPLOYMENT.md`.

⚠️ **Прод деплоится с `main`, не с `dev`.** С 18.06.2026: `dev` → авто-деплой на дев-стенд (`@botkin_dev_bot`, `dev.botkin.health`), `main` → прод (через PR `dev→main` + ручной `gh workflow run deploy-prod.yml -f branch=main`).

**Про мини-апп:** при изменении `index.html` / `day.js` / `dashboard.js` / `api.js` / `day.css` / `settings.js` — тот же деплой (не отдельный шаг). Хеш auto-versioning (`?v=<md5>`) у статики обновится автоматически (вычисляется из mtime в `apple_health.py:_webapp_version()`). Telegram WebView подтянет свежий — но **сначала пользователю надо полностью закрыть мини-апп** (свайп из многозадачности на iPhone), иначе кеш WebView держит старую версию.

**Smoke test после деплоя** (auth должен корректно отбивать):
```bash
$SSH root@116.203.213.137 "
  curl -sk -o /dev/null -w 'webapp: %{http_code}\n' https://health.orangegate.cc/webapp/
  curl -sk -o /dev/null -w 'settings: %{http_code}\n' -H 'Authorization: tma x' https://health.orangegate.cc/api/settings
"
# Ожидаем: webapp 200, settings 403 (отбивает невалидный токен)
```

---

## 2. Добавить новую интеграцию (источник данных)

1. **Создать скрипт** в `scripts/import/<source>.py` (ВНИМАНИЕ: внутри подпапки `import/`, не корня `scripts/`).
2. **Секреты** — добавить шаблон в `.env.example`, читать через `os.getenv()`. Никогда не хардкодить.
3. **Куда писать данные:**
   - **Если боту нужно прямо сейчас** → PostgreSQL через `database/crud.py` (например, `activity_log` для Garmin).
   - **Сырые JSON-выгрузки** → `data/<source>/<file>.json`. Создавать папку через `os.makedirs(..., exist_ok=True)`.
4. **Зарегистрировать** источник в `02_data_sources.md` (таблица «откуда брать»).
5. **Обновить `/sync`** skill чтобы он подтягивал свежие данные (`~/.claude/skills/sync/SKILL.md` если он есть, либо `scripts/sync_all_data.sh`).
6. **AI_CHANGELOG.md** — запись.

**Канонический пример:** `scripts/import/netatmo.py` (CO₂ + температура).

---

## 3. Изменить LLM-промпт

LLM-роутинг живёт в `core/llm/router.py` (главный classifier + food parser) и `core/vision/chatgpt_vision.py` (фото-флоу).

**Шаги:**
1. Не менять *логику* (parsing/routing). Менять только *prompt-строку*.
2. После изменения **обязательно прогнать** релевантные тесты:
   - `tests/test_nutrition_parsing.py` — текстовый food
   - `tests/test_supplement_recognition.py` — добавки
   - `tests/test_alcohol_drinks.py` — алкоголь
   - `tests/test_fruit_quantities.py` — единицы измерения
3. **Live-проверка с боевым LLM** (опционально): `tests/test_live_llm.py` — 4 deselected тестов. Запустить `pytest tests/test_live_llm.py -k <test_name>` если нужно. Стоит токены OpenAI.
4. Деплой по схеме из §1.
5. AI_CHANGELOG.

**Anti-pattern:** менять промпт ради одного крайнего случая, ломая 5 общих. Сначала посмотреть какие тесты упадут.

---

## 4. Изменить схему БД

**Alembic** (см. [ADR-0003](../architecture/decisions/0003-alembic-for-db-migrations.md) — заменил ручной `ALTER TABLE`-процесс из ранних версий этой доки).

1. Изменить `database/models.py`.
2. Написать миграцию в `database/alembic/versions/` (короткое slug-имя вроде существующих `nlplan01_add_nutrition_log_status.py`, `pat0token01_add_personal_access_tokens.py` — не автогенерированный хэш).
3. Прогнать alembic-check локально — он ловит расхождение ORM ↔ схема (например, лишний `UniqueConstraint`, см. комментарий у `PersonalAccessToken.token` в `models.py` — почему там сознательно НЕ `unique=True` на уровне колонки).
4. **С согласия пользователя** применить на сервере (как правило — часть того же деплоя, что и код; миграция должна выполняться **после**, не до, выката образа — прецедент 21.08: миграция против старого образа была тихим no-op).
5. Обновить `03_database_schema.md` (полная инвентаризация полей + anti-patterns).
6. Обновить `database/crud.py` — функции для нового поля.
7. AI_CHANGELOG.

**Если меняешь существующее поле / удаляешь** — backfill-скрипт обязателен. Шаблон: `scripts/backfill_fiber_all_history.py` (идемпотентный, dry-run support).

---

## 5. Добавить экран / фичу в мини-аппе

Архитектура мини-аппа: **4 таба** (Дневник / Добавки / **Здоровье** / Настройки), один HTML-файл `telegram-bot/webapp/index.html`. Таб «Здоровье» (`dashboard.js`) — просто iframe на публичный персональный дашборд `GET /mc/{token}`, отдельного backend-эндпоинта не имеет. Inline `<style>`/`<script>` для Settings; Дневник — `day.js`, Настройки — `settings.js`.

**Шаги:**
1. **Backend:** добавить endpoint в `nutrition_api.py` или `supplements_api.py` (или новый router-файл и подключить в `apple_health.py`).
   - Auth — `Depends(get_tg_user)` обязателен.
   - Возврат — JSON. Pydantic-модели для request bodies.
2. **Frontend:** редактировать `index.html`. Если фича сложная — отдельный `*.js` файл.
3. **Auto-versioning:** при изменении `day.js` / `api.js` / `day.css` хеш в URL обновится автоматически. Для `index.html` — `Cache-Control: no-cache`.
4. **Smoke-test:** после деплоя curl на endpoint (см. §1).
5. **Manual test:** полностью закрыть мини-апп на телефоне → открыть заново → проверить.

**Anti-patterns мини-аппа:**
- Не использовать `toISOString().slice(0,10)` для даты — это UTC, после 21:00 МСК даст завтрашний день. Использовать локальные `getFullYear/Month/Date` (см. `currentSuppDate()` в `index.html`).
- Не показывать date picker на не-date-scoped табах. Сейчас `switchTab()` прячет `.app-header` если tab ≠ `day`/`supplements-tab`.
- Не вызывать full re-render после optimistic update — гонка с in-flight тапами (см. ревью пункт #5).

---

## 6. Бэкап БД

**Автоматически** (через `/cleanup` skill раз в сутки):
```bash
ssh root@116.203.213.137 "docker exec healthvault_postgres pg_dump -U healthvault healthvault" \
  > "data/backups/healthvault_backup_$(date +%Y%m%d_%H%M%S).sql"
```

Ротация — 7 последних файлов:
```bash
ls -t data/backups/healthvault_backup_*.sql | tail -n +8 | xargs rm -f
```

**Восстановление** — `docs/RESTORE_BACKUP.md`.

---

## 7. Прогнать тесты

```bash
# Полный набор unit-тестов (integration и live LLM исключены по умолчанию)
PYTHONPATH=. pytest tests/ -v \
  --ignore=tests/integration \
  --ignore=tests/test_nutrition_parsing.py

# Один файл / один тест
PYTHONPATH=. pytest tests/test_nutrition_logic.py -v
PYTHONPATH=. pytest tests/test_plan_prefix.py::test_plan_colon -v

# Integration-тесты (RLS, onboarding wizard, Telegram router, audit trail) — отдельно, требуют реального Postgres
PYTHONPATH=. pytest tests/integration/ -v

# Live LLM / nutrition parsing тесты (стоят токены — запускать осознанно)
PYTHONPATH=. pytest tests/test_nutrition_parsing.py -v
```

Env-переменные для юнит-тестов не нужны: dummy-ключи ставит `tests/conftest.py` (`setdefault` + autouse-фикстура, защищающая от реальных LLM-вызовов за деньги); `DATABASE_URL` не нужна — `conftest.py` создаёт in-memory SQLite. Integration-тесты (`tests/integration/test_rls_isolation.py` и др.) реально бьют по RLS-политикам Postgres — им нужна настоящая БД.

---

## 8. Обновить AI_CHANGELOG

**Формат записи:**
```markdown
## YYYY-MM-DD — Краткое название

**Что:** одно-два предложения сути.

**Технические детали:**
- Файл1 (строки X-Y): что изменилось
- Файл2: что изменилось

**Зачем:** одно предложение про мотивацию.
```

**Антишаблон:** `[2026-04-21] Update file - Claude` (бесполезно через месяц).

---

## 9. Поднять мини-апп локально для отладки

⚠️ Mini-app использует Telegram `initData` для auth — без Telegram WebView его не получить. Поэтому **локально мини-апп без backend моков работает только частично** (UI отрисуется, но `/api/*` отвалятся 403).

Варианты:
1. **Деплой на сервер и тестировать через Telegram** (основной путь, см. §1).
2. **Mock initData локально:** в `webhook/tg_auth.py` временно вернуть фиксированного user'а если `os.getenv("DEV_MODE")`. Не коммитить!

---

## 10. Удалить устаревшую фичу (как делали с /my_products)

1. **Подтвердить что нужно** (продуктовое решение пользователя).
2. **Проверить что фича действительно мёртвая:**
   ```bash
   # Использование в БД
   docker exec healthvault_postgres psql -U healthvault -d healthvault -c \
     "SELECT COUNT(*) FROM <table> WHERE user_id = 895655"
   # Должно быть 0 у всех пользователей
   ```
3. **Удалить из меню** Telegram (`bot.py` → `set_my_commands`).
4. **Удалить handler'ы** (`commands.py`).
5. **Удалить ORM модели** (`models.py`).
6. **Удалить CRUD функции** (`crud.py` + `__init__.py` exports).
7. **Удалить ссылки в других модулях** (grep!).
8. **DROP TABLE** на сервере (с CASCADE если есть FK).
9. **Удалить из доков** (`03_database_schema.md`, упоминания в `01`).
10. **Прогнать тесты** — ничего не должно сломаться.
11. **Commit + push.**
12. **AI_CHANGELOG.md.**

**Точный пример:** см. AI_CHANGELOG `2026-04-21 — Полная чистка /my_products фичи`.

---

## 11. Расследование «у пользователя что-то не так»

Алгоритм debug'а:
1. **Логи бота:** `ssh root@116.203.213.137 "docker logs healthvault_bot --tail 200" | grep -iE 'error|exception|traceback'`
2. **Состояние БД:** SQL probe (см. `03_database_schema.md` сниппеты)
3. **Лог Telegram:** в боте есть `debug_logger` — пишет в файл, проверять `data/logs/`
4. **Network к API:** `curl -sk -H 'Authorization: tma x' …` чтобы увидеть статус
5. **Если фронт мини-аппа не обновляется** — проверить хеш в HTML: `curl -sk https://health.orangegate.cc/webapp/ | grep day.js`. Хеш должен меняться при изменении JS/CSS.

---

## 12. Уборка рабочего места

Использовать `/cleanup` skill (`~/.claude/skills/cleanup/SKILL.md` имеет HealthVault-специфичный сценарий). Делает:
1. Удаление `__pycache__`, `.pyc`, `.DS_Store` локально
2. Git commit + push (если есть незакоммиченное)
3. Уборка на сервере
4. Бэкап БД (если последний >24ч)

---

## 13. Загрузил новый анализ в `knowledge_base.json` — что синкать на сервер

Самый частый workflow для биомаркеров. Источник истины — `~/FamilyHealth/<Имя>/knowledge_base.json` на маке; на сервере биомаркеры живут в **двух** местах (`02_data_sources.md`, секция 16), и канонизация ключей происходит на чтении. Если синкать только одно — увидишь рассогласование (дашборд знает, агент нет — или наоборот).

**Одна команда для ЛЮБОГО юзера:**
```bash
python3 scripts/sync_user_health.py --user <telegram_id> --apply
# или для всех сразу:
python3 scripts/sync_user_health.py --all --apply
```
Две идемпотентные стадии: (1) KB → bind-mount `kb_<id>.json` (для агентских `/kb_value`, `/list_kb_keys`), (2) KB → Postgres `blood_tests` (для дашборда и `/recent_biomarkers`/`/phenoage`). Маппинг `telegram_id → папка` — единый `config/users.py::KB_USERS`.

⚠️ `sync_user_health.py` льёт из **локального** KB на маке. Если на сервере данные богаче локального (например, юзер сам догрузил анализ через `/doc`, см. `02_data_sources.md` §17) — сперва свести руками, иначе перезатрёшь более свежие серверные данные локальными.

**Проверка после синка:**
- Дашборд: открыть `https://botkin.health/mc/<share_token>` → раздел Биомаркеры → смотреть свежую дату
- Агент: написать боту «какие анализы за <месяц>?» — должен вернуть свежую запись
- PostgreSQL: `psql -c "SELECT test_date FROM blood_tests WHERE user_id=<id> ORDER BY test_date DESC LIMIT 3"`

**Если добавили новую запись в KB — не забыть регенерировать журнал обследований:**
```bash
python3 scripts/generate_exam_journal.py "Имя — Здоровье" --update-profile
```

---

## 14. Добавить/изменить agent tool (BotkinClaw + MCP-коннектор)

Инструменты агента живут в **двух синхронизируемых вручную местах** — нет codegen, связывающего их.

1. **Backend-эндпоинт**: добавить в `telegram-bot/webhook/agent_tools_api.py` — Pydantic `BaseModel` для запроса, `Depends(get_agent_user)` для чтения или `Depends(require_agent_scope("rw"))` для записи. Импорты `database.crud`/`core.*` — внутри функции (избегает циклических импортов, этому следуют все существующие эндпоинты). Никогда не доверять `user_id` из тела запроса — брать `user.telegram_id` из разрешённого JWT.
2. **Схема инструмента**: добавить запись в константу `TOOLS` в `core/agent_chat.py` (JSON Schema для Claude — имя, описание, параметры).
3. **Диспетчинг**: добавить ветку в `_call_tool()` (тот же файл) — маппинг имени инструмента на HTTP-вызов к эндпоинту из шага 1.
4. **Прогресс-индикатор** (опционально): короткая строка в `_TOOL_PROGRESS_LABEL` («🍽 собираю питание» и т.п.) — показывается в Telegram пока агент работает.
5. **Права**: если инструмент только для админов (как `list_feedback`/`triage_feedback`) — фильтровать из списка `TOOLS` по `config.users.is_admin`, а не через JWT-scope (это отдельная ось прав от `ro`/`rw`).
6. **Тест**: e2e через `ask_agent(uid, query)` — см. `feedback_e2e_means_ask_agent` в памяти проекта; HTTP-пинг эндпоинта отдельно НЕ проверяет, что агент реально вызывает инструмент правильно.
7. **MCP-коннектор** получает тот же эндпоинт бесплатно через generic `botkin_api(method, path, params)` в `scripts/mcp/botkin_pat_mcp.py` — отдельный named-tool там нужен только для часто используемых операций (см. текущий список: `get_day_summary`, `get_recent_meals`, `log_meal_text`, и т.п.).
8. Обновить `01_architecture.md` (список инструментов) и `AI_CHANGELOG.md`.

**Anti-pattern:** забыть один из шагов 1-3 — агент либо получит 404 от несуществующего эндпоинта, либо не будет знать, что инструмент существует, либо не сможет его вызвать. Все три места независимы, компилятор не поймает рассинхрон.

---

## 15. Подключить MCP-коннектор (Claude Desktop) — со стороны пользователя

1. В боте: `/connect_mcp [опциональное имя]` → выбрать scope («🖊 Полный доступ» = `rw` или «👁 Только чтение» = `ro`).
2. Бот один раз показывает PAT-токен (`pat_<telegram_id>_<hex32>`) — сохранить его, повторно не покажет.
3. Установить MCP-бандл (`scripts/mcp/manifest.json`, entry point `botkin_pat_mcp.py`) в Claude Desktop, вставить PAT в `user_config.pat` (хранится в системном keychain, не в открытом виде).
4. Проверка: спросить личного Claude что-то про данные из Botkin (например «какой у меня был вес на этой неделе») — должен вызвать `botkin_api`/`get_weight_history`.
5. **Отозвать доступ**: `/my_connections` в боте → «❌ Отозвать» у нужного токена (soft-delete через `revoked_at`; уже выданные JWT ещё поработают до истечения своего ~5-минутного TTL).

Дизайн — [ADR-0006](../architecture/decisions/0006-mcp-connector-pat-jwt.md).

---

## 16. Подключить CGM (глюкозу, `/connect_cgm`)

1. `/connect_cgm` в боте → бот спрашивает регион.
2. **Регион EU**: пригласить `dr@botkin.health` как follower в приложении FreeStyle LibreLink → бот сам находит новый `patient_id` (поллинг до 10 мин) и связывает с `telegram_id`.
3. **Другой регион** (#381, обязательно — LibreLinkUp-приглашения работают только внутри одного региона Abbott): создать отдельный follower-аккаунт под свой регион, ввести email/пароль в бота. Пароль удаляется из истории чата немедленно, логин валидируется live ДО сохранения кредов.
4. Ночной cron (`scripts/import/librelinkup.py` через `scripts/server/sync_all.sh`) подтягивает точки глюкозы для всех подключённых пользователей.
5. **Диагностика проблем с login**: rate-limit 5 попыток/15 мин на пользователя — специально чтобы не словить Cloudflare 476 бан всего региона у Abbott. Если словили — ждать, не долбить повторными попытками.

Подробности — `02_data_sources.md` §16b, [ADR-0005](../architecture/decisions/0005-cgm-librelinkup-integration.md).

---

## 17. Режим план→факт (внести еду авансом)

Пользователь пишет «План: 3 яйца, творог 200г» до того, как поел — распознаётся `core/food/plan_prefix.py`, тот же confirm-flow, но `status='plan'` в `nutrition_log` (эмодзи 📋). План уже входит в итог дня.

**Закрытие плана — три пути:**
1. **Вечернее авто-напоминание** (`scripts/server/send_reminders.py::dispatch_plan_close`, cron вне aiogram) — присылает вопрос «доеден целиком?» всем с открытыми планами. «Да, всё» массово переключает `status='eaten'`.
2. **Через агента**: пользователь пишет «съела не всё, минус творог» → BotkinClaw вызывает `adjust_meal_items` (dry-run превью → подтверждение → применение), может закрыть план (`close_plan=True`) или оставить остаток новым планом.
3. **В мини-аппе**: правка веса/удаление item'а напрямую в Дневнике.

**Если меняешь эту логику** — прогнать `tests/test_plan_prefix.py`, `tests/test_nutrition_plan_crud.py`, `tests/test_plan_close.py`, `tests/test_agent_dispatch_plan.py`. Готча из прецедента 06.09.2026: любая валидация нового веса должна проверять `math.isfinite() and >= 0` (NaN/отрицательные значения могут тихо испортить запись); при массовом обновлении из cron-скрипта коммитить **после каждого пользователя**, не одним махом в конце — иначе ошибка на одном пользователе откатывает уже обработанных.

---

## Что НЕ делать

❌ Не делать `git push --force` на main без явного согласия.

❌ Не коммитить `.env`, `.env.production`, `data/cache/tokens.json`. Они в `.gitignore`, но проверь.

❌ Не запускать `DELETE FROM …` на проде без `WHERE` фильтра по `user_id`.

❌ Не чинить production через `docker exec` правки внутри контейнера, не отражённые в репо. После рестарта пропадёт.

❌ Не менять `requirements.txt` без обновления Docker image (нужен rebuild).

❌ Не плодить `nutrition_api_v2.py`, `commands_new.py`, `index2.html` — переписывать существующее, не дублировать.

---

[← Документация Botkin — Index](../INDEX.md)
