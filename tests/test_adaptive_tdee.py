"""Адаптивный TDEE по методу MacroFactor (#358, 15.08.2026).

TDEE (maintenance) выводится из собственных данных пользователя:
    TDEE = средний фактический рацион − 7700 × скорость изменения веса (кг/день)

Девайсовые оценки (Garmin/Apple) систематически смещены: у владельца Garmin
показывал 2400–2500, старое среднее бота — 1961, а два независимых сегмента
питание+вес (135 и 48 дней) дали 2260 и 2362. Адаптивная оценка честнее,
потому что закрывает энергобаланс по факту.

Условия включения: ≥21 полный день лога питания и ≥2 взвешивания с разбросом
≥14 дней внутри окна. Иначе — None (вызывающий откатывается на Garmin/Apple/manual).
"""

from datetime import date, datetime, time, timedelta, timezone
from unittest.mock import MagicMock, patch

from core.health.adaptive_tdee import (
    KCAL_PER_KG_FAT,
    MIN_LOGGED_DAYS,
    MIN_WEIGHT_SPAN_DAYS,
    compute_adaptive_tdee,
    get_adaptive_tdee,
)
from core.health.caloric_budget import get_daily_budget
from core.health.nutrition_targets import calculate_targets

AS_OF = date(2026, 8, 15)


def _intake(days: int, kcal: float = 2000, end: date = AS_OF) -> dict:
    """N подряд идущих полностью залогированных дней, заканчивая end."""
    return {end - timedelta(days=i): kcal for i in range(days)}


# ── compute_adaptive_tdee: чистая функция ────────────────────────────────────


def test_maintenance_on_stable_weight():
    """Вес не меняется → TDEE = средний рацион."""
    intake = _intake(30, kcal=2000)
    weights = [(AS_OF - timedelta(days=27), 84.0), (AS_OF, 84.0)]
    r = compute_adaptive_tdee(intake, weights, AS_OF)
    assert r is not None
    assert r["tdee"] == 2000
    assert r["days_used"] == 30


def test_deficit_weight_loss_raises_tdee_above_intake():
    """Худеет на рационе 1900 → maintenance выше рациона на 7700×скорость."""
    intake = _intake(30, kcal=1900)
    weights = [(AS_OF - timedelta(days=27), 84.0), (AS_OF, 83.0)]
    r = compute_adaptive_tdee(intake, weights, AS_OF)
    # rate = -1.0/27 кг/день → tdee = 1900 + 7700/27 ≈ 2185
    assert r["tdee"] == round(1900 + KCAL_PER_KG_FAT / 27)


def test_surplus_weight_gain_lowers_tdee_below_intake():
    intake = _intake(30, kcal=2500)
    weights = [(AS_OF - timedelta(days=27), 83.0), (AS_OF, 84.0)]
    r = compute_adaptive_tdee(intake, weights, AS_OF)
    assert r["tdee"] == round(2500 - KCAL_PER_KG_FAT / 27)


def test_weight_anchors_smoothed_pm5_days():
    """Взвешивания в ±5 дней от крайних усредняются (сглаживание шума весов)."""
    intake = _intake(30, kcal=1900)
    weights = [
        (AS_OF - timedelta(days=27), 84.4),
        (AS_OF - timedelta(days=26), 83.6),  # анкер старта = (84.4+83.6)/2 = 84.0
        (AS_OF - timedelta(days=1), 83.2),
        (AS_OF, 82.8),  # анкер конца = (83.2+82.8)/2 = 83.0
    ]
    r = compute_adaptive_tdee(intake, weights, AS_OF)
    assert r["tdee"] == round(1900 + KCAL_PER_KG_FAT / 27)


def test_not_enough_logged_days_returns_none():
    intake = _intake(MIN_LOGGED_DAYS - 1, kcal=2000)
    weights = [(AS_OF - timedelta(days=20), 84.0), (AS_OF, 84.0)]
    assert compute_adaptive_tdee(intake, weights, AS_OF) is None


def test_weight_span_too_short_returns_none():
    intake = _intake(30, kcal=2000)
    weights = [(AS_OF - timedelta(days=MIN_WEIGHT_SPAN_DAYS - 1), 84.0), (AS_OF, 84.0)]
    assert compute_adaptive_tdee(intake, weights, AS_OF) is None


def test_single_weighing_returns_none():
    intake = _intake(30, kcal=2000)
    assert compute_adaptive_tdee(intake, [(AS_OF, 84.0)], AS_OF) is None


def test_gap_days_do_not_dilute_average():
    """Дни вообще без записей — дырки в логировании, в средний рацион не входят."""
    intake = {AS_OF - timedelta(days=i): 2000 for i in range(0, 50, 2)}  # 25 дней через день
    weights = [(AS_OF - timedelta(days=40), 84.0), (AS_OF, 84.0)]
    r = compute_adaptive_tdee(intake, weights, AS_OF)
    assert r is not None
    assert r["tdee"] == 2000
    assert r["days_used"] >= 21


def test_partially_logged_day_excluded():
    """День с подозрительно малым логом (< 50% медианы) — не логирован целиком."""
    intake = _intake(30, kcal=2000)
    intake[AS_OF - timedelta(days=3)] = 300  # явно неполный день
    weights = [(AS_OF - timedelta(days=27), 84.0), (AS_OF, 84.0)]
    r = compute_adaptive_tdee(intake, weights, AS_OF)
    assert r["tdee"] == 2000
    assert r["days_used"] == 29


def test_implausible_tdee_returns_none():
    """Мусорные данные (нереальная скорость похудения) → None, не кривая цель."""
    intake = _intake(30, kcal=1800)
    weights = [(AS_OF - timedelta(days=14), 95.0), (AS_OF, 84.0)]  # -11 кг за 2 недели
    assert compute_adaptive_tdee(intake, weights, AS_OF) is None


# ── get_adaptive_tdee: обёртка над Postgres ──────────────────────────────────


def test_get_adaptive_tdee_from_db(test_db):
    from database.crud import create_nutrition_log, create_weight

    uid = 895655
    for i in range(30):
        d = AS_OF - timedelta(days=i)
        create_nutrition_log(
            db=test_db,
            user_id=uid,
            date=d,
            meal_time=time(12, 0),
            meal_name="lunch",
            items=[{"name": "еда"}],
            totals={"calories": 1900, "protein": 100, "fats": 60, "carbs": 200},
        )
    create_weight(test_db, uid, datetime(2026, 7, 19, 8, 0, tzinfo=timezone.utc), 84.0)
    create_weight(test_db, uid, datetime(2026, 8, 15, 8, 0, tzinfo=timezone.utc), 83.0)

    r = get_adaptive_tdee(uid, as_of=AS_OF, db=test_db)
    assert r is not None
    assert r["tdee"] == round(1900 + KCAL_PER_KG_FAT / 27)
    assert r["days_used"] == 30


def test_get_adaptive_tdee_isolated_by_user(test_db):
    """Данные другого юзера не подмешиваются (обязательный WHERE user_id)."""
    from database.crud import create_nutrition_log, create_weight

    other = 111
    for i in range(30):
        create_nutrition_log(
            db=test_db,
            user_id=other,
            date=AS_OF - timedelta(days=i),
            meal_time=time(12, 0),
            meal_name="lunch",
            items=[],
            totals={"calories": 3000},
        )
    create_weight(test_db, other, datetime(2026, 7, 19, tzinfo=timezone.utc), 90.0)
    create_weight(test_db, other, datetime(2026, 8, 15, tzinfo=timezone.utc), 90.0)

    assert get_adaptive_tdee(895655, as_of=AS_OF, db=test_db) is None


def test_weight_dates_converted_to_user_timezone(test_db):
    """Дата взвешивания — локальный день пользователя, не UTC-день (HIGH из ревью).

    21:30 UTC = 00:30 следующего дня в UTC+3: взвешивание должно лечь на
    локальную дату, иначе анкер и span сдвигаются на день.
    """
    from database.crud import create_nutrition_log, create_weight

    uid = 895655
    for i in range(30):
        create_nutrition_log(
            db=test_db,
            user_id=uid,
            date=AS_OF - timedelta(days=i),
            meal_time=time(12, 0),
            meal_name="lunch",
            items=[],
            totals={"calories": 1900},
        )
    # По UTC это AS_OF-28, но в UTC+3 уже AS_OF-27
    w1 = datetime.combine(AS_OF - timedelta(days=28), time(21, 30), tzinfo=timezone.utc)
    w2 = datetime.combine(AS_OF, time(8, 0), tzinfo=timezone.utc)
    create_weight(test_db, uid, w1, 84.0)
    create_weight(test_db, uid, w2, 83.0)

    with patch("core.infra.tz.get_user_tz", return_value=timezone(timedelta(hours=3))):
        r = get_adaptive_tdee(uid, as_of=AS_OF, db=test_db)

    assert r is not None
    assert r["span_days"] == 27
    assert r["tdee"] == round(1900 + KCAL_PER_KG_FAT / 27)


def test_smoothing_includes_weights_after_as_of(test_db):
    """Ретроспективный расчёт (/day за прошлую дату): взвешивания в ±5 дней
    ПОСЛЕ as_of тоже сглаживают конечный анкер (MEDIUM из ревью — иначе TDEE
    для одной и той же даты различался «в моменте» и задним числом)."""
    from database.crud import create_nutrition_log, create_weight

    uid = 895655
    for i in range(30):
        create_nutrition_log(
            db=test_db,
            user_id=uid,
            date=AS_OF - timedelta(days=i),
            meal_time=time(12, 0),
            meal_name="lunch",
            items=[],
            totals={"calories": 1900},
        )
    create_weight(test_db, uid, datetime.combine(AS_OF - timedelta(days=27), time(8, 0), tzinfo=timezone.utc), 84.0)
    create_weight(test_db, uid, datetime.combine(AS_OF, time(8, 0), tzinfo=timezone.utc), 83.0)
    # Будущая точка в ±5 дней от конечного анкера — должна попасть в сглаживание
    create_weight(test_db, uid, datetime.combine(AS_OF + timedelta(days=3), time(8, 0), tzinfo=timezone.utc), 82.0)

    with patch("core.infra.tz.get_user_tz", return_value=timezone.utc):
        r = get_adaptive_tdee(uid, as_of=AS_OF, db=test_db)

    # анкер конца = mean(83.0, 82.0) = 82.5 → Δ = -1.5 кг за 27 дней
    assert r["tdee"] == round(1900 + KCAL_PER_KG_FAT * 1.5 / 27)


# ── calculate_targets: адаптивный TDEE приоритетнее девайсов и manual ────────


def test_calculate_targets_adaptive_beats_stats():
    t = calculate_targets(stats={"total_calories": 1961}, user=None, adaptive_tdee=2300)
    assert t["avg_tdee"] == 2300
    assert t["calories"] == round(2300 * 0.85)


def test_calculate_targets_adaptive_beats_manual_user():
    class _User:
        bmr = 1700
        avg_active_calories = 300  # manual TDEE = 2000
        target_weight_kg = 82.0
        telegram_id = 895655

    t = calculate_targets(stats=None, user=_User(), adaptive_tdee=2300)
    assert t["avg_tdee"] == 2300


def test_calculate_targets_today_boost_applies_over_adaptive():
    """Тяжёлая тренировка: факт дня выше адаптивного → цель растёт по факту."""
    t = calculate_targets(stats=None, user=None, adaptive_tdee=2300, today_tdee=2600)
    assert t["avg_tdee"] == 2600


# ── get_daily_budget: интеграция (bmr_source='adaptive') ─────────────────────


def _fake_settings(pct=-15):
    s = MagicMock()
    s.bmr_source = "auto"
    s.bmr_override = None
    s.activity_avg_override = None
    s.calorie_goal_pct = pct
    return s


def _patched_budget(adaptive, today_tdee=None):
    avg = {"total_calories": 2151, "bmr_calories": 1771, "active_calories": 380}
    return [
        patch("database.SessionLocal", return_value=MagicMock()),
        patch("database.crud.get_user_settings", return_value=_fake_settings()),
        patch("database.crud.get_average_activity_stats", return_value=avg),
        patch("database.crud.get_activities_by_period", return_value=[]),
        patch("database.crud.get_nutrition_totals_by_date", return_value={"calories": 0}),
        patch("core.health.adaptive_tdee.get_adaptive_tdee", return_value=adaptive),
        patch("core.health.caloric_budget.get_day_actual_tdee", return_value=today_tdee),
        patch("core.health.caloric_budget.get_user_tz", return_value=timezone.utc),
    ]


def _run_budget(patches):
    for p in patches:
        p.start()
    try:
        return get_daily_budget(user_id=895655, for_date=datetime.now(timezone.utc).date())
    finally:
        for p in patches:
            p.stop()


def test_get_daily_budget_uses_adaptive_as_base():
    adaptive = {"tdee": 2300, "days_used": 40, "span_days": 27}
    b = _run_budget(_patched_budget(adaptive))
    assert b["bmr_source"] == "adaptive"
    assert b["tdee_avg"] == 2300
    assert b["target"] == round(2300 * 0.85)
    assert b["tdee_days"] == 40
    assert b["has_garmin"] is True  # цель достоверная, хинт «≈ среднее» не нужен


def test_get_daily_budget_today_boost_survives_adaptive():
    adaptive = {"tdee": 2300, "days_used": 40, "span_days": 27}
    b = _run_budget(_patched_budget(adaptive, today_tdee=2600))
    assert b["target"] == round(2600 * 0.85)
    assert b["bmr_source"] == "adaptive"


def test_get_daily_budget_adaptive_keeps_device_activity_avg():
    """activity_avg — честное девайсовое среднее, а не «адаптивный TDEE − BMR»
    (HIGH из арх-ревью: 2300−1771=529 — выдуманное число)."""
    adaptive = {"tdee": 2300, "days_used": 40, "span_days": 27}
    b = _run_budget(_patched_budget(adaptive))
    assert b["activity_avg"] == 380  # 2151 − 1771 по девайсу
    assert b["tdee_avg"] == 2300


def test_get_daily_budget_past_day_fact_clears_adaptive_label():
    """Прошедший день с полным фактом: цель от факта девайса → ярлык не
    «adaptive» и без tdee_days (HIGH из арх-ревью: противоречивая подпись
    «от факта дня · по вашим данным за N дн.»)."""
    adaptive = {"tdee": 2300, "days_used": 40, "span_days": 27}
    yesterday = datetime.now(timezone.utc).date() - timedelta(days=1)
    patches = _patched_budget(adaptive) + [
        patch(
            "core.health.caloric_budget.get_day_energy_fact",
            return_value={"tdee": 2400.0, "bmr": 1650.0, "active": 750.0, "incomplete": False},
        ),
    ]
    for p in patches:
        p.start()
    try:
        b = get_daily_budget(user_id=895655, for_date=yesterday)
    finally:
        for p in patches:
            p.stop()
    assert b["target"] == round(2400 * 0.85)
    assert b["bmr_source"] != "adaptive"
    assert b["tdee_days"] is None


def test_get_day_stats_past_fact_clears_adaptive_source(test_db, mock_session_local):
    """/day за прошедший день с полным Garmin-фактом: подпись «по вашим
    данным» не показывается — цель посчитана от факта, не от adaptive."""
    from database.crud import create_nutrition_log, create_weight, create_or_update_activity
    from services.nutrition_service import NutritionService

    uid = 895655
    yesterday = datetime.now(timezone.utc).date() - timedelta(days=1)
    for i in range(30):
        create_nutrition_log(
            db=test_db,
            user_id=uid,
            date=yesterday - timedelta(days=i),
            meal_time=time(12, 0),
            meal_name="lunch",
            items=[],
            totals={"calories": 1900},
        )
    create_weight(test_db, uid, datetime.combine(yesterday - timedelta(days=27), time(8, 0), tzinfo=timezone.utc), 84.0)
    create_weight(test_db, uid, datetime.combine(yesterday, time(8, 0), tzinfo=timezone.utc), 83.0)
    create_or_update_activity(
        db=test_db,
        user_id=uid,
        date=yesterday,
        total_calories=2400,
        bmr_calories=1650,
        active_calories=750,
        source="garmin_connect",
    )

    with patch("core.infra.tz.get_user_tz", return_value=timezone.utc):
        stats = NutritionService(user_id=uid).get_day_stats(yesterday)

    assert stats["targets"]["calories"] == round(2400 * 0.85)
    assert stats["targets"]["tdee_source"] is None
    assert stats["targets"]["tdee_days"] is None


def test_get_daily_budget_falls_back_without_adaptive():
    b = _run_budget(_patched_budget(None))
    assert b["bmr_source"] in ("garmin", "apple_health")
    assert b["target"] == round(2151 * 0.85)
    assert b.get("tdee_days") is None


# ── get_day_stats (/day): адаптивный TDEE и подпись источника ────────────────


def test_get_day_stats_uses_adaptive(test_db, mock_session_local):
    """/day считает цель от адаптивного TDEE и отдаёт источник для подписи."""
    from database.crud import create_nutrition_log, create_weight
    from services.nutrition_service import NutritionService

    uid = 895655
    today = datetime.now(timezone.utc).date()
    for i in range(30):
        create_nutrition_log(
            db=test_db,
            user_id=uid,
            date=today - timedelta(days=i),
            meal_time=time(12, 0),
            meal_name="lunch",
            items=[],
            totals={"calories": 1900},
        )
    t0 = datetime.combine(today - timedelta(days=27), time(8, 0), tzinfo=timezone.utc)
    t1 = datetime.combine(today, time(8, 0), tzinfo=timezone.utc)
    create_weight(test_db, uid, t0, 84.0)
    create_weight(test_db, uid, t1, 83.0)

    with patch("core.infra.tz.get_user_tz", return_value=timezone.utc):
        stats = NutritionService(user_id=uid).get_day_stats(today)

    expected_tdee = round(1900 + KCAL_PER_KG_FAT / 27)
    assert stats["targets"]["tdee_source"] == "adaptive"
    assert stats["targets"]["tdee_days"] == 30
    assert stats["targets"]["avg_tdee"] == expected_tdee
    assert stats["targets"]["calories"] == round(expected_tdee * 0.85)


# ── compute_goals (миниапп): прокидывание источника ──────────────────────────


def test_compute_goals_passes_adaptive_source():
    from webhook.nutrition_goals import compute_goals

    budget = {
        "target": 1955,
        "bmr_avg": 1771,
        "activity_avg": 529,
        "tdee_avg": 2300,
        "bmr_source": "adaptive",
        "tdee_days": 40,
        "calorie_goal_pct": -15,
        "data_incomplete": False,
    }
    with patch("webhook.nutrition_goals.get_daily_budget", return_value=budget):
        g = compute_goals(user_id=895655)
    assert g["tdee_source"] == "adaptive"
    assert g["tdee_days"] == 40
