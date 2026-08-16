#!/usr/bin/env bash
# ============================================================================
# Ночной синк данных прод-стенда → дев-стенд (issue #101).
#
# Запускается НА сервере Hetzner (GitHub Action заходит по SSH и вызывает этот
# скрипт). Оба Postgres-контейнера живут на одном хосте → синк идёт через
# host-side pipe `docker exec ... \copy`.
#
# Стратегия (гибрид):
#   • UPSERT  — таблицы с естественным ключом: прод-данные добавляются/обновляют
#               совпадающие по ключу, дев-only строки сохраняются. surrogate `id`
#               НЕ переносим (дев выдаёт свой) → нет коллизий PK между БД.
#   • REPLACE — таблицы только с serial `id` (нет естественного ключа): TRUNCATE
#               + копия прода + setval. Дев-only строки в них теряются (так
#               решено: там обычно импорт, а не ручной тест).
#   • SKIP    — служебные/orphan таблицы не трогаем вовсе.
#
# Прод — СТРОГО READ-ONLY: единственное обращение к нему `\copy ... TO STDOUT`.
#
# Безопасность загрузки в дев:
#   • роль `healthvault` (владелец+superuser) → RLS не мешает экспорту/импорту;
#   • заливка под `session_replication_role = replica` → глушит аудит-триггер
#     `audit_admin` (иначе построчный флуд дев-audit_log) и FK-проверки на
#     время bulk-load;
#   • дев-бот останавливается на время БД-заливки (TRUNCATE берёт ACCESS
#     EXCLUSIVE) и поднимается обратно (trap, даже при ошибке).
#
# Использование:
#   scripts/ci/sync-prod-to-dev.sh [--dry-run]
#
# --dry-run — показать план и счётчики строк, НИЧЕГО не менять.
# ============================================================================
set -euo pipefail

# ── Константы окружения ─────────────────────────────────────────────────────
PROD_PG="${PROD_PG:-healthvault_postgres}"
DEV_PG="${DEV_PG:-botkin_dev_postgres}"
DEV_BOT="${DEV_BOT:-botkin_dev_bot}"
DB="${SYNC_DB:-healthvault}"
DB_USER="${SYNC_DB_USER:-healthvault}"
PROD_DATA="${PROD_DATA:-/opt/botkin/data}"
DEV_DATA="${DEV_DATA:-/opt/botkin-dev/data}"
STG_SCHEMA="sync_stg"

DRY_RUN=0
[ "${1:-}" = "--dry-run" ] && DRY_RUN=1

# ── Классификация таблиц (явные списки — без магии) ─────────────────────────
# UPSERT: "<table>:<conflict_col1,conflict_col2,...>"
UPSERT_TABLES=(
  "users:telegram_id"
  "user_settings:user_id"
  "nutrition_log:user_id,date,meal_time,meal_name"
  "weights:user_id,measured_at"
  "activity_log:user_id,date"
  "blood_pressure_logs:user_id,measured_at"
  "blood_tests:user_id,test_date,test_type"
  "cgm_connections:patient_id"
  "glucose_readings:user_id,ts"
)
# REPLACE: только serial id, без естественного ключа.
REPLACE_TABLES=(
  body_measurements
  supplements_log
  workouts
  agent_conversations
)
# SKIP: служебные/orphan таблицы, которые синкать не нужно. Явный массив (не
# комментарий) — чтобы coverage-guard ниже видел их как «осознанно пропущенные».
SKIP_TABLES=(
  audit_log
  llm_usage_log
  daily_summaries
  sleep_records
  user_feedback   # триаж фидбека — на проде (админы + agent-tools); дев-копия не нужна
)

log()  { printf '%s %s\n' "[$(date -u +%H:%M:%S)]" "$*"; }
die()  { printf '❌ %s\n' "$*" >&2; exit 1; }

# psql-обёртки. Прод — всегда без -w-мутаций (только TO STDOUT).
prod_psql() { docker exec -i "$PROD_PG" psql -U "$DB_USER" -d "$DB" "$@"; }
dev_psql()  { docker exec -i "$DEV_PG"  psql -U "$DB_USER" -d "$DB" "$@"; }

container_running() { [ "$(docker inspect -f '{{.State.Running}}' "$1" 2>/dev/null || echo false)" = true ]; }

# ── 0. Guard'ы ──────────────────────────────────────────────────────────────
log "Проверка контейнеров…"
container_running "$PROD_PG" || die "прод-Postgres '$PROD_PG' не запущен"
container_running "$DEV_PG"  || die "дев-Postgres '$DEV_PG' не запущен"
docker exec "$PROD_PG" pg_isready -U "$DB_USER" -d "$DB" >/dev/null 2>&1 \
  || die "прод-Postgres не отвечает на pg_isready"

# data-пути монтируются в контейнер для cp — не дать увести их за пределы
# ожидаемых префиксов (если кто-то переопределит env при ручном запуске).
[[ "$PROD_DATA" == /opt/botkin/* ]]     || die "PROD_DATA вне /opt/botkin/: '$PROD_DATA'"
[[ "$DEV_DATA"  == /opt/botkin-dev/* ]] || die "DEV_DATA вне /opt/botkin-dev/: '$DEV_DATA'"

# Sanity-guard: прод реально отдаёт строки (ловит силент-пустоту при FORCE RLS).
PROD_USERS="$(prod_psql -tAc 'SELECT count(*) FROM users')"
[ "${PROD_USERS:-0}" -gt 0 ] 2>/dev/null \
  || die "на проде 0 строк в users — экспорт отменён (FORCE RLS? пустая БД?)"
log "Прод: users=$PROD_USERS — экспорт безопасен."

# Coverage-guard: КАЖДАЯ прод-таблица должна быть классифицирована (UPSERT /
# REPLACE / SKIP). Новая таблица из миграции (как недавно CGM) иначе молча
# выпала бы из синка — здесь это становится громкой ошибкой.
KNOWN_TABLES="$(printf '%s\n' "${UPSERT_TABLES[@]%%:*}" "${REPLACE_TABLES[@]}" "${SKIP_TABLES[@]}" | sort -u)"
PROD_TABLES="$(prod_psql -tAc "SELECT tablename FROM pg_tables WHERE schemaname='public'" | sort -u)"
UNCLASSIFIED="$(comm -23 <(printf '%s\n' "$PROD_TABLES") <(printf '%s\n' "$KNOWN_TABLES"))"
[ -z "$UNCLASSIFIED" ] \
  || die "неклассифицированные прод-таблицы: $(echo $UNCLASSIFIED) — внеси в UPSERT/REPLACE/SKIP"

# Pre-flight: на REPLACE-таблицы не должно быть входящих FK (TRUNCATE без CASCADE
# иначе упал бы ночью). Ловим в pre-flight, а не в 3 часа ночи.
REPLACE_CSV="$(printf "'%s'," "${REPLACE_TABLES[@]}")"; REPLACE_CSV="${REPLACE_CSV%,}"
FK_TARGETS="$(prod_psql -tAc "
  SELECT DISTINCT c.relname FROM pg_constraint con
  JOIN pg_class c ON c.oid = con.confrelid
  JOIN pg_namespace n ON n.oid = c.relnamespace
  WHERE con.contype='f' AND n.nspname='public' AND c.relname IN ($REPLACE_CSV)")"
[ -z "$FK_TARGETS" ] \
  || die "на REPLACE-таблицы есть входящие FK: $(echo $FK_TARGETS) — TRUNCATE небезопасен, переведи в upsert"

# ── Хелперы построения SQL ──────────────────────────────────────────────────
# Список колонок таблицы (public), исключая 'id', по ordinal_position.
# Имя таблицы — через psql-переменную :'t' (квотируется как литерал), не
# string-интерполяцией → injection-safe, даже если хелпер переиспользуют.
cols_no_id() {
  dev_psql -v t="$1" -tAc "SELECT string_agg(quote_ident(column_name), ',' ORDER BY ordinal_position)
                 FROM information_schema.columns
                 WHERE table_schema='public' AND table_name = :'t' AND column_name <> 'id'"
}
# Все колонки таблицы (для full-replace COPY с id).
cols_all() {
  dev_psql -v t="$1" -tAc "SELECT string_agg(quote_ident(column_name), ',' ORDER BY ordinal_position)
                 FROM information_schema.columns
                 WHERE table_schema='public' AND table_name = :'t'"
}

# ── 1. Заполнение staging-схемы прод-данными (прод READ-ONLY) ───────────────
load_staging() {
  log "Создаю staging-схему '$STG_SCHEMA' в дев-БД…"
  dev_psql -q -c "DROP SCHEMA IF EXISTS $STG_SCHEMA CASCADE; CREATE SCHEMA $STG_SCHEMA;"

  local entry table
  for entry in "${UPSERT_TABLES[@]}" "${REPLACE_TABLES[@]}"; do
    table="${entry%%:*}"
    log "  staging ← prod.$table"
    dev_psql -q -c "CREATE TABLE $STG_SCHEMA.$table (LIKE public.$table INCLUDING DEFAULTS);"
    # Единственный контакт с продом — экспорт. Пайп host-side между контейнерами.
    prod_psql -c "\copy public.$table TO STDOUT" \
      | dev_psql -c "\copy $STG_SCHEMA.$table FROM STDIN"
    # Guard: staging обязан совпасть с продом по числу строк. Ловит частичный
    # COPY и силент-пустоту при внезапном RLS на не-users таблице (иначе
    # full-replace затёр бы дев-таблицу в ноль). Легитимно пустые таблицы
    # (prod=0) проходят — сверяем равенство, не «>0».
    local pc sc
    pc="$(prod_psql -tAc "SELECT count(*) FROM public.$table")"
    sc="$(dev_psql  -tAc "SELECT count(*) FROM $STG_SCHEMA.$table")"
    [ "$pc" = "$sc" ] || die "несовпадение строк '$table': prod=$pc staging=$sc"
  done
}

# ── 2. Слияние staging → public (под session_replication_role=replica) ──────
# Генерируем ОДИН SQL-батч и шлём в одну psql-сессию (session_replication_role
# не переживает отдельные -c вызовы).
build_merge_sql() {
  echo "SET session_replication_role = replica;"
  echo "BEGIN;"

  local entry table conflict cols set_clause c
  # UPSERT
  for entry in "${UPSERT_TABLES[@]}"; do
    table="${entry%%:*}"
    conflict="${entry#*:}"
    cols="$(cols_no_id "$table")"
    [ -n "$cols" ] || die "пустой список колонок (без id) для '$table' — рассинхрон схемы?"
    # SET для DO UPDATE: все колонки кроме конфликт-ключа (id уже исключён).
    # $cols идёт из quote_ident → токены в кавычках ("user_id"); $conflict —
    # сырой ("user_id"). Сравниваем по сырому имени, иначе фильтр не сработает.
    set_clause=""
    IFS=',' read -ra _cols <<< "$cols"
    for c in "${_cols[@]}"; do
      c_raw="${c//\"/}"
      case ",$conflict," in *",$c_raw,"*) continue ;; esac
      set_clause+="${set_clause:+, }$c = EXCLUDED.$c"
    done
    # NOT NULL по всем колонкам конфликт-ключа: иначе строки с NULL в ключе не
    # ловятся ON CONFLICT (NULL != NULL) и плодят дубли при каждом прогоне.
    local notnull=""
    IFS=',' read -ra _ck <<< "$conflict"
    for c in "${_ck[@]}"; do notnull+="${notnull:+ AND }$c IS NOT NULL"; done

    echo "-- upsert $table on ($conflict)"
    if [ -n "$set_clause" ]; then
      echo "INSERT INTO public.$table ($cols)
            SELECT $cols FROM $STG_SCHEMA.$table WHERE $notnull
            ON CONFLICT ($conflict) DO UPDATE SET $set_clause;"
    else
      echo "INSERT INTO public.$table ($cols)
            SELECT $cols FROM $STG_SCHEMA.$table WHERE $notnull
            ON CONFLICT ($conflict) DO NOTHING;"
    fi
  done

  # REPLACE. TRUNCATE без CASCADE намеренно: на эти таблицы нет входящих FK
  # (проверено по baseline-схеме). Если связь появится — TRUNCATE упадёт под
  # ON_ERROR_STOP, транзакция откатится, дев останется в исходном состоянии.
  for table in "${REPLACE_TABLES[@]}"; do
    cols="$(cols_all "$table")"
    [ -n "$cols" ] || die "пустой список колонок для '$table' — рассинхрон схемы?"
    echo "-- replace $table"
    echo "TRUNCATE public.$table;"
    echo "INSERT INTO public.$table ($cols) SELECT $cols FROM $STG_SCHEMA.$table;"
    # serial id перенесён as-is → сдвинуть sequence, иначе дев-вставки словят PK-конфликт.
    echo "SELECT setval(pg_get_serial_sequence('public.$table','id'),
                        GREATEST((SELECT COALESCE(MAX(id),1) FROM public.$table), 1));"
  done

  # COMMIT завершает транзакцию; session_replication_role сбрасывать не нужно —
  # это per-session настройка, а сессия psql закрывается сразу после.
  echo "COMMIT;"
}

# ── data-файлы: cp -a без --delete (докладываем, дев-only файлы целы) ────────
sync_data_files() {
  log "Синк data-файлов: $PROD_DATA → $DEV_DATA (без удаления дев-only)…"
  # Каталоги принадлежат uid 10001 → копируем через одноразовый root-контейнер.
  # Образ postgres:15 уже на хосте (стек проекта) — не тянем сторонний образ и
  # не ходим в сеть (apk). `cp -a SRC/. DST/` = рекурсивно с атрибутами, мерж в
  # DST с сохранением дев-only файлов (семантика rsync без --delete).
  docker run --rm \
    -v "$PROD_DATA":/src:ro \
    -v "$DEV_DATA":/dst \
    postgres:15 cp -a /src/. /dst/
}

# ── Оркестрация ─────────────────────────────────────────────────────────────
if [ "$DRY_RUN" -eq 1 ]; then
  log "DRY-RUN: изменений не будет. План:"
  for entry in "${UPSERT_TABLES[@]}"; do
    table="${entry%%:*}"
    cnt="$(prod_psql -tAc "SELECT count(*) FROM public.$table" 2>/dev/null || echo '?')"
    log "  UPSERT  $table (prod rows=$cnt) on (${entry#*:})"
  done
  for table in "${REPLACE_TABLES[@]}"; do
    cnt="$(prod_psql -tAc "SELECT count(*) FROM public.$table" 2>/dev/null || echo '?')"
    log "  REPLACE $table (prod rows=$cnt)"
  done
  log "  RSYNC   $PROD_DATA → $DEV_DATA (без --delete)"
  log "DRY-RUN завершён."
  exit 0
fi

# Дев-бот вниз на время БД-заливки (вернётся даже при ошибке).
BOT_WAS_RUNNING=0
if container_running "$DEV_BOT"; then
  BOT_WAS_RUNNING=1
  log "Останавливаю дев-бот '$DEV_BOT' на время заливки…"
  docker stop "$DEV_BOT" >/dev/null
fi
cleanup() {
  dev_psql -q -c "DROP SCHEMA IF EXISTS $STG_SCHEMA CASCADE;" >/dev/null 2>&1 || true
  if [ "$BOT_WAS_RUNNING" -eq 1 ]; then
    log "Поднимаю дев-бот '$DEV_BOT'…"
    docker start "$DEV_BOT" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

load_staging
log "Слияние staging → public (session_replication_role=replica)…"
build_merge_sql | dev_psql -q -v ON_ERROR_STOP=1
sync_data_files

log "✅ Синк прод→дев завершён."
