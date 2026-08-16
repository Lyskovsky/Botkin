"""MealStateData — typed schema для meal-flow ветки UserState.data.

Заводится как follow-up к #256/#258: dict-литералы с ключами meal_items/
meal_totals/photo_paths собирались вручную в 4+ местах в photo.py/text.py,
опечатка в имени ключа (photo_path вместо photo_paths, #256) молча теряла
данные вместо ошибки. MealStateData (extra="forbid") ловит такую опечатку
в момент создания состояния — не в save_meal_to_db() при чтении.
"""

import pytest
from pydantic import ValidationError

from services.state_helpers import build_meal_state_data
from services.state_models import MealStateData


def test_rejects_unknown_field_photo_path_typo():
    with pytest.raises(ValidationError):
        MealStateData(
            meal_items=[{"product": "Банан"}],
            meal_totals={"calories": 100},
            photo_path=["/tmp/x.jpg"],  # опечатка: должно быть photo_paths
        )


def test_requires_meal_items():
    with pytest.raises(ValidationError):
        MealStateData(meal_totals={"calories": 100})


def test_requires_meal_totals():
    with pytest.raises(ValidationError):
        MealStateData(meal_items=[{"product": "Банан"}])


def test_accepts_valid_minimal_data():
    data = MealStateData(meal_items=[{"product": "Банан"}], meal_totals={"calories": 100})
    assert data.photo_paths == []
    assert data.meal_name is None


def test_accepts_meal_totals_with_kcal_warnings_list():
    """#279: kcal_warnings — list[dict] (не float) внутри meal_totals,
    заполняется check_kcal_consistency()/check_density_sanity() в
    core/food/nutrition.py. Раньше meal_totals был Dict[str, float] и
    падал с ValidationError на слот-пикере фото без подписи (#181)."""
    data = MealStateData(
        meal_items=[{"product": "Банан"}],
        meal_totals={
            "calories": 100,
            "kcal_warnings": [{"name": "Кофе", "stated": 45.0, "macro": 24.0, "diff": 21.0}],
        },
    )
    assert data.meal_totals["kcal_warnings"][0]["diff"] == 21.0


def test_build_meal_state_data_returns_plain_dict_for_user_state():
    result = build_meal_state_data(
        meal_items=[{"product": "Банан"}],
        meal_totals={"calories": 100},
        photo_paths=["/tmp/x.jpg"],
        meal_name="Завтрак",
    )
    assert result == {
        "meal_items": [{"product": "Банан"}],
        "meal_totals": {"calories": 100.0},
        "photo_paths": ["/tmp/x.jpg"],
        "meal_name": "Завтрак",
    }


def test_build_meal_state_data_raises_on_typo():
    with pytest.raises(ValidationError):
        build_meal_state_data(
            meal_items=[{"product": "Банан"}],
            meal_totals={"calories": 100},
            photo_path=["/tmp/x.jpg"],  # опечатка
        )


def test_multi_meals_alone_is_valid_without_meal_items():
    """Контейнер multi_meals (несколько явных приёмов пищи, #53) не несёт

    meal_items/meal_totals на верхнем уровне — они вложены в каждый элемент
    списка. Модель должна принимать эту форму, не требуя top-level meal_items.
    """
    data = MealStateData(
        source="text",
        description="завтрак и обед",
        multi_meals=[
            {"meal_name": "Завтрак", "meal_items": [{"product": "Яйца"}], "meal_totals": {"calories": 200}},
        ],
    )
    assert data.meal_items is None
    assert data.meal_totals is None
    assert len(data.multi_meals) == 1


def test_neither_meal_items_nor_multi_meals_raises():
    with pytest.raises(ValidationError):
        MealStateData(description="пусто")


def test_rejects_multi_meals_container_with_typo_in_sub_meal_via_helper():
    """Опечатка внутри вложенного элемента multi_meals (тот же класс бага,

    #256) должна ловиться build_meal_state_data() для каждого под-приёма.
    """
    with pytest.raises(ValidationError):
        build_meal_state_data(
            meal_name="Завтрак",
            meal_items=[{"product": "Яйца"}],
            meal_totals={"calories": 200},
            photo_path=["/tmp/x.jpg"],  # опечатка, как и в основном флоу
        )
