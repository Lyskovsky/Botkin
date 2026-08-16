"""Тесты ядра ретро-ре-матча (#257) — детерминированный пересчёт КБЖУ по verified_products."""

from core.food.retro_match import plan_record_fix, DEFAULT_EPSILON
from core.food.verified_products import normalize_product_name


class _Prod:
    """Мок VerifiedProduct: только поля, которые читают _build_lookup/apply_verified."""

    _next_id = 1

    def __init__(self, name, per100, *, brand=None, aliases=None, portion_g=None, fiber=None):
        self.id = _Prod._next_id
        _Prod._next_id += 1
        self.name = name
        self.name_norm = normalize_product_name(name)
        self.brand = brand
        self.aliases = aliases or []
        self.portion_g = portion_g
        self.calories_per_100g = per100["calories"]
        self.protein_per_100g = per100["protein"]
        self.fats_per_100g = per100["fats"]
        self.carbs_per_100g = per100["carbs"]
        self.fiber_per_100g = fiber


_SOLVIE_PER100 = {"calories": 360, "protein": 20, "fats": 12, "carbs": 40}


def _solvie_product():
    return _Prod("Solvie Protein Barre", _SOLVIE_PER100, portion_g=50, fiber=22.8)


# --- guard-ветки -------------------------------------------------------------


def test_no_products_returns_none():
    assert plan_record_fix([{"name": "банан", "weight_g": 100}], []) is None


def test_no_items_returns_none():
    assert plan_record_fix([], [_solvie_product()]) is None


def test_no_name_match_returns_none():
    items = [{"name": "борщ", "weight_g": 300, "calories": 150, "protein": 5, "fats": 6, "carbs": 18, "fiber": 3}]
    assert plan_record_fix(items, [_solvie_product()]) is None


# --- основной кейс: Solvie (клетчатка 1.8 → 11.4) ----------------------------


def test_solvie_fiber_corrected():
    item = {
        "name": "Solvie Protein Barre",
        "weight_g": 50,
        "calories": 180,
        "protein": 10,
        "fats": 6,
        "carbs": 20,
        "fiber": 1.8,
    }
    fix = plan_record_fix([item], [_solvie_product()])
    assert fix is not None
    assert fix.matched_count == 1
    # 22.8 per 100g × 0.5 (50 г) = 11.4
    assert fix.new_items[0]["fiber"] == 11.4
    assert any(c.field == "fiber" and c.new == 11.4 and c.old == 1.8 for c in fix.changes)


def test_input_items_not_mutated():
    item = {
        "name": "Solvie Protein Barre",
        "weight_g": 50,
        "calories": 180,
        "protein": 10,
        "fats": 6,
        "carbs": 20,
        "fiber": 1.8,
    }
    plan_record_fix([item], [_solvie_product()])
    assert item["fiber"] == 1.8  # оригинал не тронут


def test_totals_recomputed_from_fixed_items():
    item = {
        "name": "Solvie Protein Barre",
        "weight_g": 50,
        "calories": 180,
        "protein": 10,
        "fats": 6,
        "carbs": 20,
        "fiber": 1.8,
    }
    fix = plan_record_fix([item], [_solvie_product()])
    assert fix.new_totals["fiber"] == 11.4
    assert fix.new_totals["calories"] == 180


# --- идемпотентность + порог -------------------------------------------------


def test_idempotent_when_values_already_correct():
    prod = _Prod("Bombbar", {"calories": 100, "protein": 10, "fats": 5, "carbs": 8}, portion_g=60, fiber=2)
    # 100 г → ровно per100
    item = {"name": "Bombbar", "weight_g": 100, "calories": 100, "protein": 10, "fats": 5, "carbs": 8, "fiber": 2}
    assert plan_record_fix([item], [prod]) is None


def test_epsilon_ignores_tiny_diff():
    prod = _Prod("Bombbar", {"calories": 100, "protein": 10, "fats": 5, "carbs": 8}, portion_g=60, fiber=2)
    item = {"name": "Bombbar", "weight_g": 100, "calories": 100.3, "protein": 10, "fats": 5, "carbs": 8, "fiber": 2}
    # diff 0.3 < DEFAULT_EPSILON → не считаем изменением
    assert DEFAULT_EPSILON >= 0.5
    assert plan_record_fix([item], [prod]) is None


# --- матч по алиасу и «бренд имя» --------------------------------------------


def test_match_by_alias():
    prod = _Prod(
        "Протеиновый батончик",
        {"calories": 360, "protein": 20, "fats": 12, "carbs": 40},
        portion_g=50,
        fiber=22.8,
        aliases=["Solvie Protein Barre"],
    )
    item = {
        "name": "Solvie Protein Barre",
        "weight_g": 50,
        "calories": 180,
        "protein": 10,
        "fats": 6,
        "carbs": 20,
        "fiber": 1.8,
    }
    fix = plan_record_fix([item], [prod])
    assert fix is not None and fix.new_items[0]["fiber"] == 11.4


def test_match_by_brand_plus_name():
    prod = _Prod(
        "Barre", {"calories": 360, "protein": 20, "fats": 12, "carbs": 40}, portion_g=50, fiber=22.8, brand="Solvie"
    )
    item = {
        "name": "Solvie Barre",
        "weight_g": 50,
        "calories": 180,
        "protein": 10,
        "fats": 6,
        "carbs": 20,
        "fiber": 1.8,
    }
    fix = plan_record_fix([item], [prod])
    assert fix is not None and fix.new_items[0]["fiber"] == 11.4


def test_unmatched_item_kept_alongside_matched():
    prod = _solvie_product()
    items = [
        {
            "name": "Solvie Protein Barre",
            "weight_g": 50,
            "calories": 180,
            "protein": 10,
            "fats": 6,
            "carbs": 20,
            "fiber": 1.8,
        },
        {"name": "борщ", "weight_g": 300, "calories": 150, "protein": 5, "fats": 6, "carbs": 18, "fiber": 3},
    ]
    fix = plan_record_fix(items, [prod])
    assert fix is not None
    assert len(fix.new_items) == 2
    # борщ не тронут
    assert fix.new_items[1]["calories"] == 150
