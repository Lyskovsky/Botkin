"""Today-TDEE boost: дневная цель = max(14-дн среднее, фактический расход за день).

Корень бага (17.06.2026): цель калорий считалась строго по 14-дневному среднему
расходу и не реагировала на сегодняшнюю тяжёлую тренировку. В день, когда Garmin
насчитал 762 активных против среднего 380, цель оставалась заниженной и юзер
"недоедал". Решение (выбор владельца): max(среднее, сегодня) — пол по среднему
(не падает в неполный/ленивый день, закрывает апрельский баг скачущей вниз цели)
плюс рост по факту тренировки.
"""

from datetime import date, datetime, timezone
from unittest.mock import patch, MagicMock

from core.health.nutrition_targets import calculate_targets
from core.health.caloric_budget import get_day_actual_tdee, get_daily_budget
from database.crud import create_or_update_activity


# ── calculate_targets: чистая функция, ветка stats.total_calories ────────────


def test_calculate_targets_today_boost_overrides_avg():
    """today_tdee выше среднего → цель считается по сегодняшнему расходу."""
    t = calculate_targets(stats={"total_calories": 2151}, user=None, today_tdee=2416)
    # round(2416 * 0.85) = 2054
    assert t["calories"] == 2054
    assert t["avg_tdee"] == 2416


def test_calculate_targets_today_lower_keeps_avg():
    """today_tdee ниже среднего (неполный/ленивый день) → берём среднее."""
    t = calculate_targets(stats={"total_calories": 2151}, user=None, today_tdee=1796)
    # round(2151 * 0.85) = 1828
    assert t["calories"] == 1828
    assert t["avg_tdee"] == 2151


def test_calculate_targets_no_today_unchanged():
    """Без today_tdee поведение прежнее (регрессионный гард)."""
    t = calculate_targets(stats={"total_calories": 2151}, user=None)
    assert t["calories"] == 1828


def test_calculate_targets_today_boost_applies_over_manual_user():
    """Даже у юзера с ручным BMR boost применяется поверх (max)."""

    class _User:
        bmr = 1700
        avg_active_calories = 300  # manual TDEE = 2000
        target_weight_kg = 82.0

    t = calculate_targets(stats=None, user=_User(), today_tdee=2416)
    assert t["calories"] == 2054  # 2416 > 2000 → boost


# ── get_day_actual_tdee: фактический расход за дату из activity_log ───────────


def test_get_day_actual_tdee_uses_total_calories(test_db):
    d = date(2026, 6, 17)
    create_or_update_activity(
        db=test_db,
        user_id=895655,
        date=d,
        total_calories=2416,
        bmr_calories=1654,
        active_calories=762,
        source="garmin_connect",
    )
    assert get_day_actual_tdee(895655, d, db=test_db) == 2416.0


def test_get_day_actual_tdee_falls_back_to_bmr_plus_active(test_db):
    d = date(2026, 6, 16)
    create_or_update_activity(
        db=test_db,
        user_id=895655,
        date=d,
        total_calories=None,
        bmr_calories=1600,
        active_calories=400,
        source="apple_health",
    )
    assert get_day_actual_tdee(895655, d, db=test_db) == 2000.0


def test_get_day_actual_tdee_none_when_no_row(test_db):
    assert get_day_actual_tdee(895655, date(2026, 1, 1), db=test_db) is None


# ── get_daily_budget (webapp/дашборд): тот же boost, чтобы не разъезжалось ────


def _fake_settings(pct=-15):
    s = MagicMock()
    s.bmr_source = "auto"
    s.bmr_override = None
    s.activity_avg_override = None
    s.calorie_goal_pct = pct
    return s


def _patched_budget(today_tdee):
    """get_daily_budget со всеми внешними зависимостями замоканными."""
    avg = {"total_calories": 2151, "bmr_calories": 1771, "active_calories": 380}
    return [
        patch("database.SessionLocal", return_value=MagicMock()),
        patch("database.crud.get_user_settings", return_value=_fake_settings()),
        patch("database.crud.get_average_activity_stats", return_value=avg),
        patch("database.crud.get_activities_by_period", return_value=[]),
        patch("database.crud.get_nutrition_totals_by_date", return_value={"calories": 0}),
        patch("core.health.caloric_budget.get_day_actual_tdee", return_value=today_tdee),
        patch("core.health.caloric_budget.get_user_tz", return_value=timezone.utc),
    ]


def test_get_daily_budget_boosts_target_on_heavy_day():
    # Boost-семантика действует только для СЕГОДНЯ: для прошедших дней с 02.07.2026
    # цель считается от факта дня (см. tests/test_day_energy_fact.py).
    today = datetime.now(timezone.utc).date()
    patches = _patched_budget(today_tdee=2416)
    for p in patches:
        p.start()
    try:
        b = get_daily_budget(user_id=895655, for_date=today)
    finally:
        for p in patches:
            p.stop()
    assert b["target"] == 2054  # round(2416 * 0.85)
    # Среднее для отображения остаётся средним, растёт только цель.
    assert b["activity_avg"] == 380
    assert b["bmr_avg"] == 1771


def test_get_daily_budget_keeps_avg_when_today_lower():
    today = datetime.now(timezone.utc).date()
    patches = _patched_budget(today_tdee=1796)
    for p in patches:
        p.start()
    try:
        b = get_daily_budget(user_id=895655, for_date=today)
    finally:
        for p in patches:
            p.stop()
    assert b["target"] == 1828  # round(2151 * 0.85), today ниже среднего
