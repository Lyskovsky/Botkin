from datetime import date, time

from database.crud import create_nutrition_log

UID = 895655
ITEMS = [
    {"product": "Яйца варёные", "weight_g": 165, "calories": 232, "protein": 21, "fats": 16, "carbs": 1, "fiber": 0},
    {"product": "Творог 5%", "weight_g": 200, "calories": 242, "protein": 34, "fats": 10, "carbs": 6, "fiber": 0},
    {"product": "Огурец", "weight_g": 150, "calories": 22, "protein": 1, "fats": 0, "carbs": 4, "fiber": 1},
]
TOTALS = {"calories": 496, "protein": 56, "fats": 26, "carbs": 11, "fiber": 1}


def _plan(db, **kw):
    return create_nutrition_log(
        db=db,
        user_id=UID,
        date=date(2026, 9, 6),
        meal_time=time(9, 0),
        meal_name="Завтрак",
        items=[dict(i) for i in ITEMS],
        totals=dict(TOTALS),
        status="plan",
        **kw,
    )


def test_default_status_is_eaten(test_db):
    row = create_nutrition_log(
        db=test_db,
        user_id=UID,
        date=date(2026, 9, 6),
        meal_time=time(9, 0),
        meal_name="Завтрак",
        items=[dict(i) for i in ITEMS],
        totals=dict(TOTALS),
    )
    assert row.status == "eaten"


def test_create_plan(test_db):
    assert _plan(test_db).status == "plan"
