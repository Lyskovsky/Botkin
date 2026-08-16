"""Тесты CLI ретро-ре-матча nutrition_log по verified_products (#257) на in-memory БД."""

import datetime

from core.food.verified_products import normalize_product_name
from database.models import NutritionLog, User, VerifiedProduct
from scripts.retro_match_verified_products import apply_fixes, scan

USER = 555
OTHER = 777


def _seed_user(db, uid=USER):
    db.add(User(telegram_id=uid, first_name=f"u{uid}"))
    db.commit()


def _add_solvie_product(db, uid=USER):
    db.add(
        VerifiedProduct(
            user_id=uid,
            name="Solvie Protein Barre",
            name_norm=normalize_product_name("Solvie Protein Barre"),
            calories_per_100g=360.0,
            protein_per_100g=20.0,
            fats_per_100g=12.0,
            carbs_per_100g=40.0,
            fiber_per_100g=22.8,
            portion_g=50.0,
            source="manual",
        )
    )
    db.commit()


def _solvie_item(fiber=1.8):
    return {
        "name": "Solvie Protein Barre",
        "weight_g": 50,
        "calories": 180,
        "protein": 10,
        "fats": 6,
        "carbs": 20,
        "fiber": fiber,
    }


def _add_log(db, items, uid=USER, meal_name="батончик"):
    totals = {"calories": sum(i.get("calories", 0) for i in items), "fiber": sum(i.get("fiber", 0) for i in items)}
    rec = NutritionLog(user_id=uid, date=datetime.date(2026, 6, 1), meal_name=meal_name, items=items, totals=totals)
    db.add(rec)
    db.commit()
    return rec


def test_scan_finds_fixable_record(test_db):
    _seed_user(test_db)
    _add_solvie_product(test_db)
    _add_log(test_db, [_solvie_item(fiber=1.8)])
    fixes, checked = scan(test_db)
    assert checked == 1
    assert len(fixes) == 1
    _rec, fix = fixes[0]
    assert any(c.field == "fiber" and c.new == 11.4 for c in fix.changes)


def test_scan_dry_run_does_not_write(test_db):
    _seed_user(test_db)
    _add_solvie_product(test_db)
    rec = _add_log(test_db, [_solvie_item(1.8)])
    scan(test_db)  # только чтение
    test_db.refresh(rec)
    assert rec.items[0]["fiber"] == 1.8


def test_apply_writes_items_and_totals(test_db):
    _seed_user(test_db)
    _add_solvie_product(test_db)
    rec = _add_log(test_db, [_solvie_item(1.8)])
    fixes, _ = scan(test_db)
    n = apply_fixes(test_db, fixes)
    assert n == 1
    test_db.refresh(rec)
    assert rec.items[0]["fiber"] == 11.4
    assert rec.totals["fiber"] == 11.4


def test_no_verified_products_no_fixes(test_db):
    _seed_user(test_db)  # без справочника
    _add_log(test_db, [_solvie_item(1.8)])
    fixes, checked = scan(test_db)
    assert checked == 1
    assert fixes == []


def test_unmatched_record_untouched(test_db):
    _seed_user(test_db)
    _add_solvie_product(test_db)
    _add_log(test_db, [{"name": "борщ", "weight_g": 300, "calories": 150, "protein": 5, "fats": 6, "carbs": 18}])
    fixes, checked = scan(test_db)
    assert checked == 1
    assert fixes == []


def test_user_filter_scopes_scan(test_db):
    _seed_user(test_db, USER)
    _seed_user(test_db, OTHER)
    _add_solvie_product(test_db, USER)
    _add_solvie_product(test_db, OTHER)
    _add_log(test_db, [_solvie_item(1.8)], uid=USER)
    _add_log(test_db, [_solvie_item(1.8)], uid=OTHER)
    fixes, checked = scan(test_db, user_id=USER)
    assert checked == 1
    assert all(rec.user_id == USER for rec, _ in fixes)


def test_idempotent_second_run_finds_nothing(test_db):
    _seed_user(test_db)
    _add_solvie_product(test_db)
    _add_log(test_db, [_solvie_item(1.8)])
    apply_fixes(test_db, scan(test_db)[0])
    fixes, _ = scan(test_db)
    assert fixes == []


def test_limit_caps_records(test_db):
    _seed_user(test_db)
    _add_solvie_product(test_db)
    _add_log(test_db, [_solvie_item(1.8)])
    _add_log(test_db, [_solvie_item(1.8)])
    _fixes, checked = scan(test_db, limit=1)
    assert checked == 1
