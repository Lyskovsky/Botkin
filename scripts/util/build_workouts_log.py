#!/usr/bin/env python3
"""
build_workouts_log.py — серверный derived-builder для дашборда.

Что делает:
  1. Запускает parse_workouts.py — он пересобирает data/garmin/workouts_log.json
     из сырых Garmin activity-файлов (data/garmin/activities/*.json).
  2. Обрезает до последних 180 дней и пишет в финальное место,
     которое читает dashboard_generator.py:
         /app/telegram-bot/workouts_log_{user_id}.json
     (180 дней — максимум, на который опирается /recent_workouts API; больше
     дашборду не нужно, экономим память контейнера.)

Зачем существует отдельным скриптом:
  parse_workouts.py пишет в data/garmin/workouts_log.json (исторический формат
  без user_id). Дашборд же читает per-user файл в telegram-bot/. До этого
  скрипта связка делалась только локально на маке через
  scripts/import/push_workouts_to_container.py. Теперь то же самое делается
  прямо на сервере, без зависимости от мак-pipeline.

Использование:
    BOTKIN_USER_ID=<id> python3 scripts/util/build_workouts_log.py   # владелец из env
    python3 scripts/util/build_workouts_log.py --user-id 12345       # явный юзер

В sync_all.sh:
    run garmin    /app/scripts/garmin/download_garmin_data.py
    run workouts  /app/scripts/util/build_workouts_log.py

В /sync в боте: ключ "workouts" в SOURCES (handlers/sync_cmd.py).

Multi-user готовность:
  Сейчас Garmin тянется только для владельца и сырые файлы лежат в общем
  data/garmin/activities/. Когда свой Garmin подключат другие пользователи —
  нужно будет разделить хранение на data/garmin/{user_id}/.
  Этот скрипт уже принимает --user-id, чтобы переход был механическим:
  достаточно будет научить parse_workouts.py читать из per-user папки.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

# Дефолт — владелец (единственный сейчас активный Garmin-пользователь), из env (#303).
# Когда подключатся другие — sync_all.sh будет вызывать скрипт по разу на каждого.
DEFAULT_USER_ID = int(os.getenv("BOTKIN_USER_ID") or os.getenv("HEALTHVAULT_USER_ID") or 0)
KEEP_DAYS = 180

BASE_DIR = Path(__file__).resolve().parent.parent.parent
PARSE_SCRIPT = BASE_DIR / "scripts" / "util" / "parse_workouts.py"
AEROBIC_SCRIPT = BASE_DIR / "scripts" / "util" / "compute_aerobic_base.py"
SOURCE_LOG = BASE_DIR / "data" / "garmin" / "workouts_log.json"


def out_path_for(user_id: int) -> Path:
    """Финальное место, откуда читает dashboard_generator.py."""
    return BASE_DIR / "telegram-bot" / f"workouts_log_{user_id}.json"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--user-id", type=int, default=DEFAULT_USER_ID, help=f"Telegram user_id (default {DEFAULT_USER_ID})"
    )
    parser.add_argument(
        "--skip-parse",
        action="store_true",
        help="Не запускать parse_workouts.py, использовать существующий workouts_log.json",
    )
    parser.add_argument(
        "--skip-aerobic",
        action="store_true",
        help="Не запускать compute_aerobic_base.py (без Garmin Connect / для скорости)",
    )
    args = parser.parse_args()

    # Шаг 1: пересобираем data/garmin/workouts_log.json из сырых activity-файлов
    if not args.skip_parse:
        if not PARSE_SCRIPT.exists():
            print(f"❌ {PARSE_SCRIPT} не найден", file=sys.stderr)
            return 2
        print(f"🔄 Запуск {PARSE_SCRIPT.name}…")
        result = subprocess.run(
            [sys.executable, str(PARSE_SCRIPT)],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            # Печатаем stdout+stderr чтобы _summarize_error в боте мог их прочесть
            print(result.stdout)
            print(result.stderr, file=sys.stderr)
            print(f"❌ parse_workouts.py вернул {result.returncode}", file=sys.stderr)
            return result.returncode
        # Показываем только последние пару строк, чтобы лог /sync не распухал
        tail = "\n".join(result.stdout.strip().splitlines()[-3:])
        if tail:
            print(tail)

    if not SOURCE_LOG.exists():
        print(f"❌ {SOURCE_LOG} не появился после parse_workouts", file=sys.stderr)
        return 2

    # Шаг 1.5: точная Z2-база (aerobic_base_min) из посекундных HR-сэмплов.
    # Best-effort: нужен Garmin Connect (garth-токены + garminconnect). Если их нет
    # или fetch упал — НЕ роняем сборку, merge-guard ниже подхватит старые значения.
    # Без этого шага новые тренировки приходят с aerobic_base=None → «Z2 база» = 0.
    # См. F-001 (08.06.2026). --skip-parse тоже пропускает compute (источник не менялся).
    if not args.skip_parse and not args.skip_aerobic and AEROBIC_SCRIPT.exists():
        print(f"🎯 Запуск {AEROBIC_SCRIPT.name} (точная Z2-база по HR)…")
        try:
            result = subprocess.run(
                [sys.executable, str(AEROBIC_SCRIPT), "--days", "30"],
                capture_output=True,
                text=True,
                timeout=300,
            )
            tail = "\n".join((result.stdout or "").strip().splitlines()[-3:])
            if tail:
                print(tail)
            if result.returncode != 0:
                print(
                    f"   ⚠️ compute_aerobic_base вернул {result.returncode} — "
                    f"пропускаем (старые значения сохранит merge-guard)",
                    file=sys.stderr,
                )
        except Exception as e:  # noqa: BLE001 — compute не должен ронять /sync
            print(f"   ⚠️ compute_aerobic_base не отработал ({e}) — пропускаем", file=sys.stderr)

    # Шаг 2: обрезаем до 180 дней и кладём в per-user место для дашборда
    full = json.loads(SOURCE_LOG.read_text())
    cutoff = (date.today() - timedelta(days=KEEP_DAYS)).isoformat()
    workouts = [w for w in full.get("workouts", []) if w.get("date", "") >= cutoff]

    out = out_path_for(args.user_id)

    # Шаг 2.5: сохранить HR-производные поля из существующего per-user файла.
    # aerobic_base_min / maf_zones / hr_sample_minutes считаются ТОЛЬКО на маке
    # (scripts/util/compute_aerobic_base.py — нужны посекундные HR-сэмплы из Garmin
    # Connect, на сервере токенов нет). parse_workouts их не восстанавливает, поэтому
    # серверная пересборка (/sync, cron) затирала их в None и «Z2 база» обнулялась.
    # Переносим по activity_id (fallback date) из старого файла. См. F-001 (08.06.2026).
    _CARRY = ("aerobic_base_min", "maf_zones", "hr_sample_minutes")
    if out.exists():
        try:
            prev = json.loads(out.read_text())
            prev_map = {}
            for w in prev.get("workouts", []):
                k = w.get("activity_id") or w.get("garmin_activity_id") or w.get("date")
                if k is not None:
                    prev_map[k] = w
            carried = 0
            for w in workouts:
                k = w.get("activity_id") or w.get("garmin_activity_id") or w.get("date")
                old = prev_map.get(k)
                if not old:
                    continue
                for field in _CARRY:
                    if w.get(field) is None and old.get(field) is not None:
                        w[field] = old[field]
                        if field == "aerobic_base_min":
                            carried += 1
            if carried:
                print(f"   ↺ перенесён aerobic_base_min из старого файла: {carried} трен.")
        except Exception as e:  # noqa: BLE001 — merge не должен ронять сборку
            print(f"   ⚠️ не удалось смержить aerobic_base из {out.name}: {e}", file=sys.stderr)

    payload = {
        "generated_at": full.get("generated_at"),
        "workouts": workouts,
        "kept_days": KEEP_DAYS,
    }

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    latest = max((w["date"] for w in workouts), default="—")
    print(f"✅ {out.name}: {len(workouts)} тренировок за {KEEP_DAYS} дней (latest: {latest})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
