#!/usr/bin/env python3
"""
Импорт Apple Health XML → PostgreSQL для одного пользователя.

Разовый инструмент: парсит выгрузку `export.xml`, агрегирует по дням, генерирует
SQL-файл — потом заливается на сервер через SSH.

ФИО и telegram_id пользователя в коде НЕ хардкодятся (#303) — задаются аргументами:

    python3 scripts/import/import_apple_health_xml_user.py \\
        --user-id <telegram_id> \\
        --xml /tmp/<dir>/apple_health_export/export.xml \\
        --out /tmp/apple_health_import.sql

Таблицы:
  - activity_log        (steps, hr, hrv, sleep, distance + raw_data с кольцами/VO2/etc)
  - blood_pressure_logs (каждое измерение)
  - weights             (вес + жир + lean mass + BMI)
  - workouts            (каждая тренировка)
  - menstrual_log       (менструальный цикл, CREATE TABLE IF NOT EXISTS)

ON CONFLICT DO UPDATE — идемпотентно.
НЕ трогает других пользователей.
"""

import argparse
import defusedxml.ElementTree as ET
from collections import defaultdict
from datetime import datetime
from statistics import mean
import json
from pathlib import Path

_parser = argparse.ArgumentParser(description="Импорт Apple Health XML одного пользователя в PostgreSQL")
_parser.add_argument("--user-id", type=int, required=True, help="telegram_id пользователя")
_parser.add_argument("--xml", type=Path, required=True, help="Путь к export.xml из выгрузки Apple Health")
_parser.add_argument(
    "--out", type=Path, default=Path("/tmp/apple_health_import.sql"), help="Куда писать сгенерированный SQL"
)
_args = _parser.parse_args()

XML = _args.xml
OUT_SQL = _args.out
USER_ID = _args.user_id

# ── Маппинг HKWorkoutActivityType → читаемое название ────────────────────────
WORKOUT_TYPE_MAP = {
    "HKWorkoutActivityTypeWalking": "Walking",
    "HKWorkoutActivityTypeRunning": "Running",
    "HKWorkoutActivityTypeCycling": "Cycling",
    "HKWorkoutActivityTypeSwimming": "Swimming",
    "HKWorkoutActivityTypeYoga": "Yoga",
    "HKWorkoutActivityTypeHighIntensityIntervalTraining": "HIIT",
    "HKWorkoutActivityTypeCardioDance": "Cardio Dance",
    "HKWorkoutActivityTypeFlexibility": "Flexibility",
    "HKWorkoutActivityTypeElliptical": "Elliptical",
    "HKWorkoutActivityTypeStairClimbing": "Stair Climbing",
    "HKWorkoutActivityTypeSkatingSports": "Skating",
    "HKWorkoutActivityTypeSurfingSports": "Surfing",
    "HKWorkoutActivityTypePilates": "Pilates",
    "HKWorkoutActivityTypeFunctionalStrengthTraining": "Strength Training",
    "HKWorkoutActivityTypeTraditionalStrengthTraining": "Strength Training",
    "HKWorkoutActivityTypeWaterSports": "Water Sports",
    "HKWorkoutActivityTypeSquash": "Squash",
    "HKWorkoutActivityTypePickleball": "Pickleball",
    "HKWorkoutActivityTypeHiking": "Hiking",
    "HKWorkoutActivityTypePreparationAndRecovery": "Recovery",
    "HKWorkoutActivityTypeOther": "Other",
}

MENSTRUAL_FLOW_MAP = {
    "HKCategoryValueMenstrualFlowNone": "none",
    "HKCategoryValueMenstrualFlowLight": "light",
    "HKCategoryValueMenstrualFlowMedium": "medium",
    "HKCategoryValueMenstrualFlowHeavy": "heavy",
    "HKCategoryValueMenstrualFlowUnspecified": "unspecified",
}

# ── Приоритет источников шагов ────────────────────────────────────────────────
PRIORITY_PATTERNS = [
    ("garmin", ("garmin", "connect")),
    ("watch", ("apple watch", "watch")),
    ("iphone", ("iphone",)),
]


def pick_primary_steps(sources: dict) -> tuple[int, str]:
    for label, patterns in PRIORITY_PATTERNS:
        for src_name, val in sources.items():
            sn_lower = src_name.lower()
            if any(p in sn_lower for p in patterns):
                return int(val), f"{label}:{src_name}"
    if not sources:
        return 0, "none"
    best = max(sources, key=lambda k: sources[k])
    return int(sources[best]), f"fallback:{best}"


# ── Аккумуляторы ─────────────────────────────────────────────────────────────
steps_by_source = defaultdict(lambda: defaultdict(float))
distance_daily = defaultdict(float)
active_cal_daily = defaultdict(float)
bmr_cal_daily = defaultdict(float)
hr_daily = defaultdict(list)
resting_hr_daily = defaultdict(list)
hrv_daily = defaultdict(list)
weight_records = []  # (datetime, kg, body_fat_pct, lean_kg, bmi)
bp_sys_records = []
bp_dia_records = []
sleep_intervals = []
spo2_daily = defaultdict(list)
walking_speed_daily = defaultdict(list)
walking_step_len_daily = defaultdict(list)
walking_ds_daily = defaultdict(list)
walking_asym_daily = defaultdict(list)
workout_records = []

# Новые метрики
vo2max_daily = defaultdict(list)
resp_rate_daily = defaultdict(list)
exercise_min_daily = defaultdict(float)  # AppleExerciseTime (кольцо)
stand_time_daily = defaultdict(float)  # AppleStandTime мин
flights_daily = defaultdict(float)  # FlightsClimbed
daylight_daily = defaultdict(float)  # TimeInDaylight сек→мин
steadiness_daily = defaultdict(list)  # AppleWalkingSteadiness 0-1
hr_recovery_daily = defaultdict(list)  # HeartRateRecoveryOneMinute
mindful_daily = defaultdict(float)  # MindfulSession, минуты
hr_walking_avg_daily = defaultdict(list)  # WalkingHeartRateAverage
physical_effort_daily = defaultdict(list)  # PhysicalEffort METs

# Состав тела по дате (для join к weight_records)
body_fat_by_dt = {}  # datetime → pct
lean_mass_by_dt = {}  # datetime → kg
bmi_by_dt = {}  # datetime → float

menstrual_records = []  # dict per event

print(f"Парсинг {XML} ({XML.stat().st_size / 1e9:.2f} ГБ)...", flush=True)

n = 0
for event, elem in ET.iterparse(str(XML), events=("end",)):
    # ── Тренировки ────────────────────────────────────────────────────────────
    if elem.tag == "Workout":
        try:
            raw_type = elem.get("workoutActivityType", "HKWorkoutActivityTypeOther")
            workout_type = WORKOUT_TYPE_MAP.get(raw_type, raw_type.replace("HKWorkoutActivityType", ""))
            start_str = elem.get("startDate", "")
            end_str = elem.get("endDate", "")
            duration_min = float(elem.get("duration", 0) or 0)
            source = elem.get("sourceName", "apple_health")

            if start_str:
                start_dt = datetime.fromisoformat(start_str)
                end_dt = datetime.fromisoformat(end_str) if end_str else None
                workout_date = start_dt.date().isoformat()

                calories = None
                distance = None
                for stat in elem.findall("WorkoutStatistics"):
                    stat_type = stat.get("type", "")
                    if stat_type == "HKQuantityTypeIdentifierActiveEnergyBurned":
                        try:
                            calories = int(float(stat.get("sum", 0) or 0))
                        except (TypeError, ValueError):
                            pass
                    elif stat_type in (
                        "HKQuantityTypeIdentifierDistanceWalkingRunning",
                        "HKQuantityTypeIdentifierDistanceCycling",
                        "HKQuantityTypeIdentifierDistanceSwimming",
                    ):
                        try:
                            unit = (stat.get("unit") or "km").lower()
                            v = float(stat.get("sum", 0) or 0)
                            if unit == "m":
                                v /= 1000
                            elif "mi" in unit:
                                v *= 1.60934
                            distance = round(v, 3)
                        except (TypeError, ValueError):
                            pass

                workout_records.append(
                    {
                        "date": workout_date,
                        "workout_type": workout_type,
                        "duration_minutes": int(round(duration_min)),
                        "start_time": start_dt.strftime("%Y-%m-%d %H:%M:%S%z"),
                        "end_time": end_dt.strftime("%Y-%m-%d %H:%M:%S%z") if end_dt else None,
                        "calories_burned": calories,
                        "distance_km": distance,
                        "source": source[:100],
                    }
                )
        except Exception:
            pass
        elem.clear()
        continue

    if elem.tag not in ("Record",):
        elem.clear()
        continue

    rtype = elem.get("type", "")
    start_str = elem.get("startDate", "")
    end_str = elem.get("endDate", "")
    val_str = elem.get("value")
    src = elem.get("sourceName", "unknown")

    if not start_str:
        elem.clear()
        continue

    try:
        start_dt = datetime.fromisoformat(start_str)
    except ValueError:
        elem.clear()
        continue

    d = start_dt.date().isoformat()
    n += 1

    if n % 500_000 == 0:
        print(f"  обработано {n:,} записей...", flush=True)

    # ── Уже были ─────────────────────────────────────────────────────────────
    if rtype == "HKQuantityTypeIdentifierStepCount":
        try:
            steps_by_source[d][src] += float(val_str)
        except (TypeError, ValueError):
            pass

    elif rtype == "HKQuantityTypeIdentifierDistanceWalkingRunning":
        try:
            unit = (elem.get("unit") or "km").lower()
            v = float(val_str)
            if unit == "m":
                v /= 1000
            elif "mi" in unit:
                v *= 1.60934
            distance_daily[d] += v
        except (TypeError, ValueError):
            pass

    elif rtype == "HKQuantityTypeIdentifierActiveEnergyBurned":
        try:
            unit = (elem.get("unit") or "kcal").lower()
            v = float(val_str)
            if unit == "kj":
                v /= 4.184
            active_cal_daily[d] += v
        except (TypeError, ValueError):
            pass

    elif rtype == "HKQuantityTypeIdentifierBasalEnergyBurned":
        try:
            unit = (elem.get("unit") or "kcal").lower()
            v = float(val_str)
            if unit == "kj":
                v /= 4.184
            bmr_cal_daily[d] += v
        except (TypeError, ValueError):
            pass

    elif rtype == "HKQuantityTypeIdentifierHeartRate":
        try:
            hr_daily[d].append(float(val_str))
        except (TypeError, ValueError):
            pass

    elif rtype == "HKQuantityTypeIdentifierRestingHeartRate":
        try:
            resting_hr_daily[d].append(float(val_str))
        except (TypeError, ValueError):
            pass

    elif rtype == "HKQuantityTypeIdentifierHeartRateVariabilitySDNN":
        try:
            hrv_daily[d].append(float(val_str))
        except (TypeError, ValueError):
            pass

    elif rtype == "HKQuantityTypeIdentifierBodyMass":
        try:
            unit = (elem.get("unit") or "kg").lower()
            v = float(val_str)
            if "lb" in unit:
                v *= 0.453592
            weight_records.append((start_dt, round(v, 2)))
        except (TypeError, ValueError):
            pass

    elif rtype == "HKQuantityTypeIdentifierBodyFatPercentage":
        try:
            v = float(val_str)
            if v > 1.0:
                v /= 100.0
            body_fat_by_dt[start_dt] = round(v * 100, 1)
        except (TypeError, ValueError):
            pass

    elif rtype == "HKQuantityTypeIdentifierLeanBodyMass":
        try:
            unit = (elem.get("unit") or "kg").lower()
            v = float(val_str)
            if "lb" in unit:
                v *= 0.453592
            lean_mass_by_dt[start_dt] = round(v, 1)
        except (TypeError, ValueError):
            pass

    elif rtype == "HKQuantityTypeIdentifierBodyMassIndex":
        try:
            bmi_by_dt[start_dt] = round(float(val_str), 1)
        except (TypeError, ValueError):
            pass

    elif rtype == "HKQuantityTypeIdentifierBloodPressureSystolic":
        try:
            bp_sys_records.append((start_dt, int(float(val_str))))
        except (TypeError, ValueError):
            pass

    elif rtype == "HKQuantityTypeIdentifierBloodPressureDiastolic":
        try:
            bp_dia_records.append((start_dt, int(float(val_str))))
        except (TypeError, ValueError):
            pass

    elif rtype == "HKCategoryTypeIdentifierSleepAnalysis":
        sleep_val = val_str or ""
        if "Asleep" in sleep_val or sleep_val == "0":
            try:
                end_dt = datetime.fromisoformat(end_str)
                hours = (end_dt - start_dt).total_seconds() / 3600.0
                wake_date = end_dt.date().isoformat()
                if 0.1 < hours < 20:
                    sleep_intervals.append((wake_date, hours))
            except (TypeError, ValueError):
                pass

    elif rtype == "HKQuantityTypeIdentifierOxygenSaturation":
        try:
            v = float(val_str)
            if v <= 1.0:
                v *= 100
            spo2_daily[d].append(round(v, 1))
        except (TypeError, ValueError):
            pass

    elif rtype == "HKQuantityTypeIdentifierWalkingSpeed":
        try:
            unit = (elem.get("unit") or "km/hr").lower()
            v = float(val_str)
            if "m/s" in unit:
                v *= 3.6
            elif "mi" in unit:
                v *= 1.60934
            walking_speed_daily[d].append(round(v, 2))
        except (TypeError, ValueError):
            pass

    elif rtype == "HKQuantityTypeIdentifierWalkingStepLength":
        try:
            unit = (elem.get("unit") or "cm").lower()
            v = float(val_str)
            if unit == "m":
                v *= 100
            walking_step_len_daily[d].append(round(v, 1))
        except (TypeError, ValueError):
            pass

    elif rtype == "HKQuantityTypeIdentifierWalkingDoubleSupportPercentage":
        try:
            v = float(val_str)
            if v <= 1.0:
                v *= 100
            walking_ds_daily[d].append(round(v, 2))
        except (TypeError, ValueError):
            pass

    elif rtype == "HKQuantityTypeIdentifierWalkingAsymmetryPercentage":
        try:
            v = float(val_str)
            if v <= 1.0:
                v *= 100
            walking_asym_daily[d].append(round(v, 2))
        except (TypeError, ValueError):
            pass

    # ── Новые метрики ─────────────────────────────────────────────────────────
    elif rtype == "HKQuantityTypeIdentifierVO2Max":
        try:
            vo2max_daily[d].append(round(float(val_str), 1))
        except (TypeError, ValueError):
            pass

    elif rtype == "HKQuantityTypeIdentifierRespiratoryRate":
        try:
            resp_rate_daily[d].append(round(float(val_str), 1))
        except (TypeError, ValueError):
            pass

    elif rtype == "HKQuantityTypeIdentifierAppleExerciseTime":
        try:
            unit = (elem.get("unit") or "min").lower()
            v = float(val_str)
            if unit == "s":
                v /= 60
            exercise_min_daily[d] += v
        except (TypeError, ValueError):
            pass

    elif rtype == "HKQuantityTypeIdentifierAppleStandTime":
        try:
            unit = (elem.get("unit") or "min").lower()
            v = float(val_str)
            if unit == "s":
                v /= 60
            stand_time_daily[d] += v
        except (TypeError, ValueError):
            pass

    elif rtype == "HKQuantityTypeIdentifierFlightsClimbed":
        try:
            flights_daily[d] += float(val_str)
        except (TypeError, ValueError):
            pass

    elif rtype == "HKQuantityTypeIdentifierTimeInDaylight":
        try:
            unit = (elem.get("unit") or "min").lower()
            v = float(val_str)
            if unit == "s":
                v /= 60
            daylight_daily[d] += v
        except (TypeError, ValueError):
            pass

    elif rtype == "HKQuantityTypeIdentifierAppleWalkingSteadiness":
        try:
            v = float(val_str)
            if v <= 1.0:
                v *= 100
            steadiness_daily[d].append(round(v, 1))
        except (TypeError, ValueError):
            pass

    elif rtype == "HKQuantityTypeIdentifierHeartRateRecoveryOneMinute":
        try:
            hr_recovery_daily[d].append(int(float(val_str)))
        except (TypeError, ValueError):
            pass

    elif rtype == "HKCategoryTypeIdentifierMindfulSession":
        try:
            end_dt_m = datetime.fromisoformat(end_str)
            mins = (end_dt_m - start_dt).total_seconds() / 60.0
            if 0 < mins < 300:
                mindful_daily[d] += mins
        except (TypeError, ValueError):
            pass

    elif rtype == "HKQuantityTypeIdentifierWalkingHeartRateAverage":
        try:
            hr_walking_avg_daily[d].append(int(float(val_str)))
        except (TypeError, ValueError):
            pass

    elif rtype == "HKQuantityTypeIdentifierPhysicalEffort":
        try:
            physical_effort_daily[d].append(round(float(val_str), 2))
        except (TypeError, ValueError):
            pass

    elif rtype == "HKCategoryTypeIdentifierMenstrualFlow":
        try:
            flow_val = val_str or "HKCategoryValueMenstrualFlowUnspecified"
            flow = MENSTRUAL_FLOW_MAP.get(flow_val, "unspecified")
            menstrual_records.append(
                {
                    "date": d,
                    "flow": flow,
                    "source": src[:100],
                }
            )
        except Exception:
            pass

    elif rtype == "HKCategoryTypeIdentifierIntermenstrualBleeding":
        menstrual_records.append(
            {
                "date": d,
                "flow": "spotting",
                "source": src[:100],
            }
        )

    elem.clear()

print(f"Прочитано {n:,} записей. Агрегирую...", flush=True)

# ── Агрегация шагов ───────────────────────────────────────────────────────────
steps_daily = {}
for d, sources in steps_by_source.items():
    steps, _ = pick_primary_steps(dict(sources))
    steps_daily[d] = steps

# ── Агрегация сна ─────────────────────────────────────────────────────────────
sleep_daily: dict[str, float] = defaultdict(float)
for wake_date, hours in sleep_intervals:
    sleep_daily[wake_date] += hours
sleep_daily = {d: round(min(v, 20.0), 2) for d, v in sleep_daily.items() if v > 0.5}

# ── Паровка давления ──────────────────────────────────────────────────────────
bp_dia_map: dict[str, list] = defaultdict(list)
for dt, v in bp_dia_records:
    bp_dia_map[dt.date().isoformat()].append((dt, v))

bp_pairs = []
for sys_dt, sys_v in sorted(bp_sys_records, key=lambda x: x[0]):
    d = sys_dt.date().isoformat()
    candidates = bp_dia_map.get(d, [])
    if not candidates:
        continue
    closest = min(candidates, key=lambda x: abs((x[0] - sys_dt).total_seconds()))
    if abs((closest[0] - sys_dt).total_seconds()) < 120:
        bp_pairs.append((sys_dt, sys_v, closest[1]))

# ── Все дни ───────────────────────────────────────────────────────────────────
all_days = sorted(
    set(
        list(steps_daily.keys())
        + list(hr_daily.keys())
        + list(sleep_daily.keys())
        + list(distance_daily.keys())
        + list(resting_hr_daily.keys())
        + list(vo2max_daily.keys())
        + list(exercise_min_daily.keys())
    )
)

print(f"Дней: {len(all_days)}, первый: {all_days[0]}, последний: {all_days[-1]}", flush=True)
print(f"  вес-замеров: {len(weight_records)}, BP-пар: {len(bp_pairs)}", flush=True)
print(f"  HRV: {len(hrv_daily)}, SpO2: {len(spo2_daily)}, сон: {len(sleep_daily)}", flush=True)
print(f"  тренировок: {len(workout_records)}", flush=True)
print(f"  VO2max-дней: {len(vo2max_daily)}, дыхание: {len(resp_rate_daily)}", flush=True)
print(f"  упражнения (кольцо): {len(exercise_min_daily)}, стояние: {len(stand_time_daily)}", flush=True)
print(f"  менструальных записей: {len(menstrual_records)}", flush=True)
print(f"  медитаций: {len(mindful_daily)}, этажей-дней: {len(flights_daily)}", flush=True)

# ── SQL helpers ───────────────────────────────────────────────────────────────


def esc(v):
    if v is None:
        return "NULL"
    if isinstance(v, (int, float)):
        return str(v)
    return "'" + str(v).replace("'", "''") + "'"


def jsonb(d: dict) -> str:
    if not d:
        return "NULL"
    return "'" + json.dumps(d, ensure_ascii=False).replace("'", "''") + "'::jsonb"


lines = [
    f"-- Импорт Apple Health, user_id={USER_ID}",
    f"-- Сгенерировано: {datetime.now().isoformat()}",
    f"-- Дней activity: {len(all_days)}, весов: {len(weight_records)}, BP: {len(bp_pairs)}, "
    f"тренировок: {len(workout_records)}, менструальных: {len(menstrual_records)}",
    "BEGIN;",
    "",
]

# ── 0. Создать таблицу menstrual_log если нет ─────────────────────────────────
lines.append("-- menstrual_log DDL")
lines.append("""CREATE TABLE IF NOT EXISTS menstrual_log (
    id SERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
    date DATE NOT NULL,
    flow VARCHAR(20),
    source VARCHAR(100),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    CONSTRAINT uq_menstrual_user_date UNIQUE (user_id, date)
);
CREATE INDEX IF NOT EXISTS idx_menstrual_user_date ON menstrual_log (user_id, date DESC);""")
lines.append("")

# ── 1. activity_log ───────────────────────────────────────────────────────────
lines.append("-- activity_log")
for d in all_days:
    steps = steps_daily.get(d)
    dist = round(distance_daily.get(d, 0), 3) or None
    hr_list = hr_daily.get(d, [])
    rhr_list = resting_hr_daily.get(d, [])
    hrv_list = hrv_daily.get(d, [])
    sleep_h = sleep_daily.get(d)
    bmr_cal = round(bmr_cal_daily.get(d, 0), 1) or None
    active_cal = round(active_cal_daily.get(d, 0), 1) or None

    hr_avg = int(round(mean(hr_list))) if hr_list else None
    rhr_avg = int(round(mean(rhr_list))) if rhr_list else None
    hrv_avg = int(round(mean(hrv_list))) if hrv_list else None
    final_hr = rhr_avg or hr_avg

    raw: dict = {}
    if hr_list:
        raw["heart_rate_min"] = int(min(hr_list))
        raw["heart_rate_max"] = int(max(hr_list))
    if hr_avg:
        raw["heart_rate_avg_all"] = hr_avg
    if spo2_daily.get(d):
        raw["spo2_pct"] = round(mean(spo2_daily[d]), 1)
    if walking_speed_daily.get(d):
        raw["walking_speed_km_h"] = round(mean(walking_speed_daily[d]), 2)
    if walking_step_len_daily.get(d):
        raw["walking_step_length_cm"] = round(mean(walking_step_len_daily[d]), 1)
    if walking_ds_daily.get(d):
        raw["walking_double_support_pct"] = round(mean(walking_ds_daily[d]), 2)
    if walking_asym_daily.get(d):
        raw["walking_asymmetry_pct"] = round(mean(walking_asym_daily[d]), 2)
    if active_cal:
        raw["apple_active_energy_kcal"] = active_cal
    if bmr_cal:
        raw["apple_basal_energy_kcal"] = bmr_cal
    # Новые поля
    if vo2max_daily.get(d):
        raw["vo2_max"] = round(mean(vo2max_daily[d]), 1)
    if resp_rate_daily.get(d):
        raw["respiratory_rate"] = round(mean(resp_rate_daily[d]), 1)
    if exercise_min_daily.get(d):
        raw["exercise_minutes"] = int(round(exercise_min_daily[d]))
    if stand_time_daily.get(d):
        raw["stand_time_min"] = int(round(stand_time_daily[d]))
    if flights_daily.get(d):
        raw["flights_climbed"] = int(round(flights_daily[d]))
    if daylight_daily.get(d):
        raw["daylight_min"] = int(round(daylight_daily[d]))
    if steadiness_daily.get(d):
        raw["walking_steadiness_pct"] = round(mean(steadiness_daily[d]), 1)
    if hr_recovery_daily.get(d):
        raw["hr_recovery_1min"] = int(round(mean(hr_recovery_daily[d])))
    if mindful_daily.get(d):
        raw["mindful_min"] = round(mindful_daily[d], 1)
    if hr_walking_avg_daily.get(d):
        raw["hr_walking_avg"] = int(round(mean(hr_walking_avg_daily[d])))
    if physical_effort_daily.get(d):
        raw["physical_effort_mets"] = round(mean(physical_effort_daily[d]), 2)

    lines.append(
        f"INSERT INTO activity_log "
        f"(user_id, date, steps, distance_km, heart_rate_avg, hrv, sleep_hours, bmr_calories, source, raw_data) "
        f"VALUES ({USER_ID}, '{d}', {esc(steps)}, {esc(dist)}, {esc(final_hr)}, {esc(hrv_avg)}, "
        f"{esc(sleep_h)}, {esc(bmr_cal)}, 'apple_health_xml_import', {jsonb(raw)}) "
        f"ON CONFLICT (user_id, date) DO UPDATE SET "
        f"steps = COALESCE(EXCLUDED.steps, activity_log.steps), "
        f"distance_km = COALESCE(EXCLUDED.distance_km, activity_log.distance_km), "
        f"heart_rate_avg = COALESCE(EXCLUDED.heart_rate_avg, activity_log.heart_rate_avg), "
        f"hrv = COALESCE(EXCLUDED.hrv, activity_log.hrv), "
        f"sleep_hours = COALESCE(EXCLUDED.sleep_hours, activity_log.sleep_hours), "
        f"bmr_calories = COALESCE(EXCLUDED.bmr_calories, activity_log.bmr_calories), "
        f"raw_data = COALESCE(EXCLUDED.raw_data, activity_log.raw_data);"
    )

lines.append("")

# ── 2. blood_pressure_logs ────────────────────────────────────────────────────
lines.append("-- blood_pressure_logs")
for sys_dt, sys_v, dia_v in bp_pairs:
    ts = sys_dt.strftime("%Y-%m-%d %H:%M:%S")
    lines.append(
        f"INSERT INTO blood_pressure_logs (user_id, measured_at, systolic, diastolic, source) "
        f"VALUES ({USER_ID}, '{ts}', {sys_v}, {dia_v}, 'apple_health_xml_import') "
        f"ON CONFLICT (user_id, measured_at) DO UPDATE "
        f"SET systolic = EXCLUDED.systolic, diastolic = EXCLUDED.diastolic;"
    )

lines.append("")

# ── 3. weights ────────────────────────────────────────────────────────────────
lines.append("-- weights")
for w_dt, w_kg in weight_records:
    ts = w_dt.strftime("%Y-%m-%d %H:%M:%S")
    # Найти ближайшие body_fat / lean / bmi в пределах суток
    bf = None
    lean = None
    bmi = None
    for cmp_dt, cmp_val in body_fat_by_dt.items():
        if abs((cmp_dt - w_dt).total_seconds()) < 86400:
            bf = cmp_val
            break
    for cmp_dt, cmp_val in lean_mass_by_dt.items():
        if abs((cmp_dt - w_dt).total_seconds()) < 86400:
            lean = cmp_val
            break
    for cmp_dt, cmp_val in bmi_by_dt.items():
        if abs((cmp_dt - w_dt).total_seconds()) < 86400:
            bmi = cmp_val
            break
    lines.append(
        f"INSERT INTO weights (user_id, measured_at, weight, body_fat, muscle_mass, bmi, source) "
        f"VALUES ({USER_ID}, '{ts}', {w_kg}, {esc(bf)}, {esc(lean)}, {esc(bmi)}, 'apple_health_xml_import') "
        f"ON CONFLICT (user_id, measured_at) DO UPDATE SET "
        f"weight = EXCLUDED.weight, "
        f"body_fat = COALESCE(EXCLUDED.body_fat, weights.body_fat), "
        f"muscle_mass = COALESCE(EXCLUDED.muscle_mass, weights.muscle_mass), "
        f"bmi = COALESCE(EXCLUDED.bmi, weights.bmi);"
    )

lines.append("")

# ── 4. workouts ───────────────────────────────────────────────────────────────
lines.append("-- workouts")
for w in workout_records:
    lines.append(
        f"INSERT INTO workouts "
        f"(user_id, date, workout_type, duration_minutes, start_time, end_time, calories_burned, distance_km, source) "
        f"VALUES ({USER_ID}, '{w['date']}', {esc(w['workout_type'])}, {esc(w['duration_minutes'])}, "
        f"{esc(w['start_time'])}, {esc(w['end_time'])}, {esc(w['calories_burned'])}, {esc(w['distance_km'])}, {esc(w['source'])}) "
        f"ON CONFLICT DO NOTHING;"
    )

lines.append("")

# ── 5. menstrual_log ──────────────────────────────────────────────────────────
lines.append("-- menstrual_log")
for m in menstrual_records:
    lines.append(
        f"INSERT INTO menstrual_log (user_id, date, flow, source) "
        f"VALUES ({USER_ID}, '{m['date']}', {esc(m['flow'])}, {esc(m['source'])}) "
        f"ON CONFLICT (user_id, date) DO UPDATE SET flow = EXCLUDED.flow;"
    )

lines.append("")
lines.append("COMMIT;")
lines.append(
    f"-- Итог: {len(all_days)} activity + {len(bp_pairs)} BP + {len(weight_records)} weights "
    f"+ {len(workout_records)} workouts + {len(menstrual_records)} menstrual"
)

OUT_SQL.write_text("\n".join(lines), encoding="utf-8")
sql_kb = OUT_SQL.stat().st_size / 1024
print(f"\n✅ SQL записан: {OUT_SQL} ({sql_kb:.0f} КБ)")
print("\nСледующий шаг:")
print(f"  scp {OUT_SQL} root@116.203.213.137:/tmp/{OUT_SQL.name}")
print(
    f"  ssh root@116.203.213.137 'docker exec -i healthvault_postgres psql -U healthvault -d healthvault < /tmp/{OUT_SQL.name}'"
)
