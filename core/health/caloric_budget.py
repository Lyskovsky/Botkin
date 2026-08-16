"""
Caloric budget: daily limit = (BMR + active_kcal) × 0.85
Used to show remaining calories after each meal save.
"""

import logging
from datetime import date as date_type, datetime, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

from core.infra.tz import get_user_tz  # noqa: E402


WARN_THRESHOLD = 0.80  # warn when consumed ≥ 80% of target
DEFAULT_TOTAL = 2150  # fallback if no Garmin data (≈ avg from analysis)
DEFAULT_GOAL_PCT = -15  # default calorie goal: 15% deficit

# День считается «неполным» (частичный синк Garmin), если накопленный BMR дня
# ниже этой доли от 14-дневного среднего BMR пользователя. BMR растёт линейно
# в течение дня, поэтому низкий дневной BMR = часы не досинкали день. Порог
# относительный, а не абсолютный — абсолютный (1500) ломается для пользователей
# с низким BMR (~1400).
INCOMPLETE_BMR_RATIO = 0.85
# Абсолютный fallback, когда средний BMR пользователя неизвестен.
MIN_PLAUSIBLE_TDEE = 1500


def get_day_actual_tdee(user_id: int, for_date: date_type, db=None) -> Optional[float]:
    """Фактический расход энергии (BMR + активные) за конкретный день из activity_log.

    Источник для today-boost (см. calculate_targets / get_daily_budget): на тяжёлый
    тренировочный день фактический расход выше 14-дневного среднего, и цель должна
    подняться по факту, а не по среднему.

    Возвращает total_calories (как показывает Garmin Connect), либо bmr+active как
    fallback, либо None если за день нет данных о расходе.

    db: переиспользовать сессию вызывающего (без вложенных коннектов). Если None —
    открыть свою.
    """
    own = db is None
    if own:
        from database import SessionLocal

        db = SessionLocal()
    try:
        from database import get_activity_by_date

        act = get_activity_by_date(db, user_id, for_date)
        if not act:
            return None
        if act.total_calories and act.total_calories > 0:
            return float(act.total_calories)
        if act.bmr_calories and act.active_calories:
            return float(act.bmr_calories) + float(act.active_calories)
        return None
    finally:
        if own:
            db.close()


def get_day_energy_fact(user_id: int, for_date: date_type, avg_bmr: Optional[float] = None, db=None) -> dict:
    """Факт расхода за день + оценка полноты Garmin-данных.

    Для завершённого дня фактический TDEE — истина (в отличие от прогноза по
    среднему), но только если синк был полным. Полноту оцениваем по BMR дня
    относительно среднего BMR пользователя (см. INCOMPLETE_BMR_RATIO).

    Returns:
        {'tdee': float|None, 'bmr': float|None, 'active': float|None, 'incomplete': bool}
        incomplete=True — данные битые/частичные, tdee дня доверять нельзя.
    """
    own = db is None
    if own:
        from database import SessionLocal

        db = SessionLocal()
    try:
        from database import get_activity_by_date

        act = get_activity_by_date(db, user_id, for_date)
        if not act:
            return {"tdee": None, "bmr": None, "active": None, "incomplete": True}

        bmr = float(act.bmr_calories) if act.bmr_calories else None
        if act.total_calories and act.total_calories > 0:
            tdee = float(act.total_calories)
        elif act.bmr_calories and act.active_calories:
            tdee = float(act.bmr_calories) + float(act.active_calories)
        else:
            tdee = None

        active = max(0.0, tdee - bmr) if (tdee and bmr) else None

        if tdee is None:
            incomplete = True
        elif bmr and avg_bmr and avg_bmr > 0:
            incomplete = bmr < INCOMPLETE_BMR_RATIO * avg_bmr
        else:
            incomplete = tdee < MIN_PLAUSIBLE_TDEE

        return {"tdee": tdee, "bmr": bmr, "active": active, "incomplete": incomplete}
    finally:
        if own:
            db.close()


def get_daily_budget(
    user_id: int,
    for_date: Optional[date_type] = None,
    calorie_goal_pct: Optional[int] = None,
) -> dict:
    """
    Returns caloric budget for the day.

    calorie_goal_pct: signed % vs maintenance.
        -15 = 15% deficit (default), 0 = maintenance, +10 = 10% surplus.
        If None, reads from user_settings (falls back to DEFAULT_GOAL_PCT).

    Keys:
        consumed   – kcal eaten so far
        target     – daily limit adjusted for goal
        remaining  – target - consumed (can be negative)
        pct        – consumed / target * 100
        warn       – True if consumed >= 80% of target
        has_garmin – True if Garmin 14-day average data was found
    """
    from database import SessionLocal
    from database.crud import (
        get_nutrition_totals_by_date,
        get_average_activity_stats,
        get_user_settings,
        get_activities_by_period,
    )

    user_tz = get_user_tz(user_id)
    today = for_date or datetime.now(user_tz).date()
    db = SessionLocal()
    try:
        s = get_user_settings(db, user_id)
        bmr_source_setting = s.bmr_source if s and s.bmr_source else "auto"

        # ── Resolve BMR + activity by source priority ──────────────────────────
        # 'manual'    → user's Mifflin-St Jeor params (bmr_override + activity_avg_override)
        # 'auto'      → Garmin (14-day avg) > Apple Health (14-day avg) > default
        bmr_avg = None
        total_avg = None
        source_label = None  # 'garmin' | 'apple_health' | 'manual' | 'default'

        if bmr_source_setting == "manual" and s and s.bmr_override:
            bmr_avg = s.bmr_override
            activity_avg_manual = s.activity_avg_override or 0
            total_avg = bmr_avg + activity_avg_manual
            source_label = "manual"
        else:
            # Auto mode: try Garmin first (most accurate), then Apple Health.
            avg_stats = get_average_activity_stats(db, user_id, days=14)
            if avg_stats and avg_stats.get("total_calories", 0) > 1500:
                # Garmin path — has full triple (bmr + active + total).
                # Determine if data is from Garmin or Apple by checking source field
                # of recent activity rows. Garmin pushes total_calories;
                # Apple-only users have total_calories = NULL but bmr_calories filled.
                start = today - timedelta(days=14)
                rows = get_activities_by_period(db, user_id, start, today)
                garmin_rows = [r for r in rows if r.source and "garmin" in r.source.lower() and r.total_calories]
                apple_rows = [r for r in rows if r.source and "apple" in r.source.lower()]
                if len(garmin_rows) >= len(apple_rows):
                    source_label = "garmin"
                else:
                    source_label = "apple_health"
                bmr_avg = round(avg_stats.get("bmr_calories", 0))
                total_avg = round(avg_stats.get("total_calories", 0))

        # ── Adaptive TDEE (питание + вес, метод MacroFactor) ────────────────────
        # Самый честный источник maintenance: закрывает энергобаланс по факту
        # съеденного и динамике веса. Приоритетнее девайсов и manual — но
        # включается только при достатке данных (≥21 день лога, ≥2 взвешивания
        # с разбросом ≥14 дней), иначе None и остаёмся на цепочке выше.
        from core.health.adaptive_tdee import get_adaptive_tdee

        tdee_days = None
        # Девайсовые значения до adaptive-подмены: activity_avg для отображения
        # обязан оставаться честным девайсовым средним (адаптивный TDEE − BMR —
        # выдуманное число), а ярлык нужен для отката, если цель прошедшего дня
        # посчитается от факта девайса.
        device_source_label = source_label
        device_total_avg = total_avg
        adaptive = get_adaptive_tdee(user_id, as_of=today, db=db)
        if adaptive:
            total_avg = adaptive["tdee"]
            source_label = "adaptive"
            tdee_days = adaptive["days_used"]

        # ── Default fallback (no wearable, no manual setup) ─────────────────────
        if not total_avg:
            total_burned = DEFAULT_TOTAL
            source_label = source_label or "default"
            has_garmin = False
        else:
            total_burned = total_avg
            has_garmin = source_label in ("garmin", "apple_health", "adaptive")

        if calorie_goal_pct is None:
            calorie_goal_pct = s.calorie_goal_pct if s and s.calorie_goal_pct is not None else DEFAULT_GOAL_PCT
        ratio = 1.0 + calorie_goal_pct / 100.0  # -15 → 0.85, 0 → 1.0, +10 → 1.10

        # Прошедший день vs сегодня — разная семантика цели (фикс 02.07.2026):
        #
        # • Сегодня — прогноз: max(среднее, фактический расход на текущий момент).
        #   Today-boost поднимает цель в день тяжёлой тренировки, а в ленивый/
        #   недосинканный день среднее не даёт цели упасть (день ещё не закончен).
        #
        # • Прошедший день — факт: день закончен, Garmin-данные финальны, поэтому
        #   цель честно считается от фактического расхода дня (даже если он ниже
        #   среднего — иначе в ленивые дни «остаток» разрешал переедать: июнь-2026
        #   просел до ~4% дефицита при цели 15%). Если данные дня битые (частичный
        #   синк, BMR дня « среднего) — оставляем оценку по среднему и ставим флаг
        #   data_incomplete: UI не должен показывать «перебор» по мусорным данным.
        data_incomplete = False
        real_today = datetime.now(user_tz).date()
        if today < real_today:
            fact = get_day_energy_fact(user_id, today, avg_bmr=bmr_avg, db=db)
            if fact["tdee"] and not fact["incomplete"]:
                total_burned = fact["tdee"]
                has_garmin = True
                if source_label == "adaptive":
                    # Цель посчитана от факта девайса — подпись «по вашим
                    # данным за N дней» здесь лгала бы.
                    source_label = device_source_label or "default"
                    tdee_days = None
            else:
                data_incomplete = True
        else:
            actual_tdee = get_day_actual_tdee(user_id, today, db=db)
            if actual_tdee and actual_tdee > total_burned:
                total_burned = actual_tdee
                has_garmin = True
        target = round(total_burned * ratio)

        # --- Consumed: today's nutrition_log ---
        totals = get_nutrition_totals_by_date(db, user_id, today)
        consumed = round(totals.get("calories", 0))

        remaining = target - consumed
        pct = round(consumed / target * 100) if target else 0

        # Activity = total − bmr. Derived (NOT from active_calories field) because
        # Apple Health may overwrite that field, breaking the (total = bmr + active)
        # invariant. Keeps display math internally consistent. Считается от
        # ДЕВАЙСОВОГО среднего (device_total_avg == total_avg, когда adaptive
        # не применился): «адаптивный TDEE − девайсовый BMR» — выдуманное число.
        activity_avg = (device_total_avg - bmr_avg) if (bmr_avg and device_total_avg) else None
        if activity_avg is not None and activity_avg < 0:
            activity_avg = 0
        return {
            "consumed": consumed,
            "target": target,
            "remaining": remaining,
            "pct": pct,
            "warn": pct >= WARN_THRESHOLD * 100,
            "has_garmin": has_garmin,
            "bmr_avg": bmr_avg,
            "activity_avg": activity_avg,
            "tdee_avg": total_avg,
            "bmr_source": source_label,  # 'adaptive' | 'garmin' | 'apple_health' | 'manual' | 'default'
            # Сколько полных дней лога питания легло в адаптивную оценку
            # (для подписи «по вашим данным за N дней»); None для других источников.
            "tdee_days": tdee_days,
            "calorie_goal_pct": calorie_goal_pct,
            # True для прошедшего дня с битым/частичным синком Garmin: цель — оценка
            # по среднему, вердикт «перебор» показывать нельзя.
            "data_incomplete": data_incomplete,
        }
    except Exception as e:
        logger.warning(f"get_daily_budget failed: {e}")
        return {}
    finally:
        db.close()


def make_block_bar(consumed: float, target: float, invert: bool = False) -> tuple:
    """
    Returns (bar_string, pct) — emoji progress bar, 10 squares, no hybrid chars.

    invert=False — over target is bad (calories, fat, carbs)
    invert=True  — under target is bad (protein, fiber)
    """
    pct = round(consumed / target * 100) if target else 0
    filled = min(10, round(pct / 10))

    if invert:
        sq = "🟩" if pct >= 70 else ("🟧" if pct >= 50 else "🟥")
    else:
        sq = "🟥" if pct > 100 else ("🟧" if pct >= 80 else "🟩")

    bar = sq * filled + "⬜" * (10 - filled)
    return bar, pct


def format_budget_line(user_id: int, for_date: Optional[date_type] = None, show_bar: bool = True) -> str:
    """
    Returns a compact one-block string for appending to a Telegram message.

    Example (within limit):
        📊 1 240 / 1 820 ккал · осталось 580

    Example (warning):
        ⚠️ 1 650 / 1 820 ккал · осталось 170

    Example (over):
        🔴 2 100 / 1 820 ккал · перебор +280
    """
    b = get_daily_budget(user_id, for_date)
    if not b:
        return ""

    consumed = b["consumed"]
    target = b["target"]
    remaining = b["remaining"]
    pct = b["pct"]

    # Progress bar: 10 colored squares
    filled = min(10, round(pct / 10))
    if b.get("data_incomplete"):
        icon = "⚠️"
        sq_fill = "🟧"
        tail = "Garmin-данные дня неполные — итог оценочный"
    elif remaining < 0:
        icon = "🔴"
        sq_fill = "🟥"
        tail = f"перебор +{abs(remaining)} ккал"
    elif b["warn"]:
        icon = "⚠️"
        sq_fill = "🟧"
        tail = f"осталось {remaining} ккал"
    else:
        icon = "📊"
        sq_fill = "🟩"
        tail = f"осталось {remaining} ккал"

    hint = "" if b["has_garmin"] else " (≈ среднее)"
    today = datetime.now(get_user_tz(user_id)).date()
    yesterday = today - timedelta(days=1)
    if for_date is None or for_date == today:
        day_label = "Сегодня"
    elif for_date == yesterday:
        day_label = "Вчера"
    else:
        day_label = for_date.strftime("%d.%m")
    if show_bar:
        bar = sq_fill * filled + "⬜" * (10 - filled)
        return f"\n{icon} {bar} {pct}%\n{day_label}: {consumed} / {target} ккал · {tail}{hint}"
    else:
        return f"\n{icon} {day_label}: {consumed} / {target} ккал · {tail}{hint}"
