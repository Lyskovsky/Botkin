"""Адаптивный TDEE (maintenance) из собственных данных питания и веса.

Метод MacroFactor (https://macrofactor.com/algorithm-accuracy/): энергобаланс
закрывается по факту — сколько человек реально ел и как при этом менялся вес:

    TDEE = средний рацион − 7700 × скорость изменения веса (кг/день)

(при похудении скорость отрицательная, поэтому maintenance выше рациона).

Зачем: девайсовые оценки систематически смещены. Валидация на владельце
(15.08.2026): два независимых сегмента по 135 и 48 дней дали TDEE 2260 и 2362
при оценках Garmin 2400–2500 и старом среднем бота 1961.

Включается только когда данных достаточно (см. константы ниже); иначе
get_adaptive_tdee возвращает None и вызывающий остаётся на цепочке
Garmin/Apple/manual/default.
"""

import logging
from datetime import date as date_type
from datetime import datetime, timedelta, timezone
from statistics import fmean, median
from typing import Mapping, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

KCAL_PER_KG_FAT = 7700  # энергетическая цена 1 кг массы тела

# Трейлинг-окно, в котором ищем данные. Шире 14-дневного среднего Garmin,
# потому что сигнал вес+рацион медленный и шумный.
WINDOW_DAYS = 42
# Минимум полных дней лога питания в окне.
MIN_LOGGED_DAYS = 21
# Минимальный разброс между первым и последним взвешиванием.
MIN_WEIGHT_SPAN_DAYS = 14
# Сглаживание веса: анкер = среднее взвешиваний в ±N дней от крайней точки.
SMOOTH_HALF_WINDOW_DAYS = 5
# День считается «не логированным целиком», если его калории < доли медианы
# логированных дней (перекус записали, остальное — нет).
MIN_DAY_KCAL_RATIO = 0.5
# Санити-границы результата: вышли за них — данные мусорные, оценке не верим.
PLAUSIBLE_TDEE_MIN = 1200
PLAUSIBLE_TDEE_MAX = 6000


def compute_adaptive_tdee(
    intake_by_day: Mapping[date_type, float],
    weights: Sequence[Tuple[date_type, float]],
    as_of: date_type,
) -> Optional[dict]:
    """Чистый расчёт адаптивного TDEE. None — данных мало или они мусорные.

    intake_by_day: дата → суммарные ккал за день (только дни, где лог вообще есть).
    weights: (дата, кг); допускаются несколько замеров в день и точки чуть
        старше окна (используются для сглаживания стартового анкера).
    """
    window_start = as_of - timedelta(days=WINDOW_DAYS - 1)

    # ── Вес: анкеры со сглаживанием ±5 дней ─────────────────────────────────
    in_window = sorted((d, w) for d, w in weights if window_start <= d <= as_of)
    if len(in_window) < 2:
        return None
    anchor_start_date = in_window[0][0]
    anchor_end_date = in_window[-1][0]
    span_days = (anchor_end_date - anchor_start_date).days
    if span_days < MIN_WEIGHT_SPAN_DAYS:
        return None

    def _smoothed(anchor: date_type) -> float:
        near = [w for d, w in weights if abs((d - anchor).days) <= SMOOTH_HALF_WINDOW_DAYS]
        return fmean(near)

    w_start = _smoothed(anchor_start_date)
    w_end = _smoothed(anchor_end_date)
    rate_kg_day = (w_end - w_start) / span_days

    # ── Питание: только полностью логированные дни окна ─────────────────────
    candidates = {d: kcal for d, kcal in intake_by_day.items() if window_start <= d <= as_of and (kcal or 0) > 0}
    if not candidates:
        return None
    med = median(candidates.values())
    logged = {d: kcal for d, kcal in candidates.items() if kcal >= MIN_DAY_KCAL_RATIO * med}
    if len(logged) < MIN_LOGGED_DAYS:
        return None
    avg_intake = fmean(logged.values())

    tdee = avg_intake - KCAL_PER_KG_FAT * rate_kg_day
    if not (PLAUSIBLE_TDEE_MIN <= tdee <= PLAUSIBLE_TDEE_MAX):
        logger.warning(
            "adaptive_tdee: неправдоподобный результат %.0f (intake=%.0f, rate=%.3f кг/д) — игнорируем",
            tdee,
            avg_intake,
            rate_kg_day,
        )
        return None

    return {
        "tdee": round(tdee),
        "avg_intake": round(avg_intake),
        "weight_rate_kg_day": rate_kg_day,
        "days_used": len(logged),
        "span_days": span_days,
        "window_days": WINDOW_DAYS,
    }


def get_adaptive_tdee(user_id: int, as_of: Optional[date_type] = None, db=None) -> Optional[dict]:
    """Адаптивный TDEE пользователя из Postgres (nutrition_log + weights).

    db: переиспользовать сессию вызывающего; если None — открыть свою.
    """
    own = db is None
    if own:
        from database import SessionLocal

        db = SessionLocal()
    try:
        from database.crud import get_nutrition_logs_by_period, get_weights_by_period

        if as_of is None:
            as_of = datetime.now(timezone.utc).date()
        window_start = as_of - timedelta(days=WINDOW_DAYS - 1)

        intake_by_day: dict = {}
        for log in get_nutrition_logs_by_period(db, user_id, window_start, as_of):
            kcal = (log.totals or {}).get("calories") or 0
            intake_by_day[log.date] = intake_by_day.get(log.date, 0) + kcal

        # Взвешивания с запасом ±5 дней слева — для сглаживания стартового анкера.
        w_rows = get_weights_by_period(
            db,
            user_id,
            datetime.combine(window_start - timedelta(days=SMOOTH_HALF_WINDOW_DAYS), datetime.min.time()),
            datetime.combine(as_of + timedelta(days=1), datetime.min.time()),
        )
        weights = [(w.measured_at.date(), float(w.weight)) for w in w_rows]

        return compute_adaptive_tdee(intake_by_day, weights, as_of)
    except Exception as e:
        logger.warning(f"get_adaptive_tdee failed for user {user_id}: {e}")
        return None
    finally:
        if own:
            db.close()
