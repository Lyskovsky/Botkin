"""Regression tests for #408: явный вес из текста не должен перетираться дефолтной порцией."""

from core.food.nutrition import process_llm_food_data

DESC = "100 грамм гречневой каши с добавлением оливкового масла 1 чайная ложка и 2 вареных яйца"


def _llm(items):
    return {"type": "food", "data": {"dish_name": "Завтрак", "items": items}}


def test_explicit_grams_survive_declension_mismatch():
    items, totals = process_llm_food_data(
        _llm(
            [
                {
                    "name": "Гречневая каша варёная",
                    "weight": 100,
                    "calories": 110,
                    "protein": 4,
                    "fats": 1,
                    "carbs": 21,
                }
            ]
        ),
        description=DESC,
    )
    assert items[0]["weight_g"] == 100
    assert items[0]["calories"] == 110


def test_default_override_scales_macros_when_no_user_weight():
    # LLM дал 50 г каши, вес в тексте не указан → дефолт 250 г, макросы масштабируются пропорционально
    items, _ = process_llm_food_data(
        _llm(
            [
                {
                    "name": "Гречневая каша варёная",
                    "weight": 50,
                    "calories": 55,
                    "protein": 2,
                    "fats": 0.5,
                    "carbs": 10.5,
                }
            ]
        ),
        description="гречневая каша",
    )
    assert items[0]["weight_g"] == 250
    assert items[0]["calories"] == 275


def test_llm_weight_equal_to_user_weight_is_trusted():
    # Имя от LLM не пересекается с текстом даже по стемам, но вес совпал с явно указанным пользователем
    items, _ = process_llm_food_data(
        _llm(
            [
                {
                    "name": "Крупа отварная",
                    "weight": 100,
                    "calories": 110,
                    "protein": 4,
                    "fats": 1,
                    "carbs": 21,
                }
            ]
        ),
        description="100 грамм гречки",
    )
    assert items[0]["weight_g"] == 100


def test_multi_item_weights_assigned_to_right_products():
    # Два продукта с разными весами: каждый item получает свой вес по стему, дефолт не трогает
    items, _ = process_llm_food_data(
        _llm(
            [
                {
                    "name": "Гречневая каша варёная",
                    "weight": 100,
                    "calories": 110,
                    "protein": 4,
                    "fats": 1,
                    "carbs": 21,
                },
                {"name": "Творог 5%", "weight": 200, "calories": 242, "protein": 34, "fats": 10, "carbs": 6},
            ]
        ),
        description="100 грамм гречневой каши и 200 грамм творога",
    )
    by_name = {it["product"]: it for it in items}
    assert by_name["Гречневая каша варёная"]["weight_g"] == 100
    assert by_name["Творог 5%"]["weight_g"] == 200


def test_ambiguous_equal_weights_do_not_bypass_default_override():
    # Два продукта по 50 г в тексте; LLM-имя третьего не совпадает ни с одним, но вес 50 совпадает
    # с обоими — совпадение неоднозначно, дефолтная порция для «каши» применяется (и макросы масштабируются)
    items, _ = process_llm_food_data(
        _llm([{"name": "Каша", "weight": 50, "calories": 55, "protein": 2, "fats": 0.5, "carbs": 10.5}]),
        description="50 г огурцов и 50 г сыра",
    )
    assert items[0]["weight_g"] == 250
    assert items[0]["calories"] == 275
