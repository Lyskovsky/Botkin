#!/usr/bin/env python3
"""
Генерирует telegram-bot/biomarkers_<telegram_id>.json из knowledge_base.json владельца.

Debug-инструмент: дашборд читает биомаркеры из Postgres, файл нужен только для
локальной отладки. Канонический синк KB → сервер:
    python3 scripts/sync_user_health.py --user <telegram_id> --apply

Usage:
    BOTKIN_USER_ID=<telegram_id> BOTKIN_KB_FOLDER="<Папка — Здоровье>" \
        python3 scripts/generate_biomarkers_json.py
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

# Владелец задаётся через env — ФИО и telegram_id не хардкодятся в публичном репо (#303).
USER_ID = os.getenv("BOTKIN_USER_ID") or os.getenv("HEALTHVAULT_USER_ID") or ""
KB_FOLDER = os.getenv("BOTKIN_KB_FOLDER", "")
FAMILY_ROOT = Path(
    os.getenv("BOTKIN_FAMILY_HEALTH_DIR") or (Path.home() / "Library/CloudStorage/GoogleDrive/Мой диск/FamilyHealth")
)

KB_PATH = FAMILY_ROOT / KB_FOLDER / "knowledge_base.json"
OUT_PATH = Path(__file__).resolve().parent.parent / "telegram-bot" / f"biomarkers_{USER_ID}.json"

SERVER = "root@116.203.213.137"
SSH_OPTS = ["-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=10"]


def build_biomarkers(kb: dict) -> dict:
    """Тонкая обёртка над core.health.biomarkers.aggregate_biomarkers.
    Сохранена для обратной совместимости (legacy biomarkers_<id>.json)."""
    from pathlib import Path as _P

    sys.path.insert(0, str(_P(__file__).resolve().parent.parent))
    from core.health.biomarkers import aggregate_biomarkers

    rows = []
    for section in ("blood_tests", "hormones", "vitamins"):
        for e in kb.get(section, []):
            if e.get("date"):
                rows.append({"date": e["date"], "values": e.get("values") or {}})
    return aggregate_biomarkers(rows)


def warn_empty_values(kb: dict) -> None:
    """Выводит предупреждение для записей с пустым или отсутствующим полем values."""
    sections = ["blood_tests", "urine_tests", "hormones", "vitamins"]
    found = 0
    for section in sections:
        for entry in kb.get(section, []):
            vals = entry.get("values") or entry.get("results")
            if not vals:
                date = entry.get("date", "?")
                lab = entry.get("laboratory", entry.get("lab", "?"))
                print(f"⚠️  Пустые values: {section} {date} {lab}")
                found += 1
    if found:
        print(f"   Итого {found} записей с пустыми values — они не попадут в biomarkers JSON.")
        print("   Добавь данные в knowledge_base.json или оставь как есть (не влияет на деплой).")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--deploy", action="store_true", help="Deploy to server after generating")
    args = parser.parse_args()

    if not USER_ID or not KB_FOLDER:
        sys.exit(
            "❌ Не заданы BOTKIN_USER_ID и/или BOTKIN_KB_FOLDER.\n"
            '   Пример: BOTKIN_USER_ID=123456789 BOTKIN_KB_FOLDER="Имя — Здоровье" \\\n'
            "            python3 scripts/generate_biomarkers_json.py"
        )

    print(f"📖 Reading {KB_PATH}")
    kb = json.loads(KB_PATH.read_text())

    warn_empty_values(kb)

    bio = build_biomarkers(kb)
    print(f"✅ Built {len(bio)} biomarkers")

    # Локальный JSON — отладочный артефакт; дашборд читает биомаркеры из Postgres.
    OUT_PATH.write_text(json.dumps(bio, indent=2, ensure_ascii=False) + "\n")
    print(f"💾 Saved to {OUT_PATH} (debug-артефакт; дашборд читает Postgres)")

    if args.deploy:
        # Legacy-деплой файла в контейнер удалён 11.06.2026 (аудит): дашборд
        # читает Postgres. Канонический синк KB → сервер:
        print("⚠️  --deploy отключён. Используй: python3 scripts/sync_user_health.py --user <telegram_id> --apply")
        return


if __name__ == "__main__":
    main()
