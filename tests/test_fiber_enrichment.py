"""Tests for fiber enrichment pipeline.

Covers:
  - fiber_per_100g / estimate_fiber (base lookup)
  - _item_name / _item_weight (schema adapter for the 3 item formats in DB)
  - enrich_items_with_fiber (idempotent mutation)
  - sum_fiber (roll-up)
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.food.fiber_table import (
    enrich_items_with_fiber,
    estimate_fiber,
    fiber_per_100g,
    sum_fiber,
    _item_name,
    _item_weight,
)


# ── base lookup ───────────────────────────────────────────────────────────────


def test_fiber_per_100g_known_foods():
    assert fiber_per_100g("тыква") == 2.1
    assert fiber_per_100g("шпинат") == 2.2
    assert fiber_per_100g("чечевица отварная") == 7.9  # matches "чечевиц"
    assert fiber_per_100g("семена чиа") == 34.4


def test_fiber_per_100g_psyllium_variants():
    """Псиллиум — почти чистая клетчатка (~85%). LLM иногда забывает про fiber
    когда название без скобок «(БАД)» — fiber_table должен подстраховать."""
    assert fiber_per_100g("Псиллиум") == 85.0
    assert fiber_per_100g("Псиллиум (БАД)") == 85.0
    assert fiber_per_100g("псилиум") == 85.0  # misspelling
    assert fiber_per_100g("Psyllium husk") == 85.0


def test_estimate_fiber_psyllium_for_nika_case():
    """Регрессия: Ника залогала «Псиллиум 3г», LLM вернул fiber=0,
    enrich_items_with_fiber должен дать ~2.5г клетчатки.
    Python round() использует банковское округление: 2.55 → 2.5."""
    assert estimate_fiber("Псиллиум", 3) == 2.5  # 85 × 3 / 100 = 2.55 → 2.5
    assert estimate_fiber("Псиллиум (БАД)", 5) == 4.2  # 85 × 5 / 100 = 4.25 → 4.2
    assert estimate_fiber("Псиллиум", 10) == 8.5  # 8.5 точно


def test_fiber_per_100g_unknown_returns_none():
    assert fiber_per_100g("говядина тушёная") is None
    assert fiber_per_100g("") is None
    assert fiber_per_100g("нечто незнакомое") is None


def test_estimate_fiber_scales_by_weight():
    # 100g tyk = 2.1g fiber, 200g = 4.2g
    assert estimate_fiber("тыква запечённая", 200) == 4.2
    assert estimate_fiber("шпинат", 50) == 1.1


def test_estimate_fiber_zero_when_no_match():
    assert estimate_fiber("говядина", 300) == 0.0
    assert estimate_fiber("сыр", 100) == 0.0


def test_estimate_fiber_zero_when_no_weight():
    assert estimate_fiber("тыква", None) == 0.0
    assert estimate_fiber("тыква", 0) == 0.0


# ── schema adapter ────────────────────────────────────────────────────────────


def test_item_name_handles_all_schemas():
    # DB LLM format
    assert _item_name({"food": "Уха"}) == "Уха"
    # Internal meal_items format
    assert _item_name({"product": "Рис"}) == "Рис"
    # Supplements format
    assert _item_name({"name": "Псиллиум"}) == "Псиллиум"
    # Priority: product > name > food
    assert _item_name({"product": "A", "name": "B", "food": "C"}) == "A"
    assert _item_name({}) == ""


def test_item_weight_handles_all_schemas():
    assert _item_weight({"weight_g": 100}) == 100.0
    assert _item_weight({"amount": 150}) == 150.0
    assert _item_weight({"weight": 200}) == 200.0
    # Priority: weight_g > amount > weight
    assert _item_weight({"weight_g": 50, "amount": 999}) == 50.0
    assert _item_weight({}) == 0.0
    # Handles string numbers
    assert _item_weight({"amount": "100.0"}) == 100.0


# ── enrich_items_with_fiber ───────────────────────────────────────────────────


def test_enrich_idempotent_preserves_existing_fiber():
    items = [{"food": "Псиллиум", "amount": 5, "fiber": 4.0}]
    enrich_items_with_fiber(items)
    # Existing fiber > 0 must not be overwritten
    assert items[0]["fiber"] == 4.0


def test_enrich_fills_missing_fiber():
    items = [{"food": "Салат с тыквой", "amount": 200}]
    enrich_items_with_fiber(items)
    # "тыкв" matches before "салат" in the ordered table — it's placed after though,
    # so "салат" wins with 1.3g/100g × 200g = 2.6g
    # Actually order: салат (line ~110) matched first; тыкв (line ~90) even earlier.
    # Either way, fiber must be > 0.
    assert items[0]["fiber"] > 0


def test_enrich_overwrites_zero_fiber():
    # Legacy items stored with explicit fiber=0 should still get estimate
    items = [{"food": "Шпинат припущенный", "amount": 100, "fiber": 0}]
    enrich_items_with_fiber(items)
    assert items[0]["fiber"] == 2.2  # from fiber_table


def test_enrich_handles_unknown_food():
    items = [{"food": "Говядина тушёная", "amount": 200, "fiber": 0}]
    enrich_items_with_fiber(items)
    # Meat has no fiber match — stays 0
    assert items[0].get("fiber", 0) == 0


def test_enrich_handles_mixed_meal():
    """Real-world example: today's meal with mixed items."""
    items = [
        {"food": "Уха", "amount": 300},  # уха dish → 0.8 * 3 = 2.4
        {"food": "Салат с креветками, тыквой и зеленью", "amount": 200},  # matches
        {"name": "Псиллиум (БАД)", "weight_g": 5, "fiber": 4.0},  # preserve
    ]
    enrich_items_with_fiber(items)
    assert items[0]["fiber"] == 2.4  # Уха dish-level match
    assert items[1]["fiber"] > 0  # Салат matches
    assert items[2]["fiber"] == 4.0  # Псиллиум preserved


# ── dish-level entries (added via frequency analysis of real logs) ──────────


def test_dish_soups_match():
    assert fiber_per_100g("Уха") == 0.8
    assert fiber_per_100g("Борщ со свёклой") == 2.0
    assert fiber_per_100g("Щи") == 1.5
    assert fiber_per_100g("Куриный суп с лапшой") == 0.8
    # Generic suffix fallback
    assert fiber_per_100g("Какой-то суп") == 1.0


def test_dish_bakery_match():
    assert fiber_per_100g("Сочник с творогом") == 1.0
    assert fiber_per_100g("Сырники") == 0.6
    assert fiber_per_100g("Пирог с курицей") == 1.5
    assert fiber_per_100g("Овсяное печенье") == 4.0
    # Generic печенье fallback
    assert fiber_per_100g("Печенье Oreo") == 2.0


def test_dish_bars_and_chocolate():
    # Bombbar substring match
    assert fiber_per_100g("Протеиновый батончик Bombbar") == 4.5
    assert fiber_per_100g("Bombbar Slim Nuts") == 4.5
    # Dark chocolate gets higher value than milk
    assert fiber_per_100g("Горький шоколад 85%") == 10.5
    assert fiber_per_100g("Молочный шоколад") == 2.5


def test_dish_level_wins_over_ingredient():
    """Dish-level entries are placed first in the table, so compound dish
    names match the dish estimate even when a high-fiber ingredient is
    mentioned. This gives consistent per-dish expectations at the cost
    of slight accuracy loss on known-ingredient variants."""
    # "Паста со шпинатом" → паст (1.8) wins, not шпинат (2.2)
    assert fiber_per_100g("Паста со шпинатом и кальмарами") == 1.8
    # "Пирог с тыквой" → пирог (1.5) wins, not тыкв (2.1)
    assert fiber_per_100g("Пирог с тыквой") == 1.5
    # "Овсяное печенье" → печенье овсян (4.0) wins, not овсян (10.0)
    assert fiber_per_100g("Овсяное печенье") == 4.0


def test_dish_real_unmatched_from_migration():
    """Names taken directly from backfill_fiber_all_history.py unmatched report."""
    # From today's actual DB: these used to be 0, should now have values
    assert estimate_fiber("Сочник с творогом", 160) == 1.6  # 1.0 * 1.6
    assert estimate_fiber("Уха", 300) == 2.4  # 0.8 * 3
    assert estimate_fiber("Куриный суп с лапшой", 400) == 3.2  # 0.8 * 4
    # Пирог с курицей 100g — should match "пирог" (1.5) since курица has no fiber match
    assert estimate_fiber("Пирог с курицей", 100) == 1.5


def test_enrich_returns_same_list():
    items = [{"food": "Тыква", "amount": 100}]
    result = enrich_items_with_fiber(items)
    assert result is items  # same reference, mutation in place


# ── sum_fiber ─────────────────────────────────────────────────────────────────


def test_sum_fiber_basic():
    items = [
        {"fiber": 2.5},
        {"fiber": 1.0},
        {"fiber": 0},
    ]
    assert sum_fiber(items) == 3.5


def test_sum_fiber_missing_field():
    items = [{"fiber": 2.0}, {}]
    assert sum_fiber(items) == 2.0


def test_sum_fiber_null_values():
    items = [{"fiber": None}, {"fiber": "invalid"}, {"fiber": 1.5}]
    assert sum_fiber(items) == 1.5


# --- Прецедент 02.07.2026: "Овсяная каша готовая" 187г возвращала 18.7г ---
# клетчатки (сухая норма 10.0/100г) вместо варёной ~1.7/100г. Пользователь и
# агент сами заметили аномалию в чате. Варёная каша должна перехватываться
# ДО generic ингредиента "овсян" (10.0, для сырой крупы/хлопьев).


@pytest.mark.parametrize(
    "name,weight,expected",
    [
        ("Овсяная каша готовая", 187, 3.2),
        ("Овсяная каша варёная", 250, 4.2),
        ("Каша овсяная на молоке", 200, 3.4),
        ("овсяная каша", 200, 3.4),
        ("Овсяная каша с маслом", 150, 2.5),
    ],
)
def test_cooked_oatmeal_porridge_uses_cooked_fiber_value(name, weight, expected):
    assert estimate_fiber(name, weight) == expected


def test_oat_cookie_still_uses_cookie_value_not_porridge():
    """Овсяное печенье не должно перехватываться новым паттерном "овсяная каша"."""
    assert estimate_fiber("Овсяное печенье", 40) == 1.6


def test_raw_dry_oats_still_uses_dry_value():
    """Явно сырая крупа (без слова "каша") — прежняя сухая норма 10.0/100г."""
    assert estimate_fiber("овсянка", 40) == 4.0


# --- Прецедент 02.07.2026: "сух-УХА-я"/"с-УХА-рики" ложно матчили суп "уха" ---
# (0.8/100г) вместо разумной нормы для сухариков/крутонов. "уха" как отдельное
# блюдо не встретилась ни разу в реальной истории бота — только в этих ложных
# срабатываниях. См. остаточный кейс "Овсянка (сухая)" в todo.md.


def test_crouton_salad_not_misdetected_as_fish_soup():
    assert estimate_fiber("Салат с кальмаром и сухариками в майонезе", 300) == 9.0


def test_real_fish_soup_still_matches():
    """Явное 'уха' (без 'сух-' коллизии) продолжает матчить суп."""
    assert estimate_fiber("Уха из окуня", 300) == 2.4


# --- Прецедент 10.07.2026: "Exponenta High-Pro Малина-Банан (протеиновый кефир)"
# 250г получил 16.2г клетчатки от ключа "малин" (6.5/100г, для настоящей малины),
# хотя "Малина-Банан" здесь — вкус ароматизатора, а не ингредиент. У протеинового
# кефира клетчатки нет. Специфичные ключи для протеиновых напитков должны стоять
# ДО fruit-секции.


def test_protein_drink_flavor_name_not_misdetected_as_fruit():
    assert estimate_fiber("Exponenta High-Pro Малина-Банан (протеиновый кефир)", 250) == 0.0
    assert fiber_per_100g("Протеиновый кефир вкус клубника-банан") == 0.0
    assert fiber_per_100g("Exponenta High-Pro") == 0.0
