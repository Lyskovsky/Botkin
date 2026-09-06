from datetime import date, time

import pytest

from core.food.fiber_table import _item_name as _n
from database.crud import adjust_meal_items, create_nutrition_log, get_nutrition_log, get_open_plans, set_meal_status

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


def test_adjust_scales_item_and_totals(test_db):
    row = _plan(test_db)
    res = adjust_meal_items(test_db, row.id, UID, changes=[{"idx": 0, "new_weight": 55}])
    fresh = get_nutrition_log(test_db, row.id, UID)
    assert fresh.items[0]["amount"] == 55
    assert fresh.items[0]["calories"] == pytest.approx(232 / 3, abs=1)
    assert fresh.totals["calories"] == pytest.approx(496 - 232 * 2 / 3, abs=1)
    assert res["changes"][0]["old_weight"] == 165 and res["changes"][0]["new_weight"] == 55
    assert res["deleted"] is False and res["leftover"] is None


def test_adjust_dry_run_does_not_write(test_db):
    row = _plan(test_db)
    res = adjust_meal_items(test_db, row.id, UID, changes=[{"idx": 0, "remove": True}], dry_run=True)
    fresh = get_nutrition_log(test_db, row.id, UID)
    assert len(fresh.items) == 3 and fresh.totals["calories"] == 496
    assert res["after_totals"]["calories"] == pytest.approx(264, abs=1)


def test_adjust_leftover_creates_plan_row(test_db):
    row = _plan(test_db)
    res = adjust_meal_items(
        test_db,
        row.id,
        UID,
        changes=[{"idx": 0, "new_weight": 55}, {"idx": 1, "remove": True}],
        leftover_to={"date": date(2026, 9, 6), "meal_time": time(13, 0), "meal_name": "Обед"},
        close_plan=True,
    )
    src = get_nutrition_log(test_db, row.id, UID)
    assert src.status == "eaten"
    left = get_nutrition_log(test_db, res["leftover"]["id"], UID)
    assert left.status == "plan" and left.meal_name == "Обед"
    assert sorted(_n(it) for it in left.items) == ["Творог 5%", "Яйца варёные"]
    assert left.totals["calories"] == pytest.approx(232 * 2 / 3 + 242, abs=1)


def test_adjust_removing_everything_deletes_row(test_db):
    row = _plan(test_db)
    res = adjust_meal_items(test_db, row.id, UID, changes=[{"idx": i, "remove": True} for i in range(3)])
    assert res["deleted"] is True
    assert get_nutrition_log(test_db, row.id, UID) is None


def test_adjust_close_only(test_db):
    row = _plan(test_db)
    res = adjust_meal_items(test_db, row.id, UID, changes=[], close_plan=True)
    assert get_nutrition_log(test_db, row.id, UID).status == "eaten"
    assert res["after_totals"]["calories"] == 496
    with pytest.raises(ValueError):
        adjust_meal_items(test_db, row.id, UID, changes=[])


def test_adjust_wrong_user_raises(test_db):
    row = _plan(test_db)
    with pytest.raises(LookupError):
        adjust_meal_items(test_db, row.id, 111, changes=[{"idx": 0, "remove": True}])


def test_adjust_bad_idx_raises(test_db):
    row = _plan(test_db)
    with pytest.raises(IndexError):
        adjust_meal_items(test_db, row.id, UID, changes=[{"idx": 9, "remove": True}])


def test_get_open_plans_only_today_and_plan(test_db):
    _plan(test_db)
    create_nutrition_log(
        db=test_db,
        user_id=UID,
        date=date(2026, 9, 6),
        meal_time=time(12, 0),
        meal_name="Обед",
        items=[dict(ITEMS[2])],
        totals={"calories": 22},
    )
    create_nutrition_log(
        db=test_db,
        user_id=UID,
        date=date(2026, 9, 5),
        meal_time=time(9, 0),
        meal_name="Завтрак",
        items=[dict(ITEMS[2])],
        totals={"calories": 22},
        status="plan",
    )
    assert [p.meal_name for p in get_open_plans(test_db, UID, date(2026, 9, 6))] == ["Завтрак"]


def test_set_meal_status(test_db):
    row = _plan(test_db)
    set_meal_status(test_db, row.id, UID, "eaten")
    assert get_nutrition_log(test_db, row.id, UID).status == "eaten"
    with pytest.raises(ValueError):
        set_meal_status(test_db, row.id, UID, "weird")


def test_adjust_item_without_weight_remove_goes_to_leftover(test_db):
    row = create_nutrition_log(
        db=test_db,
        user_id=UID,
        date=date(2026, 9, 6),
        meal_time=time(9, 0),
        meal_name="Завтрак",
        items=[{"product": "Кофе", "calories": 6}, dict(ITEMS[2])],
        totals={"calories": 28},
        status="plan",
    )
    res = adjust_meal_items(
        test_db,
        row.id,
        UID,
        changes=[{"idx": 0, "remove": True}],
        leftover_to={"date": date(2026, 9, 6), "meal_time": time(13, 0), "meal_name": "Обед"},
    )
    assert [_n(it) for it in res["leftover"]["items"]] == ["Кофе"]
    assert [_n(it) for it in get_nutrition_log(test_db, row.id, UID).items] == ["Огурец"]


def test_adjust_item_without_weight_partial_raises(test_db):
    row = create_nutrition_log(
        db=test_db,
        user_id=UID,
        date=date(2026, 9, 6),
        meal_time=time(9, 0),
        meal_name="Завтрак",
        items=[{"product": "Кофе", "calories": 6}],
        totals={"calories": 6},
        status="plan",
    )
    with pytest.raises(ValueError):
        adjust_meal_items(test_db, row.id, UID, changes=[{"idx": 0, "new_weight": 3}])


def test_adjust_duplicate_idx_raises(test_db):
    row = _plan(test_db)
    with pytest.raises(ValueError):
        adjust_meal_items(test_db, row.id, UID, changes=[{"idx": 0, "remove": True}, {"idx": 0, "new_weight": 10}])
