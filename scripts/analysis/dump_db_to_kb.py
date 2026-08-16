#!/usr/bin/env python3
"""
Бэкап-дамп БД Botkin (Postgres на сервере) → локальный knowledge_base.json.

Принцип (решение Александра 12.07.2026):
  - Живые дневные потоки первичны в БД; локальная KB — периодически обновляемый бэкап.
  - В KB кладём КУРИРУЕМЫЕ АГРЕГАТЫ, а не сырьё: KB должна оставаться читаемой человеком
    и другой нейронкой, без мусора вроде каждого приёма пищи или каждого замера сна.

Что дампится (allowlist — ТОЛЬКО эти ключи скрипт имеет право менять):
  - body_composition_history : все строки body_measurements (обхваты + сила хвата) — редкие, осмысленные
  - supplements_regimen      : режим БАДов + приверженность за 90 дней (сводка, НЕ каждый приём)
  - nutrition_summary        : помесячные агрегаты питания (НЕ каждый приём)
  - _db_snapshot_meta        : когда/откуда сделан снимок

Что НЕ дампится: glucose_readings (сырьё; результаты — в курируемом cgm_experiments),
  sleep_records, weights, workouts (сырьё). Курируемые секции (blood_tests, genetics,
  ultrasound, medical_records, cgm_experiments, functional_tests, health_goals и пр.) —
  НЕ трогаются: перед записью проверяется их байтовая идентичность.

Безопасность:
  - только SELECT к серверу;
  - бэкап KB с таймстемпом ПЕРЕД записью;
  - ассерт «ни один существующий ключ не удалён»;
  - ассерт «любой ключ вне allowlist остался байт-в-байт прежним»;
  - по умолчанию --dry-run (ничего не пишет), запись только с --apply.

Использование:
  python3 scripts/analysis/dump_db_to_kb.py --user 895655            # dry-run
  python3 scripts/analysis/dump_db_to_kb.py --user 895655 --apply    # записать (с бэкапом)
  python3 scripts/analysis/dump_db_to_kb.py --kb /путь/knowledge_base.json --user 895655
    # явный путь к KB в обход KB_USERS (например для чужого клона без users_private.py)
"""

import argparse
import copy
import datetime
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from config.users import KB_USERS  # noqa: E402
from sync_family_kb import FAMILY_HEALTH  # noqa: E402

SERVER = "root@116.203.213.137"
PG = "docker exec -i healthvault_postgres psql -U healthvault -d healthvault -tA"
DEFAULT_UID = 895655
ADHERENCE_WINDOW_DAYS = 90

# Единственные ключи верхнего уровня, которые скрипт вправе создавать/обновлять.
WRITABLE_KEYS = {
    "body_composition_history",
    "supplements_regimen",
    "nutrition_summary",
    "_db_snapshot_meta",
}


def run_sql(sql: str) -> str:
    """Выполнить SQL на сервере (только чтение) и вернуть stdout."""
    proc = subprocess.run(
        ["ssh", "-o", "StrictHostKeyChecking=no", SERVER, PG],
        input=sql.encode("utf-8"),
        capture_output=True,
        timeout=60,
    )
    if proc.returncode != 0:
        sys.exit(f"SQL error:\n{proc.stderr.decode('utf-8', 'replace')}")
    return proc.stdout.decode("utf-8").strip()


def fetch_snapshot(uid: int) -> dict:
    """Один запрос → один JSON со всеми тремя агрегатами."""
    sql = f"""
SELECT json_build_object(
  'body_composition_history', (
    SELECT json_agg(row_to_json(b)) FROM (
      SELECT date::text AS date, waist_cm, neck_cm, hips_cm, chest_cm,
             thigh_cm, biceps_cm, grip_right_kg, grip_left_kg, notes
      FROM body_measurements WHERE user_id={uid} ORDER BY date
    ) b),
  'supplements_raw', (
    SELECT json_agg(row_to_json(s)) FROM (
      SELECT supplement_name,
             count(DISTINCT date) AS days_taken,
             min(date)::text AS first_seen,
             max(date)::text AS last_taken
      FROM supplements_log
      WHERE user_id={uid} AND date >= current_date - {ADHERENCE_WINDOW_DAYS}
      GROUP BY supplement_name
      ORDER BY count(DISTINCT date) DESC
    ) s),
  'nutrition_summary', (
    SELECT json_agg(row_to_json(n)) FROM (
      SELECT to_char(date,'YYYY-MM') AS month,
             count(DISTINCT date) AS days_logged,
             round(avg(dk)) AS avg_kcal,
             round(avg(dc)) AS avg_carbs_g,
             round(avg(dp)) AS avg_protein_g,
             round(avg(df)) AS avg_fat_g,
             round(avg(dfib),1) AS avg_fiber_g
      FROM (
        SELECT date,
               sum((totals->>'calories')::numeric) dk,
               sum((totals->>'carbs')::numeric) dc,
               sum((totals->>'protein')::numeric) dp,
               sum((totals->>'fats')::numeric) df,
               sum((totals->>'fiber')::numeric) dfib
        FROM nutrition_log WHERE user_id={uid} GROUP BY date
      ) t GROUP BY 1 ORDER BY 1
    ) n)
);
""".strip()
    data = json.loads(run_sql(sql))

    # supplements: досчитать приверженность в Python (читаемо для человека)
    regimen = []
    for s in data.get("supplements_raw") or []:
        days = s["days_taken"]
        regimen.append(
            {
                "name": s["supplement_name"],
                "days_taken": days,
                "adherence_pct": round(days / ADHERENCE_WINDOW_DAYS * 100),
                "first_seen": s["first_seen"],
                "last_taken": s["last_taken"],
            }
        )

    return {
        "body_composition_history": data.get("body_composition_history") or [],
        "supplements_regimen": {
            "window_days": ADHERENCE_WINDOW_DAYS,
            "items": regimen,
        },
        "nutrition_summary": {
            "note": "Помесячные средние по дням с логом. Сырьё (каждый приём) — в БД Botkin.",
            "months": data.get("nutrition_summary") or [],
        },
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--user", type=int, default=DEFAULT_UID, help="telegram_id пользователя")
    ap.add_argument("--kb", default=None, help="явный путь к knowledge_base.json (в обход KB_USERS)")
    ap.add_argument("--apply", action="store_true", help="записать (иначе dry-run)")
    args = ap.parse_args()

    if args.kb:
        kb_path = Path(args.kb)
    else:
        if args.user not in KB_USERS:
            sys.exit(
                f"❌ User {args.user} не найден в config.users.KB_USERS "
                f"(нужен config/users_private.py — приватный, не в git). "
                f"Известные: {sorted(KB_USERS)}. Либо передайте путь напрямую через --kb."
            )
        kb_path = FAMILY_HEALTH / KB_USERS[args.user] / "knowledge_base.json"

    if not kb_path.exists():
        sys.exit(f"KB не найдена: {kb_path}")

    orig = json.loads(kb_path.read_text(encoding="utf-8"))
    snap = fetch_snapshot(args.user)

    new = copy.deepcopy(orig)
    for k, v in snap.items():
        new[k] = v
    new["_db_snapshot_meta"] = {
        "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "source": "Botkin Postgres (healthvault_postgres) via dump_db_to_kb.py",
        "user_id": args.user,
        "note": "Курируемые агрегаты живых потоков. Первичен — Botkin; KB = бэкап.",
    }

    # --- ПРЕДОХРАНИТЕЛИ ---
    for k in orig:  # 1) ничего не удаляем
        assert k in new, f"АВАРИЯ: ключ '{k}' был бы удалён"
    for k in orig:  # 2) курируемые ключи неизменны байт-в-байт
        if k not in WRITABLE_KEYS:
            a = json.dumps(orig[k], sort_keys=True, ensure_ascii=False)
            b = json.dumps(new[k], sort_keys=True, ensure_ascii=False)
            assert a == b, f"АВАРИЯ: скрипт изменил бы курируемый ключ '{k}'"

    # --- ОТЧЁТ ---
    print(f"KB: {kb_path.name}")
    print(f"body_composition_history: {len(snap['body_composition_history'])} строк")
    print(f"supplements_regimen: {len(snap['supplements_regimen']['items'])} добавок за {ADHERENCE_WINDOW_DAYS}д")
    for it in snap["supplements_regimen"]["items"]:
        print(f"  - {it['name']}: {it['days_taken']}д ({it['adherence_pct']}%), посл. {it['last_taken']}")
    print(f"nutrition_summary: {len(snap['nutrition_summary']['months'])} месяцев")
    changed = [k for k in WRITABLE_KEYS if k in orig]
    added = [k for k in WRITABLE_KEYS if k not in orig]
    print(f"Обновятся ключи: {sorted(changed) or '—'}")
    print(f"Добавятся ключи: {sorted(added) or '—'}")
    print(f"Курируемых ключей нетронуто: {len([k for k in orig if k not in WRITABLE_KEYS])}")

    if not args.apply:
        print("\n[DRY-RUN] ничего не записано. Запуск с --apply запишет (с бэкапом).")
        return

    backup = kb_path.with_name(kb_path.name + ".bak_" + datetime.datetime.now().strftime("%Y%m%d_%H%M%S") + "_dbdump")
    backup.write_text(kb_path.read_text(encoding="utf-8"), encoding="utf-8")
    kb_path.write_text(json.dumps(new, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[APPLY] записано. Бэкап: {backup.name}")


if __name__ == "__main__":
    main()
