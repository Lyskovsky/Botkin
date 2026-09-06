"""Тесты для #409: КБЖУ этикетки «на 100 г» пересчитывается на вес упаковки."""

import pytest

from core.food.label_consistency import reconcile_items_with_label
from core.food.nutrition import process_llm_food_data

LABEL = {
    "name": "Ролл",
    "calories_per_100g": 284,
    "protein_per_100g": 13,
    "fats_per_100g": 17,
    "carbs_per_100g": 20,
}


def test_per100_applied_to_whole_pack_is_fixed():
    items = [{"name": "Ролл", "weight": 235, "calories": 284, "protein": 13, "fats": 17, "carbs": 20}]
    out = reconcile_items_with_label(items, LABEL)
    assert out[0]["calories"] == pytest.approx(667, abs=1)
    assert out[0]["protein"] == pytest.approx(30.6, abs=0.1)
    assert out[0].get("label_fixed") is True


def test_consistent_item_untouched():
    items = [{"name": "Ролл", "weight": 235, "calories": 667, "protein": 30.6, "fats": 40, "carbs": 47}]
    out = reconcile_items_with_label(items, LABEL)
    assert out[0]["calories"] == 667 and "label_fixed" not in out[0]


def test_weight_100_is_untouched():
    assert reconcile_items_with_label([{"name": "Ролл", "weight": 100, "calories": 284}], LABEL)[0]["calories"] == 284


def test_no_label_or_multi_item_untouched():
    items = [{"name": "A", "weight": 235, "calories": 284}, {"name": "B", "weight": 50, "calories": 100}]
    assert reconcile_items_with_label(items, LABEL) == items
    assert reconcile_items_with_label(items[:1], None) == items[:1]


def test_process_llm_food_data_uses_label():
    llm = {
        "type": "food",
        "data": {
            "dish_name": "Ролл",
            "product_label": LABEL,
            "items": [{"name": "Ролл", "weight": 235, "calories": 284, "protein": 13, "fats": 17, "carbs": 20}],
        },
    }
    items, totals = process_llm_food_data(llm, description="")
    assert totals["calories"] == pytest.approx(667, abs=1)
