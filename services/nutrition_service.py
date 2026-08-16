"""
Nutrition Service - PostgreSQL Version

Сервис для работы с данными о питании через PostgreSQL.
Заменяет JsonNutritionRepository на database CRUD operations.
"""

import logging
from datetime import date
from typing import Dict, Optional

from database import (
    SessionLocal,
    get_nutrition_logs_by_date,
    get_nutrition_totals_by_date,
    get_average_activity_stats,
    get_user_by_telegram_id,
)
from core.health.nutrition_targets import calculate_targets

logger = logging.getLogger(__name__)


class NutritionService:
    """Service for nutrition data operations using PostgreSQL"""

    def __init__(self, user_id: int):
        """
        Initialize service for specific user

        Args:
            user_id: Telegram ID of the user
        """
        self.user_id = user_id

    def get_day_stats(self, day: date) -> Dict:
        """
        Возвращает полную статистику за день для UI:
        - Totals (кбжу)
        - Targets (цели)
        - Remaining (остаток)

        Args:
            day: Дата для статистики

        Returns:
            Dict with totals, targets, remaining, meals_count
        """
        db = SessionLocal()
        try:
            # Get nutrition totals for the day
            totals_dict = get_nutrition_totals_by_date(db, self.user_id, day)

            # Get meal count
            meals = get_nutrition_logs_by_date(db, self.user_id, day)
            meals_count = len(meals)

            # Get average activity stats for targets calculation
            user = get_user_by_telegram_id(db, self.user_id)
            data_incomplete = False

            # Адаптивный TDEE из питания+веса — приоритетнее девайсов (см.
            # core/health/adaptive_tdee.py). None если данных мало — тогда
            # calculate_targets идёт по старой цепочке user/stats/fallback.
            from core.health.adaptive_tdee import get_adaptive_tdee

            adaptive = get_adaptive_tdee(self.user_id, as_of=day, db=db)
            adaptive_val = adaptive["tdee"] if adaptive else None
            try:
                avg_stats = get_average_activity_stats(db, self.user_id, days=14)
                from core.health.caloric_budget import get_day_actual_tdee, get_day_energy_fact
                from core.infra.tz import get_user_tz
                from datetime import datetime

                real_today = datetime.now(get_user_tz(self.user_id)).date()
                if day < real_today:
                    # Завершённый день: факт дня — истина (даже если ниже среднего),
                    # но только при полном синке. Битый день → оценка по среднему + флаг,
                    # UI не должен выносить вердикт «перебор» (фикс 02.07.2026).
                    avg_bmr = avg_stats.get("bmr_calories") if avg_stats else None
                    fact = get_day_energy_fact(self.user_id, day, avg_bmr=avg_bmr, db=db)
                    if fact["tdee"] and not fact["incomplete"]:
                        targets_dict = calculate_targets(
                            stats=avg_stats,
                            user=user,
                            today_tdee=fact["tdee"],
                            today_tdee_final=True,
                            adaptive_tdee=adaptive_val,
                        )
                    else:
                        data_incomplete = True
                        targets_dict = calculate_targets(stats=avg_stats, user=user, adaptive_tdee=adaptive_val)
                else:
                    # Сегодня: прогноз. Today-boost = max(14-дн среднее, факт на сейчас) —
                    # день не закончен, честнее не посчитать (см. calculate_targets).
                    today_tdee = get_day_actual_tdee(self.user_id, day, db=db)
                    logger.info(f"[day_stats] user_id={self.user_id} avg_stats={avg_stats} today_tdee={today_tdee}")
                    targets_dict = calculate_targets(
                        stats=avg_stats, user=user, today_tdee=today_tdee, adaptive_tdee=adaptive_val
                    )
            except Exception as e:
                logger.error(f"Error calculating targets: {e}", exc_info=True)
                targets_dict = calculate_targets(stats=None, user=user, adaptive_tdee=adaptive_val)

            # Источник TDEE для подписи в UI («по вашим данным за N дней»)
            targets_dict["tdee_source"] = "adaptive" if adaptive_val else None
            targets_dict["tdee_days"] = adaptive["days_used"] if adaptive else None

            # Calculate remaining
            remaining = {
                "calories": targets_dict["calories"] - totals_dict["calories"],
                "protein": targets_dict["protein"] - totals_dict["protein"],
                "fats": targets_dict["fats"] - totals_dict["fats"],
                "carbs": targets_dict["carbs"] - totals_dict["carbs"],
            }

            # Create MacroStats-like object for compatibility
            class MacroStats:
                def __init__(self, data):
                    self.calories = data["calories"]
                    self.protein = data["protein"]
                    self.fats = data["fats"]
                    self.carbs = data["carbs"]
                    self.fiber = data.get("fiber", 0)

            return {
                "date": day,
                "totals": MacroStats(totals_dict),
                "targets": targets_dict,
                "remaining": remaining,
                "meals_count": meals_count,
                # Прошедший день с частичным Garmin-синком: цель оценочная,
                # вердикт «перебор» показывать нельзя.
                "data_incomplete": data_incomplete,
            }

        finally:
            db.close()


# Singleton
_service_instance: Optional[NutritionService] = None


def get_nutrition_service(user_id: int) -> NutritionService:
    """
    Get or create nutrition service instance

    Args:
        user_id: Telegram ID of the user

    Returns:
        NutritionService instance
    """
    global _service_instance
    if _service_instance is None or _service_instance.user_id != user_id:
        _service_instance = NutritionService(user_id=user_id)
    return _service_instance
