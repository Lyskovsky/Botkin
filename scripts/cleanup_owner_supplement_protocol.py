"""Одноразовая очистка остаточного owner-протокола добавок (#251).

Контекст: до фикса #42 (b1dc1cc, 12.06.2026) код засевал личный протокол
владельца (`DEFAULT_SUPPLEMENTS`) в `user_settings.supplements` новым и
мигрируемым юзерам. Фикс остановил засев (`DEFAULT_SUPPLEMENTS=[]`), но уже
записанные строки не подчистил — у юзеров, созданных до 12.06, вкладка
«Добавки» в мини-аппе показывает чужой протокол.

Этот скрипт сбрасывает `supplements=[]` ТОЛЬКО у строк, ТОЧНО равных старому
owner-дефолту, и НЕ трогает владельца (`cohort='owner'`), который этот протокол
реально ведёт. Строки с частичным/похожим набором только репортятся — их
решает человек.

Запуск на сервере (прод):
    docker exec healthvault_bot python -m scripts.cleanup_owner_supplement_protocol            # dry-run
    docker exec healthvault_bot python -m scripts.cleanup_owner_supplement_protocol --apply     # запись

Идемпотентно: после сброса строки становятся `[]` и повторно не матчатся.
"""

from __future__ import annotations

import argparse
import sys

from database import SessionLocal
from database.models import User, UserSettings

# Старый owner-`DEFAULT_SUPPLEMENTS` до фикса #42
# (git `b1dc1cc~1:core/health/supplements.py`). Хардкод намеренный: в текущем
# коде `DEFAULT_SUPPLEMENTS=[]`, эталон живёт только в истории.
OWNER_DEFAULT = [
    {"name": "Псиллиум", "slot": "morning_before", "dose": "2 ч.л."},
    {"name": "Витамин D3", "slot": "morning_with", "dose": "5000 IU"},
    {"name": "Омега 3", "slot": "morning_with", "dose": "2 капс"},
    {"name": "Plant Sterols", "slot": "morning_with", "dose": "2 капс"},
    {"name": "Метилфолат", "slot": "morning_with", "dose": "400 мкг"},
    {"name": "K2 MK-7", "slot": "morning_with", "dose": "100 мкг"},
    {"name": "Plant Sterols", "slot": "evening", "dose": "2 капс"},
    {"name": "Магний", "slot": "evening", "dose": "2 табл"},
    {"name": "Креатин", "slot": "evening", "dose": "5 г"},
    {"name": "Whey", "slot": "evening", "dose": "2 ложки"},
]


def _key_multiset(items):
    """Порядко-независимое представление расписания: отсортированный список
    кортежей `(name, slot, dose)`. Возвращает None, если структура неизвестна
    (не список словарей) — такой строке не даём совпасть с эталоном."""
    if not isinstance(items, list):
        return None
    out = []
    for it in items:
        if not isinstance(it, dict):
            return None
        out.append(
            (
                (it.get("name") or "").strip(),
                (it.get("slot") or "").strip(),
                (it.get("dose") or "").strip(),
            )
        )
    return sorted(out)


_OWNER_KEY = _key_multiset(OWNER_DEFAULT)
_OWNER_NAMES = {(it["name"], it["slot"]) for it in OWNER_DEFAULT}


def _is_stamped(supplements) -> bool:
    """True, если расписание ТОЧНО равно старому owner-дефолту (как мультимножество,
    без учёта порядка). Пустое/None/частичное/с лишними позициями → False."""
    key = _key_multiset(supplements)
    return key is not None and key == _OWNER_KEY


def _looks_similar(supplements) -> bool:
    """True для непустого расписания, которое НЕ точный матч, но пересекается с
    owner-позициями (кандидат на ручной разбор, автоматически не чистим)."""
    if not isinstance(supplements, list) or not supplements:
        return False
    names = {((it.get("name") or ""), (it.get("slot") or "")) for it in supplements if isinstance(it, dict)}
    return bool(names & _OWNER_NAMES)


def collect(db) -> dict:
    """Классифицировать все строки user_settings по совпадению с owner-дефолтом.

    Возвращает dict с ключами:
      • stamped       — [(user_id, cohort)] точный матч, cohort != 'owner' (под очистку);
      • owner_skipped — [user_id] точный матч, но cohort == 'owner' (не трогаем);
      • similar       — [(user_id, cohort, n)] похоже, но не точный матч (только репорт);
      • total         — всего строк user_settings.
    """
    rows = (
        db.query(UserSettings.user_id, UserSettings.supplements, User.cohort)
        .join(User, User.telegram_id == UserSettings.user_id)
        .all()
    )
    stamped, owner_skipped, similar = [], [], []
    for user_id, supplements, cohort in rows:
        if _is_stamped(supplements):
            if cohort == "owner":
                owner_skipped.append(user_id)
            else:
                stamped.append((user_id, cohort))
        elif _looks_similar(supplements):
            similar.append((user_id, cohort, len(supplements)))
    return {"stamped": stamped, "owner_skipped": owner_skipped, "similar": similar, "total": len(rows)}


def apply_cleanup(db, stamped_user_ids) -> int:
    """Сбросить supplements=[] у переданных user_id и закоммитить. Вернуть число строк."""
    for uid in stamped_user_ids:
        row = db.query(UserSettings).filter(UserSettings.user_id == uid).one()
        row.supplements = []
    db.commit()
    return len(stamped_user_ids)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Очистка остаточного owner-протокола добавок из user_settings.supplements (#251)."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Записать изменения. По умолчанию — dry-run (только показать).",
    )
    args = parser.parse_args()

    db = SessionLocal()
    try:
        report = collect(db)
        stamped = report["stamped"]
        owner_skipped = report["owner_skipped"]
        similar = report["similar"]

        print(f"Всего строк user_settings: {report['total']}")
        print(f"Точный owner-дефолт, НЕ owner (под очистку): {len(stamped)}")
        for uid, cohort in stamped:
            print(f"  • user_id={uid} cohort={cohort}")
        if owner_skipped:
            print(f"Пропущено (cohort=owner, реальный протокол): {len(owner_skipped)} → {owner_skipped}")
        if similar:
            print(f"Похожие, но НЕ точный матч (только репорт, не трогаем): {len(similar)}")
            for uid, cohort, n in similar:
                print(f"  • user_id={uid} cohort={cohort} позиций={n}")

        if not args.apply:
            print("\n[dry-run] Ничего не записано. Повтори с --apply для очистки.")
            return 0

        if not stamped:
            print("\nНечего чистить.")
            return 0

        n = apply_cleanup(db, [uid for uid, _cohort in stamped])
        print(f"\n[apply] Сброшено в [] строк: {n}.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
