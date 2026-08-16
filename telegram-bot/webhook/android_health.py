"""
Android Health Connect Webhook — принимает данные от mcnaveen/health-connect-webhook (APK v1.9.10).
Регистрирует маршрут /android_health_v1 на том же FastAPI app, что и apple_health.py.

Ключевое отличие от Apple Health:
  HAE шлёт уже посчитанные дневные агрегаты, Health Connect шлёт сырые записи
  с временными метками (steps: [{count, start_time, end_time}, ...]).
  Агрегацию по дням мы делаем сами — в таймзоне пользователя (users.timezone),
  иначе записи после 21:00 МСК уедут на следующий день.

Auth: Bearer token (users.health_token, та же таблица что и Apple Health).
Source tag: health_connect.
"""

import logging
from datetime import date, datetime, timedelta, timezone
from typing import List, Optional

from fastapi import Depends, HTTPException, Request
from pydantic import BaseModel, Field

from webhook.apple_health import app, verify_token  # регистрируем маршрут на тот же app

logger = logging.getLogger(__name__)

# ── Pydantic-схема: сырые записи Health Connect ──────────────────────────────


class HCStepsRecord(BaseModel):
    count: int
    start_time: str
    end_time: str


class HCHeartRateRecord(BaseModel):
    bpm: float
    time: str


class HCWeightRecord(BaseModel):
    kilograms: float
    time: str


class HCBloodPressureRecord(BaseModel):
    systolic: float
    diastolic: float
    time: str


class HCSleepRecord(BaseModel):
    session_end_time: str
    duration_seconds: float
    stages: Optional[list] = None


class HCDistanceRecord(BaseModel):
    meters: float
    start_time: str
    end_time: str


class HCCaloriesRecord(BaseModel):
    calories: float
    start_time: str
    end_time: str


class HCHRVRecord(BaseModel):
    rmssd_millis: float
    time: str


class HCSpo2Record(BaseModel):
    percentage: float
    time: str


class HCVo2MaxRecord(BaseModel):
    ml_per_kg_per_min: float
    time: str


class HCBodyFatRecord(BaseModel):
    percentage: float
    time: str


class HealthConnectPayload(BaseModel):
    """
    Формат mcnaveen/health-connect-webhook v1.9.10.
    Все массивы опциональны — приложение шлёт только то, что есть за период.
    Defensive: .get() с дефолтами, не падать на отсутствующих полях.
    """

    timestamp: Optional[str] = None
    app_version: Optional[str] = None

    steps: Optional[List[HCStepsRecord]] = Field(default_factory=list)
    heart_rate: Optional[List[HCHeartRateRecord]] = Field(default_factory=list)
    resting_heart_rate: Optional[List[HCHeartRateRecord]] = Field(default_factory=list)
    weight: Optional[List[HCWeightRecord]] = Field(default_factory=list)
    blood_pressure: Optional[List[HCBloodPressureRecord]] = Field(default_factory=list)
    sleep: Optional[List[HCSleepRecord]] = Field(default_factory=list)
    distance: Optional[List[HCDistanceRecord]] = Field(default_factory=list)
    active_calories: Optional[List[HCCaloriesRecord]] = Field(default_factory=list)
    total_calories: Optional[List[HCCaloriesRecord]] = Field(default_factory=list)
    heart_rate_variability: Optional[List[HCHRVRecord]] = Field(default_factory=list)
    oxygen_saturation: Optional[List[HCSpo2Record]] = Field(default_factory=list)
    vo2_max: Optional[List[HCVo2MaxRecord]] = Field(default_factory=list)
    body_fat: Optional[List[HCBodyFatRecord]] = Field(default_factory=list)


# ── Агрегация по дням в таймзоне пользователя ────────────────────────────────


def _parse_utc(ts: str) -> Optional[datetime]:
    """Парсить ISO 8601 timestamp с Z или +00:00 суффиксом → datetime (UTC-aware)."""
    if not ts:
        return None
    # Нормализуем: заменяем суффикс Z на +00:00 для fromisoformat (Python <3.11)
    ts_norm = ts.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(ts_norm)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def _merge_intervals_seconds(intervals: list) -> float:
    """Слить пересекающиеся (start, end) интервалы и вернуть суммарные секунды покрытия."""
    if not intervals:
        return 0.0
    ordered = sorted(intervals, key=lambda pair: pair[0])
    merged = [ordered[0]]
    for start, end in ordered[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return sum((end - start).total_seconds() for start, end in merged)


def _to_local_date(ts: str, user_tz) -> Optional[date]:
    """Конвертировать UTC timestamp → локальную дату в таймзоне пользователя."""
    dt = _parse_utc(ts)
    if dt is None:
        return None
    return dt.astimezone(user_tz).date()


def _hc_aggregate_by_day(payload: HealthConnectPayload, user_tz) -> dict:
    """
    Сгруппировать сырые записи Health Connect по локальным датам юзера.

    Возвращает dict[date, dict] с агрегатами:
      steps, distance_km, heart_rate_avg, heart_rate_min, heart_rate_max,
      resting_heart_rate, hrv, sleep_hours, body_fat_pct,
      weight (последний за день, >30 кг),
      blood_pressure (список отдельных замеров — НЕ агрегируется),
      raw_data (hc_active_calories, hc_total_calories, hc_spo2_pct, hc_vo2_max).

    ⚠️ Timezone correctness: все timestamp'ы конвертируются в user_tz
       чтобы записи после 21:00 МСК не уезжали на следующий день.
    ⚠️ active_calories НЕ пишется в поле calories — только в raw_data
       (Garmin = source of truth для тройки bmr/active/total).
    ⚠️ weight: фильтр >30 кг (отсечь нулевые/мусорные записи).
    ⚠️ blood_pressure: каждый замер — отдельная строка (у папы до 10 в день).
    """
    # days: dict[date, dict с накопителями]
    days: dict = {}

    def _slot(d: date) -> dict:
        return days.setdefault(
            d,
            {
                "steps": 0,
                "distance_m": 0.0,
                "hr_sum": 0.0,
                "hr_count": 0,
                "hr_min": None,
                "hr_max": None,
                "rhr_values": [],  # берём последний
                "hrv_values": [],  # берём последний
                "sleep_intervals": [],  # (start, end) — мёрджим пересечения перед суммой
                "weight_records": [],  # берём последний >30 кг
                "body_fat_pct": None,  # берём последний
                "blood_pressure": [],  # все замеры отдельно
                "raw_data": {},
            },
        )

    # ── steps: суммируем ──────────────────────────────────────────────────────
    for rec in payload.steps or []:
        d = _to_local_date(rec.end_time, user_tz) or _to_local_date(rec.start_time, user_tz)
        if d:
            _slot(d)["steps"] += rec.count

    # ── distance: суммируем метры ─────────────────────────────────────────────
    for rec in payload.distance or []:
        d = _to_local_date(rec.end_time, user_tz) or _to_local_date(rec.start_time, user_tz)
        if d:
            _slot(d)["distance_m"] += rec.meters

    # ── heart_rate: avg/min/max ───────────────────────────────────────────────
    for rec in payload.heart_rate or []:
        d = _to_local_date(rec.time, user_tz)
        if d:
            s = _slot(d)
            s["hr_sum"] += rec.bpm
            s["hr_count"] += 1
            if s["hr_min"] is None or rec.bpm < s["hr_min"]:
                s["hr_min"] = rec.bpm
            if s["hr_max"] is None or rec.bpm > s["hr_max"]:
                s["hr_max"] = rec.bpm

    # ── resting_heart_rate: последний за день ─────────────────────────────────
    for rec in payload.resting_heart_rate or []:
        d = _to_local_date(rec.time, user_tz)
        if d:
            _slot(d)["rhr_values"].append((rec.time, rec.bpm))

    # ── HRV: последний за день ────────────────────────────────────────────────
    for rec in payload.heart_rate_variability or []:
        d = _to_local_date(rec.time, user_tz)
        if d:
            _slot(d)["hrv_values"].append((rec.time, rec.rmssd_millis))

    # ── sleep: копим интервалы (мёрджим пересечения перед суммой) ────────────
    # Health Connect отдаёт сессии сырыми (readRecords, без aggregate()) — если
    # источник пришлёт две пересекающиеся сессии за одну ночь (пере-синк истории),
    # наивная сумма duration_seconds задвоит часы сна.
    for rec in payload.sleep or []:
        d = _to_local_date(rec.session_end_time, user_tz)
        if d:
            end_dt = _parse_utc(rec.session_end_time)
            if end_dt:
                start_dt = end_dt - timedelta(seconds=rec.duration_seconds)
                _slot(d)["sleep_intervals"].append((start_dt, end_dt))

    # ── weight: копим все записи >30 кг, потом берём последнюю ───────────────
    for rec in payload.weight or []:
        d = _to_local_date(rec.time, user_tz)
        if d and rec.kilograms > 30:
            _slot(d)["weight_records"].append((rec.time, rec.kilograms))

    # ── body_fat: последний за день ───────────────────────────────────────────
    for rec in payload.body_fat or []:
        d = _to_local_date(rec.time, user_tz)
        if d:
            s = _slot(d)
            # сохраняем последний (список в хронологическом порядке ← ±OK)
            if s["body_fat_pct"] is None or rec.time >= (s.get("_bf_time") or ""):
                s["body_fat_pct"] = rec.percentage
                s["_bf_time"] = rec.time

    # ── blood_pressure: каждый замер отдельно ────────────────────────────────
    for rec in payload.blood_pressure or []:
        d = _to_local_date(rec.time, user_tz)
        if d:
            dt = _parse_utc(rec.time)
            _slot(d)["blood_pressure"].append(
                {
                    "systolic": int(round(rec.systolic)),
                    "diastolic": int(round(rec.diastolic)),
                    "measured_at": dt,
                }
            )

    # ── active_calories → raw_data ONLY (не в calories!) ─────────────────────
    for rec in payload.active_calories or []:
        d = _to_local_date(rec.end_time, user_tz) or _to_local_date(rec.start_time, user_tz)
        if d:
            s = _slot(d)
            prev = s["raw_data"].get("hc_active_calories", 0.0)
            s["raw_data"]["hc_active_calories"] = prev + rec.calories

    # ── total_calories → raw_data ONLY ───────────────────────────────────────
    for rec in payload.total_calories or []:
        d = _to_local_date(rec.end_time, user_tz) or _to_local_date(rec.start_time, user_tz)
        if d:
            s = _slot(d)
            prev = s["raw_data"].get("hc_total_calories", 0.0)
            s["raw_data"]["hc_total_calories"] = prev + rec.calories

    # ── SpO2 → raw_data ───────────────────────────────────────────────────────
    for rec in payload.oxygen_saturation or []:
        d = _to_local_date(rec.time, user_tz)
        if d:
            # среднее по дню
            s = _slot(d)
            prev_sum = s["raw_data"].get("_spo2_sum", 0.0)
            prev_n = s["raw_data"].get("_spo2_n", 0)
            s["raw_data"]["_spo2_sum"] = prev_sum + rec.percentage
            s["raw_data"]["_spo2_n"] = prev_n + 1

    # ── VO2Max → raw_data ─────────────────────────────────────────────────────
    for rec in payload.vo2_max or []:
        d = _to_local_date(rec.time, user_tz)
        if d:
            s = _slot(d)
            s["raw_data"]["hc_vo2_max"] = rec.ml_per_kg_per_min

    # ── Финализация накопителей → готовые поля ────────────────────────────────
    result = {}
    for d, s in days.items():
        agg: dict = {}

        # steps
        if s["steps"] > 0:
            agg["steps"] = s["steps"]
            if s["steps"] > 40000:
                # Health Connect отдаёт steps через свой aggregate() (дедуп по origin
                # встроен в ОС) — если итог всё равно аномален, обычно виноват сам
                # источник (напр. Mi Fitness пишет и континуальный трекинг, и отдельные
                # exercise-сессии). Не блокируем запись — только сигнализируем в логах.
                logger.warning(f"HC_v1 anomalous steps={s['steps']} for {d} — check Health Connect source priority")

        # distance_km
        if s["distance_m"] > 0:
            agg["distance_km"] = round(s["distance_m"] / 1000, 3)

        # heart_rate_avg (из обычных замеров)
        if s["hr_count"] > 0:
            agg["heart_rate_avg"] = int(round(s["hr_sum"] / s["hr_count"]))
        if s["hr_min"] is not None:
            agg["heart_rate_min"] = int(round(s["hr_min"]))
        if s["hr_max"] is not None:
            agg["heart_rate_max"] = int(round(s["hr_max"]))

        # resting_heart_rate: последний по времени (приоритетнее avg)
        if s["rhr_values"]:
            latest_rhr = sorted(s["rhr_values"], key=lambda x: x[0])[-1][1]
            agg["resting_heart_rate"] = int(round(latest_rhr))

        # HRV: последний за день
        if s["hrv_values"]:
            latest_hrv = sorted(s["hrv_values"], key=lambda x: x[0])[-1][1]
            agg["hrv"] = int(round(latest_hrv))

        # sleep_hours: суммарное покрытие после мёрджа пересекающихся сессий
        sleep_seconds = _merge_intervals_seconds(s["sleep_intervals"])
        if sleep_seconds > 0:
            agg["sleep_hours"] = round(sleep_seconds / 3600, 2)

        # weight: последний за день
        if s["weight_records"]:
            latest_w = sorted(s["weight_records"], key=lambda x: x[0])[-1][1]
            agg["weight_kg"] = latest_w
        if s["body_fat_pct"] is not None:
            agg["body_fat_pct"] = s["body_fat_pct"]

        # blood_pressure: все замеры
        if s["blood_pressure"]:
            agg["blood_pressure"] = s["blood_pressure"]

        # raw_data: финализируем SpO2 среднее, убираем служебные ключи
        raw = {k: v for k, v in s["raw_data"].items() if not k.startswith("_")}
        if "_spo2_sum" in s["raw_data"] and s["raw_data"]["_spo2_n"] > 0:
            raw["hc_spo2_pct"] = round(s["raw_data"]["_spo2_sum"] / s["raw_data"]["_spo2_n"], 1)
        if s["hr_min"] is not None:
            raw["hc_hr_min"] = int(round(s["hr_min"]))
        if s["hr_max"] is not None:
            raw["hc_hr_max"] = int(round(s["hr_max"]))
        if raw:
            agg["raw_data"] = raw

        result[d] = agg

    return result


# ── Endpoint POST /android_health_v1 ─────────────────────────────────────────


@app.post("/android_health_v1")
async def receive_android_health(
    request: Request,
    bearer_token: str = Depends(verify_token),
):
    """
    Принимает JSON от mcnaveen/health-connect-webhook (APK v1.9.10).

    Парсит массивы сырых записей, агрегирует по дням в таймзоне юзера,
    пишет в те же таблицы что и Apple Health.

    Роутинг пользователя — через users.health_token (та же логика, что в /apple_health_v2).
    """
    try:
        raw = await request.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {e}")

    # Debug: логируем версию APK чтобы отслеживать формат
    app_version = raw.get("app_version", "?") if isinstance(raw, dict) else "?"
    logger.info(f"HC_v1 received payload, app_version={app_version}")

    try:
        payload = HealthConnectPayload(**raw)
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Payload validation error: {e}")

    # Импортируем здесь чтобы не ломать импорт если БД недоступна при старте
    import sys
    from pathlib import Path
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

    from database import SessionLocal
    from database.crud import create_or_update_activity, get_activity_by_date, get_user_by_health_token
    from sqlalchemy import text as _text

    # ── Resolve user ──────────────────────────────────────────────────────────
    _db_auth = SessionLocal()
    try:
        _user = get_user_by_health_token(_db_auth, bearer_token)
    finally:
        _db_auth.close()
    if not _user:
        raise HTTPException(status_code=403, detail="Unknown token")
    target_user_id = _user.telegram_id

    # ── Определяем таймзону юзера ─────────────────────────────────────────────
    _tz_name = getattr(_user, "timezone", None) or "Europe/Moscow"
    try:
        user_tz = ZoneInfo(_tz_name)
    except (ZoneInfoNotFoundError, KeyError):
        logger.warning(f"Unknown timezone {_tz_name!r} for user {target_user_id}, falling back to Europe/Moscow")
        user_tz = ZoneInfo("Europe/Moscow")

    # ── Агрегируем по дням в таймзоне юзера ──────────────────────────────────
    daily = _hc_aggregate_by_day(payload, user_tz)
    if not daily:
        return {"status": "ok", "days": 0, "details": []}

    details = []
    db = SessionLocal()
    try:
        for d, agg in sorted(daily.items()):
            record_date = d
            saved = []

            # ── 1. activity_log ───────────────────────────────────────────────
            # resting_heart_rate приоритетнее avg (точнее отражает состояние покоя)
            heart_rate = agg.get("resting_heart_rate") or agg.get("heart_rate_avg")

            raw_extra = {}
            if agg.get("heart_rate_min") is not None:
                raw_extra["hc_hr_min"] = agg["heart_rate_min"]
            if agg.get("heart_rate_max") is not None:
                raw_extra["hc_hr_max"] = agg["heart_rate_max"]
            # Пробрасываем raw_data из агрегации (active_calories, total_calories, spo2, vo2max)
            raw_extra.update(agg.get("raw_data") or {})

            # BMR: Health Connect не даёт BMR (только total/active).
            # Пишем bmr_calories только если Garmin ещё не заполнил (Garmin > HC).
            existing_row = get_activity_by_date(db, target_user_id, record_date)
            # HC не имеет BMR поля — не пишем (в отличие от Apple Health)

            create_or_update_activity(
                db=db,
                user_id=target_user_id,
                date=record_date,
                steps=agg.get("steps"),
                distance_km=agg.get("distance_km"),
                heart_rate_avg=heart_rate,
                hrv=agg.get("hrv"),
                sleep_hours=agg.get("sleep_hours"),
                source="health_connect",
                raw_data=raw_extra if raw_extra else None,
            )
            saved.append(
                f"activity (steps={agg.get('steps')}, HR={heart_rate}, "
                f"HRV={agg.get('hrv')}, sleep={agg.get('sleep_hours')}h, "
                f"dist={agg.get('distance_km')}km)"
            )

            # ── 2. blood_pressure_logs — каждый замер отдельно ────────────────
            # У папы по 10 замеров в день — все нужны (в отличие от Apple Health,
            # где BP = последний за день). measured_at = реальный time из записи.
            bp_list = agg.get("blood_pressure") or []
            for bp in bp_list:
                db.execute(
                    _text(
                        """INSERT INTO blood_pressure_logs
                           (user_id, measured_at, systolic, diastolic, source)
                           VALUES (:uid, :ts, :sys, :dia, 'health_connect')
                           ON CONFLICT (user_id, measured_at) DO UPDATE
                             SET systolic = EXCLUDED.systolic,
                                 diastolic = EXCLUDED.diastolic"""
                    ),
                    {
                        "uid": target_user_id,
                        "ts": bp["measured_at"],
                        "sys": bp["systolic"],
                        "dia": bp["diastolic"],
                    },
                )
            if bp_list:
                saved.append(f"BP: {len(bp_list)} записей")

            # ── 3. weights — последний за день, фильтр >30 кг ─────────────────
            if agg.get("weight_kg"):
                # Берём полдень дня как timestamp (у HC нет точного времени в agg)
                # В реальном замере время из weight_records уже учтено агрегатором
                from datetime import time as _time

                weight_ts = datetime.combine(record_date, _time(12, 0), tzinfo=timezone.utc)
                db.execute(
                    _text(
                        """INSERT INTO weights
                           (user_id, measured_at, weight, body_fat, source)
                           VALUES (:uid, :ts, :w, :bf, 'health_connect')
                           ON CONFLICT (user_id, measured_at) DO UPDATE
                             SET weight = EXCLUDED.weight,
                                 body_fat = EXCLUDED.body_fat,
                                 source = EXCLUDED.source"""
                    ),
                    {
                        "uid": target_user_id,
                        "ts": weight_ts,
                        "w": agg["weight_kg"],
                        "bf": agg.get("body_fat_pct"),
                    },
                )
                saved.append(f"weight {agg['weight_kg']}kg")

            details.append({"date": d.isoformat(), "saved": saved})

        db.commit()

    except Exception as e:
        db.rollback()
        logger.error(f"Android Health webhook error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"DB error: {e}")
    finally:
        db.close()

    logger.info(f"✅ Android Health Connect import: {len(daily)} day(s)")
    return {
        "status": "ok",
        "days": len(daily),
        "details": details,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
