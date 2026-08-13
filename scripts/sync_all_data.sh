#!/bin/bash
# Синхронизация всех источников данных HealthVault в локальную файловую систему
# ИИ-Агенты (Cursor, Claude, Antigravity) ОБЯЗАНЫ запускать этот скрипт первым делом,
# чтобы получить свежий 100% срез по питанию, сну, шагам, весам и климату из всех источников.
#
# Альтернатива: скилл /sync (автоматически вызывает этот скрипт + показывает таблицу актуальности).

echo "================================================="
echo "🔄 HealthVault: Master Data Sync"
echo "================================================="

cd "$(dirname "$0")/.." || exit 1

echo "1/4 📥 Синхронизация БД с удаленного сервера (Nutrition, Weights, Activity, Supplements)..."
scp -o StrictHostKeyChecking=no scripts/util/sync_remote_db.py root@116.203.213.137:/tmp/sync_remote_db_tmp.py
ssh -o StrictHostKeyChecking=no root@116.203.213.137 "docker cp /tmp/sync_remote_db_tmp.py healthvault_bot:/tmp/sync_remote_db.py"
ssh -o StrictHostKeyChecking=no root@116.203.213.137 "docker exec healthvault_bot bash -c 'cd /tmp && python sync_remote_db.py'"
ssh -o StrictHostKeyChecking=no root@116.203.213.137 "docker cp healthvault_bot:/tmp/data /opt/healthvault/tmp_data"

echo "1.5/4 📸 Скачивание сгенерированных дампов БД и новых фотографий весов/еды..."
# Синхронизируем выгруженные логи из tmp_data и медиа из рабочей data.
# weights/ теперь содержит и weights_remote.json, и blood_pressure_remote.json (с 09.05.2026).
rsync -avz -e "ssh -o StrictHostKeyChecking=no" \
    root@116.203.213.137:/opt/healthvault/tmp_data/nutrition/ ./data/nutrition/ || true
rsync -avz -e "ssh -o StrictHostKeyChecking=no" \
    root@116.203.213.137:/opt/healthvault/tmp_data/supplements/ ./data/supplements/ || true
rsync -avz -e "ssh -o StrictHostKeyChecking=no" \
    root@116.203.213.137:/opt/healthvault/tmp_data/weights/ ./data/weights/ || true
rsync -avz -e "ssh -o StrictHostKeyChecking=no" \
    root@116.203.213.137:/opt/healthvault/tmp_data/activities/activities_remote.json ./data/activities/ || true
rsync -avz -e "ssh -o StrictHostKeyChecking=no" \
    root@116.203.213.137:/opt/healthvault/data/media/ ./data/media/ || true

# Уборка за собой
ssh -o StrictHostKeyChecking=no root@116.203.213.137 "rm -rf /opt/healthvault/tmp_data /tmp/sync_remote_db_tmp.py && docker exec healthvault_bot rm -rf /tmp/data /tmp/sync_remote_db.py"

# Helper: повторный дамп БД (нужен после push_garmin_to_db.sh, чтобы activities_remote.json
# содержал свежие шаги/HRV, а не старые из первого дампа).
sync_db_again() {
    scp -o StrictHostKeyChecking=no scripts/util/sync_remote_db.py root@116.203.213.137:/tmp/sync_remote_db_tmp.py
    ssh -o StrictHostKeyChecking=no root@116.203.213.137 \
        "docker cp /tmp/sync_remote_db_tmp.py healthvault_bot:/tmp/sync_remote_db.py && \
         docker exec healthvault_bot bash -c 'cd /tmp && python sync_remote_db.py' >/dev/null && \
         docker cp healthvault_bot:/tmp/data /opt/healthvault/tmp_data"
    rsync -aqz -e "ssh -o StrictHostKeyChecking=no" \
        root@116.203.213.137:/opt/healthvault/tmp_data/weights/ ./data/weights/ || true
    rsync -aqz -e "ssh -o StrictHostKeyChecking=no" \
        root@116.203.213.137:/opt/healthvault/tmp_data/activities/activities_remote.json ./data/activities/ || true
    ssh -o StrictHostKeyChecking=no root@116.203.213.137 \
        "rm -rf /opt/healthvault/tmp_data /tmp/sync_remote_db_tmp.py && docker exec healthvault_bot rm -rf /tmp/data /tmp/sync_remote_db.py"
}

echo "1.8/4 ⚖️  Синхронизация умных весов (Zepp Life / Xiaomi SmartScale)..."
# Основной канал веса/жира — Mi Body Scale → Zepp Life → Apple Health → HAE → server weights table.
# Прямой API zepp_api.py — резервный (CN3 сервер, токен живёт ~7 дней). Если упал — это OK,
# данные всё равно подтянутся через Apple Health webhook. Не показываем как ошибку.
PY=/opt/homebrew/bin/python3.13
ZEPP_OUT=$($PY scripts/import/zepp_api.py 2>&1)
ZEPP_RC=$?
if [ $ZEPP_RC -eq 0 ]; then
    echo "$ZEPP_OUT" | tail -3
else
    echo "   ℹ️  Zepp API direct недоступен (резервный канал) — основные данные идут через Apple Health → HAE"
    echo "   Если хочешь починить прямой канал: $PY scripts/import/zepp_api.py --reauth"
fi
$PY scripts/import/zepp_csv.py 2>/dev/null || true

echo "1.85/4 ⚖️  Полный состав тела с весов Withings (мышцы/вода/кости/висцеральный)..."
# Зачем отдельно от Apple Health: в HealthKit НЕТ типов для мышечной массы, воды,
# костной массы и висцерального жира — через HAE доходят только вес, % жира и
# безжировая масса. Пишем через POST /api/agent/log_body_composition (HTTPS + PAT):
# доступ к серверу не нужен, апсерт идемпотентный, COALESCE не перетирает HAE.
#
# Требуется в .env: BOTKIN_PAT (scope rw). WITHINGS_TOKENS_PATH — общий токен-файл
# с локальным Withings-MCP, чтобы не плодить второе приложение и не ротировать
# токен вразнобой. WITHINGS_MIN_WEIGHT — весы домашние общие: на них встают другие
# члены семьи, а Withings относит нераспознанный замер к владельцу аккаунта.
# Шаг необязательный: нет кред — молча пропускаем, вес всё равно придёт через HAE.
# .env читает python (load_dotenv), а bash о нём не знает — вытаскиваем только
# факт наличия PAT, чтобы решить, включён ли шаг. Значение никуда не печатаем.
if [ -z "$BOTKIN_PAT" ] && [ -f .env ]; then
    BOTKIN_PAT=$(grep -E '^BOTKIN_PAT=' .env | head -1 | cut -d= -f2- | tr -d '"'"'"' ')
fi
if [ -n "$BOTKIN_PAT" ]; then
    # --min-weight берётся из WITHINGS_MIN_WEIGHT в .env самим скриптом
    WITHINGS_OUT=$($PY scripts/import/withings_api.py \
        --user "${BOTKIN_USER_ID:-836757955}" --days "${WITHINGS_SYNC_DAYS:-30}" --push-api 2>&1)
    if [ $? -eq 0 ]; then
        echo "$WITHINGS_OUT" | tail -3
    else
        echo "   ℹ️  Withings пропущен (API/токен недоступен) — вес всё равно придёт через HAE"
        echo "$WITHINGS_OUT" | tail -2
    fi
else
    echo "   ℹ️  Withings пропущен: нет BOTKIN_PAT в .env"
fi

echo "1.9/4 🍷 Обновление alcohol_daily.json из nutrition_log..."
$PY scripts/import/sync_alcohol.py

echo "2/4 🏃 Загрузка свежего сна, стресса, HRV, Body Battery и тренировок из Garmin Connect..."
$PY scripts/garmin/download_garmin_data.py || echo "   ⚠️  Garmin пропущен (см. выше)"
echo "2.3/4 📤 Пуш Garmin daily-summary → activity_log на сервере (batch, инкрементно)..."
$PY scripts/push_garmin_to_db.py
echo "2.4/4 🔁 Повторный дамп БД (чтобы activities_remote.json содержал свежие шаги/HRV)..."
sync_db_again
echo "2.5/4 🏋️  Агрегация тренировок в workouts_log.json..."
$PY scripts/util/parse_workouts.py
echo "2.55/4 🎯 Точный aerobic_base_min по HR-сэмплам (115-132 bpm) — для CrossFit-метконов..."
$PY scripts/util/compute_aerobic_base.py --days 30
# Пуш workouts_log в контейнер делает серверный scripts/server/sync_all.sh (cron),
# локальный push_workouts_to_container.py удалён из репо.
echo "2.7/4 🏋️  Бэкфилл тренировок → workouts table (треугольники на главном графике)..."
$PY scripts/backfill_to_postgres.py 2>&1 | grep -E "ТРЕНИРОВКИ|Вставлено|Нечего|Новых|ИТОГОВОЕ|Тренировки \(" || true

echo "3/4 🌬 Загрузка данных климата в спальне из Netatmo..."
$PY scripts/import/netatmo.py
# Пуш env_data в контейнер делает серверный scripts/server/sync_all.sh (cron),
# локальный push_netatmo_to_container.py удалён из репо.

echo "4/4 📱 Загрузка экранного времени (iPhone, Mac)..."
# Требует Full Disk Access у терминала!
# 1. Подкачать свежие события iPhone из iCloud Sync DB → ActivityWatch
#    (без этого activitywatch.py видит только старые события)
/Users/alexlyskovsky/.local/bin/aw-import-screentime events import \
    --device D2727389-2B2E-4E31-88FE-7BF0E925C580 2>&1 | tail -3
# 2. Конвертировать события из ActivityWatch в JSON
$PY scripts/import/activitywatch.py
# 3. Mac Screen Time из knowledgeC.db + ActivityWatch Mac watcher
$PY scripts/import/mac_screentime.py

echo "5/4 🍎 Apple Health (шаги, ходьба, АД, вес, пульс)..."
# Необязательный шаг — требует ручного экспорта Apple Health (Health → Профиль → Экспорт данных)
# Ищет export.xml в ~/Downloads/apple_health_export*/
# ЗАЩИТА ОТ РЕГРЕССА: если найденный XML старше уже имеющихся плоских файлов — пропускаем,
# чтобы не затереть свежие данные, импортированные вручную через parse_apple_health_xml.py
AH_XML=$(ls -t ~/Downloads/apple_health_export*/apple_health_export/export.xml ~/Downloads/apple_health_export*/apple_health_export/экспорт.xml 2>/dev/null | head -1)
AH_FLAT="data/apple_health_steps_daily.json"
if [ -n "$AH_XML" ] && [ -f "$AH_FLAT" ] && [ "$AH_FLAT" -nt "$AH_XML" ]; then
    echo "   ⏭  Пропущен — $AH_FLAT новее чем экспорт в ~/Downloads/ (защита от регресса)"
    echo "      Если хочешь пере-импортировать: удали $AH_FLAT и запусти снова"
elif $PY scripts/import/apple_health.py 2>/dev/null; then
    echo "   ✅ Apple Health обновлён"
else
    echo "   ⏭  Пропущен — нет export.xml в ~/Downloads/apple_health_export*/"
    echo "      Чтобы обновить: iPhone → Health → Профиль → Экспорт данных → разархивировать zip"
fi

echo "================================================="
echo "✅ Все данные успешно скачаны и обновлены!"
echo "➡️  Данные в папках: data/nutrition, data/garmin, data/activities, data/weights, data/environment."
echo "➡️  Запусти скилл /sync для просмотра таблицы актуальности."
echo "================================================="
