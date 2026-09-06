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
