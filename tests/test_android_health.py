"""
Тесты для android_health.py — агрегация Health Connect raw records по дням.

Проверяем:
1. steps суммируются по дню
2. blood_pressure: N отдельных строк (не агрегируется)
3. weight: последний замер за день
4. timezone correctness: запись в 22:00 МСК НЕ уезжает на следующий день
5. active_calories только в raw_data, не в activity
6. weight: фильтр >30 кг

Запуск: PYTHONPATH=. pytest tests/test_android_health.py -v
"""

import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "telegram-bot"))

from webhook.android_health import (
    HealthConnectPayload,
    _hc_aggregate_by_day,
    _parse_utc,
    _to_local_date,
)

# Таймзона для тестов: Москва (UTC+3)
MSK = ZoneInfo("Europe/Moscow")


# ── Вспомогательные fixtures ──────────────────────────────────────────────────


def make_payload(**kwargs) -> HealthConnectPayload:
    """Создать payload с дефолтными пустыми списками, переопределив нужные поля."""
    defaults = {
        "timestamp": "2026-06-10T14:00:00Z",
        "app_version": "1.9.10",
    }
    defaults.update(kwargs)
    return HealthConnectPayload(**defaults)


# ── _parse_utc ────────────────────────────────────────────────────────────────


def test_parse_utc_z_suffix():
    """Z-суффикс парсится корректно."""
    dt = _parse_utc("2026-05-09T08:15:00Z")
    assert dt is not None
    assert dt.tzinfo is not None
    assert dt.year == 2026 and dt.month == 5 and dt.day == 9
    assert dt.hour == 8 and dt.minute == 15


def test_parse_utc_offset():
    """Явный +00:00 суффикс тоже работает."""
    dt = _parse_utc("2026-05-09T08:15:00+00:00")
    assert dt is not None
    assert dt.hour == 8


def test_parse_utc_invalid():
    """Неверный формат → None, не падает."""
    assert _parse_utc("not-a-date") is None
    assert _parse_utc("") is None
    assert _parse_utc(None) is None


# ── _to_local_date ────────────────────────────────────────────────────────────


def test_to_local_date_msk_same_day():
    """19:00 UTC = 22:00 МСК — тот же день (10 июня), не следующий."""
    d = _to_local_date("2026-06-10T19:00:00Z", MSK)
    assert d == date(2026, 6, 10)


def test_to_local_date_22_msk_not_next_day():
    """
    Ключевой тест timezone correctness.
    22:00 МСК = 19:00 UTC. Должно попасть в 10 июня, а не в 11-е.
    Именно этот баг чинил commit efd9f0f.
    """
    # 19:00 UTC → 22:00 МСК → дата 10 июня
    d = _to_local_date("2026-06-10T19:00:00Z", MSK)
    assert d == date(2026, 6, 10), f"22:00 МСК должно быть 10-е, получили {d}"


def test_to_local_date_midnight_utc_is_prev_day_msk():
    """00:01 UTC = 03:01 МСК — ТОЖЕ текущий день."""
    d = _to_local_date("2026-06-10T00:01:00Z", MSK)
    assert d == date(2026, 6, 10)


def test_to_local_date_21_utc_is_next_day_msk():
    """21:00 UTC = 00:00 МСК следующего дня."""
    d = _to_local_date("2026-06-10T21:00:00Z", MSK)
    assert d == date(2026, 6, 11)


# ── _hc_aggregate_by_day: steps ──────────────────────────────────────────────


def test_steps_summed_single_day():
    """Шаги за один день суммируются из нескольких записей."""
    payload = make_payload(
        steps=[
            {"count": 3000, "start_time": "2026-06-10T06:00:00Z", "end_time": "2026-06-10T09:00:00Z"},
            {"count": 2500, "start_time": "2026-06-10T09:00:00Z", "end_time": "2026-06-10T12:00:00Z"},
            {"count": 2921, "start_time": "2026-06-10T12:00:00Z", "end_time": "2026-06-10T19:00:00Z"},
        ]
    )
    result = _hc_aggregate_by_day(payload, MSK)
    assert date(2026, 6, 10) in result
    assert result[date(2026, 6, 10)]["steps"] == 8421


def test_steps_split_across_days():
    """Шаги из разных дней попадают в правильные бакеты."""
    payload = make_payload(
        steps=[
            {"count": 5000, "start_time": "2026-06-09T10:00:00Z", "end_time": "2026-06-09T18:00:00Z"},
            {"count": 3000, "start_time": "2026-06-10T10:00:00Z", "end_time": "2026-06-10T18:00:00Z"},
        ]
    )
    result = _hc_aggregate_by_day(payload, MSK)
    assert result[date(2026, 6, 9)]["steps"] == 5000
    assert result[date(2026, 6, 10)]["steps"] == 3000


def test_steps_22_utc_bucket_correct():
    """
    Запись в 22:00 UTC = 01:00 МСК следующего дня → бакет следующего дня.
    """
    payload = make_payload(
        steps=[
            {"count": 1000, "start_time": "2026-06-10T22:00:00Z", "end_time": "2026-06-10T22:30:00Z"},
        ]
    )
    result = _hc_aggregate_by_day(payload, MSK)
    # 22:00 UTC = 01:00 МСК 11 июня
    assert date(2026, 6, 11) in result
    assert result[date(2026, 6, 11)]["steps"] == 1000
    assert date(2026, 6, 10) not in result


def test_steps_19_utc_is_today_msk():
    """
    Ключевой сценарий: 19:00 UTC = 22:00 МСК.
    Должно попасть в СЕГОДНЯ (10 июня), не завтра.
    """
    payload = make_payload(
        steps=[
            {"count": 500, "start_time": "2026-06-10T19:00:00Z", "end_time": "2026-06-10T19:30:00Z"},
        ]
    )
    result = _hc_aggregate_by_day(payload, MSK)
    assert date(2026, 6, 10) in result, "22:00 МСК должно быть в бакете 10-го, а не 11-го"
    assert result[date(2026, 6, 10)]["steps"] == 500
    assert date(2026, 6, 11) not in result


# ── _hc_aggregate_by_day: blood_pressure ─────────────────────────────────────


def test_bp_each_record_separate():
    """
    Blood pressure: каждый замер отдельная строка, не агрегируется.
    У папы до 10 замеров в день — все нужны.
    """
    payload = make_payload(
        blood_pressure=[
            {"systolic": 120, "diastolic": 80, "time": "2026-06-10T07:00:00Z"},
            {"systolic": 118, "diastolic": 78, "time": "2026-06-10T07:05:00Z"},
            {"systolic": 125, "diastolic": 82, "time": "2026-06-10T20:00:00Z"},
            {"systolic": 122, "diastolic": 79, "time": "2026-06-10T20:05:00Z"},
        ]
    )
    result = _hc_aggregate_by_day(payload, MSK)
    d = date(2026, 6, 10)
    assert d in result
    bp_list = result[d]["blood_pressure"]
    assert len(bp_list) == 4, f"Ожидали 4 замера BP, получили {len(bp_list)}"

    # Проверяем что значения сохранены корректно
    systolics = {bp["systolic"] for bp in bp_list}
    assert systolics == {120, 118, 125, 122}


def test_bp_measured_at_uses_real_timestamp():
    """measured_at = реальный timestamp из записи, не синтетический полдень."""
    ts = "2026-06-10T07:15:00Z"
    payload = make_payload(
        blood_pressure=[
            {"systolic": 120, "diastolic": 80, "time": ts},
        ]
    )
    result = _hc_aggregate_by_day(payload, MSK)
    bp = result[date(2026, 6, 10)]["blood_pressure"][0]
    # measured_at — datetime объект
    assert isinstance(bp["measured_at"], datetime)
    assert bp["measured_at"].hour == 7
    assert bp["measured_at"].minute == 15


def test_bp_split_midnight_msk():
    """
    Замер в 21:30 UTC = 00:30 МСК следующего дня →
    попадает в следующий день, а не в текущий.
    """
    payload = make_payload(
        blood_pressure=[
            {"systolic": 120, "diastolic": 80, "time": "2026-06-10T21:30:00Z"},  # 00:30 МСК 11-го
            {"systolic": 118, "diastolic": 78, "time": "2026-06-10T10:00:00Z"},  # 13:00 МСК 10-го
        ]
    )
    result = _hc_aggregate_by_day(payload, MSK)
    # 10:00 UTC = 13:00 МСК → 10 июня
    assert len(result[date(2026, 6, 10)]["blood_pressure"]) == 1
    # 21:30 UTC = 00:30 МСК → 11 июня
    assert len(result[date(2026, 6, 11)]["blood_pressure"]) == 1


# ── _hc_aggregate_by_day: weight ─────────────────────────────────────────────


def test_weight_last_of_day():
    """Вес: берётся последний замер за день (хронологически по time)."""
    payload = make_payload(
        weight=[
            {"kilograms": 82.0, "time": "2026-06-10T06:00:00Z"},  # утренний
            {"kilograms": 82.5, "time": "2026-06-10T18:00:00Z"},  # вечерний
            {"kilograms": 82.3, "time": "2026-06-10T12:00:00Z"},  # дневной
        ]
    )
    result = _hc_aggregate_by_day(payload, MSK)
    d = date(2026, 6, 10)
    assert d in result
    # Последний по timestamp = 18:00 UTC → 82.5
    assert result[d]["weight_kg"] == 82.5


def test_weight_filter_below_30():
    """Вес <30 кг (мусорные нулевые записи) отсекаются."""
    payload = make_payload(
        weight=[
            {"kilograms": 0.0, "time": "2026-06-10T06:00:00Z"},
            {"kilograms": 15.0, "time": "2026-06-10T07:00:00Z"},
            {"kilograms": 82.0, "time": "2026-06-10T08:00:00Z"},
        ]
    )
    result = _hc_aggregate_by_day(payload, MSK)
    d = date(2026, 6, 10)
    assert result[d]["weight_kg"] == 82.0


def test_weight_all_below_30_skipped():
    """Если все записи <30 кг — weight_kg отсутствует в аггрегате."""
    payload = make_payload(
        weight=[
            {"kilograms": 0.0, "time": "2026-06-10T06:00:00Z"},
        ]
    )
    result = _hc_aggregate_by_day(payload, MSK)
    d = date(2026, 6, 10)
    if d in result:
        assert "weight_kg" not in result[d]


# ── _hc_aggregate_by_day: active_calories → raw_data ─────────────────────────


def test_active_calories_only_in_raw_data():
    """
    active_calories НЕ попадает в activity calories — только в raw_data.
    Garmin = source of truth для тройки bmr/active/total.
    """
    payload = make_payload(
        active_calories=[
            {"calories": 350.0, "start_time": "2026-06-10T08:00:00Z", "end_time": "2026-06-10T09:00:00Z"},
        ]
    )
    result = _hc_aggregate_by_day(payload, MSK)
    d = date(2026, 6, 10)
    assert d in result
    # НЕ должно быть поля calories/active_calories на верхнем уровне аггрегата
    assert "calories" not in result[d]
    assert "active_calories" not in result[d]
    # Должно быть в raw_data
    assert "raw_data" in result[d]
    assert result[d]["raw_data"].get("hc_active_calories") == 350.0


def test_active_calories_summed_in_raw_data():
    """Несколько записей active_calories суммируются в raw_data."""
    payload = make_payload(
        active_calories=[
            {"calories": 200.0, "start_time": "2026-06-10T08:00:00Z", "end_time": "2026-06-10T09:00:00Z"},
            {"calories": 150.0, "start_time": "2026-06-10T15:00:00Z", "end_time": "2026-06-10T16:00:00Z"},
        ]
    )
    result = _hc_aggregate_by_day(payload, MSK)
    assert result[date(2026, 6, 10)]["raw_data"]["hc_active_calories"] == 350.0


# ── _hc_aggregate_by_day: heart rate ─────────────────────────────────────────


def test_heart_rate_avg_min_max():
    """HR avg/min/max вычисляются корректно."""
    payload = make_payload(
        heart_rate=[
            {"bpm": 60, "time": "2026-06-10T08:00:00Z"},
            {"bpm": 90, "time": "2026-06-10T12:00:00Z"},
            {"bpm": 75, "time": "2026-06-10T18:00:00Z"},
        ]
    )
    result = _hc_aggregate_by_day(payload, MSK)
    d = date(2026, 6, 10)
    assert result[d]["heart_rate_avg"] == 75  # round((60+90+75)/3) = 75
    assert result[d]["heart_rate_min"] == 60
    assert result[d]["heart_rate_max"] == 90


def test_resting_hr_last_of_day():
    """Resting HR: последний за день."""
    payload = make_payload(
        resting_heart_rate=[
            {"bpm": 58, "time": "2026-06-10T06:00:00Z"},
            {"bpm": 62, "time": "2026-06-10T20:00:00Z"},
        ]
    )
    result = _hc_aggregate_by_day(payload, MSK)
    # Последний = 20:00 UTC → bpm 62
    assert result[date(2026, 6, 10)]["resting_heart_rate"] == 62


# ── _hc_aggregate_by_day: пустой payload ─────────────────────────────────────


def test_empty_payload():
    """Пустой payload → пустой результат."""
    payload = make_payload()
    result = _hc_aggregate_by_day(payload, MSK)
    assert result == {}


# ── _hc_aggregate_by_day: HRV ─────────────────────────────────────────────────


def test_hrv_last_of_day():
    """HRV: последний за день, округляется до int."""
    payload = make_payload(
        heart_rate_variability=[
            {"rmssd_millis": 45.5, "time": "2026-06-10T06:00:00Z"},
            {"rmssd_millis": 52.3, "time": "2026-06-10T22:00:00Z"},  # 22:00 UTC = 01:00 МСК 11-го
        ]
    )
    result = _hc_aggregate_by_day(payload, MSK)
    # 06:00 UTC = 09:00 МСК → 10 июня
    assert result[date(2026, 6, 10)]["hrv"] == 46  # round(45.5) = 46
    # 22:00 UTC = 01:00 МСК → 11 июня
    assert result[date(2026, 6, 11)]["hrv"] == 52  # round(52.3) = 52


# ── _hc_aggregate_by_day: sleep ───────────────────────────────────────────────


def test_sleep_hours_summed():
    """Сон: суммируем секунды всех сессий → часы."""
    payload = make_payload(
        sleep=[
            {"session_end_time": "2026-06-10T06:00:00Z", "duration_seconds": 25200},  # 7ч
            {"session_end_time": "2026-06-10T14:00:00Z", "duration_seconds": 3600},  # 1ч (дрёма)
        ]
    )
    result = _hc_aggregate_by_day(payload, MSK)
    d = date(2026, 6, 10)
    assert result[d]["sleep_hours"] == pytest.approx(8.0, rel=0.01)


def test_sleep_hours_overlapping_sessions_not_double_counted():
    """Сон: пересекающиеся сессии (ре-синк истории от Health Connect) мёрджатся, не суммируются наивно."""
    payload = make_payload(
        sleep=[
            # основная сессия: 23:00 → 06:00 (7ч)
            {"session_end_time": "2026-06-10T06:00:00Z", "duration_seconds": 25200},
            # дубль/пере-синк той же ночи, пересекается с первой (04:00 → 06:30)
            {"session_end_time": "2026-06-10T06:30:00Z", "duration_seconds": 9000},
        ]
    )
    result = _hc_aggregate_by_day(payload, MSK)
    d = date(2026, 6, 10)
    # Наивная сумма дала бы 7 + 2.5 = 9.5ч. Правильный мёрдж: 23:00 → 06:30 = 7.5ч.
    assert result[d]["sleep_hours"] == pytest.approx(7.5, rel=0.01)


# ── _hc_aggregate_by_day: distance ───────────────────────────────────────────


def test_distance_km_converted():
    """Дистанция: метры суммируются и конвертируются в км."""
    payload = make_payload(
        distance=[
            {"meters": 3000, "start_time": "2026-06-10T08:00:00Z", "end_time": "2026-06-10T09:00:00Z"},
            {"meters": 2000, "start_time": "2026-06-10T15:00:00Z", "end_time": "2026-06-10T16:00:00Z"},
        ]
    )
    result = _hc_aggregate_by_day(payload, MSK)
    assert result[date(2026, 6, 10)]["distance_km"] == pytest.approx(5.0, rel=0.01)


# ── Комплексный мок-payload ───────────────────────────────────────────────────


def test_full_mock_payload():
    """
    Полный мок-payload — проверяем что все поля агрегируются корректно.
    Симулируем типичный день папы на Samsung.
    """
    payload = make_payload(
        steps=[
            {"count": 4000, "start_time": "2026-06-10T06:00:00Z", "end_time": "2026-06-10T10:00:00Z"},
            {"count": 2500, "start_time": "2026-06-10T10:00:00Z", "end_time": "2026-06-10T14:00:00Z"},
            {"count": 1921, "start_time": "2026-06-10T14:00:00Z", "end_time": "2026-06-10T18:00:00Z"},
        ],
        heart_rate=[
            {"bpm": 65, "time": "2026-06-10T07:00:00Z"},
            {"bpm": 80, "time": "2026-06-10T12:00:00Z"},
            {"bpm": 70, "time": "2026-06-10T17:00:00Z"},
        ],
        resting_heart_rate=[
            {"bpm": 58, "time": "2026-06-10T06:30:00Z"},
        ],
        blood_pressure=[
            {"systolic": 128, "diastolic": 82, "time": "2026-06-10T07:10:00Z"},
            {"systolic": 126, "diastolic": 80, "time": "2026-06-10T07:15:00Z"},
            {"systolic": 130, "diastolic": 84, "time": "2026-06-10T20:00:00Z"},
        ],
        weight=[
            {"kilograms": 84.2, "time": "2026-06-10T06:00:00Z"},
            {"kilograms": 84.5, "time": "2026-06-10T20:30:00Z"},
        ],
        active_calories=[
            {"calories": 280.0, "start_time": "2026-06-10T08:00:00Z", "end_time": "2026-06-10T18:00:00Z"},
        ],
        sleep=[
            {"session_end_time": "2026-06-10T05:30:00Z", "duration_seconds": 28800},  # 8ч
        ],
        distance=[
            {"meters": 5200, "start_time": "2026-06-10T08:00:00Z", "end_time": "2026-06-10T18:00:00Z"},
        ],
    )

    result = _hc_aggregate_by_day(payload, MSK)
    d = date(2026, 6, 10)
    assert d in result
    agg = result[d]

    # Шаги
    assert agg["steps"] == 8421

    # BP: 3 отдельных замера
    assert len(agg["blood_pressure"]) == 3

    # Вес: последний (18:30 UTC → 84.5)
    assert agg["weight_kg"] == 84.5

    # HR avg = round((65+80+70)/3) = 72
    assert agg["heart_rate_avg"] == 72
    assert agg["heart_rate_min"] == 65
    assert agg["heart_rate_max"] == 80

    # Resting HR приоритет
    assert agg["resting_heart_rate"] == 58

    # active_calories только в raw_data
    assert "calories" not in agg
    assert agg["raw_data"]["hc_active_calories"] == 280.0

    # sleep_hours
    assert agg["sleep_hours"] == pytest.approx(8.0, rel=0.01)

    # distance
    assert agg["distance_km"] == pytest.approx(5.2, rel=0.01)
