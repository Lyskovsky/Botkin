"""Фикс 02.07.2026: честный итог завершённого дня + флаг битых Garmin-дней.

История бага:
- Цель дня всегда считалась как max(14-дн среднее, факт дня) — асимметрично.
  В ленивые дни цель оставалась по среднему → «остаток» разрешал переедать
  (июнь-2026 просел до ~4% дефицита при цели 15%).
- Дни с частичным синком Garmin (25.06.2026: BMR 1467 при среднем 1855)
  показывали ложный «перебор» — расход дня был занижен навсегда.

Что проверяем:
1. get_day_energy_fact: эвристика полноты дня (BMR дня vs средний BMR юзера)
2. calculate_targets(today_tdee_final=True): факт завершённого дня — истина
   в обе стороны (даже если ниже среднего)
3. Обратная совместимость: today-boost для «сегодня» не изменился
"""

from unittest.mock import MagicMock, patch

# Заглушка telegram_id (не реальный PII)
UID = 111111


def _act_row(bmr=None, active=None, total=None):
    row = MagicMock()
    row.bmr_calories = bmr
    row.active_calories = active
    row.total_calories = total
    return row


class TestGetDayEnergyFact:
    """Эвристика полноты Garmin-данных дня."""

    def _fact(self, row, avg_bmr):
        from core.health import caloric_budget
        from datetime import date

        with patch("database.get_activity_by_date", return_value=row):
            return caloric_budget.get_day_energy_fact(UID, date(2026, 6, 25), avg_bmr=avg_bmr, db=MagicMock())

    def test_complete_day_not_flagged(self):
        """Полный день: BMR дня ≈ среднему → incomplete=False, tdee = total."""
        fact = self._fact(_act_row(bmr=1855, active=403, total=2258), avg_bmr=1850)
        assert fact["incomplete"] is False
        assert fact["tdee"] == 2258
        assert fact["bmr"] == 1855

    def test_partial_sync_flagged(self):
        """Прецедент 25.06.2026: BMR 1467 при среднем 1855 → битый день."""
        fact = self._fact(_act_row(bmr=1467, active=152, total=1619), avg_bmr=1855)
        assert fact["incomplete"] is True

    def test_low_bmr_user_not_flagged(self):
        """Пользователь с низким BMR (~1400): полный день не флагится
        (абсолютный порог 1500 сломал бы этот кейс)."""
        fact = self._fact(_act_row(bmr=1400, active=250, total=1650), avg_bmr=1410)
        assert fact["incomplete"] is False

    def test_missing_row_flagged(self):
        fact = self._fact(None, avg_bmr=1850)
        assert fact["incomplete"] is True
        assert fact["tdee"] is None

    def test_no_avg_bmr_falls_back_to_absolute(self):
        """Нет истории среднего BMR → абсолютный порог MIN_PLAUSIBLE_TDEE."""
        low = self._fact(_act_row(bmr=900, active=100, total=1000), avg_bmr=None)
        ok = self._fact(_act_row(bmr=1800, active=400, total=2200), avg_bmr=None)
        assert low["incomplete"] is True
        assert ok["incomplete"] is False

    def test_bmr_plus_active_fallback_when_no_total(self):
        """total_calories пуст → tdee = bmr + active (как в get_day_actual_tdee)."""
        fact = self._fact(_act_row(bmr=1800, active=400, total=None), avg_bmr=1810)
        assert fact["tdee"] == 2200
        assert fact["incomplete"] is False


class TestCalculateTargetsFinalDay:
    """today_tdee_final: факт завершённого дня — истина в обе стороны."""

    def _stats(self, total=2311, bmr=1797):
        return {"total_calories": total, "bmr_calories": bmr, "active_calories": total - bmr}

    def test_final_lower_than_avg_lowers_target(self):
        """Ленивый завершённый день: факт 2029 < среднего 2311 → цель от факта.
        Прецедент 01.07.2026: по среднему цель 1964, честная — 1725."""
        from core.health.nutrition_targets import calculate_targets

        t = calculate_targets(stats=self._stats(), today_tdee=2029, today_tdee_final=True)
        assert t["calories"] == round(2029 * 0.85)

    def test_final_higher_than_avg_raises_target(self):
        from core.health.nutrition_targets import calculate_targets

        t = calculate_targets(stats=self._stats(), today_tdee=2800, today_tdee_final=True)
        assert t["calories"] == round(2800 * 0.85)

    def test_default_boost_behavior_unchanged(self):
        """Сегодня (today_tdee_final=False): max() — низкий факт не роняет цель."""
        from core.health.nutrition_targets import calculate_targets

        t = calculate_targets(stats=self._stats(), today_tdee=2029)
        assert t["calories"] == round(2311 * 0.85)

    def test_boost_still_works_upward(self):
        from core.health.nutrition_targets import calculate_targets

        t = calculate_targets(stats=self._stats(), today_tdee=2800)
        assert t["calories"] == round(2800 * 0.85)


class TestFormatBudgetLineIncomplete:
    """format_budget_line не выносит «перебор» по битым дням."""

    def test_incomplete_day_no_verdict(self):
        from core.health.caloric_budget import format_budget_line

        budget = {
            "consumed": 2079,
            "target": 1951,
            "remaining": -128,
            "pct": 107,
            "warn": True,
            "has_garmin": True,
            "data_incomplete": True,
        }
        with patch("core.health.caloric_budget.get_daily_budget", return_value=budget):
            line = format_budget_line(user_id=UID)
        assert "перебор" not in line
        assert "оценочный" in line

    def test_complete_day_verdict_kept(self):
        from core.health.caloric_budget import format_budget_line

        budget = {
            "consumed": 2079,
            "target": 1951,
            "remaining": -128,
            "pct": 107,
            "warn": True,
            "has_garmin": True,
            "data_incomplete": False,
        }
        with patch("core.health.caloric_budget.get_daily_budget", return_value=budget):
            line = format_budget_line(user_id=UID)
        assert "перебор +128" in line
