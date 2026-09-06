# 04 · Workflows (SOP для ИИ-ассистентов)

> **Last verified:** 2026-04-21

Стандартные операционные процедуры. Когда тебя просят сделать «X» — найди X в этой доке и следуй шагам. Если процедуры нет — добавь её сюда после первого выполнения.

---

## Глобальные правила

1. **Сначала grep, потом код.** Почти всё уже написано — `grep -rn 'thing' .` экономит часы.
2. **Один коммит = одна логическая задача.** Не миксовать рефакторинг и фичу.
3. **AI_CHANGELOG.md обновлять при завершении** любой непустой задачи. Формат: `[YYYY-MM-DD] Описание (затронутые файлы) - Автор`.
4. **Тесты перед commit:** `./venv/bin/python3 -m pytest tests/ --ignore=tests/test_live_llm.py -q`. Должно быть 0 failed.
5. **Pre-commit hook сам форматирует Python через ruff** — если коммит «упал» из-за форматирования, просто `git add -A && git commit` ещё раз.

---

## 1. Деплой кода в продакшен

⚠️ Нет CI/CD. Деплой — ручной `docker cp` + `docker restart`.

```bash
SERVER_PASS=$(grep -m1 'PASS=' "scripts/util/diagnose_remote.sh" | cut -d'"' -f2)
SCP="/opt/homebrew/bin/sshpass -p $SERVER_PASS scp -o StrictHostKeyChecking=no"
SSH="/opt/homebrew/bin/sshpass -p $SERVER_PASS ssh -o StrictHostKeyChecking=no"

# 1. Скопировать файл на сервер
$SCP path/to/changed.py root@116.203.213.137:/tmp/changed.py

# 2. Положить в контейнер и рестартануть
$SSH root@116.203.213.137 "docker cp /tmp/changed.py healthvault_bot:/app/path/to/changed.py && docker restart healthvault_bot"

# 3. Дождаться старта (~6 сек) и проверить логи
sleep 8
$SSH root@116.203.213.137 "docker logs healthvault_bot --tail 15 2>&1 | grep -iE 'error|готов|команды'"
```

**Про мини-апп:** при изменении `index.html` / `day.js` / `api.js` / `day.css` — те же шаги. Хеш auto-versioning (`?v=<md5>`) у статики обновится автоматически (вычисляется из mtime в `apple_health.py:_webapp_version()`). Telegram WebView подтянет свежий — но **сначала пользователю надо полностью закрыть мини-апп** (свайп из многозадачности на iPhone), иначе кеш WebView держит старую версию.

**Smoke test после деплоя** (auth должен корректно отбивать):
```bash
$SSH root@116.203.213.137 "
  curl -sk -o /dev/null -w 'webapp: %{http_code}\n' https://botkin.health/webapp/
  curl -sk -o /dev/null -w 'settings: %{http_code}\n' -H 'Authorization: tma x' https://botkin.health/api/settings
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

**Нет Alembic — миграции вручную.** При первой возможности — внедрить Alembic (см. ревью).

1. Изменить `database/models.py`.
2. Подготовить SQL: `ALTER TABLE … ADD COLUMN …` или `CREATE TABLE …`.
3. **С согласия пользователя** запустить:
   ```bash
   ssh root@116.203.213.137 "docker exec healthvault_postgres psql -U healthvault -d healthvault -c '<SQL>'"
   ```
4. Обновить `03_database_schema.md` (полная инвентаризация полей + anti-patterns).
5. Обновить `database/crud.py` — функции для нового поля.
6. AI_CHANGELOG.

**Если меняешь существующее поле / удаляешь** — backfill-скрипт обязателен. Шаблон: `scripts/backfill_fiber_all_history.py` (идемпотентный, dry-run support).

---

## 5. Добавить экран / фичу в мини-аппе

Архитектура мини-аппа: 3 таба (Дневник / Добавки / Настройки), один HTML-файл `telegram-bot/webapp/index.html`. Inline `<style>` и inline `<script>` для всего кроме Дневника (он в `day.js`).

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
# Полный набор (быстро, ~2 сек)
./venv/bin/python3 -m pytest tests/ --ignore=tests/test_live_llm.py -q

# С вербозом (видно каждый тест)
./venv/bin/python3 -m pytest tests/ --ignore=tests/test_live_llm.py -v

# Один файл
./venv/bin/python3 -m pytest tests/test_fiber_enrichment.py -v

# Live LLM тесты (стоят токены — запускать осознанно)
./venv/bin/python3 -m pytest tests/test_live_llm.py -v
```

Текущее состояние: **307 тестов**, все зелёные. 4 deselected — live LLM. **`handlers/photo.py` (1217 LOC) не покрыт ничем** — самое слабое место (см. ревью пункт #10).

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
5. **Если фронт мини-аппа не обновляется** — проверить хеш в HTML: `curl -sk https://botkin.health/webapp/ | grep day.js`. Хеш должен меняться при изменении JS/CSS.

---

## 12. Уборка рабочего места

Использовать `/cleanup` skill (`~/.claude/skills/cleanup/SKILL.md` имеет HealthVault-специфичный сценарий). Делает:
1. Удаление `__pycache__`, `.pyc`, `.DS_Store` локально
2. Git commit + push (если есть незакоммиченное)
3. Уборка на сервере
4. Бэкап БД (если последний >24ч)

---

## 13. Загрузил новый анализ в `knowledge_base.json` — что синкать на сервер

Самый частый workflow для биомаркеров. Источник истины — `~/FamilyHealth/<Имя>/knowledge_base.json` на маке; на сервере читается из **трёх** мест (`02_data_sources.md`, секция 16). Если синкать только одно — увидишь рассогласование (дашборд знает, агент нет — или наоборот).

**Для Александра (одна команда):**
```bash
python3 scripts/generate_biomarkers_json.py --deploy
```
Делает 3 стадии: (1) flat-JSON для дашборда → scp+docker cp, (2) `kb_to_blood_tests.py` → PostgreSQL `blood_tests`, (3) `sync_family_kb.py` → `/app/data/kb/kb_895655.json`.

**Для других юзеров** (Папа/Андрей/Олег/Игорь — пока без авто-обёртки):
```bash
python3 scripts/sync_family_kb.py --user <telegram_id> --apply
python3 scripts/import/kb_to_blood_tests.py --user-id <telegram_id> --folder "<имя папки>"
```

**Проверка после синка:**
- Дашборд: открыть `https://botkin.health/mc/<share_token>` → раздел Биомаркеры → смотреть свежую дату
- Агент: написать боту «какие анализы за <месяц>?» — должен вернуть свежую запись
- PostgreSQL: `psql -c "SELECT test_date FROM blood_tests WHERE user_id=<id> ORDER BY test_date DESC LIMIT 3"`

**Прецедент 24.05.2026:** обновили только flat-JSON, забыли (2) и (3) → дашборд показывал май, агент утверждал «последний 19 марта». С тех пор автомат `--deploy` закрывает все 3 канала для Александра.

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
