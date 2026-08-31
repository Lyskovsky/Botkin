"""
Apple Health Webhook — принимает данные от iPhone Shortcuts.
Запускается параллельно с Telegram-ботом на порту 8080.
Данные пишутся в PostgreSQL в те же таблицы что и Garmin/Zepp.

Endpoint: POST https://botkin.health/apple_health (legacy-домен health.orangegate.cc остаётся redirect-каналом)
Auth: Bearer token (APPLE_HEALTH_TOKEN из .env)
"""

import hmac
import os
import logging
from datetime import date, datetime, timezone
from typing import Optional, List

from fastapi import FastAPI, HTTPException, Depends, Header, Request
from pydantic import BaseModel, Field, field_validator
import uvicorn

from bot_token import resolve_bot_token
from core.health.hr_artifact import classify_hr_min

logger = logging.getLogger(__name__)

app = FastAPI(title="Botkin Apple Health Webhook", docs_url=None, redoc_url=None)

# ── Telegram webhook dispatcher (set by bot.py at startup) ───────────────────
# These are populated by set_telegram_dispatcher() so the /telegram/webhook
# endpoint can feed updates to aiogram without circular imports.
_tg_bot = None
_tg_dp = None


def set_telegram_dispatcher(bot, dp):
    """Called from bot.py after Bot/Dispatcher are created."""
    global _tg_bot, _tg_dp
    _tg_bot = bot
    _tg_dp = dp


APPLE_HEALTH_TOKEN = os.getenv("APPLE_HEALTH_TOKEN", "")
BOT_TOKEN = resolve_bot_token()
# Fallback user for backward compat (single-user setup).
# In multi-user setup each user has their own health_token in users.health_token.
_target_user_id = int(os.getenv("TELEGRAM_USER_ID", "895655"))


# ── Telegram WebApp Auth ──────────────────────────────────────────────────────

from webhook.tg_auth import get_tg_user, verify_telegram_init_data  # noqa: F401


# ── Auth ──────────────────────────────────────────────────────────────────────


def _resolve_apple_bmr(existing_row, basal_energy_kcal):
    """Значение для записи в bmr_calories из Apple-источника (#329).

    `basal_energy_kcal` (Apple RMR) — накопительный внутридневной счётчик:
    free-путь (iOS Shortcuts) шлёт «сегодня на текущий момент» при каждом
    открытии Telegram, поэтому утренний запуск несёт частичное значение.
    `create_or_update_activity(monotonic=True)` держит per-day максимум, поэтому
    достаточно отдавать basal на каждом вызове — CRUD сам дорастит его до полного
    дневного значения (регрессию от частичного повторного синка гасит там же).

    Исключение: если BMR уже проставлен Garmin-синком — не трогаем (Garmin > Apple),
    возвращаем None, чтобы CRUD оставил bmr_calories как есть. Строка считается
    Garmin-овой по `source` её создателя (update-путь CRUD не меняет source).
    """
    if basal_energy_kcal is None:
        return None
    if (
        existing_row is not None
        and existing_row.bmr_calories is not None
        and (existing_row.source or "").startswith("garmin")
    ):
        return None
    return basal_energy_kcal


def verify_token(authorization: Optional[str] = Header(None), token: Optional[str] = None):
    """Bearer token auth.

    Принимает токен либо заголовком `Authorization: Bearer <token>`, либо
    query-параметром `?token=<token>` — некоторые Android-приложения
    (напр. health-connect-webhook APK) не дают настроить кастомные заголовки.

    Returns the raw token string so the endpoint can resolve which user sent the data.
    """
    if authorization:
        if not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="Missing Bearer token")
        resolved = authorization.removeprefix("Bearer ").strip()
    elif token:
        resolved = token.strip()
    else:
        raise HTTPException(status_code=401, detail="Missing Bearer token or token query param")
    if not resolved:
        raise HTTPException(status_code=403, detail="Invalid token")
    # Accept either the global APPLE_HEALTH_TOKEN (backward compat / single-user)
    # OR any per-user token stored in users.health_token (multi-user).
    # Actual user resolution happens in the endpoint after DB lookup.
    if APPLE_HEALTH_TOKEN and resolved == APPLE_HEALTH_TOKEN:
        return resolved  # global token — will resolve to _target_user_id
    # Per-user tokens are validated inside the endpoint against the DB.
    return resolved


# ── Request schema ────────────────────────────────────────────────────────────


class AppleHealthPayload(BaseModel):
    """
    Данные за один день от iPhone Shortcuts.
    Все поля опциональны — Shortcut присылает только то что есть.
    """

    date: str = Field(..., description="YYYY-MM-DD, обычно вчерашняя дата")

    # Активность
    steps: Optional[int] = None
    distance_walking_km: Optional[float] = None
    flights_climbed: Optional[int] = None
    active_energy_kcal: Optional[float] = None
    basal_energy_kcal: Optional[float] = None  # Apple's RMR — used as BMR for non-Garmin users

    # Пульс
    resting_heart_rate: Optional[int] = None
    heart_rate_min: Optional[int] = None
    heart_rate_min_verify: Optional[bool] = None  # True → 30-39 bpm, держим но «проверить»
    heart_rate_max: Optional[int] = None
    heart_rate_avg: Optional[int] = None

    # Давление (последнее измерение дня)
    blood_pressure_systolic: Optional[int] = None
    blood_pressure_diastolic: Optional[int] = None

    # Ходьба / Gait
    walking_speed_km_h: Optional[float] = None
    walking_step_length_cm: Optional[float] = None
    walking_double_support_pct: Optional[float] = None
    walking_asymmetry_pct: Optional[float] = None

    # Состав тела (от Zepp через Apple Health)
    weight_kg: Optional[float] = None
    body_fat_pct: Optional[float] = None
    muscle_mass_kg: Optional[float] = None
    water_pct: Optional[float] = None

    # Сон и восстановление
    sleep_hours: Optional[float] = None
    sleep_deep_h: Optional[float] = None  # Глубокий сон, часы
    sleep_rem_h: Optional[float] = None  # REM сон, часы
    sleep_core_h: Optional[float] = None  # Core (light) сон, часы
    sleep_awake_h: Optional[float] = None  # Пробуждения в ночь, часы
    hrv: Optional[int] = None  # HRV SDNN, мс
    spo2_pct: Optional[float] = None

    # Дополнительно
    vo2_max: Optional[float] = None
    respiratory_rate: Optional[float] = None
    wrist_temperature: Optional[float] = None

    @field_validator(
        "steps",
        "flights_climbed",
        "resting_heart_rate",
        "heart_rate_min",
        "heart_rate_max",
        "heart_rate_avg",
        "blood_pressure_systolic",
        "blood_pressure_diastolic",
        "hrv",
        mode="before",
    )
    @classmethod
    def _round_float_to_int(cls, v):
        """Округлять дробные значения до int.

        iOS Shortcuts (бесплатный путь Apple Health) присылает усреднённые
        метрики как float (например heart_rate_avg=72.4). Pydantic v2 strict-int
        иначе отклоняет такие значения с 422 "got a number with a fractional part".
        Округляем до целого, сохраняя int-семантику поля. None и уже-целые int
        проходят без изменений; v2-путь (_hae_to_daily_payloads) уже шлёт int —
        для него валидатор no-op.
        """
        if isinstance(v, float):
            return round(v)
        return v


# ── Endpoint ──────────────────────────────────────────────────────────────────


@app.get("/health")
async def health_check():
    """Liveness probe для мониторинга."""
    return {"status": "ok", "service": "apple_health_webhook"}


@app.get("/api/public_stats")
async def public_stats():
    """Публичная статистика для лендинга botkin.health (CORS-открыто, без auth).

    Возвращает агрегаты: общее число приёмов пищи в системе, число активных
    пользователей. Никакого PII — только counts. Используется JS-fetch'ем в
    docs/landing/index.html для live-цифр в hero-блоке.
    """
    from fastapi.responses import JSONResponse
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
    try:
        from database import SessionLocal
        from sqlalchemy import text as sql_text

        db = SessionLocal()
        try:
            meals = db.execute(sql_text("SELECT COUNT(*) FROM nutrition_log")).scalar() or 0
            users = db.execute(sql_text("SELECT COUNT(*) FROM users WHERE is_active=true")).scalar() or 0
            biomarker_types = (
                db.execute(
                    sql_text(
                        "SELECT COUNT(DISTINCT k) FROM blood_tests, "
                        "jsonb_object_keys(values) AS k "
                        "WHERE k NOT LIKE '%_ref' AND k NOT LIKE '%_unit' AND k NOT LIKE '%_flag'"
                    )
                ).scalar()
                or 0
            )
            llm_month = (
                db.execute(
                    sql_text(
                        "SELECT COALESCE(SUM(cost_usd),0) FROM llm_usage_log "
                        "WHERE created_at >= date_trunc('month', NOW())"
                    )
                ).scalar()
                or 0
            )
        finally:
            db.close()
    except Exception as e:
        logger.warning(f"public_stats failed: {e}")
        return JSONResponse({"error": "stats unavailable"}, status_code=503)

    return JSONResponse(
        {
            "meals": meals,
            "active_users": users,
            "biomarker_types": biomarker_types,
            "llm_cost_this_month_usd": round(float(llm_month), 2),
        },
        headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "public, max-age=300"},
    )


@app.post("/apple_health")
async def receive_apple_health(
    payload: AppleHealthPayload,
    bearer_token: str = Depends(verify_token),
):
    """
    Принимает JSON от iPhone Shortcuts и пишет в БД.

    Роутинг пользователя:
    - Если bearer == APPLE_HEALTH_TOKEN (global) → _target_user_id (backward compat)
    - Иначе → ищем по users.health_token → получаем user_id из БД
    """
    try:
        record_date = date.fromisoformat(payload.date)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid date format: {payload.date!r}. Use YYYY-MM-DD.")

    saved = []

    try:
        # Импортируем здесь чтобы не ломать импорт если БД недоступна при старте
        import sys
        from pathlib import Path

        sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

        from database import SessionLocal
        from database.crud import (
            create_or_update_activity,
            get_user_by_health_token,
            get_activity_by_date,
        )

        # ── Resolve user ──────────────────────────────────────────────────────
        if APPLE_HEALTH_TOKEN and bearer_token == APPLE_HEALTH_TOKEN:
            target_user_id = _target_user_id
        else:
            # Per-user token: look up in DB
            _db_auth = SessionLocal()
            try:
                _user = get_user_by_health_token(_db_auth, bearer_token)
            finally:
                _db_auth.close()
            if not _user:
                raise HTTPException(status_code=403, detail="Unknown token")
            target_user_id = _user.telegram_id

        db = SessionLocal()
        try:
            # ── 1. activity_log (шаги, пульс, дистанция, калории) ───────────
            heart_rate = payload.resting_heart_rate or payload.heart_rate_avg

            # Gait и дополнительные метрики идут в raw_data
            raw = {}
            if payload.walking_speed_km_h is not None:
                raw["walking_speed_km_h"] = payload.walking_speed_km_h
            if payload.walking_step_length_cm is not None:
                raw["walking_step_length_cm"] = payload.walking_step_length_cm
            if payload.walking_double_support_pct is not None:
                raw["walking_double_support_pct"] = payload.walking_double_support_pct
            if payload.walking_asymmetry_pct is not None:
                raw["walking_asymmetry_pct"] = payload.walking_asymmetry_pct
            if payload.flights_climbed is not None:
                raw["flights_climbed"] = payload.flights_climbed
            if payload.heart_rate_min is not None:
                hr_min, needs_verify = classify_hr_min(payload.heart_rate_min)
                if hr_min is not None:
                    raw["heart_rate_min"] = hr_min
                    if needs_verify:
                        raw["heart_rate_min_verify"] = True
            if payload.heart_rate_max is not None:
                raw["heart_rate_max"] = payload.heart_rate_max
            # NOTE: blood_pressure_systolic/diastolic НЕ идут в raw_data —
            # они пишутся только в blood_pressure_logs (см. ниже).
            if payload.vo2_max is not None:
                raw["vo2_max"] = payload.vo2_max
            if payload.respiratory_rate is not None:
                raw["respiratory_rate"] = payload.respiratory_rate
            if payload.wrist_temperature is not None:
                raw["wrist_temperature"] = payload.wrist_temperature
            # #328: spo2 хранится только в raw_data (как в v2) — раньше v1 терял его.
            if payload.spo2_pct is not None:
                raw["spo2_pct"] = payload.spo2_pct

            # NOTE: do NOT pass active_calories — Apple Health's "active energy" is
            # computed differently than Garmin's and breaks the (total = bmr + active)
            # invariant. Garmin remains the source of truth for the calories triple.
            # Apple's value is preserved in raw_data for archival.
            if payload.active_energy_kcal is not None:
                raw = raw or {}
                raw["apple_active_energy_kcal"] = payload.active_energy_kcal
            if payload.basal_energy_kcal is not None:
                raw = raw or {}
                raw["apple_basal_energy_kcal"] = payload.basal_energy_kcal
            # BMR (basal): max-за-день для Apple, но Garmin-BMR не трогаем (#329).
            existing_row = get_activity_by_date(db, target_user_id, record_date)
            apple_bmr_for_db = _resolve_apple_bmr(existing_row, payload.basal_energy_kcal)
            activity = create_or_update_activity(
                db=db,
                user_id=target_user_id,
                date=record_date,
                steps=payload.steps,
                bmr_calories=apple_bmr_for_db,
                distance_km=payload.distance_walking_km,
                heart_rate_avg=heart_rate,
                hrv=payload.hrv,
                sleep_hours=payload.sleep_hours,
                source="apple_health_shortcut",
                raw_data=raw if raw else None,
            )
            saved.append(f"activity_log: steps={payload.steps}, HR={heart_rate}, dist={payload.distance_walking_km}km")

            # ── 2. blood_pressure_logs (BP в отдельную таблицу — не затирается Garmin) ──
            if payload.blood_pressure_systolic and payload.blood_pressure_diastolic:
                from sqlalchemy import text as _text

                measured_at = datetime.combine(record_date, datetime.min.time().replace(hour=8))
                db.execute(
                    _text(
                        """INSERT INTO blood_pressure_logs
                           (user_id, measured_at, systolic, diastolic, heart_rate, source)
                           VALUES (:uid, :ts, :sys, :dia, :hr, 'apple_health_shortcut')
                           ON CONFLICT (user_id, measured_at) DO UPDATE
                             SET systolic = EXCLUDED.systolic,
                                 diastolic = EXCLUDED.diastolic"""
                    ),
                    {
                        "uid": target_user_id,
                        "ts": measured_at,
                        "sys": payload.blood_pressure_systolic,
                        "dia": payload.blood_pressure_diastolic,
                        "hr": heart_rate,
                    },
                )
                saved.append(f"blood_pressure: {payload.blood_pressure_systolic}/{payload.blood_pressure_diastolic}")

            # ── 3. weights (если пришёл вес от весов через Apple Health) ─────
            # Issue #56: раньше тут был create_weight(date=record_date, ...) —
            # но сигнатура принимает measured_at: datetime, не date → TypeError,
            # а у create_weight нет ON CONFLICT → повторная отправка за день падала
            # на UniqueViolation. Зеркалим идемпотентный UPSERT из v2 (см. ниже).
            if payload.weight_kg and payload.weight_kg > 30:
                from sqlalchemy import text as _text

                weight_ts = datetime.combine(record_date, datetime.min.time().replace(hour=8))
                db.execute(
                    _text(
                        """INSERT INTO weights
                           (user_id, measured_at, weight, body_fat, muscle_mass, water, source)
                           VALUES (:uid, :ts, :w, :bf, :mm, :wt, 'apple_health_shortcut')
                           ON CONFLICT (user_id, measured_at) DO UPDATE
                             SET weight = EXCLUDED.weight,
                                 body_fat = EXCLUDED.body_fat,
                                 muscle_mass = EXCLUDED.muscle_mass,
                                 water = EXCLUDED.water,
                                 source = EXCLUDED.source"""
                    ),
                    {
                        "uid": target_user_id,
                        "ts": weight_ts,
                        "w": payload.weight_kg,
                        "bf": payload.body_fat_pct,
                        "mm": payload.muscle_mass_kg,
                        "wt": payload.water_pct,
                    },
                )
                saved.append(f"weights: {payload.weight_kg}kg, fat={payload.body_fat_pct}%")

            db.commit()

        finally:
            db.close()

    except Exception as e:
        logger.error(f"Apple Health webhook error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"DB error: {str(e)}")

    logger.info(f"✅ Apple Health import [{payload.date}]: {'; '.join(saved)}")

    return {
        "status": "ok",
        "date": payload.date,
        "saved": saved,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# ── /apple_health_v2 — приёмник нативного формата Health Auto Export ─────────
#
# HAE присылает вложенную структуру:
#   {"data": {"metrics": [
#       {"name": "step_count", "units": "count",
#        "data": [{"date": "2026-05-01 00:00:00 +0300", "qty": 12345}]},
#       {"name": "heart_rate", "units": "count/min",
#        "data": [{"date": "...", "Avg": 72, "Min": 55, "Max": 130}]},
#       ...
#   ]}}
# Адаптер группирует записи по датам и переиспользует ту же логику записи в БД.


def _hae_pick(rec: dict, *keys, default=None):
    """Достать первое не-None поле из записи (qty / Avg / Min / Max)."""
    for k in keys:
        if k in rec and rec[k] is not None:
            return rec[k]
    return default


_KCAL_PER_MJ = 239.006
_KJ_PER_KCAL = 4.184
# Ниже этой суточной суммы в «кДж» подозреваем мислейбл МДж (см. _energy_to_kcal).
_MJ_MISLABEL_MAX_KJ = 100.0


def _energy_to_kcal(qty: float, units: str, points: int = 1, label: str = "energy") -> float:
    """Сумма энергии за день (в исходных units) → ккал.

    Известный баг HAE: приложение присылает МЕГАджоули, подписывая их как "kJ".
    Признак — неправдоподобно малое суточное значение: 5,9 кДж это 1,4 ккал (не
    бывает даже за минуту сна), а 5,9 МДж = 1400 ккал — нормальный базовый обмен.

    ⚠️ Конвертируем СУММУ за день, а НЕ каждую запись. Энергия приходит минутными
    интервалами, и на отдельном куске порог «<100» срабатывает всегда — именно так
    базовый обмен превращался в миллионы ккал (bmr_calories 16–21.08.2026: 1,9 млн
    вместо 2100).

    ⚠️ И порог применяем только к ОДИНОЧНОЙ записи: мислейбл МДж приходит суточным
    агрегатом, то есть одной точкой. Много точек с малой суммой — это честный
    частичный день (неполная синхронизация), множить его на 239 нельзя: 21 кДж
    (~5 ккал) превращались в 5019 ккал. Переоценка травит TDEE и цель по калориям,
    тогда как недооценка видна как дырка в данных — поэтому в спорном случае
    трактуем буквально и пишем в лог.
    """
    if units == "mj":
        return round(qty * _KCAL_PER_MJ, 1)
    if units == "kj":
        if qty < _MJ_MISLABEL_MAX_KJ:
            if points <= 1:
                return round(qty * _KCAL_PER_MJ, 1)  # мислейбл HAE: это МДж
            logger.warning(
                "HAE %s: %.3f кДж за %d записей — частичный день, эвристику МДж не применяю (трактую буквально)",
                label,
                qty,
                points,
            )
        return round(qty / _KJ_PER_KCAL, 1)
    return round(qty, 1)  # kcal как есть


def _add_energy(slot: dict, key: str, units: str, qty) -> None:
    """Накопить энергию в ИСХОДНЫХ единицах, РАЗДЕЛЬНО по units.

    Раздельно — потому что в одном дне могут встретиться записи в разных единицах
    (пользователь сменил настройку HAE, или две автоматизации шлют по-разному).
    Складывать kcal с kJ в одно число до конверсии нельзя: 1000 kcal + 4184 kJ
    (это ещё 1000 ккал) давали 1239 вместо 2000 — молча, без ошибки.

    Число точек храним, чтобы отличить суточный агрегат от нарезки интервалами.
    """
    bucket = slot.setdefault(key, {}).setdefault(units, {"sum": 0.0, "points": 0})
    bucket["sum"] += float(qty)
    bucket["points"] += 1


def _hae_to_daily_payloads(metrics: list[dict]) -> dict[str, AppleHealthPayload]:
    """Сгруппировать HAE metrics → {YYYY-MM-DD: AppleHealthPayload}.

    Кумулятивные метрики (шаги, дистанция, этажи, энергия) СУММИРУЮТСЯ по дню:
    HAE присылает день одной записью только при «Суммировать: ON» + группировке
    «День». Если настройка сбита (или экспорт идёт интервалами), за день приходит
    несколько записей — раньше каждая следующая перезатирала предыдущую, и в БД
    оседал последний огрызок интервала: реальные ~6000 шагов превращались в 3–6
    (зафиксировано на данных 11–12.08.2026).

    Метрики-состояния (пульс, ВСР, вес) не кумулятивны — для них перезапись
    последним значением остаётся верной, суммировать их нельзя.
    """
    by_date: dict[str, dict] = {}

    for m in metrics:
        name = (m.get("name") or "").lower()
        units = (m.get("units") or "").lower()
        for rec in m.get("data") or []:
            d = (rec.get("date") or "")[:10]
            if not d or len(d) != 10 or d[4] != "-":
                continue
            slot = by_date.setdefault(d, {"date": d})

            if name == "step_count":
                slot["steps"] = int(slot.get("steps") or 0) + int(_hae_pick(rec, "qty", "Avg", default=0))
            elif name in ("walking_running_distance", "walking_distance", "distance_walking_running"):
                qty = float(_hae_pick(rec, "qty", "Avg", default=0))
                if units == "m":
                    qty /= 1000
                elif units == "mi":
                    qty *= 1.60934
                slot["distance_walking_km"] = round(float(slot.get("distance_walking_km") or 0) + qty, 3)
            elif name == "flights_climbed":
                slot["flights_climbed"] = int(slot.get("flights_climbed") or 0) + int(
                    _hae_pick(rec, "qty", "Avg", default=0)
                )
            elif name in ("active_energy", "active_energy_burned"):
                # Копим в ИСХОДНЫХ единицах; конверсия — один раз к суточной сумме
                # (см. _energy_to_kcal: иначе минутные интервалы дают миллионы ккал).
                _add_energy(slot, "_active_raw", units, _hae_pick(rec, "qty", "Avg", default=0))
            elif name in ("basal_energy_burned", "resting_energy"):
                _add_energy(slot, "_basal_raw", units, _hae_pick(rec, "qty", "Avg", default=0))
            elif name == "heart_rate":
                if "Avg" in rec and rec["Avg"] is not None:
                    slot["heart_rate_avg"] = int(round(float(rec["Avg"])))
                if "Min" in rec and rec["Min"] is not None:
                    hr_min, needs_verify = classify_hr_min(float(rec["Min"]))
                    if hr_min is not None:
                        slot["heart_rate_min"] = hr_min
                        if needs_verify:
                            slot["heart_rate_min_verify"] = True
                if "Max" in rec and rec["Max"] is not None:
                    slot["heart_rate_max"] = int(round(float(rec["Max"])))
                if "qty" in rec and rec["qty"] is not None and "heart_rate_avg" not in slot:
                    slot["heart_rate_avg"] = int(round(float(rec["qty"])))
            elif name == "resting_heart_rate":
                slot["resting_heart_rate"] = int(round(float(_hae_pick(rec, "qty", "Avg", default=0))))
            elif name == "blood_pressure_systolic":
                slot["blood_pressure_systolic"] = int(round(float(_hae_pick(rec, "qty", "Avg", default=0))))
            elif name == "blood_pressure_diastolic":
                slot["blood_pressure_diastolic"] = int(round(float(_hae_pick(rec, "qty", "Avg", default=0))))
            elif name == "blood_pressure":
                if rec.get("systolic") is not None:
                    slot["blood_pressure_systolic"] = int(round(float(rec["systolic"])))
                if rec.get("diastolic") is not None:
                    slot["blood_pressure_diastolic"] = int(round(float(rec["diastolic"])))
            elif name == "walking_speed":
                qty = float(_hae_pick(rec, "qty", "Avg", default=0))
                if units == "m/s":
                    qty *= 3.6
                slot["walking_speed_km_h"] = round(qty, 2)
            elif name == "walking_step_length":
                qty = float(_hae_pick(rec, "qty", "Avg", default=0))
                if units == "m":
                    qty *= 100
                slot["walking_step_length_cm"] = round(qty, 1)
            elif name in ("walking_double_support_percentage", "walking_double_support"):
                # HAE для *_percentage метрик всегда шлёт в %, не во фракции.
                # Не множим — реальные значения асимметрии 0.5-3% и попадали в фолс-ветвь ×100.
                slot["walking_double_support_pct"] = round(float(_hae_pick(rec, "qty", "Avg", default=0)), 2)
            elif name in ("walking_asymmetry_percentage", "walking_asymmetry"):
                slot["walking_asymmetry_pct"] = round(float(_hae_pick(rec, "qty", "Avg", default=0)), 2)
            elif name in ("weight_body_mass", "body_mass", "weight"):
                slot["weight_kg"] = round(float(_hae_pick(rec, "qty", "Avg", default=0)), 2)
            elif name == "body_fat_percentage":
                # HAE шлёт уже в процентах (например 27.4), не во фракции.
                slot["body_fat_pct"] = round(float(_hae_pick(rec, "qty", "Avg", default=0)), 1)
            elif name == "lean_body_mass":
                slot["muscle_mass_kg"] = round(float(_hae_pick(rec, "qty", "Avg", default=0)), 2)
            elif name == "vo2_max":
                slot["vo2_max"] = round(float(_hae_pick(rec, "qty", "Avg", default=0)), 1)
            elif name == "respiratory_rate":
                slot["respiratory_rate"] = round(float(_hae_pick(rec, "qty", "Avg", default=0)), 1)
            elif name in ("apple_sleeping_wrist_temperature", "wrist_temperature"):
                slot["wrist_temperature"] = round(float(_hae_pick(rec, "qty", "Avg", default=0)), 2)
            elif name in ("heart_rate_variability_sdnn", "heart_rate_variability"):
                # HRV SDNN в мс (Apple Watch ночное среднее)
                v = _hae_pick(rec, "qty", "Avg", default=None)
                if v is not None:
                    slot["hrv"] = int(round(float(v)))
            elif name == "sleep_analysis":
                # HAE sleep_analysis format (Apple Watch, summarize=ON):
                # {date: "YYYY-MM-DD 00:00:00 +TZ" (midnight of WAKING day),
                #  totalSleep: hours, core: h, deep: h, rem: h, awake: h,
                #  sleepStart: timestamp, sleepEnd: timestamp,
                #  inBedStart: timestamp, inBedEnd: timestamp,
                #  asleep: 0, inBed: 0,  ← always 0, not useful
                #  source: "Apple Watch — Name"}
                # Нет поля qty/Avg/Asleep! Нужно totalSleep.
                # Резервные форматы:
                # - {qty=hours, value="Asleep"|"InBed"} — value-стиль
                # - {Asleep: hours, InBed: hours, qty: hours} — старый стиль
                v = None

                if "totalSleep" in rec and rec["totalSleep"] is not None:
                    v = float(rec["totalSleep"])
                    # Сохраняем стадии сна в AppleHealthPayload поля
                    if rec.get("deep") is not None:
                        slot["sleep_deep_h"] = round(float(rec["deep"]), 2)
                    if rec.get("rem") is not None:
                        slot["sleep_rem_h"] = round(float(rec["rem"]), 2)
                    if rec.get("core") is not None:
                        slot["sleep_core_h"] = round(float(rec["core"]), 2)
                    if rec.get("awake") is not None:
                        slot["sleep_awake_h"] = round(float(rec["awake"]), 2)
                elif "Asleep" in rec and rec["Asleep"] is not None:
                    # Старый HAE формат с именованными полями
                    v = float(rec["Asleep"])
                else:
                    # value-стиль: {qty, value="Asleep"|"InBed"}
                    sleep_value = (rec.get("value") or rec.get("Value") or "").lower()
                    raw_v = _hae_pick(rec, "qty", "Avg", default=None)
                    if raw_v is not None and sleep_value not in ("inbed", "in_bed"):
                        v = float(raw_v)
                    # startDate/endDate (summarize=OFF fallback)
                    if v is None and rec.get("startDate") and rec.get("endDate"):
                        try:
                            _fmt = "%Y-%m-%d %H:%M:%S %z"
                            from datetime import datetime as _dt2

                            _s = _dt2.strptime(str(rec["startDate"]), _fmt)
                            _e = _dt2.strptime(str(rec["endDate"]), _fmt)
                            v = (_e - _s).total_seconds() / 3600.0
                        except Exception:
                            pass

                if v is not None:
                    hours = round(v, 2)
                    if hours > 0.5:
                        existing_sleep = slot.get("sleep_hours", 0) or 0
                        slot["sleep_hours"] = round(max(existing_sleep, hours), 2)
            elif name == "blood_oxygen_saturation":
                v = _hae_pick(rec, "qty", "Avg", default=None)
                if v is not None:
                    slot["spo2_pct"] = round(float(v), 1)

    # Преобразуем dict-ы в pydantic AppleHealthPayload (для валидации)
    # Энергию конвертируем ПОСЛЕ суммирования: служебные ключи в payload не нужны.
    # Каждая группа units конвертируется своим правилом, и только потом складываем.
    for fields in by_date.values():
        for raw_key, out_key in (("_active_raw", "active_energy_kcal"), ("_basal_raw", "basal_energy_kcal")):
            buckets = fields.pop(raw_key, None)
            if not buckets:
                continue
            fields[out_key] = round(
                sum(_energy_to_kcal(b["sum"], u, b["points"], out_key) for u, b in buckets.items()), 1
            )

    return {d: AppleHealthPayload(**fields) for d, fields in by_date.items()}


# ── Workouts (HAE data.workouts[]) ────────────────────────────────────────────
# HAE шлёт тренировки отдельным POST. Формат между вики HAE и примером issue
# расходится (name vs workoutActivityType, секунды vs {qty,units}) — поддерживаем
# обе схемы (#100).

_MILES_TO_KM = 1.60934
_WORKOUT_DATE_FMT = "%Y-%m-%d %H:%M:%S %z"


def _hae_quantity(value) -> float | None:
    """Число из поля HAE: либо {'qty': N, 'units': ...}, либо само число."""
    if isinstance(value, dict):
        value = value.get("qty")
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _hae_workout_type(rec: dict) -> str:
    """Тип активности: HKWorkoutActivityType* (снять префикс) либо поле name."""
    raw = _hae_pick(rec, "workoutActivityType", "name")
    if not raw:
        return "Workout"
    raw = str(raw)
    prefix = "HKWorkoutActivityType"
    return raw[len(prefix) :] if raw.startswith(prefix) else raw


def _hae_workout_duration_min(rec: dict) -> int | None:
    """Длительность → минуты. Принимает секунды (число) или {'qty','units'}."""
    dur = rec.get("duration")
    qty = _hae_quantity(dur)
    if qty is None:
        return None
    if isinstance(dur, dict):
        units = str(dur.get("units") or "").lower()
        if units.startswith("min"):
            return round(qty)
        if units.startswith("h"):
            return round(qty * 60)
        return round(qty / 60)  # секунды по умолчанию
    return round(qty / 60)  # голое число = секунды (формат вики HAE)


def _hae_workout_distance_km(rec: dict) -> float | None:
    dist = rec.get("distance")
    qty = _hae_quantity(dist)
    if qty is None:
        return None
    units = str(dist.get("units") if isinstance(dist, dict) else "").lower()
    if units in ("mi", "mile", "miles"):
        return round(qty * _MILES_TO_KM, 3)
    return round(qty, 3)


def _hae_workout_calories(rec: dict) -> int | None:
    val = _hae_quantity(_hae_pick(rec, "activeEnergyBurned", "activeEnergy", "totalEnergy"))
    return round(val) if val is not None else None


def _hae_workouts_to_rows(workouts: list[dict], user_id: int) -> list[dict]:
    """HAE `data.workouts[]` → строки для таблицы `workouts`.

    Невалидные записи (без распознаваемой даты старта) пропускаются. Поддержаны
    обе схемы HAE: v2 (есть `id` и `duration`) и v1/legacy (нет `id` — duration
    считается из `start`/`end`, ключ дедупа `source` строится из `name+start+end`).
    """
    rows: list[dict] = []
    for rec in workouts:
        if not isinstance(rec, dict):
            continue
        start_raw = _hae_pick(rec, "start", "startDate")
        try:
            start_dt = datetime.strptime(str(start_raw), _WORKOUT_DATE_FMT)
        except (TypeError, ValueError):
            continue
        end_raw = _hae_pick(rec, "end", "endDate")
        try:
            end_dt = datetime.strptime(str(end_raw), _WORKOUT_DATE_FMT)
        except (TypeError, ValueError):
            end_dt = None

        workout_type = _hae_workout_type(rec)
        # v1/legacy не присылает duration — считаем из интервала.
        duration_min = _hae_workout_duration_min(rec)
        if duration_min is None and end_dt is not None:
            duration_min = round((end_dt - start_dt).total_seconds() / 60)

        # Ключ дедупа: v2 — стабильный id; v1 — name+start+end (id отсутствует).
        wid = rec.get("id")
        if wid:
            source = f"hae_{wid}"
        elif end_dt is not None:
            source = f"hae_{workout_type}_{start_dt.isoformat()}_{end_dt.isoformat()}"
        else:
            source = f"hae_{workout_type}_{start_dt.isoformat()}"

        rows.append(
            {
                "user_id": user_id,
                "date": start_dt.date().isoformat(),
                "workout_type": workout_type,
                "duration_minutes": duration_min,
                "start_time": start_dt,
                "end_time": end_dt,
                "calories_burned": _hae_workout_calories(rec),
                "distance_km": _hae_workout_distance_km(rec),
                "source": source,
            }
        )
    return rows


def _insert_new_workouts(db, user_id: int, rows: list[dict]) -> int:
    """Вставляет тренировки в `workouts`, пропуская уже известные по `source`.

    Дедуп на уровне приложения (HAE может слать несколько тренировок за день,
    а Garmin-путь дедупит по дате) — без миграции схемы. `db` — SQLAlchemy
    session. Возвращает число реально вставленных строк.
    """
    if not rows:
        return 0
    from sqlalchemy import text as _text

    sources = [r["source"] for r in rows]
    existing = {
        row[0]
        for row in db.execute(
            _text("SELECT source FROM workouts WHERE user_id = :uid AND source = ANY(:srcs)"),
            {"uid": user_id, "srcs": sources},
        )
    }
    inserted = 0
    for r in rows:
        if r["source"] in existing:
            continue
        db.execute(
            _text(
                """INSERT INTO workouts
                   (user_id, date, workout_type, duration_minutes, start_time,
                    end_time, calories_burned, distance_km, source)
                   VALUES (:user_id, :date, :workout_type, :duration_minutes,
                           :start_time, :end_time, :calories_burned, :distance_km, :source)
                   ON CONFLICT DO NOTHING"""
            ),
            r,
        )
        existing.add(r["source"])
        inserted += 1
    return inserted


@app.post("/apple_health_v2")
async def receive_apple_health_v2(
    request: Request,
    bearer_token: str = Depends(verify_token),
):
    """Приёмник нативного формата Health Auto Export (data.metrics[]).

    Парсит HAE JSON, группирует по дням, и для каждого дня вызывает ту же
    логику записи в БД, что и /apple_health (v1).
    """
    try:
        raw = await request.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {e}")

    # Debug: logнём имена всех метрик чтобы понять какие HAE на самом деле шлёт.
    try:
        names_seen = sorted({(m.get("name") or "?") for m in (raw.get("data") or {}).get("metrics") or []})
        logger.warning(f"HAE_v2 metrics received ({len(names_seen)}): {', '.join(names_seen)}")
    except Exception:
        pass

    data_block = raw.get("data") if isinstance(raw, dict) else None
    if not isinstance(data_block, dict):
        raise HTTPException(status_code=400, detail="Expected {'data': {'metrics': [...]}}")
    metrics = data_block.get("metrics") or []
    if not isinstance(metrics, list):
        raise HTTPException(status_code=400, detail="'data.metrics' must be a list")
    # HAE шлёт тренировки отдельным POST с data.workouts[] (без data.metrics) — #100.
    workouts_raw = data_block.get("workouts") or []
    if not isinstance(workouts_raw, list):
        raise HTTPException(status_code=400, detail="'data.workouts' must be a list")

    daily = _hae_to_daily_payloads(metrics)
    if not daily and not workouts_raw:
        return {"status": "ok", "days": 0, "details": [], "workouts_inserted": 0}

    # Resolve target user (та же логика, что в v1)
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

    from database import SessionLocal
    from database.crud import create_or_update_activity, get_user_by_health_token, get_activity_by_date
    from sqlalchemy import text as _text

    if APPLE_HEALTH_TOKEN and bearer_token == APPLE_HEALTH_TOKEN:
        target_user_id = _target_user_id
    else:
        _db_auth = SessionLocal()
        try:
            _user = get_user_by_health_token(_db_auth, bearer_token)
        finally:
            _db_auth.close()
        if not _user:
            raise HTTPException(status_code=403, detail="Unknown token")
        target_user_id = _user.telegram_id

    details = []
    workouts_inserted = 0
    db = SessionLocal()
    try:
        for d_str, payload in sorted(daily.items()):
            record_date = date.fromisoformat(d_str)
            saved = []

            heart_rate = payload.resting_heart_rate or payload.heart_rate_avg
            raw_extra = {}
            for k in (
                "walking_speed_km_h",
                "walking_step_length_cm",
                "walking_double_support_pct",
                "walking_asymmetry_pct",
                "flights_climbed",
                "heart_rate_min",
                "heart_rate_min_verify",
                "heart_rate_max",
                "vo2_max",
                "respiratory_rate",
                "wrist_temperature",
                "spo2_pct",
                "sleep_deep_h",
                "sleep_rem_h",
                "sleep_core_h",
                "sleep_awake_h",
            ):
                v = getattr(payload, k, None)
                if v is not None:
                    raw_extra[k] = v

            # See note above: Apple's active_energy_kcal is NOT written to active_calories.
            # Stored in raw_data only — Garmin remains source of truth for calories triple.
            if payload.active_energy_kcal is not None:
                raw_extra = raw_extra or {}
                raw_extra["apple_active_energy_kcal"] = payload.active_energy_kcal
            if payload.basal_energy_kcal is not None:
                raw_extra = raw_extra or {}
                raw_extra["apple_basal_energy_kcal"] = payload.basal_energy_kcal
            # Apple's basal → bmr_calories: max-за-день, но Garmin приоритетнее (#329).
            existing_row_v2 = get_activity_by_date(db, target_user_id, record_date)
            apple_bmr_v2 = _resolve_apple_bmr(existing_row_v2, payload.basal_energy_kcal)
            create_or_update_activity(
                db=db,
                user_id=target_user_id,
                date=record_date,
                steps=payload.steps,
                bmr_calories=apple_bmr_v2,
                distance_km=payload.distance_walking_km,
                heart_rate_avg=heart_rate,
                hrv=payload.hrv,
                sleep_hours=payload.sleep_hours,
                source="apple_health_v2",
                raw_data=raw_extra if raw_extra else None,
            )
            saved.append(
                f"activity (steps={payload.steps}, HR={heart_rate}, HRV={payload.hrv}, sleep={f'{payload.sleep_hours}h' if payload.sleep_hours is not None else 'None'})"
            )

            if payload.blood_pressure_systolic and payload.blood_pressure_diastolic:
                measured_at = datetime.combine(record_date, datetime.min.time().replace(hour=8))
                db.execute(
                    _text(
                        """INSERT INTO blood_pressure_logs
                           (user_id, measured_at, systolic, diastolic, heart_rate, source)
                           VALUES (:uid, :ts, :sys, :dia, :hr, 'apple_health_v2')
                           ON CONFLICT (user_id, measured_at) DO UPDATE
                             SET systolic = EXCLUDED.systolic,
                                 diastolic = EXCLUDED.diastolic"""
                    ),
                    {
                        "uid": target_user_id,
                        "ts": measured_at,
                        "sys": payload.blood_pressure_systolic,
                        "dia": payload.blood_pressure_diastolic,
                        "hr": heart_rate,
                    },
                )
                saved.append(f"BP {payload.blood_pressure_systolic}/{payload.blood_pressure_diastolic}")

            if payload.weight_kg and payload.weight_kg > 30:
                weight_ts = datetime.combine(record_date, datetime.min.time().replace(hour=8))
                # Прямой UPSERT — create_weight не имеет ON CONFLICT, и при повторных
                # вызовах (manual export второй раз) валится на UniqueViolation.
                db.execute(
                    _text(
                        """INSERT INTO weights
                           (user_id, measured_at, weight, body_fat, muscle_mass, water, source)
                           VALUES (:uid, :ts, :w, :bf, :mm, :wt, 'apple_health_v2')
                           ON CONFLICT (user_id, measured_at) DO UPDATE
                             SET weight = EXCLUDED.weight,
                                 body_fat = EXCLUDED.body_fat,
                                 muscle_mass = EXCLUDED.muscle_mass,
                                 water = EXCLUDED.water,
                                 source = EXCLUDED.source"""
                    ),
                    {
                        "uid": target_user_id,
                        "ts": weight_ts,
                        "w": payload.weight_kg,
                        "bf": payload.body_fat_pct,
                        "mm": payload.muscle_mass_kg,
                        "wt": payload.water_pct,
                    },
                )
                saved.append(f"weight {payload.weight_kg}kg")

            details.append({"date": d_str, "saved": saved})

        # Тренировки (#100): отдельный data.workouts[], дедуп по source.
        workout_rows = _hae_workouts_to_rows(workouts_raw, target_user_id)
        workouts_inserted = _insert_new_workouts(db, target_user_id, workout_rows)
        if workouts_inserted:
            logger.info("HAE_v2 workouts inserted: %s", workouts_inserted)

        db.commit()
    except Exception as e:
        db.rollback()
        logger.error(f"Apple Health v2 error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"DB error: {e}")
    finally:
        db.close()

    logger.info(f"✅ Apple Health v2 import: {len(daily)} day(s)")
    return {
        "status": "ok",
        "days": len(daily),
        "details": details,
        "workouts_inserted": workouts_inserted,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# ── Standalone run (для локального тестирования) ──────────────────────────────

# ── Settings API ─────────────────────────────────────────────────────────────


class SupplementItem(BaseModel):
    name: str
    slot: str  # morning_before | morning_with | evening
    dose: Optional[str] = None  # короткая строка для UI/log: "2 ч.л.", "5000 IU"


class UserSettingsSchema(BaseModel):
    show_calorie_budget_bar: bool = True
    bmr_override: Optional[int] = None
    target_weight_kg: Optional[float] = None
    target_weight_date: Optional[str] = None  # YYYY-MM-DD string
    calorie_goal_pct: int = -15  # signed %: -15 = deficit, 0 = maintain, +10 = surplus
    supplement_reminders_enabled: bool = False
    supplement_reminder_time: str = "08:00"  # HH:MM string
    supplements: List[SupplementItem] = []
    # Поле живёт в users.agent_review_consent, не в user_settings — это
    # privacy-флаг, а не UI-настройка. Принимаем тут чтобы мини-апп мог
    # сохранять весь профиль одним POST'ом.
    agent_review_consent: Optional[bool] = None


@app.get("/api/settings")
async def get_settings(tg_user: dict = Depends(get_tg_user)):
    """Return current settings for authenticated Telegram user."""
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

    from database import SessionLocal
    from database.crud import get_user_settings
    from database.models import User

    user_id = tg_user.get("id")
    if not user_id:
        raise HTTPException(status_code=400, detail="No user id in initData")

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.telegram_id == user_id).first()
        # Default TRUE если юзера ещё нет (мини-апп открыт до /start) — поведение
        # согласовано с server_default колонки.
        review_consent = bool(user.agent_review_consent) if user else True

        s = get_user_settings(db, user_id)
        if s is None:
            return {
                "show_calorie_budget_bar": True,
                "bmr_override": None,
                "target_weight_kg": None,
                "target_weight_date": None,
                "supplement_reminders_enabled": False,
                "supplement_reminder_time": "08:00",
                "supplements": [],  # new users start with empty list, not owner's supplements
                "agent_review_consent": review_consent,
            }
        return {
            "show_calorie_budget_bar": s.show_calorie_budget_bar,
            "bmr_override": s.bmr_override,
            "target_weight_kg": s.target_weight_kg,
            "target_weight_date": s.target_weight_date.isoformat() if s.target_weight_date else None,
            "calorie_goal_pct": s.calorie_goal_pct if s.calorie_goal_pct is not None else -15,
            "supplement_reminders_enabled": s.supplement_reminders_enabled,
            "supplement_reminder_time": s.supplement_reminder_time.strftime("%H:%M")
            if s.supplement_reminder_time
            else "08:00",
            "supplements": s.supplements or [],
            "agent_review_consent": review_consent,
        }
    finally:
        db.close()


@app.post("/api/settings")
async def save_settings(payload: UserSettingsSchema, tg_user: dict = Depends(get_tg_user)):
    """Save settings for authenticated Telegram user."""
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

    from database import SessionLocal
    from database.crud import upsert_user_settings
    from database.models import User
    from datetime import date as date_cls, time as time_cls

    user_id = tg_user.get("id")
    if not user_id:
        raise HTTPException(status_code=400, detail="No user id in initData")

    twd = None
    if payload.target_weight_date:
        try:
            twd = date_cls.fromisoformat(payload.target_weight_date)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid target_weight_date format, use YYYY-MM-DD")

    try:
        h, m = payload.supplement_reminder_time.split(":")
        reminder_time = time_cls(int(h), int(m))
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid supplement_reminder_time, use HH:MM")

    # exclude_none keeps the JSON tidy: items без дозы не несут "dose": null.
    supplements_list = [s.model_dump(exclude_none=True) for s in payload.supplements]

    db = SessionLocal()
    try:
        upsert_user_settings(
            db,
            user_id=user_id,
            show_calorie_budget_bar=payload.show_calorie_budget_bar,
            bmr_override=payload.bmr_override,
            target_weight_kg=payload.target_weight_kg,
            target_weight_date=twd,
            calorie_goal_pct=payload.calorie_goal_pct,
            supplement_reminders_enabled=payload.supplement_reminders_enabled,
            supplement_reminder_time=reminder_time,
            supplements=supplements_list,
        )

        # agent_review_consent живёт на users, не user_settings — обновляем
        # отдельным UPDATE'ом, только если поле явно пришло в payload.
        if payload.agent_review_consent is not None:
            user = db.query(User).filter(User.telegram_id == user_id).first()
            if user is not None:
                user.agent_review_consent = payload.agent_review_consent
                db.commit()
    finally:
        db.close()

    return {"status": "ok"}


# ── Nutrition day editor API ─────────────────────────────────────────────────

from webhook.nutrition_api import router as nutrition_router
from webhook.supplements_api import router as supplements_router

app.include_router(nutrition_router)
app.include_router(supplements_router)

from webhook.dashboard import router as dashboard_router

app.include_router(dashboard_router)

from webhook.report import router as report_router

app.include_router(report_router)

from webhook.profile_api import router as profile_router

app.include_router(profile_router)

from webhook.feedback_api import router as feedback_router

app.include_router(feedback_router)

from webhook.doctor_report_api import router as doctor_report_router

app.include_router(doctor_report_router)

from webhook.agent_tools_api import router as agent_tools_router

app.include_router(agent_tools_router)

from webhook.telegram_router import router as telegram_router

app.include_router(telegram_router)

from webhook.admin import router as admin_router

app.include_router(admin_router)

from webhook.whoop_oauth import router as whoop_router

app.include_router(whoop_router)


# ── Static webapp ─────────────────────────────────────────────────────────────

import hashlib
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path as _Path

_webapp_dir = _Path(__file__).parent.parent / "webapp"


def _webapp_version() -> str:
    """Short hash of mtimes for the webapp assets — forces cache bust on any change."""
    parts = []
    for fname in ("day.js", "api.js", "day.css", "settings.css", "settings.js", "dashboard.js"):
        p = _webapp_dir / fname
        if p.exists():
            parts.append(str(p.stat().st_mtime_ns))
    return hashlib.sha256("-".join(parts).encode()).hexdigest()[:8] if parts else "0"


async def _serve_index() -> HTMLResponse:
    """Serve index.html with {{V}} placeholder replaced by version hash."""
    from core._version import __version__

    index_path = _webapp_dir / "index.html"
    html = index_path.read_text(encoding="utf-8")
    html = html.replace("{{V}}", _webapp_version())
    html = html.replace("{{APP_VERSION}}", __version__)  # семантическая версия в футере (#86)
    # Prevent CDN/browser from caching the HTML itself — JS/CSS get cached via ?v=hash
    headers = {"Cache-Control": "no-cache, no-store, must-revalidate"}
    return HTMLResponse(content=html, headers=headers)


if _webapp_dir.exists():
    # Explicit routes for index — must be registered BEFORE the mount.
    app.get("/webapp/")(_serve_index)
    app.get("/webapp/index.html")(_serve_index)
    # Everything else (day.js, api.js, day.css, etc.) served as static.
    app.mount("/webapp", StaticFiles(directory=str(_webapp_dir), html=True), name="webapp")


# ── Telegram Bot Webhook ──────────────────────────────────────────────────────


@app.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    """Receives Telegram updates and feeds them to the aiogram dispatcher.

    NB: основной обработчик /telegram/webhook — в webhook/telegram_router.py.
    Этот маршрут затенён роутером; защищаем тем же секретом на случай смены
    порядка регистрации.
    """
    from aiogram.types import Update as TgUpdate  # lazy import — avoids breaking tests

    _expected_secret = os.getenv("TELEGRAM_WEBHOOK_SECRET", "")
    if _expected_secret:
        _got = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not hmac.compare_digest(_got, _expected_secret):
            raise HTTPException(status_code=403, detail="Invalid webhook secret")

    if _tg_bot is None or _tg_dp is None:
        logger.error("Telegram dispatcher not initialised — update dropped")
        return {"status": "not_ready"}
    data = await request.json()
    update = TgUpdate(**data)
    await _tg_dp.feed_update(_tg_bot, update)
    return {"status": "ok"}


def start_webhook_server(host: str = "0.0.0.0", port: int = 8081):
    """Запускается из bot.py через asyncio.gather."""
    config = uvicorn.Config(app, host=host, port=port, log_level="warning", access_log=False)
    server = uvicorn.Server(config)
    return server.serve()


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8081)
