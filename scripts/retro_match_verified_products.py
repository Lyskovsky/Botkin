"""Ретро-ре-матч КБЖУ исторических записей nutrition_log по verified_products (#257).

Прогоняет записи ``nutrition_log`` через детерминированное ядро
``core.food.retro_match``: где сохранённое имя продукта совпадает со справочником
проверенных продуктов (#255), пересчитывает КБЖУ из этикеточных данных.
dry-run по умолчанию; ``--apply`` записывает.

Запуск на сервере (через robot-run workflow или SSH):
    docker exec <bot> python -m scripts.retro_match_verified_products              # dry-run, все юзеры
    docker exec <bot> python -m scripts.retro_match_verified_products --user 895655 # один юзер
    docker exec <bot> python -m scripts.retro_match_verified_products --apply       # запись

Идемпотентно: повторный прогон не находит изменений (epsilon-порог в ядре).
Вне scope (follow-up #257): LLM-vision повторное распознавание фото и сиротский
backfill photo_paths по дате+mtime.
"""

from __future__ import annotations

import argparse
import sys
from typing import List, Optional, Tuple

from core.food.retro_match import DEFAULT_EPSILON, RetroFix, plan_record_fix
from database import SessionLocal
from database.crud import get_verified_products
from database.models import NutritionLog


def scan(
    db,
    *,
    user_id: Optional[int] = None,
    limit: Optional[int] = None,
    epsilon: float = DEFAULT_EPSILON,
) -> Tuple[List[Tuple[NutritionLog, RetroFix]], int]:
    """Найти записи с исправимым КБЖУ (без записи в БД).

    Возвращает ``(fixes, checked)``: ``fixes`` — список ``(NutritionLog, RetroFix)``
    для записей, где ядро предложило правку; ``checked`` — сколько записей проверено.
    Справочник грузится один раз на пользователя (кэш).
    """
    q = db.query(NutritionLog).filter(NutritionLog.user_id.isnot(None)).order_by(NutritionLog.id)
    if user_id is not None:
        q = q.filter(NutritionLog.user_id == user_id)
    if limit:
        q = q.limit(limit)

    products_cache: dict = {}
    fixes: List[Tuple[NutritionLog, RetroFix]] = []
    checked = 0
    for rec in q.all():
        checked += 1
        if rec.user_id not in products_cache:
            products_cache[rec.user_id] = get_verified_products(db, rec.user_id)
        products = products_cache[rec.user_id]
        if not products:
            continue
        fix = plan_record_fix(rec.items or [], products, epsilon=epsilon)
        if fix is not None:
            fixes.append((rec, fix))
    return fixes, checked


def apply_fixes(db, fixes: List[Tuple[NutritionLog, RetroFix]]) -> int:
    """Записать предложенные правки (items + totals) и закоммитить. Вернуть число записей."""
    for rec, fix in fixes:
        rec.items = fix.new_items  # переприсваивание атрибута → SQLAlchemy видит dirty
        rec.totals = fix.new_totals
    db.commit()
    return len(fixes)


def _print_fix(rec: NutritionLog, fix: RetroFix) -> None:
    date = rec.date.isoformat() if rec.date else "—"
    print(f"  • log #{rec.id} user={rec.user_id} {date} «{rec.meal_name or '—'}»")
    for c in fix.changes:
        print(f"      {c.item_name}: {c.field} {c.old:g} → {c.new:g}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Ретро-ре-матч КБЖУ nutrition_log по справочнику verified_products (#257)."
    )
    parser.add_argument("--apply", action="store_true", help="Записать изменения. По умолчанию — dry-run.")
    parser.add_argument("--user", type=int, default=None, help="Только этот telegram_id (иначе все).")
    parser.add_argument("--limit", type=int, default=None, help="Ограничить число проверяемых записей.")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        fixes, checked = scan(db, user_id=args.user, limit=args.limit)
        print(f"Проверено записей: {checked}")
        print(f"С исправлениями:   {len(fixes)}")
        print(f"Без изменений:     {checked - len(fixes)}")
        if fixes:
            print("\nПредлагаемые правки:")
            for rec, fix in fixes:
                _print_fix(rec, fix)

        if not args.apply:
            print("\n[dry-run] Ничего не записано. Повтори с --apply для записи.")
            return 0
        if not fixes:
            print("\nНечего исправлять.")
            return 0
        n = apply_fixes(db, fixes)
        print(f"\n[apply] Обновлено записей: {n}.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
