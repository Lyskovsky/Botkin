"""Derive daily macro goals from caloric budget + fixed split.

Used by GET /api/day to populate progress bars.
"""

from datetime import date as date_type
from typing import Optional

from core.health.caloric_budget import get_daily_budget

PROTEIN_SHARE = 0.30
FATS_SHARE = 0.30
CARBS_SHARE = 0.40
FIBER_GOAL_G = 30


def compute_goals(user_id: int, for_date: Optional[date_type] = None) -> dict:
    # calorie_goal_pct is read inside get_daily_budget from user_settings
    budget = get_daily_budget(user_id=user_id, for_date=for_date)
    kcal = budget.get("target")
    goal_pct = budget.get("calorie_goal_pct", -15)
    if not kcal:
        return {
            "kcal": None,
            "protein": None,
            "fats": None,
            "carbs": None,
            "fiber": FIBER_GOAL_G,
            "calorie_goal_pct": goal_pct,
            "data_incomplete": False,
        }
    bmr = budget.get("bmr_avg")
    activity_avg = budget.get("activity_avg")
    # tdee_avg is the canonical "total burned" from Garmin (matches Garmin app exactly).
    # Falls back to bmr+activity for resilience, but in normal operation
    # we trust Garmin's total_calories field as source of truth.
    tdee = budget.get("tdee_avg") or ((bmr or 0) + (activity_avg or 0) if bmr and activity_avg else None)
    # deficit_pct is the magnitude (always positive for display): -15 → 15, +10 → -10 (surplus)
    return {
        "kcal": int(kcal),
        "protein": round(kcal * PROTEIN_SHARE / 4),
        "fats": round(kcal * FATS_SHARE / 9),
        "carbs": round(kcal * CARBS_SHARE / 4),
        "fiber": FIBER_GOAL_G,
        "bmr": bmr,
        "activity_avg": activity_avg,
        "tdee": tdee,
        "calorie_goal_pct": goal_pct,
        "deficit_pct": goal_pct,  # signed; mini-app formats sign itself
        # 'adaptive' — TDEE выведен из питания+веса пользователя (подпись
        # «по вашим данным за N дней» в миниаппе); иначе garmin/apple/manual/default.
        "tdee_source": budget.get("bmr_source"),
        "tdee_days": budget.get("tdee_days"),
        # Прошедший день с частичным Garmin-синком: цель оценочная, «перебор» не выносить.
        "data_incomplete": budget.get("data_incomplete", False),
    }
