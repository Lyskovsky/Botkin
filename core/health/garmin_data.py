#!/usr/bin/env python3
"""
Garmin data synchronization and retrieval functions
"""

import logging
import os
from datetime import datetime, date as date_type, time as time_type, timedelta, timezone
from typing import Optional, Dict, Tuple
from database import SessionLocal, get_activity_by_date

# Garth tokens dir (persistent volume: /opt/healthvault/data/garth/ on server)
_GARTH_HOME = os.getenv("GARTH_HOME", "/app/data/garth")
# Don't re-fetch from Garmin if synced within this many minutes
_CACHE_MINUTES = 15
# Garmin отдаёт BMR/total нарастающим итогом в течение дня. Строка за прошедший
# день финальна, только если последний синк случился спустя этот запас часов
# после конца дня (часы обычно досинкивают день в Garmin утром следующего).
_FINAL_GRACE_HOURS = 12
# Сколько прошедших дней проверять на «замороженный» частичный снимок.
# 14 = окно усреднения в caloric_budget.get_daily_budget.
_RESYNC_LOOKBACK_DAYS = 14

logger = logging.getLogger(__name__)


def get_garmin_data_for_date(date: str, user_id: int) -> Optional[Dict]:
    """
    Получает данные Garmin/Activity за указанную дату из PostgreSQL

    Args:
        date: Дата в формате YYYY-MM-DD
        user_id: Telegram ID пользователя

    Returns:
        Словарь с данными или None
    """
    try:
        target_date = datetime.strptime(date, "%Y-%m-%d").date()
    except ValueError:
        return None

    from database import get_user_by_telegram_id

    db = SessionLocal()
    try:
        user = get_user_by_telegram_id(db, user_id)
        if not user:
            return None

        activity = get_activity_by_date(db, user.telegram_id, target_date)

        if not activity:
            return None

        # Return in old format for compatibility
        return {
            "totalKilocalories": activity.total_calories,
            "activeKilocalories": activity.active_calories,
            "bmrKilocalories": activity.bmr_calories,
            "totalSteps": activity.steps,
            "totalDistanceMeters": activity.distance_km * 1000 if activity.distance_km else None,
            "sleepingSeconds": activity.sleep_hours * 3600 if activity.sleep_hours else None,
            "averageHeartRate": activity.heart_rate_avg,
            "averageStressLevel": activity.stress_level,
        }
    finally:
        db.close()


def _save_garmin_stats(db, user_id: int, day: date_type, stats: Dict) -> None:
    """Записывает дневную сводку Garmin в activity_log (общий маппинг полей)."""
    from database.crud import create_or_update_activity

    sleep_sec = stats.get("sleepingSeconds") or stats.get("measurableAsleepDuration")
    sleep_hours = round(sleep_sec / 3600.0, 2) if sleep_sec else None
    create_or_update_activity(
        db=db,
        user_id=user_id,
        date=day,
        steps=stats.get("totalSteps"),
        active_calories=stats.get("activeKilocalories"),
        total_calories=stats.get("totalKilocalories"),
        bmr_calories=stats.get("bmrKilocalories"),
        distance_km=(stats.get("totalDistanceMeters") or 0) / 1000.0,
        sleep_hours=sleep_hours,
        heart_rate_avg=stats.get("restingHeartRate") or stats.get("minHeartRate"),
        stress_level=stats.get("averageStressLevel"),
        source="garmin_connect",
        raw_data=stats,
    )


def _day_is_final(activity, day: date_type, user_tz) -> bool:
    """Финальна ли строка activity_log за прошедший день.

    Синк, случившийся до (конец дня + _FINAL_GRACE_HOURS), — это промежуточный
    снимок: BMR/total в Garmin растут в течение дня, а часы досинкивают день
    задним числом. Такую строку нужно перечитать из API.
    """
    if not activity or activity.synced_at is None:
        return False
    synced = activity.synced_at
    if synced.tzinfo is None:
        synced = synced.replace(tzinfo=timezone.utc)
    day_end = datetime.combine(day + timedelta(days=1), time_type.min, tzinfo=user_tz)
    return synced >= day_end + timedelta(hours=_FINAL_GRACE_HOURS)


def _resync_stale_past_days(client, db, user_id: int, today: date_type) -> int:
    """Досинкивает прошедшие дни с нефинальными («замороженными») снимками.

    Прецедент 15.08.2026: сервер синкал только «сегодня», день замораживался
    вечерним снимком (BMR 1604 в среднем вместо финальных ~1917) — 14-дневное
    среднее занижало базовый расход и цель калорий на ~300-400 ккал.

    Возвращает число пересинканных дней. Ошибки по отдельным дням не роняют
    основной синк.
    """
    from core.infra.tz import get_user_tz

    user_tz = get_user_tz(user_id)
    resynced = 0
    for offset in range(1, _RESYNC_LOOKBACK_DAYS + 1):
        day = today - timedelta(days=offset)
        activity = get_activity_by_date(db, user_id, day)
        if _day_is_final(activity, day, user_tz):
            continue
        try:
            stats = client.get_stats(day.strftime("%Y-%m-%d"))
        except Exception as e:
            logger.warning(f"Garmin resync {day} failed for user {user_id}: {e}")
            continue
        if not stats:
            continue
        _save_garmin_stats(db, user_id, day, stats)
        resynced += 1
    if resynced:
        logger.info(f"Garmin: finalized {resynced} stale past day(s) for user {user_id}")
    return resynced


def sync_today_garmin(user_id: int, target_date: Optional[date_type] = None) -> Tuple[float, str]:
    """
    Синхронизирует данные Garmin за target_date (по умолчанию сегодня).
    Вызывается при каждом /day — одиночный запрос к API с 15-мин кешем.

    Auth: сначала garth-токены (не требуют пароля, живут 28 дней),
          при неудаче — логин по паролю с сохранением токенов на диск.

    Returns:
        (active_calories: float, status: str)
        status: 'ok'     — свежие данные из API
                'cached' — данные из БД (< 15 мин)
                'error'  — Garmin недоступен, вернули последнее известное значение
    """
    if target_date is None:
        target_date = date_type.today()

    db = SessionLocal()
    try:
        # --- 1. Cache check ---
        activity = get_activity_by_date(db, user_id, target_date)
        if activity and activity.synced_at:
            synced_at = activity.synced_at
            if synced_at.tzinfo is None:
                synced_at = synced_at.replace(tzinfo=timezone.utc)
            age_min = (datetime.now(timezone.utc) - synced_at).total_seconds() / 60
            if age_min < _CACHE_MINUTES:
                logger.debug(f"Garmin cache hit for {target_date} (age {age_min:.1f}m)")
                return (float(activity.active_calories or 0), "cached")

        # --- 2. Get credentials ---
        primary_id = int(os.getenv("BOTKIN_USER_ID") or os.getenv("HEALTHVAULT_USER_ID") or "895655")
        if user_id == primary_id:
            email = os.getenv("GARMIN_EMAIL")
            password = os.getenv("GARMIN_PASSWORD")
        else:
            from database import get_user_by_telegram_id

            u = get_user_by_telegram_id(db, user_id)
            email = u.garmin_email if u else None
            password = u.garmin_password if u else None

        if not email:
            cached_val = float(activity.active_calories or 0) if activity else 0.0
            return (cached_val, "error")

        # --- 3. Auth: только garth-токены (no password login on server!)
        # Токены живут 28 дней, обновляются автоматически через refresh_token.
        # Обновить токены на сервере: запустить /sync на маке —
        # push_garmin_to_db.sh копирует свежие токены из data/cache/garth_tokens/.
        garth_dir = os.path.join(_GARTH_HOME, str(user_id))
        token_file = os.path.join(garth_dir, "oauth1_token.json")
        if not os.path.exists(token_file):
            logger.error(f"Garmin: no garth tokens at {garth_dir}. Run /sync on Mac to upload.")
            cached_val = float(activity.active_calories or 0) if activity else 0.0
            return (cached_val, "error")

        try:
            from garminconnect import Garmin
            import garth
            import warnings

            warnings.filterwarnings("ignore", category=DeprecationWarning)
            garth.resume(garth_dir)
            client = Garmin()
            client.login(garth_dir)  # загружает токены И делает profile-запрос (display_name)
            logger.info(f"Garmin: garth token auth OK for user {user_id}")
        except Exception as e:
            logger.error(f"Garmin token auth failed: {e}. Run /sync on Mac to refresh tokens.")
            cached_val = float(activity.active_calories or 0) if activity else 0.0
            return (cached_val, "error")

        # --- 4. Fetch stats ---
        try:
            stats = client.get_stats(target_date.strftime("%Y-%m-%d"))
        except Exception as e:
            logger.error(f"Garmin get_stats failed: {e}")
            cached_val = float(activity.active_calories or 0) if activity else 0.0
            return (cached_val, "error")

        # --- 5. Save to DB + досинк «замороженных» прошедших дней ---
        if stats:
            _save_garmin_stats(db, user_id, target_date, stats)

        try:
            _resync_stale_past_days(client, db, user_id, target_date)
        except Exception as e:
            logger.warning(f"Garmin resync of past days failed for user {user_id}: {e}")

        if not stats:
            return (0.0, "ok")
        return (float(stats.get("activeKilocalories") or 0), "ok")

    except Exception as e:
        logger.error(f"sync_today_garmin error: {e}", exc_info=True)
        cached_val = float(activity.active_calories or 0) if activity else 0.0
        return (cached_val, "error")
    finally:
        db.close()


def sync_garmin_data(user_id: int, sync_date: Optional[date_type] = None):
    """
    Синхронизирует данные Garmin за сегодня или за указанную дату

    Args:
        user_id: Telegram ID пользователя
        sync_date: Дата для синхронизации (по умолчанию сегодня)
    """
    logger.info(f"Garmin sync called for user {user_id}, date: {sync_date}")

    # 1. Get credentials
    import os
    from database import get_user_by_telegram_id

    db = SessionLocal()
    try:
        user = get_user_by_telegram_id(db, user_id)
        if not user:
            logger.warning(f"User {user_id} not found for syncing")
            return

        # Garmin: только из DB или ENV для основного пользователя (обратная совместимость)
        primary_id = int(os.getenv("BOTKIN_USER_ID") or os.getenv("HEALTHVAULT_USER_ID") or "895655")
        if user.garmin_email and user.garmin_password:
            email, password = user.garmin_email, user.garmin_password
        elif user_id == primary_id and os.getenv("GARMIN_EMAIL") and os.getenv("GARMIN_PASSWORD"):
            email = os.getenv("GARMIN_EMAIL")
            password = os.getenv("GARMIN_PASSWORD")
        else:
            logger.info(f"User {user_id} has no Garmin — skipping sync")
            return

        # 2. Connect to Garmin
        try:
            from garminconnect import Garmin

            client = Garmin(email, password)
            client.login()
        except ImportError:
            logger.error("garminconnect library not installed")
            return
        except Exception as e:
            logger.error(f"Garmin login failed: {e}")
            return

        # 3. Get Data (Default to today)
        target_date = sync_date if sync_date else date_type.today()
        target_date_str = target_date.strftime("%Y-%m-%d")

        try:
            stats = client.get_stats(target_date_str)
            # steps = client.get_steps_data(today_str) # Optional for more details
        except Exception as e:
            logger.error(f"Failed to fetch Garmin stats for {target_date_str}: {e}")
            return

        if not stats:
            logger.info(f"No stats available for {target_date_str}")
            return

        # 4. Save to DB (включая сон, если API вернул sleepingSeconds)
        _save_garmin_stats(db, user.telegram_id, target_date, stats)
        return True

    except Exception as e:
        logger.error(f"Error in sync_garmin_data: {e}", exc_info=True)
        return False
    finally:
        db.close()
