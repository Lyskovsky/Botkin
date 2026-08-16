---
name: cleanup
description: >
  Уборка рабочего места в проекте Botkin — общая часть, безопасная для всех участников.
  Используй этот skill, когда пользователь пишет «прибери за собой», «убери рабочее место»,
  «сделай уборку», «уборка», «/cleanup», «наведи порядок» и текущий проект — Botkin.
  Чистит локальный мусор, наводит порядок в git и ветках, показывает состояние коллабораторов.
  Операционная часть (прод-сервер, деплой, миграции, бэкапы) — НЕ здесь, см. раздел в конце.
---

# Уборка рабочего места — Botkin (общая часть)

**Общаться по-русски.**

Этот скилл — **общая часть**, она в git и доступна всем участникам проекта. Здесь нет ничего,
что требует доступа к прод-серверу: ни адресов, ни паролей, ни SSH. Всё, что тут есть,
безопасно запускать любому, у кого есть клон репозитория.

Операционная часть (сервер, деплой, миграции, бэкапы, проверки боевой БД) живёт отдельно,
у владельца проекта — см. [«Операционная часть»](#ops) в конце.

---

## Фаза 1: Локальный мусор

```bash
cd "$(git rev-parse --show-toplevel)"

find . -type d -name "__pycache__" -not -path "*/venv/*" -not -path "*/.venv/*" -exec rm -rf {} + 2>/dev/null || true
find . -name "*.pyc" -not -path "*/venv/*" -not -path "*/.venv/*" -delete 2>/dev/null || true
find . -name ".DS_Store" -delete 2>/dev/null || true
rm -rf .mypy_cache .pytest_cache .ruff_cache 2>/dev/null || true
find . -maxdepth 1 \( -name "*_tmp.*" -o -name "tmp_*" -o -name "temp_*" -o -name "*.dump" \) -delete 2>/dev/null || true
```

**НЕ трогать:** `data/`, `docs/research*/`, `.env`, `.env.*`, `venv/`, `tests/`, `core/`, `database/alembic/`

### Осиротевшие worktrees

Удаляем только те, чей HEAD уже влит в `origin/dev`.

⚠️ Путь проекта может содержать пробелы — парсить `awk '{print $2}'` **нельзя**, он режет
путь по первому пробелу. Только `sed`. Сверяем по SHA, а не по имени ветки: detached HEAD
именем не ищется, а `git branch --merged | grep -w` даёт ложные срабатывания на подстроках.

```bash
git fetch origin dev -q
git worktree list --porcelain 2>/dev/null | grep "^worktree " | sed 's|^worktree ||' \
  | grep "\.claude/worktrees/" | while IFS= read -r wt; do
  sha=$(git -C "$wt" rev-parse HEAD 2>/dev/null)
  branch=$(git -C "$wt" rev-parse --abbrev-ref HEAD 2>/dev/null)
  [ -z "$sha" ] && continue
  if git merge-base --is-ancestor "$sha" origin/dev 2>/dev/null; then
    echo "Удаляю worktree $wt (ветка $branch / $sha уже в dev)"
    git worktree remove "$wt" 2>/dev/null \
      || echo "  ⚠️ worktree не пуст — проверь вручную, что там нет своей работы"
  else
    echo "ℹ️ Worktree $wt (ветка $branch) — не трогаю, ещё не смержена"
  fi
done
```

### Stash — показать, не удалять

```bash
STASHES=$(git stash list 2>/dev/null | wc -l | tr -d ' ')
[ "$STASHES" -gt 0 ] && echo "ℹ️ В stash $STASHES записей — не трогаю:" && git stash list
```

---

## Фаза 2: Git — коммит и push

```bash
git fetch origin
git status --short
```

- **Только tracked-файлы** (`git add -u`) — коммитить без вопросов.
- **Новые файлы** (untracked) — показать список. `docs/`, `scripts/`, `tests/` — спросить. Остальное не трогать.
- ⛔ **Никогда `git add -A` или `git add .`** — рядом лежат сознательно незакоммиченные файлы и чужой WIP.

```bash
git add -u
git diff --cached --stat
git commit -m "chore: уборка рабочего места — <кратко что>"
git push origin dev
```

---

## Фаза 3: GitHub — ветки

Не трогать: `main`, `dev`, ветки открытых PR.

**Производительность:** НЕ дёргать `git ls-remote` на каждую ветку в цикле — это сетевой
вызов на итерацию, и на ~70 смерженных PR скрипт упирается в таймаут (проверено 16.08.2026).
Тянем список веток **один раз** и сверяем локально через `comm`.

```bash
git ls-remote --heads origin | awk '{print $2}' | sed 's|refs/heads/||' | sort -u > /tmp/remote_branches.txt
gh pr list --state merged --limit 100 --json headRefName --jq '.[].headRefName' 2>/dev/null | sort -u > /tmp/merged_branches.txt
gh pr list --state open   --json headRefName --jq '.[].headRefName' 2>/dev/null | sort -u > /tmp/open_branches.txt

comm -12 /tmp/merged_branches.txt /tmp/remote_branches.txt \
  | comm -23 - /tmp/open_branches.txt \
  | grep -vE '^(main|dev)$' > /tmp/to_delete.txt

echo "К удалению: $(wc -l < /tmp/to_delete.txt) веток"
while IFS= read -r branch; do
  [ -z "$branch" ] && continue
  gh api -X DELETE "repos/botkin-health/Botkin/git/refs/heads/$branch" >/dev/null 2>&1 \
    && echo "  ✅ удалена: $branch" || echo "  ⚠️ не удалось: $branch"
done < /tmp/to_delete.txt

git remote prune origin
```

**Ветки закрытых-но-НЕ-смерженных PR** сюда не попадают и копятся годами. Автоматически не
удаляем — там может быть чужая незавершённая работа. Только показываем:

```bash
gh pr list --state closed --limit 100 --json headRefName,number,title,author,mergedAt \
  --jq '.[] | select(.mergedAt == null) | "\(.headRefName) | #\(.number) | \(.author.login) | \(.title)"' \
  2>/dev/null | sort -u | while IFS='|' read -r branch rest; do
    b=$(echo "$branch" | xargs)
    grep -qx "$b" /tmp/remote_branches.txt && echo "  ℹ️ висит: $b |$rest"
  done
```

---

## Фаза 4: Состояние коллабораторов

Проект многопользовательский. Уборка — хороший момент увидеть, не завис ли кто-то в ожидании
и не потерялась ли чужая работа. **Ничего не чиним автоматически — только показываем.**

**а) Открытые PR: кто ждёт и сколько**

```bash
gh pr list --state open --json number,title,author,createdAt,isDraft,mergeable,mergeStateStatus \
  --template '{{range .}}#{{.number}} | {{.author.login}} | draft={{.isDraft}} | {{.mergeable}}/{{.mergeStateStatus}} | {{.createdAt}} | {{.title}}{{"\n"}}{{end}}'
```

Как читать: `CONFLICTING` — автору нужен ребейз (написать ему); `UNSTABLE`/красный CI — смотреть,
баг это или протухший линтер/миграция; висит больше двух недель — вероятно, автор не знает,
что от него чего-то ждут.

**б) Чья работа лежит в `dev`, но ещё не в проде**

```bash
git fetch origin main dev -q
git log origin/main..origin/dev --no-merges --format='  %ad | %an | %s' --date=short
```

Если там есть чужие коммиты — при релизе упомянуть авторов, а после выката написать им.

**в) Гигиена git-идентичности**

Мусорное авторство ломает `git blame`, атрибуцию в Хронологе и правило CLAUDE.md
«сверять авторство PR перед похвалой».

```bash
git log --since='3 months ago' --format='%an|%ae' | sort -u
```

Что искать:
- **email вида `*.local`** (`user@MacBook-Pro.local`) — `user.email` не задан, git подставил
  hostname; GitHub такие коммиты не привязывает к профилю;
- **чужое имя при своём email** — скопирован чужой `user.name` (прецедент 16.08.2026);
- несколько GitHub-аккаунтов у одного человека — не баг, но полезно знать при чтении истории.

Чинит сам человек у себя: `git config user.name "…"` / `git config user.email "…"`
(с `--global` — во всех проектах). Историю не переписываем.

---

## Фаза 5: AI_CHANGELOG

⚠️ `docs/ai_context/AI_CHANGELOG.md` **снят с трекинга** 14.07.2026 (PR #313/#314 — внутри
Telegram ID и детали инфраструктуры, а репозиторий публичный) и прописан в `.gitignore`.
Поэтому `git log -- <этот файл>` **не работает как проверка свежести**: он навсегда показывает
коммиты по июль включительно. Смотреть надо mtime и содержимое.

```bash
stat -f "Файл менялся: %Sm" -t "%Y-%m-%d %H:%M" docs/ai_context/AI_CHANGELOG.md 2>/dev/null \
  || stat -c "Файл менялся: %y" docs/ai_context/AI_CHANGELOG.md   # GNU/Linux
echo "--- последние записи в файле ---"
grep -m 5 "^## " docs/ai_context/AI_CHANGELOG.md
echo "--- что уехало в main за 7 дней ---"
git log origin/main --since='7 days ago' --no-merges --format='  %ad | %an | %s' --date=short
```

Если в `main` есть заметные изменения, которых нет в записях файла — дописать (новые записи сверху).

⛔ **Никогда не коммитить этот файл.** Если `git status` показывает его как изменённый
tracked-файл — значит ветка старше 14.07; сперва подтянуть `origin/dev`.

---

## Фаза 6: Отчёт

```
🧹 Уборка (Botkin, общая часть) — YYYY-MM-DD HH:MM

📁 Локально:
  - Мусор: __pycache__ (N), .pyc (N), .DS_Store (N)
  - Worktrees: удалено N / пропущено N (не смержены)
  - Stash: N записей (не тронуты)

📦 Git:
  - Коммит: "<сообщение>" / нечего коммитить
  - push: ✅ / пропущено

🌿 Ветки:
  - Удалено смерженных: N (список)
  - Висят ветки закрытых-но-несмерженных PR: N (не трогали)

👥 Коллабораторы:
  - Открытых PR: N (кто / возраст / состояние)
  - Ждут действия автора: <#NN — конфликты, #NN — красный CI> / нет
  - Чужая работа в dev, ещё не в проде: <авторы> / нет
  - Git-идентичность: чисто ✅ / ⚠️ <кому что поправить>

📝 AI_CHANGELOG: актуален / ⚠️ нужно добавить запись
```

---

<a id="ops"></a>
## Операционная часть — не здесь

Следующее в этот скилл **намеренно не входит**, потому что требует доступа к боевой
инфраструктуре и кредов, которых нет в публичном репозитории:

- уборка на прод-сервере (Docker-образы, build cache, кэши Python);
- релиз `dev → main` и `Deploy prod`;
- **накат Alembic-миграций** (`Migrate DB`) — отдельный ручной workflow, деплой их НЕ катит;
- проверка свежести бэкапов БД и офсайт-копий;
- sanity-check BotkinClaw по боевой БД;
- дамп курируемых агрегатов в личный `knowledge_base.json` владельца.

Это выполняет владелец проекта своим глобальным скиллом
(`~/.claude/skills/cleanup/SKILL.md`, раздел «Botkin — операционная часть»).

**Если ты сейчас работаешь от имени владельца** (есть `.env` с доступами к серверу) — после
этой, общей части, продолжи операционной из глобального скилла. Если ты коллаборатор — на
этом уборка закончена, релиз и сервер не твоя зона.
