---
name: complete-task
description: Доводит готовую ветку Botkin до мержа в dev и закрывает issue. Триггеры — «закрой задачу», «заверши задачу», «/complete-task» (частая опечатка «/complete-taks»). Флоу-гейты по порядку: подлить свежий dev; создать DRAFT PR в dev с «Closes #N»; code-review (python-review) и фикс MEDIUM+; архитектурная проверка; security-review; тесты (план → апрув → реализация → покрытие); финальный прогон проверок; снять draft; мерж --merge после «да»; статус Done; удалить worktree; опц. спросить про PR dev→main с ревьюером @Lyskovsky. Прод НЕ деплоит. Парный к prepare-task.
---

# complete-task — довести ветку до мержа и закрыть задачу

## Контекст репо (Botkin)

- **Трекер:** GitHub Issues через `gh`, `botkin-health/Botkin` (`docs/agents/issue-tracker.md`).
- **Проект (доска):** GitHub Project **Botkin #1** (owner `botkin-health`, id `PVT_kwDOEX3Lns4Bam1p`). Status-поле `PVTSSF_lADOEX3Lns4Bam1pzhVdNrw`, опции: **Todo** `f75ad846` · **In Progress** `47fc9ee4` · **In Review** `c02f7d5b` · **Done** `98236657`.
- **Базовая ветка:** `dev`. Мерж — **merge-коммит** (`gh pr merge --merge`), как в истории репы.
- **Worktree:** `.claude/worktrees/<ветка, `/`→`+`>`.
- **Проверки (gate):** `ruff check .` · `ruff format --check .` · `PYTHONPATH=. pytest tests/ --ignore=tests/integration --ignore=tests/test_nutrition_parsing.py`.
- **Деплой:** прод катится **только через GitHub Actions** (workflow «Deploy prod», `.github/workflows/deploy-prod.yml`). **Этот скилл прод НЕ деплоит — только мерж.**
- **Язык:** русский.

Любой шаг упал/неоднозначен → **СТОП**, спросить.

## Шаги-гейты (строго по порядку)

### 1. Подлить свежий dev в ветку
```bash
git fetch origin dev
git merge --no-edit origin/dev
```
Конфликты разрешать, не теряя чужое. После — fix-проход (`ruff check .` + `ruff format --check .` + pytest) до зелёного.

### 2. DRAFT PR в dev
Обязательно `--draft`. Тело: **Summary** + **`Closes #<N>`** (жёстко: без связки PR не создаём) + **Test plan**.
```bash
git push -u origin <branch>
gh pr create --draft --base dev --title "<conventional, рус>" --body "$(cat <<'EOF'
## Summary
…
Closes #<N>

## Test plan
…
EOF
)"
```
Перевести issue в ревью: `gh issue comment <N> --body "В ревью: <ссылка на PR>"`. Выставить статус **In Review** (`c02f7d5b`) на доске Botkin #1 (см. «Доска»).

### 3. Code-review на PR
`/everything-claude-code:python-review` (Python — основной ревьюер; при желании `/code-review`). Находки классифицировать по severity.

### 4. Фикс всего MEDIUM+
Отдельными коммитами → fix-проход → push → повторный ревью. Цикл, пока не чисто. **LOW/INFO** можно отложить — короткой строкой в PR отметить, что именно отложено.

### 5. Архитектурная проверка
`/improve-codebase-architecture` (он ищет проблемы; `architecture-patterns` — гайд, не аудит). Фикс значимого. При существенных правках — вернуться к шагу 3.

### 6. Security-review
`/security-review` (или `/everything-claude-code:security-review`). Фикс MEDIUM+. Особое внимание: деньги (LLM-стоимость), auth/JWT/RLS, пользовательский ввод, внешние интеграции (Garmin/Zepp/Apple Health/Netatmo/WHOOP/Anthropic/OpenAI).

### 7. Тесты: план → апрув → реализация → покрытие
- Показать **план тестов**, ждать апрува. Мало, но на важное.
- Реализация; добить unit-покрытие (`/tdd`; `pytest --cov`, если стоит pytest-cov).
- **E2E — только паттерн репы:** `tests/integration/` (НЕ generic-генератор). Учесть: `test_rls_isolation.py` требует SSH-туннель к проду; `onboarding_wizard` флакает.
- Финальный план тестов — комментарием в issue.

### 8. Финальный прогон
`ruff check .` + `ruff format --check .` + pytest (+ интеграционные, где применимо). CI зелёный. Push. Снять draft: `gh pr ready <PR>`.

### 9. Мерж в dev после явного «да»
```bash
gh pr merge --merge --delete-branch <PR>
```
`Closes #N` закроет issue — проверить `gh issue view <N> --json state` = `CLOSED`, иначе закрыть вручную. Выставить статус **Done** (`98236657`) на доске Botkin #1 (см. «Доска»). **Прод НЕ деплоить** (workflow «Deploy prod» не запускать).

### 10. Удалить worktree
Перейти в основной чекаут, затем:
```bash
git worktree remove .claude/worktrees/<slug>
```
Без `--force`. При «modified/untracked» в worktree → **СТОП** (разобраться, не сносить вслепую). Подтвердить одной строкой.

### 11. Опц. PR dev → main (сигнал Александру)

После удаления worktree — **всегда спросить**:

> «Ветка в `dev`, задача закрыта. Готово к проду? Открыть PR `dev → main` с ревьюером @Lyskovsky?»

Если **да**:
```bash
gh pr create \
  --base main \
  --head dev \
  --reviewer Lyskovsky \
  --title "chore: sync dev → main (после #<N>)" \
  --body "## Что включено
Задача #<N> и всё накопленное в dev с последнего релиза.

## Проверено на @botkin_dev_bot
✅

## Деплой
Александр запускает «Deploy prod» с branch=main после мержа."
```

GitHub пришлёт уведомление @Lyskovsky. Больше ничего не делать — деплой за ним.

Если **нет** — завершить без PR. Пользователь откроет его позже или попросит Claude.

---

## Справка

### Доска: выставить статус
```bash
# item id задачи в проекте Botkin #1
ITEM_ID=$(gh project item-list 1 --owner botkin-health --format json \
  --jq ".items[] | select(.content.number==<N>) | .id")
# статус: In Progress (47fc9ee4) / In Review (c02f7d5b) / Done (98236657)
gh project item-edit --id "$ITEM_ID" --project-id PVT_kwDOEX3Lns4Bam1p \
  --field-id PVTSSF_lADOEX3Lns4Bam1pzhVdNrw --single-select-option-id <option-id>
```

## Чего НЕ делать
- Не создавать не-draft PR сразу (только `--draft`).
- Не создавать PR без `Closes #N`.
- Не мержить без явного «да».
- Не пропускать MEDIUM+ (LOW/INFO — можно отложить после ревью, с пометкой в PR).
- Не плодить E2E ради покрытия; generic e2e-генератор не использовать.
- Прод не деплоить (workflow «Deploy prod»).
- `--force` (worktree remove / push) — только по явной просьбе.
- Статусы доски — Todo/In Progress/In Review/Done (другие не выдумывать).
