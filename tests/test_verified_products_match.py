"""Тесты post-match справочника проверенных продуктов (#255).

Solvie Protein Barre: этикетка 144 ккал / Б 16.7 / Ж 6.6 / У 4.5 / клетчатка
11.4 на порцию 50 г → на 100 г: 288 / 33.4 / 13.2 / 9.0 / 22.8.
"""

from unittest.mock import patch

from core.food.fiber_table import enrich_items_with_fiber
from core.food.verified_products import (
    match_and_apply_verified_products,
    normalize_product_name,
)
from database.crud import find_verified_product, upsert_verified_product
from database.models import NutritionLog, User

USER_ID = 111


def _make_user(db, telegram_id=USER_ID):
    db.add(User(telegram_id=telegram_id, first_name=f"user{telegram_id}"))
    db.commit()


def _add_solvie(db, user_id=USER_ID, **overrides):
    kwargs = dict(
        name="Solvie Protein Barre",
        name_norm=normalize_product_name("Solvie Protein Barre"),
        calories_per_100g=288.0,
        protein_per_100g=33.4,
        fats_per_100g=13.2,
        carbs_per_100g=9.0,
        fiber_per_100g=22.8,
        portion_g=50.0,
        brand="Solvie",
        aliases=["солви протеиновый батончик"],
        source="manual",
    )
    kwargs.update(overrides)
    return upsert_verified_product(db, user_id=user_id, **kwargs)


# ── матчинг и пересчёт ───────────────────────────────────────────────────────


def test_match_recalculates_macros_by_weight(test_db):
    _make_user(test_db)
    _add_solvie(test_db)
    items = [{"name": "Solvie Protein Barre", "weight": 50, "calories": 200, "protein": 12, "fats": 8, "carbs": 10}]

    matched = match_and_apply_verified_products(items, USER_ID, db=test_db)

    assert matched == 1
    assert items[0]["calories"] == 144.0
    assert items[0]["protein"] == 16.7
    assert items[0]["fats"] == 6.6
    assert items[0]["carbs"] == 4.5
    assert items[0]["fiber"] == 11.4


def test_match_without_weight_uses_portion(test_db):
    _make_user(test_db)
    _add_solvie(test_db)
    items = [{"name": "Solvie Protein Barre", "calories": 200}]

    assert match_and_apply_verified_products(items, USER_ID, db=test_db) == 1
    assert items[0]["weight_g"] == 50.0
    assert items[0]["calories"] == 144.0


def test_match_all_three_item_schemas(test_db):
    _make_user(test_db)
    _add_solvie(test_db)
    items = [
        {"product": "Solvie Protein Barre", "weight_g": 50},
        {"name": "Solvie Protein Barre", "weight_g": 100},
        {"food": "Solvie Protein Barre", "amount": 25},
    ]

    assert match_and_apply_verified_products(items, USER_ID, db=test_db) == 3
    assert items[0]["calories"] == 144.0
    assert items[1]["calories"] == 288.0
    assert items[2]["calories"] == 72.0


def test_match_by_alias_and_case_insensitive(test_db):
    _make_user(test_db)
    _add_solvie(test_db)
    items = [
        {"name": "СОЛВИ протеиновый батончик", "weight": 50},
        {"name": "solvie  protein-barre", "weight": 50},
    ]

    assert match_and_apply_verified_products(items, USER_ID, db=test_db) == 2


def test_match_by_brand_plus_name(test_db):
    _make_user(test_db)
    _add_solvie(test_db, name="Protein Barre", name_norm=normalize_product_name("Protein Barre"), aliases=None)
    items = [{"name": "Solvie Protein Barre", "weight": 50}]

    assert match_and_apply_verified_products(items, USER_ID, db=test_db) == 1


def test_no_substring_false_positive(test_db):
    """«Обычный батончик» не должен ловить справочный продукт по подстроке."""
    _make_user(test_db)
    _add_solvie(test_db)
    items = [{"name": "Протеиновый батончик", "weight": 50, "calories": 200}]

    assert match_and_apply_verified_products(items, USER_ID, db=test_db) == 0
    assert items[0]["calories"] == 200


def test_empty_catalog_leaves_items_untouched(test_db):
    _make_user(test_db)
    items = [{"name": "Овсянка", "weight": 100, "calories": 150}]

    assert match_and_apply_verified_products(items, USER_ID, db=test_db) == 0
    assert items[0]["calories"] == 150


def test_match_increments_times_used(test_db):
    _make_user(test_db)
    _add_solvie(test_db)
    items = [{"name": "Solvie Protein Barre", "weight": 50}]
    match_and_apply_verified_products(items, USER_ID, db=test_db)

    found = find_verified_product(test_db, USER_ID, "solvie protein barre")
    assert found.times_used == 1


def test_catalog_fiber_survives_enrichment(test_db):
    """fiber из справочника > 0 → enrich_items_with_fiber его не перетирает."""
    _make_user(test_db)
    _add_solvie(test_db)
    items = [{"name": "Solvie Protein Barre", "weight": 50, "fiber": 1.8}]

    match_and_apply_verified_products(items, USER_ID, db=test_db)
    enrich_items_with_fiber(items)

    assert items[0]["fiber"] == 11.4


# ── интеграция с save_meal_to_db ─────────────────────────────────────────────


def test_save_meal_recomputes_totals_after_match(test_db):
    _make_user(test_db)
    _add_solvie(test_db)
    import helpers.db_save as db_save

    meal_data = {
        "meal_items": [
            {"name": "Solvie Protein Barre", "weight": 50, "calories": 200, "protein": 12, "fats": 8, "carbs": 10}
        ],
        "meal_totals": {"calories": 200, "protein": 12, "fats": 8, "carbs": 10},
    }

    with (
        patch.object(db_save, "SessionLocal", return_value=test_db),
        patch("database.SessionLocal", return_value=test_db),
        # save_meal_to_db/матчер закрыли бы сессию-фикстуру — глушим close
        patch.object(test_db, "close"),
    ):
        log_id = db_save.save_meal_to_db(meal_data, "Перекус", user_id=USER_ID)
        assert log_id is not None

    row = test_db.query(NutritionLog).filter(NutritionLog.id == log_id).first()
    assert row is not None
    assert row.totals["calories"] == 144
    assert row.totals["protein"] == 17  # int(round(16.7))
    assert row.totals["fiber"] == 11.4
    assert row.items[0]["food"] == "Solvie Protein Barre"


def test_save_meal_survives_matcher_failure(test_db):
    """Ошибка справочника не должна ломать сохранение еды."""
    _make_user(test_db)
    import helpers.db_save as db_save

    meal_data = {
        "meal_items": [{"name": "Овсянка", "weight": 100, "calories": 150, "protein": 5, "fats": 3, "carbs": 25}],
        "meal_totals": {"calories": 150, "protein": 5, "fats": 3, "carbs": 25},
    }

    with (
        patch.object(db_save, "SessionLocal", return_value=test_db),
        patch(
            "core.food.verified_products.match_and_apply_verified_products",
            side_effect=RuntimeError("db down"),
        ),
        patch.object(test_db, "close"),
    ):
        log_id = db_save.save_meal_to_db(meal_data, "Завтрак", user_id=USER_ID)
        assert log_id is not None

    row = test_db.query(NutritionLog).filter(NutritionLog.id == log_id).first()
    assert row.totals["calories"] == 150
