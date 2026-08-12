"""
CRUD operations for Botkin database

This module provides database operations for all tables:
- Users: create, get, update, list
- NutritionLog: create, get by date/period, update, delete
- Weights: create, get latest, get by period
- Supplements: create, get by date/period
- ActivityLog: create/update, get by date/period
- BloodTests: create, get latest, get all
"""

import secrets
from datetime import datetime, date, time, timedelta, timezone
from typing import Optional, List, Dict, Any
from sqlalchemy.orm import Session
from sqlalchemy import desc, text
import logging

from database.models import (
    User,
    NutritionLog,
    Weight,
    SupplementLog,
    ActivityLog,
    BloodTest,
    BodyMeasurement,
    UserSettings,
    PersonalAccessToken,
    VerifiedProduct,
    UserFeedback,
)

logger = logging.getLogger(__name__)

# ==================== USER OPERATIONS ====================


def get_user_by_telegram_id(db: Session, telegram_id: int) -> Optional[User]:
    """Get user by Telegram ID"""
    return db.query(User).filter(User.telegram_id == telegram_id).first()


def get_user_by_health_token(db: Session, health_token: str) -> Optional[User]:
    """Get user by Apple Health API token"""
    return db.query(User).filter(User.health_token == health_token).first()


def create_user(
    db: Session, telegram_id: int, first_name: Optional[str] = None, username: Optional[str] = None, role: str = "user"
) -> User:
    """Create a new user"""
    user = User(telegram_id=telegram_id, first_name=first_name, username=username, role=role, is_active=True)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def update_user_last_active(db: Session, telegram_id: int):
    """Update user's last_active timestamp"""
    user = get_user_by_telegram_id(db, telegram_id)
    if user:
        user.last_active = datetime.now()
        db.commit()


def update_user_calorie_settings(
    db: Session,
    telegram_id: int,
    bmr: Optional[float] = None,
    avg_active_calories: Optional[float] = None,
    target_weight_kg: Optional[float] = None,
) -> Optional[User]:
    """Update manual calorie settings (for users without Garmin)"""
    user = get_user_by_telegram_id(db, telegram_id)
    if not user:
        return None
    if bmr is not None:
        user.bmr = bmr
    if avg_active_calories is not None:
        user.avg_active_calories = avg_active_calories
    if target_weight_kg is not None:
        user.target_weight_kg = target_weight_kg
    db.commit()
    db.refresh(user)
    return user


def merge_onboarding_lists(db: Session, telegram_id: int, additions: dict[str, list]) -> dict[str, int]:
    """Дописать аллергии/диагнозы в users.onboarding_data с дедупом.

    additions: {"allergies": [...], "chronic_conditions": [...]}.
    Пишет в тот ключ-синоним, что уже читается отчётом (первый непустой),
    иначе — в дефолтный ('allergies'/'chronic_conditions'). Дедуп case-insensitive.
    Возвращает {"allergies": n_added, "chronic_conditions": m_added}.
    """
    from core.health.onboarding_lists import ALLERGY_KEYS, CONDITION_KEYS, split_freetext

    user = get_user_by_telegram_id(db, telegram_id)
    if not user:
        return {}

    data = dict(user.onboarding_data or {})
    key_order = {"allergies": ALLERGY_KEYS, "chronic_conditions": CONDITION_KEYS}
    added: dict[str, int] = {}

    for kind, keys in key_order.items():
        new_items = additions.get(kind) or []
        target = kind
        for k in keys:
            if data.get(k):
                target = k
                break
        existing = data.get(target)
        if isinstance(existing, list):
            merged = [str(v).strip() for v in existing if str(v).strip()]
        elif isinstance(existing, str):
            merged = split_freetext(existing)
        else:
            merged = []
        seen = {s.strip().lower() for s in merged}
        n_added = 0
        for item in new_items:
            s = str(item).strip()
            if not s or s.lower() in seen:
                continue
            merged.append(s)
            seen.add(s.lower())
            n_added += 1
        if n_added:
            data[target] = merged
        added[kind] = n_added

    user.onboarding_data = data  # реассайн объекта → SQLAlchemy засекает JSONB-изменение
    db.commit()
    db.refresh(user)
    return added


def generate_health_token(db: Session, telegram_id: int) -> str:
    """Generate and save a unique Apple Health API token for user"""
    import secrets

    token = f"hvt_{telegram_id}_{secrets.token_hex(16)}"

    user = get_user_by_telegram_id(db, telegram_id)
    if user:
        user.health_token = token
        db.commit()

    return token


def get_user_by_share_token(db: Session, share_token: str) -> Optional[User]:
    """Get user by share dashboard token"""
    return db.query(User).filter(User.share_token == share_token).first()


def generate_share_token(db: Session, telegram_id: int) -> str:
    """Generate and save a unique share token for user's public dashboard.

    Idempotent: if user already has a token, returns it unchanged.
    Call reset_share_token() to force-regenerate.
    """
    import uuid

    user = get_user_by_telegram_id(db, telegram_id)
    if not user:
        raise ValueError(f"User {telegram_id} not found")
    if user.share_token:
        return user.share_token
    user.share_token = str(uuid.uuid4())
    db.commit()
    return user.share_token


def reset_share_token(db: Session, telegram_id: int) -> str:
    """Regenerate share token — old URL immediately stops working."""
    import uuid

    user = get_user_by_telegram_id(db, telegram_id)
    if not user:
        raise ValueError(f"User {telegram_id} not found")
    user.share_token = str(uuid.uuid4())
    db.commit()
    return user.share_token


def get_or_create_health_token(db: Session, telegram_id: int) -> str:
    """Get user's Apple Health token, creating one if absent.

    Idempotent: returns the existing token unchanged. Call reset_health_token()
    to force-regenerate. Format matches onboarding: hvt_<telegram_id>_<hex>.
    """
    import secrets

    user = get_user_by_telegram_id(db, telegram_id)
    if not user:
        raise ValueError(f"User {telegram_id} not found")
    if user.health_token:
        return user.health_token
    user.health_token = f"hvt_{telegram_id}_{secrets.token_hex(16)}"
    db.commit()
    return user.health_token


def reset_health_token(db: Session, telegram_id: int) -> str:
    """Regenerate Apple Health token — old token immediately stops working."""
    import secrets

    user = get_user_by_telegram_id(db, telegram_id)
    if not user:
        raise ValueError(f"User {telegram_id} not found")
    user.health_token = f"hvt_{telegram_id}_{secrets.token_hex(16)}"
    db.commit()
    return user.health_token


# ==================== PERSONAL ACCESS TOKEN (PAT) OPERATIONS ====================
# Долгоживущие токены для MCP-коннектора Claude Desktop (#228). Пользователь сам
# выпускает их в боте, коннектор меняет на короткоживущий JWT через /api/agent/exchange_pat_for_jwt.

ALLOWED_PAT_SCOPES = ("ro", "rw")


def create_pat(
    db: Session,
    telegram_id: int,
    name: Optional[str] = None,
    scope: str = "rw",
    created_by: Optional[int] = None,
) -> PersonalAccessToken:
    """Выпустить новый PAT для пользователя.

    Формат токена зеркалит Apple Health: ``pat_<telegram_id>_<hex32>``.
    scope: 'rw' — личный токен (чтение+запись), 'ro' — для шаринга врачу (только чтение).
    created_by — кто выпустил (telegram_id); по умолчанию сам владелец (self-service).
    """
    if scope not in ALLOWED_PAT_SCOPES:
        raise ValueError(f"Invalid PAT scope {scope!r}, expected one of {ALLOWED_PAT_SCOPES}")

    user = get_user_by_telegram_id(db, telegram_id)
    if not user:
        raise ValueError(f"User {telegram_id} not found")

    pat = PersonalAccessToken(
        user_id=telegram_id,
        token=f"pat_{telegram_id}_{secrets.token_hex(32)}",
        name=name,
        scope=scope,
        created_by_user=created_by if created_by is not None else telegram_id,
    )
    db.add(pat)
    db.commit()
    db.refresh(pat)
    return pat


def get_active_pat_by_token(db: Session, token: str) -> Optional[PersonalAccessToken]:
    """Найти активный (не отозванный) PAT по строке токена и отметить факт использования.

    Используется публичным exchange-эндпоинтом ДО установки app.user_id — поэтому
    таблица без RLS (см. миграцию pat0token01). Возвращает None для отозванных/несуществующих.
    """
    if not token:
        return None
    pat = (
        db.query(PersonalAccessToken)
        .filter(PersonalAccessToken.token == token, PersonalAccessToken.revoked_at.is_(None))
        .first()
    )
    if pat:
        pat.last_used_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(pat)
    return pat


def list_pats(db: Session, telegram_id: int, include_revoked: bool = False) -> List[PersonalAccessToken]:
    """Список PAT пользователя (по умолчанию только активные), новые сверху."""
    query = db.query(PersonalAccessToken).filter(PersonalAccessToken.user_id == telegram_id)
    if not include_revoked:
        query = query.filter(PersonalAccessToken.revoked_at.is_(None))
    return query.order_by(desc(PersonalAccessToken.created_at)).all()


def revoke_pat(db: Session, telegram_id: int, token_id: int) -> bool:
    """Отозвать PAT (soft-delete через revoked_at). Скоупится по владельцу.

    Возвращает True если токен найден и отозван, False если не найден/чужой/уже отозван.
    """
    pat = (
        db.query(PersonalAccessToken)
        .filter(
            PersonalAccessToken.id == token_id,
            PersonalAccessToken.user_id == telegram_id,
            PersonalAccessToken.revoked_at.is_(None),
        )
        .first()
    )
    if not pat:
        return False
    pat.revoked_at = datetime.now(timezone.utc)
    db.commit()
    return True


# ==================== NUTRITION LOG OPERATIONS ====================


def create_nutrition_log(
    db: Session,
    user_id: int,
    date: date,
    meal_time: Optional[time],
    meal_name: str,
    items: List[Dict],
    totals: Dict,
    photo_paths: Optional[List[str]] = None,
) -> NutritionLog:
    """Create a new nutrition log entry"""
    log = NutritionLog(
        user_id=user_id,
        date=date,
        meal_time=meal_time,
        meal_name=meal_name,
        items=items,
        totals=totals,
        photo_paths=photo_paths or [],
    )
    db.add(log)
    db.commit()
    db.refresh(log)
    return log


def get_nutrition_logs_by_date(db: Session, user_id: int, date: date) -> List[NutritionLog]:
    """Get all nutrition logs for a specific date"""
    return (
        db.query(NutritionLog)
        .filter(NutritionLog.user_id == user_id, NutritionLog.date == date)
        .order_by(NutritionLog.meal_time)
        .all()
    )


def get_nutrition_logs_by_period(db: Session, user_id: int, start_date: date, end_date: date) -> List[NutritionLog]:
    """Get nutrition logs for a date range"""
    return (
        db.query(NutritionLog)
        .filter(NutritionLog.user_id == user_id, NutritionLog.date >= start_date, NutritionLog.date <= end_date)
        .order_by(NutritionLog.date, NutritionLog.meal_time)
        .all()
    )


def get_activity_logs_by_period(db: Session, user_id: int, start_date: date, end_date: date) -> List[ActivityLog]:
    """Get activity logs (Garmin data) for a date range"""
    return (
        db.query(ActivityLog)
        .filter(ActivityLog.user_id == user_id, ActivityLog.date >= start_date, ActivityLog.date <= end_date)
        .order_by(ActivityLog.date)
        .all()
    )


def get_nutrition_totals_by_date(db: Session, user_id: int, date: date) -> Dict:
    """Calculate total nutrition for a specific date"""
    logs = get_nutrition_logs_by_date(db, user_id, date)

    total = {"calories": 0, "protein": 0, "fats": 0, "carbs": 0, "fiber": 0}

    for log in logs:
        totals = log.totals or {}
        # `totals.get(k, 0)` returns 0 only when key is MISSING. If the JSONB
        # field stores `null` explicitly, `.get()` returns None and `int +=
        # None` raises TypeError. Coerce via `or 0` so partially-filled rows
        # (e.g. from log_meal_text when LLM parser couldn't compute KБЖУ)
        # don't 500 the Mini App dashboard.
        total["calories"] += totals.get("calories") or 0
        total["protein"] += totals.get("protein") or 0
        total["fats"] += totals.get("fats") or 0
        total["carbs"] += totals.get("carbs") or 0
        total["fiber"] += totals.get("fiber") or 0

    return total


def delete_nutrition_log(db: Session, log_id: int, user_id: int) -> bool:
    """Delete a nutrition log entry"""
    log = db.query(NutritionLog).filter(NutritionLog.id == log_id, NutritionLog.user_id == user_id).first()

    if log:
        db.delete(log)
        db.commit()
        return True
    return False


# ==================== WEIGHT OPERATIONS ====================


def create_weight(
    db: Session,
    user_id: int,
    measured_at: datetime,
    weight: float,
    body_fat: Optional[float] = None,
    muscle_mass: Optional[float] = None,
    water: Optional[float] = None,
    bmi: Optional[float] = None,
    visceral_fat: Optional[int] = None,
    bone_mass: Optional[float] = None,
    source: str = "manual",
) -> Weight:
    """Create a new weight entry"""
    weight_entry = Weight(
        user_id=user_id,
        measured_at=measured_at,
        weight=weight,
        body_fat=body_fat,
        muscle_mass=muscle_mass,
        water=water,
        bmi=bmi,
        visceral_fat=visceral_fat,
        bone_mass=bone_mass,
        source=source,
    )
    db.add(weight_entry)
    db.commit()
    db.refresh(weight_entry)
    return weight_entry


# Источники веса, вводимые человеком вручную (текстом/фото). Их дедупим за день.
# Device-синки (apple_health/garmin/zepp/screenshot_ocr) пишут реальные замеры с
# intraday-таймстампами — их НЕ дедупим и НЕ перезаписываем.
MANUAL_WEIGHT_SOURCES = {"manual", "llm_text"}


def upsert_manual_weight(
    db: Session,
    user_id: int,
    measured_at: datetime,
    weight: float,
    body_fat: Optional[float] = None,
    muscle_mass: Optional[float] = None,
    water: Optional[float] = None,
    bmi: Optional[float] = None,
    visceral_fat: Optional[int] = None,
    bone_mass: Optional[float] = None,
    source: str = "manual",
) -> Weight:
    """Создать/обновить вес, не плодя дублей за календарный день (#170).

    Повторный ручной ввод веса текстом создаёт `measured_at=now()` с микросекундами,
    который не коллизит UniqueConstraint(user_id, measured_at) → два ряда за день.
    Для ручных источников (`MANUAL_WEIGHT_SOURCES`) обновляем существующий ряд того
    же дня вместо вставки. Device-синки идут обычным INSERT (реальные замеры).
    """
    if source in MANUAL_WEIGHT_SOURCES:
        day_start = datetime(measured_at.year, measured_at.month, measured_at.day)
        day_end = day_start + timedelta(days=1)
        existing = (
            db.query(Weight)
            .filter(
                Weight.user_id == user_id,
                Weight.source.in_(MANUAL_WEIGHT_SOURCES),
                Weight.measured_at >= day_start,
                Weight.measured_at < day_end,
            )
            .order_by(desc(Weight.measured_at))
            .first()
        )
        if existing is not None:
            existing.measured_at = measured_at
            existing.weight = weight
            existing.body_fat = body_fat
            existing.muscle_mass = muscle_mass
            existing.water = water
            existing.bmi = bmi
            existing.visceral_fat = visceral_fat
            existing.bone_mass = bone_mass
            existing.source = source
            db.commit()
            db.refresh(existing)
            return existing

    return create_weight(
        db,
        user_id=user_id,
        measured_at=measured_at,
        weight=weight,
        body_fat=body_fat,
        muscle_mass=muscle_mass,
        water=water,
        bmi=bmi,
        visceral_fat=visceral_fat,
        bone_mass=bone_mass,
        source=source,
    )


def upsert_device_weight(
    db: Session,
    user_id: int,
    measured_at: datetime,
    weight: float,
    body_fat: Optional[float] = None,
    muscle_mass: Optional[float] = None,
    water: Optional[float] = None,
    bmi: Optional[float] = None,
    visceral_fat: Optional[int] = None,
    bone_mass: Optional[float] = None,
    source: str = "agent_api",
) -> bool:
    """Идемпотентно записать замер с весов по ключу (user_id, measured_at).

    Возвращает True если строка создана, False если обновлена. Не коммитит —
    вызывающий решает границу транзакции (батч истории = одна транзакция).

    Device-семантика (#170): ключ — точный таймстамп замера, без дедупа по
    календарному дню. Дедуп по дню нужен только ручному вводу, где measured_at
    это `now()` с микросекундами (см. upsert_manual_weight).

    COALESCE-семантика на обновлении: None в поле НЕ затирает уже известное
    значение. Каналы приносят разные наборы полей — HAE отдаёт вес/жир/безжировую
    массу, весы Withings — полный состав (мышцы/вода/кости/висцеральный жир,
    которых нет в HealthKit). Апсерт одного канала не должен обнулять другой.

    Select-then-write, а не диалектный ON CONFLICT: один код на Postgres (прод) и
    SQLite (тесты) — тот же приём, что в upsert_blood_test. Unique-констрейнт
    weights_user_id_measured_at_key остаётся страховкой на уровне БД.
    """
    existing = (
        db.query(Weight)
        .filter(
            Weight.user_id == user_id,
            Weight.measured_at == measured_at,
        )
        .first()
    )

    if existing is None:
        db.add(
            Weight(
                user_id=user_id,
                measured_at=measured_at,
                weight=weight,
                body_fat=body_fat,
                muscle_mass=muscle_mass,
                water=water,
                bmi=bmi,
                visceral_fat=visceral_fat,
                bone_mass=bone_mass,
                source=source,
            )
        )
        # Flush обязателен: прод-сессия создаётся с autoflush=False
        # (database/__init__.py), поэтому без него только что добавленная строка не
        # видна SELECT'у выше на следующей итерации батча. Два замера с одинаковым
        # measured_at в одном запросе роняли бы commit по UNIQUE-констрейнту.
        db.flush()
        return True

    existing.weight = weight
    for field, value in (
        ("body_fat", body_fat),
        ("muscle_mass", muscle_mass),
        ("water", water),
        ("bmi", bmi),
        ("visceral_fat", visceral_fat),
        ("bone_mass", bone_mass),
    ):
        if value is not None:
            setattr(existing, field, value)
    existing.source = source
    return False


def get_latest_weight(db: Session, user_id: int) -> Optional[Weight]:
    """Get the most recent weight measurement"""
    return db.query(Weight).filter(Weight.user_id == user_id).order_by(desc(Weight.measured_at)).first()


def get_weights_by_period(db: Session, user_id: int, start_date: datetime, end_date: datetime) -> List[Weight]:
    """Get weight measurements for a date range"""
    return (
        db.query(Weight)
        .filter(Weight.user_id == user_id, Weight.measured_at >= start_date, Weight.measured_at <= end_date)
        .order_by(Weight.measured_at)
        .all()
    )


def get_weight_stats(db: Session, user_id: int, days: int = 30) -> Dict:
    """Get weight statistics for the last N days"""
    end_date = datetime.now()
    start_date = end_date - timedelta(days=days)

    weights = get_weights_by_period(db, user_id, start_date, end_date)

    if not weights:
        return {}

    weight_values = [w.weight for w in weights]

    return {
        "current": weights[-1].weight,
        "min": min(weight_values),
        "max": max(weight_values),
        "avg": sum(weight_values) / len(weight_values),
        "change": weights[-1].weight - weights[0].weight,
        "count": len(weights),
    }


# ==================== SUPPLEMENT LOG OPERATIONS ====================


def create_supplement_log(
    db: Session, user_id: int, date: date, time: Optional[time], supplement_name: str, dosage: Optional[str] = None
) -> SupplementLog:
    """Create a new supplement log entry"""
    log = SupplementLog(user_id=user_id, date=date, time=time, supplement_name=supplement_name, dosage=dosage)
    db.add(log)
    db.commit()
    db.refresh(log)
    return log


def get_supplements_by_date(db: Session, user_id: int, date: date) -> List[SupplementLog]:
    """Get all supplements taken on a specific date"""
    return (
        db.query(SupplementLog)
        .filter(SupplementLog.user_id == user_id, SupplementLog.date == date)
        .order_by(SupplementLog.time)
        .all()
    )


# User operations
def ensure_user_exists(
    db: Session, telegram_id: int, username: str = None, first_name: str = None, last_name: str = None
) -> User:
    """
    Ensure user exists in database. Create if doesn't exist.

    Args:
        db: Database session
        telegram_id: Telegram user ID
        username: Telegram username
        first_name: User's first name
        last_name: User's last name

    Returns:
        User object (existing or newly created)
    """
    user = db.query(User).filter(User.telegram_id == telegram_id).first()

    if not user:
        user = User(
            telegram_id=telegram_id,
            username=username,
            first_name=first_name,
            last_name=last_name,
            is_active=True,
            role="user",
        )
        db.add(user)
        db.flush()  # получаем PK до создания settings

        # Создаём дефолтные настройки: calorie_goal_pct=-15, show_calorie_budget_bar=True
        settings = UserSettings(user_id=telegram_id)
        db.add(settings)

        db.commit()
        db.refresh(user)
        logger.info(f"✅ New user registered: {telegram_id} (@{username})")
    else:
        # Update last active time
        from datetime import datetime

        user.last_active = datetime.now()
        db.commit()

    return user


def get_supplements_by_period(db: Session, user_id: int, start_date: date, end_date: date) -> List[SupplementLog]:
    """Get supplements for a date range"""
    return (
        db.query(SupplementLog)
        .filter(SupplementLog.user_id == user_id, SupplementLog.date >= start_date, SupplementLog.date <= end_date)
        .order_by(SupplementLog.date, SupplementLog.time)
        .all()
    )


# ==================== ACTIVITY LOG OPERATIONS ====================


def create_or_update_activity(
    db: Session,
    user_id: int,
    date: date,
    steps: Optional[int] = None,
    active_calories: Optional[float] = None,
    total_calories: Optional[float] = None,
    bmr_calories: Optional[float] = None,
    distance_km: Optional[float] = None,
    sleep_hours: Optional[float] = None,
    heart_rate_avg: Optional[int] = None,
    hrv: Optional[int] = None,
    stress_level: Optional[int] = None,
    source: str = "apple_health",
    raw_data: Optional[Dict] = None,
    monotonic: bool = True,
) -> ActivityLog:
    """Create or update activity log for a specific date.

    monotonic=True (default): cumulative intraday metrics (steps, active/total/
    bmr calories, distance) may only grow within the same day. A wearable's
    daily summary is cumulative, so a fetch that returns a value *lower* than
    what's already stored is a partial/stale sync (e.g. a get_stats() race
    where the device hadn't finished uploading the day yet) and is ignored for
    that field. Prevents a near-empty mid-sync snapshot from clobbering a full
    day. Set monotonic=False for manual corrections / backfills that must
    overwrite unconditionally.
    """
    # Check if entry exists
    existing = db.query(ActivityLog).filter(ActivityLog.user_id == user_id, ActivityLog.date == date).first()

    if existing:
        # Cumulative daily counters: keep the larger of (stored, incoming) so a
        # partial sync can't drag a full day down. Non-cumulative fields below
        # (sleep, HR, HRV, stress = averages/snapshots) always overwrite.
        def _set_cumulative(field: str, new_val):
            if new_val is None:
                return
            old_val = getattr(existing, field)
            if monotonic and old_val is not None and new_val < old_val:
                logger.warning(
                    "activity[%s %s]: ignoring regression %s %s→%s (partial/stale %s sync)",
                    user_id,
                    date,
                    field,
                    old_val,
                    new_val,
                    source,
                )
                return
            setattr(existing, field, new_val)

        # Update existing entry
        _set_cumulative("steps", steps)
        _set_cumulative("active_calories", active_calories)
        _set_cumulative("total_calories", total_calories)
        _set_cumulative("bmr_calories", bmr_calories)
        _set_cumulative("distance_km", distance_km)
        if sleep_hours is not None:
            existing.sleep_hours = sleep_hours
        if heart_rate_avg is not None:
            existing.heart_rate_avg = heart_rate_avg
        if hrv is not None:
            existing.hrv = hrv
        if stress_level is not None:
            existing.stress_level = stress_level
        if raw_data is not None:
            # Merge instead of replace — prevents Garmin sync from wiping
            # Apple Health fields (blood pressure, gait, etc.)
            existing.raw_data = {**(existing.raw_data or {}), **raw_data}

        existing.synced_at = datetime.now()
        db.commit()
        db.refresh(existing)
        return existing
    else:
        # Create new entry
        activity = ActivityLog(
            user_id=user_id,
            date=date,
            steps=steps,
            active_calories=active_calories,
            total_calories=total_calories,
            bmr_calories=bmr_calories,
            distance_km=distance_km,
            sleep_hours=sleep_hours,
            heart_rate_avg=heart_rate_avg,
            hrv=hrv,
            stress_level=stress_level,
            source=source,
            raw_data=raw_data,
        )
        db.add(activity)
        db.commit()
        db.refresh(activity)
        return activity


def get_activity_by_date(db: Session, user_id: int, date: date) -> Optional[ActivityLog]:
    """Get activity log for a specific date"""
    return db.query(ActivityLog).filter(ActivityLog.user_id == user_id, ActivityLog.date == date).first()


def get_activities_by_period(db: Session, user_id: int, start_date: date, end_date: date) -> List[ActivityLog]:
    """Get activity logs for a date range"""
    return (
        db.query(ActivityLog)
        .filter(ActivityLog.user_id == user_id, ActivityLog.date >= start_date, ActivityLog.date <= end_date)
        .order_by(ActivityLog.date)
        .all()
    )


def get_average_activity_stats(db: Session, user_id: int, days: int = 14) -> Dict:
    import logging

    _log = logging.getLogger(__name__)
    """Get average activity stats for the last N days.

    Filters out incomplete Garmin syncs (partial days with total < 1500 kcal)
    to prevent the rolling average from being dragged down by garbage data.
    """
    end_date = datetime.now().date()
    start_date = end_date - timedelta(days=days)

    activities = get_activities_by_period(db, user_id, start_date, end_date)

    if not activities:
        _log.info(f"[avg_activity] user_id={user_id} дней={days}: нет записей в activity_log")
        return {}

    # Only include days with plausible total calories (full Garmin sync).
    # Partial syncs (watch charging, early sync) produce total < 1500 — skip them.
    MIN_TOTAL_CALORIES = 1500
    valid_activities = [a for a in activities if a.total_calories and a.total_calories >= MIN_TOTAL_CALORIES]

    skipped = len(activities) - len(valid_activities)
    if skipped:
        skipped_dates = [
            str(a.date) for a in activities if not a.total_calories or a.total_calories < MIN_TOTAL_CALORIES
        ]
        _log.info(f"[avg_activity] filtered out {skipped} incomplete days: {skipped_dates}")

    if not valid_activities:
        _log.info(f"[avg_activity] user_id={user_id} дней={days}: нет полных дней")
        return {}

    result = {
        "active_calories": sum(a.active_calories or 0 for a in valid_activities) / len(valid_activities),
        "total_calories": sum(a.total_calories for a in valid_activities) / len(valid_activities),
        "bmr_calories": sum(a.bmr_calories or 0 for a in valid_activities) / len(valid_activities),
        "steps": sum(a.steps or 0 for a in valid_activities) / len(valid_activities),
        "count": len(valid_activities),
    }
    _log.info(f"[avg_activity] user_id={user_id} count={result['count']} total_cal={result['total_calories']:.0f}")
    return result


# ==================== BLOOD TEST OPERATIONS ====================


def create_blood_test(
    db: Session,
    user_id: int,
    test_date: date,
    test_type: Optional[str],
    values: Dict,
    file_path: Optional[str] = None,
    status: str = "current",
) -> BloodTest:
    """Create a new blood test entry"""
    test = BloodTest(
        user_id=user_id, test_date=test_date, test_type=test_type, values=values, file_path=file_path, status=status
    )
    db.add(test)
    db.commit()
    db.refresh(test)
    return test


def get_latest_blood_test(db: Session, user_id: int) -> Optional[BloodTest]:
    """Get the most recent blood test"""
    return (
        db.query(BloodTest)
        .filter(BloodTest.user_id == user_id, BloodTest.status == "current")
        .order_by(desc(BloodTest.test_date))
        .first()
    )


def get_blood_tests_by_period(db: Session, user_id: int, start_date: date, end_date: date) -> List[BloodTest]:
    """Get blood tests for a date range"""
    return (
        db.query(BloodTest)
        .filter(BloodTest.user_id == user_id, BloodTest.test_date >= start_date, BloodTest.test_date <= end_date)
        .order_by(BloodTest.test_date)
        .all()
    )


def get_all_blood_tests(db: Session, user_id: int) -> List[BloodTest]:
    """Get all blood tests for a user"""
    return db.query(BloodTest).filter(BloodTest.user_id == user_id).order_by(desc(BloodTest.test_date)).all()


def upsert_blood_test(db: Session, row: dict) -> bool:
    """Идемпотентно записать анализ по ключу (user_id, test_date, test_type).

    Возвращает True если строка создана, False если обновлена. Select-then-write,
    а не диалектный ON CONFLICT: один и тот же код работает и на Postgres (прод),
    и на SQLite (тесты). Уникальный индекс blood_tests_user_date_type_unique
    остаётся страховкой на уровне БД.

    Используется /doc (см. core/health/doc_to_blood_test.build_blood_test_row).
    Скрипт scripts/import/kb_to_blood_tests.py ходит своим путём — он исполняется
    на маке и пишет через ssh+psql, без ORM-сессии.
    """
    test_date = row["test_date"]
    if isinstance(test_date, str):
        test_date = date.fromisoformat(test_date)

    existing = (
        db.query(BloodTest)
        .filter(
            BloodTest.user_id == row["user_id"],
            BloodTest.test_date == test_date,
            BloodTest.test_type == row["test_type"],
        )
        .first()
    )
    status = row.get("status") or "current"

    if existing is not None:
        existing.values = row["values"]
        existing.file_path = row.get("file_path")
        existing.status = status
        db.commit()
        return False

    db.add(
        BloodTest(
            user_id=row["user_id"],
            test_date=test_date,
            test_type=row["test_type"],
            values=row["values"],
            file_path=row.get("file_path"),
            status=status,
        )
    )
    db.commit()
    return True


def get_last_activity_date(db: Session, user_id: int) -> Optional[date]:
    """Get the most recent date with activity data"""
    result = db.query(ActivityLog.date).filter(ActivityLog.user_id == user_id).order_by(ActivityLog.date.desc()).first()
    return result[0] if result else None


# ==================== USER PRODUCTS (removed Apr 2026 — /my_products feature) ====================
# The /my_products, /add_product, /add_variant commands and backing tables were
# removed after 0 rows across all users. If you need similar functionality in
# future, prefer extending the LLM prompt or using the favorites endpoint.


def create_body_measurement(
    db: Session,
    user_id: int,
    date: date,
    waist_cm: Optional[float] = None,
    neck_cm: Optional[float] = None,
    hips_cm: Optional[float] = None,
    chest_cm: Optional[float] = None,
    thigh_cm: Optional[float] = None,
    biceps_cm: Optional[float] = None,
    notes: Optional[str] = None,
) -> BodyMeasurement:
    """Create a new body measurement entry"""
    measurement = BodyMeasurement(
        user_id=user_id,
        date=date,
        waist_cm=waist_cm,
        neck_cm=neck_cm,
        hips_cm=hips_cm,
        chest_cm=chest_cm,
        thigh_cm=thigh_cm,
        biceps_cm=biceps_cm,
        notes=notes,
    )
    db.add(measurement)
    db.commit()
    db.refresh(measurement)
    return measurement


# ==================== USER SETTINGS ====================


def get_user_settings(db: Session, user_id: int) -> Optional["UserSettings"]:
    """Get settings for a user. Returns None if no settings saved yet."""
    from database.models import UserSettings

    return db.query(UserSettings).filter(UserSettings.user_id == user_id).first()


def upsert_user_settings(db: Session, user_id: int, **kwargs) -> "UserSettings":
    """Create or update user settings. Pass fields as kwargs.

    Example:
        upsert_user_settings(db, user_id=123456789, show_calorie_budget_bar=False)
    """
    from database.models import UserSettings

    settings = get_user_settings(db, user_id)
    if settings is None:
        settings = UserSettings(user_id=user_id, **kwargs)
        db.add(settings)
    else:
        for key, value in kwargs.items():
            setattr(settings, key, value)
        settings.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(settings)
    return settings


# ==================== NUTRITION ITEM-LEVEL EDIT HELPERS ====================


def get_nutrition_log(db: Session, meal_id: int, user_id: int) -> Optional[NutritionLog]:
    """Fetch single nutrition log row, scoped by user."""
    return db.query(NutritionLog).filter(NutritionLog.id == meal_id, NutritionLog.user_id == user_id).first()


def _recalc_totals(items: list) -> dict:
    totals = {"calories": 0.0, "protein": 0.0, "fats": 0.0, "carbs": 0.0, "fiber": 0.0}
    for it in items:
        for k in ("calories", "protein", "fats", "carbs", "fiber"):
            totals[k] += float(it.get(k, 0) or 0)
    return {k: round(v, 1) for k, v in totals.items()}


def update_nutrition_item_weight(db: Session, meal_id: int, user_id: int, idx: int, new_weight: float) -> tuple:
    """Scale item KBJU proportionally to new weight. Returns (item, totals)."""
    row = get_nutrition_log(db, meal_id=meal_id, user_id=user_id)
    if row is None:
        raise LookupError(f"meal {meal_id} not found for user {user_id}")
    items = list(row.items or [])
    if idx < 0 or idx >= len(items):
        raise IndexError(f"idx {idx} out of range (have {len(items)} items)")

    old = dict(items[idx])
    # Read from canonical "amount" first, fall back to legacy "weight_g" / "weight"
    old_w = float(old.get("amount") or old.get("weight_g") or old.get("weight") or 0)
    if old_w <= 0:
        old["amount"] = new_weight
    else:
        factor = new_weight / old_w
        old["amount"] = new_weight
        for k in ("calories", "protein", "fats", "carbs", "fiber"):
            if old.get(k) is not None:
                old[k] = round(float(old[k]) * factor, 1)
    # Strip legacy weight keys to prevent hybrid {amount, weight_g} rows
    old.pop("weight_g", None)
    old.pop("weight", None)
    # Ensure canonical unit marker
    if "unit" not in old:
        old["unit"] = "г"

    items[idx] = old
    row.items = items
    row.totals = _recalc_totals(items)
    from sqlalchemy.orm.attributes import flag_modified

    flag_modified(row, "items")
    flag_modified(row, "totals")
    db.commit()
    db.refresh(row)
    return old, row.totals


def delete_nutrition_item(db: Session, meal_id: int, user_id: int, idx: int) -> tuple:
    """Remove item. Deletes meal row if it was the last item. Returns (removed, new_totals_or_None)."""
    row = get_nutrition_log(db, meal_id=meal_id, user_id=user_id)
    if row is None:
        raise LookupError(f"meal {meal_id} not found for user {user_id}")
    items = list(row.items or [])
    if idx < 0 or idx >= len(items):
        raise IndexError(f"idx {idx} out of range")
    removed = items.pop(idx)
    if not items:
        db.delete(row)
        db.commit()
        return removed, None
    row.items = items
    row.totals = _recalc_totals(items)
    from sqlalchemy.orm.attributes import flag_modified

    flag_modified(row, "items")
    flag_modified(row, "totals")
    db.commit()
    db.refresh(row)
    return removed, row.totals


def update_nutrition_meal_fields(
    db: Session,
    meal_id: int,
    user_id: int,
    meal_name: Optional[str] = None,
    meal_time: Optional[time] = None,
    new_date: Optional[date] = None,
) -> NutritionLog:
    row = get_nutrition_log(db, meal_id=meal_id, user_id=user_id)
    if row is None:
        raise LookupError(f"meal {meal_id} not found for user {user_id}")
    if meal_name is not None:
        row.meal_name = meal_name
    if meal_time is not None:
        row.meal_time = meal_time
    if new_date is not None:
        row.date = new_date
    db.commit()
    db.refresh(row)
    return row


def find_meal_for_slot(db: Session, user_id: int, for_date: date, slot: str) -> Optional[NutritionLog]:
    """Find first nutrition_log on that date whose (name, time) maps to `slot`."""
    import sys as _sys
    import pathlib as _pl

    _sys.path.insert(0, str(_pl.Path(__file__).resolve().parent.parent / "telegram-bot"))
    from webhook.nutrition_slots import slot_from_meal

    rows = get_nutrition_logs_by_date(db, user_id=user_id, date=for_date)
    for r in rows:
        if slot_from_meal(r.meal_name, r.meal_time) == slot:
            return r
    return None


def get_recent_product_names(db: Session, user_id: int, limit: int = 15, lookback_days: int = 90) -> list:
    """Aggregate recent product usage from nutrition_log.items[]. Sort by last_used DESC."""
    from collections import OrderedDict

    from core.food.fiber_table import _item_name, _item_weight

    end = date.today()
    start = end - timedelta(days=lookback_days)
    rows = get_nutrition_logs_by_period(db, user_id=user_id, start_date=start, end_date=end)
    by_name: Dict[str, Any] = OrderedDict()
    for r in sorted(rows, key=lambda x: (x.date, x.meal_time or time(0, 0)), reverse=True):
        for it in r.items or []:
            name = _item_name(it).strip()
            if not name or name in by_name:
                continue
            w = _item_weight(it)
            if w <= 0:
                continue

            def per100(key, weight=w, item=it):
                v = item.get(key)
                return round(float(v) * 100 / weight, 1) if v is not None else 0

            by_name[name] = {
                "name": name,
                "default_weight": round(w, 0),
                "last_used": r.date.isoformat(),
                "per_100": {
                    "kcal": per100("calories"),
                    "p": per100("protein"),
                    "f": per100("fats"),
                    "c": per100("carbs"),
                    "fib": per100("fiber"),
                },
            }
            if len(by_name) >= limit:
                break
        if len(by_name) >= limit:
            break
    return list(by_name.values())


# ==================== VERIFIED PRODUCTS (#255) ====================


def get_verified_products(db: Session, user_id: int, limit: Optional[int] = None) -> List[VerifiedProduct]:
    """Записи справочника, видимые пользователю: личные + общие (user_id IS NULL).

    Личные идут первыми (приоритет при матчинге), внутри группы — по times_used DESC.
    """
    q = (
        db.query(VerifiedProduct)
        .filter((VerifiedProduct.user_id == user_id) | (VerifiedProduct.user_id.is_(None)))
        # nullslast нельзя: sqlite до 3.30 не умеет NULLS LAST — сортируем выражением
        .order_by(VerifiedProduct.user_id.is_(None), desc(VerifiedProduct.times_used))
    )
    if limit:
        q = q.limit(limit)
    return q.all()


def find_verified_product(db: Session, user_id: int, name_norm: str) -> Optional[VerifiedProduct]:
    """Точный поиск по нормализованному имени. Личная запись приоритетнее общей."""
    return (
        db.query(VerifiedProduct)
        .filter(
            VerifiedProduct.name_norm == name_norm,
            (VerifiedProduct.user_id == user_id) | (VerifiedProduct.user_id.is_(None)),
        )
        .order_by(VerifiedProduct.user_id.is_(None))
        .first()
    )


def upsert_verified_product(
    db: Session,
    user_id: Optional[int],
    name: str,
    name_norm: str,
    calories_per_100g: float,
    protein_per_100g: float,
    fats_per_100g: float,
    carbs_per_100g: float,
    source: str,
    fiber_per_100g: Optional[float] = None,
    portion_g: Optional[float] = None,
    brand: Optional[str] = None,
    aliases: Optional[list] = None,
    barcode: Optional[str] = None,
) -> VerifiedProduct:
    """Создаёт или обновляет запись справочника в скоупе (user_id, name_norm).

    user_id=None — общая запись (сид/оператор). Повторное «Запомнить продукт»
    с новыми цифрами обновляет существующую запись, не плодя дубли.
    """
    existing = (
        db.query(VerifiedProduct)
        .filter(
            VerifiedProduct.name_norm == name_norm,
            VerifiedProduct.user_id == user_id if user_id is not None else VerifiedProduct.user_id.is_(None),
        )
        .first()
    )
    if existing:
        existing.name = name
        existing.calories_per_100g = calories_per_100g
        existing.protein_per_100g = protein_per_100g
        existing.fats_per_100g = fats_per_100g
        existing.carbs_per_100g = carbs_per_100g
        existing.source = source
        if fiber_per_100g is not None:
            existing.fiber_per_100g = fiber_per_100g
        if portion_g is not None:
            existing.portion_g = portion_g
        if brand is not None:
            existing.brand = brand
        if aliases is not None:
            existing.aliases = aliases
        if barcode is not None:
            existing.barcode = barcode
        db.commit()
        db.refresh(existing)
        return existing

    product = VerifiedProduct(
        user_id=user_id,
        name=name,
        name_norm=name_norm,
        brand=brand,
        aliases=aliases,
        barcode=barcode,
        calories_per_100g=calories_per_100g,
        protein_per_100g=protein_per_100g,
        fats_per_100g=fats_per_100g,
        carbs_per_100g=carbs_per_100g,
        fiber_per_100g=fiber_per_100g,
        portion_g=portion_g,
        source=source,
    )
    db.add(product)
    db.commit()
    db.refresh(product)
    return product


def increment_verified_product_usage(db: Session, product_id: int) -> None:
    """times_used += 1 — для ранжирования топ-N в промпт-блоке."""
    db.query(VerifiedProduct).filter(VerifiedProduct.id == product_id).update(
        {VerifiedProduct.times_used: VerifiedProduct.times_used + 1}
    )
    db.commit()


# ==================== RLS HELPERS ====================


def set_user_session_var(db: Session, user_id: int) -> None:
    """Set app.user_id session variable for RLS filtering.

    Must be called at the start of every hv_app-role request.
    Use SET LOCAL inside a transaction so it auto-clears at commit/rollback.

    Example:
        with db.begin():
            set_user_session_var(db, user_id=123456789)
            logs = db.execute(text("SELECT * FROM nutrition_log")).fetchall()
    """
    # SET LOCAL only accepts string literals, not parameterized values — str() cast required
    db.execute(text("SET LOCAL app.user_id = :uid"), {"uid": str(user_id)})


# ==================== FEEDBACK INBOX (#188) ====================


def create_feedback(
    db: Session,
    *,
    user_id: int,
    text: str,
    source: str,
    kind: str = "unspecified",
    agent_context: Optional[dict] = None,
) -> UserFeedback:
    """Записать обратную связь в единый инбокс user_feedback (#188).

    source: 'command' | 'agent' | 'webapp'. kind: 'bug'|'feature'|'question'|'unspecified'.
    Вызывающий код ДОЛЖЕН предварительно проверить is_feedback_opted_out().
    """
    row = UserFeedback(
        user_id=user_id,
        text=text,
        source=source,
        kind=kind,
        agent_context=agent_context,
        status="new",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def list_recent_feedback(db: Session, *, status: Optional[str] = "new", limit: int = 20) -> List[UserFeedback]:
    """Последние записи фидбека, новые сверху. status=None → все статусы (#269 триаж)."""
    q = db.query(UserFeedback)
    if status is not None:
        q = q.filter(UserFeedback.status == status)
    return q.order_by(UserFeedback.created_at.desc(), UserFeedback.id.desc()).limit(limit).all()


def is_feedback_opted_out(db: Session, user_id: int) -> bool:
    """True, если пользователь отписался от захвата фидбека. Нет строки настроек → False."""
    settings = db.query(UserSettings).filter(UserSettings.user_id == user_id).one_or_none()
    return bool(settings and settings.feedback_opt_out)


# ── Триаж инбокса фидбека (#269) ────────────────────────────────────────────
FEEDBACK_STATUSES = ("new", "triaged", "in_progress", "done", "wontfix", "duplicate")
FEEDBACK_PRIORITIES = ("P0", "P1", "P2", "P3")
_FEEDBACK_RESOLVED_STATUSES = ("done", "wontfix")


def get_feedback(db: Session, feedback_id: int) -> Optional[UserFeedback]:
    """Одна запись фидбека по id (или None)."""
    return db.query(UserFeedback).filter(UserFeedback.id == feedback_id).one_or_none()


def update_feedback_status(db: Session, feedback_id: int, status: str) -> Optional[UserFeedback]:
    """Сменить статус записи (#269). `resolved_at` ставится при done/wontfix и
    сбрасывается при возврате в открытый статус. Невалидный статус → ValueError;
    записи нет → None."""
    if status not in FEEDBACK_STATUSES:
        raise ValueError(f"invalid status {status!r}; allowed: {', '.join(FEEDBACK_STATUSES)}")
    row = get_feedback(db, feedback_id)
    if row is None:
        return None
    row.status = status
    if status in _FEEDBACK_RESOLVED_STATUSES:
        row.resolved_at = row.resolved_at or datetime.now(timezone.utc)
    else:
        row.resolved_at = None
    db.commit()
    db.refresh(row)
    return row


def set_feedback_priority(db: Session, feedback_id: int, priority: Optional[str]) -> Optional[UserFeedback]:
    """Приоритет P0-P3 (или None — снять) (#269). Невалидный → ValueError; нет записи → None."""
    if priority is not None and priority not in FEEDBACK_PRIORITIES:
        raise ValueError(f"invalid priority {priority!r}; allowed: {', '.join(FEEDBACK_PRIORITIES)} or None")
    row = get_feedback(db, feedback_id)
    if row is None:
        return None
    row.priority = priority
    db.commit()
    db.refresh(row)
    return row


def set_feedback_github(db: Session, feedback_id: int, github_issue: Optional[str]) -> Optional[UserFeedback]:
    """Привязать GitHub-issue (#269). Нормализуем ведущий '#' ('#123'→'123');
    пусто/None — снять привязку. Нет записи → None."""
    normalized = github_issue.strip().lstrip("#") if github_issue else None
    normalized = normalized or None
    if normalized is not None and len(normalized) > 64:
        raise ValueError(f"github_issue too long ({len(normalized)} > 64); передавай номер, не URL")
    row = get_feedback(db, feedback_id)
    if row is None:
        return None
    row.github_issue = normalized
    db.commit()
    db.refresh(row)
    return row


def mark_feedback_notified(db: Session, feedback_id: int) -> Optional[UserFeedback]:
    """Штамп `notified_at=now()` — guard идемпотентности уведомления автора (Фаза 3, #188).

    Ставит время только если оно ещё не выставлено: повторный вызов НЕ перезаписывает
    исходный штамп (значит, второй `done` не «сдвинет» дату и не спровоцирует повторную
    отправку). Нет записи → None."""
    row = get_feedback(db, feedback_id)
    if row is None:
        return None
    if row.notified_at is None:
        row.notified_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(row)
    return row
