"""Тесты парсера уведомлений Apple Watch о пульсе вне нормы.

Часы присылают событие, когда пульс покоя держится за порогом дольше 10 минут.
31.08.2026 три таких эпизода за утро остались только на экране телефона — канала
для них не было, хотя именно они нужны, чтобы сопоставлять тахикардию с
гликемией и с ЭКГ того же момента.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "telegram-bot"))

from webhook.apple_health import _hae_hr_events_to_rows  # noqa: E402

USER = 836757955

# Форма события по образцу того, что видно в «Здоровье»: 11:21, 10 минут, 100–110.
FULL = {
    "start": "2026-08-31 11:21:00 +0300",
    "end": "2026-08-31 11:31:00 +0300",
    "type": "HKHeartRateNotificationTypeHigh",
    "threshold": 100,
    "minHeartRate": 100,
    "maxHeartRate": 110,
    "averageHeartRate": 104,
    "source": "Apple Watch",
}


def test_empty_list_returns_empty():
    assert _hae_hr_events_to_rows([], USER) == []


def test_full_event_parsed():
    row = _hae_hr_events_to_rows([FULL], USER)[0]

    assert row["user_id"] == USER
    assert row["started_at"].isoformat() == "2026-08-31T11:21:00+03:00"
    assert row["ended_at"].isoformat() == "2026-08-31T11:31:00+03:00"
    assert row["event_type"] == "High"  # префикс HealthKit снят
    assert row["threshold_bpm"] == 100
    assert (row["min_bpm"], row["max_bpm"], row["avg_bpm"]) == (100, 110, 104)
    assert row["duration_min"] == 10
    assert row["device"] == "Apple Watch"


def test_localized_type_kept_verbatim():
    """Тип может прийти локализованным — не нормализуем и не отбрасываем."""
    rec = dict(FULL, type="Высокий пульс")
    assert _hae_hr_events_to_rows([rec], USER)[0]["event_type"] == "Высокий пульс"


def test_low_heart_rate_event():
    rec = dict(FULL, type="HKHeartRateNotificationTypeLow", threshold=40, minHeartRate=38, maxHeartRate=45)
    row = _hae_hr_events_to_rows([rec], USER)[0]
    assert (row["event_type"], row["threshold_bpm"], row["min_bpm"]) == ("Low", 40, 38)


def test_duration_from_interval_when_missing():
    rec = {"start": "2026-08-31 12:29:00 +0300", "end": "2026-08-31 12:44:00 +0300"}
    assert _hae_hr_events_to_rows([rec], USER)[0]["duration_min"] == 15


def test_bare_duration_treated_as_seconds():
    rec = {"start": "2026-08-31 12:29:00 +0300", "duration": 600}
    assert _hae_hr_events_to_rows([rec], USER)[0]["duration_min"] == 10


def test_duration_with_minute_units():
    rec = {"start": "2026-08-31 12:29:00 +0300", "duration": {"qty": 12, "units": "min"}}
    assert _hae_hr_events_to_rows([rec], USER)[0]["duration_min"] == 12


def test_alternative_field_names():
    rec = {
        "startDate": "2026-08-31 11:39:00 +0300",
        "notificationType": "high",
        "heartRateMin": 100,
        "heartRateMax": 105,
        "thresholdHeartRate": 100,
    }
    row = _hae_hr_events_to_rows([rec], USER)[0]
    assert (row["event_type"], row["min_bpm"], row["max_bpm"], row["threshold_bpm"]) == ("high", 100, 105, 100)


def test_missing_optionals_are_none():
    row = _hae_hr_events_to_rows([{"start": "2026-08-31 11:21:00 +0300"}], USER)[0]
    for key in ("ended_at", "event_type", "threshold_bpm", "min_bpm", "max_bpm", "avg_bpm", "duration_min", "device"):
        assert row[key] is None, key


def test_zero_bpm_becomes_none():
    """Ноль ударов в минуту не бывает — так HAE помечает отсутствие значения."""
    rec = {"start": "2026-08-31 11:21:00 +0300", "minHeartRate": 0, "maxHeartRate": 0}
    row = _hae_hr_events_to_rows([rec], USER)[0]
    assert row["min_bpm"] is None and row["max_bpm"] is None


def test_event_without_start_skipped():
    assert _hae_hr_events_to_rows([{"type": "high"}], USER) == []
    assert _hae_hr_events_to_rows([{"start": "не дата"}], USER) == []


def test_non_dict_entries_ignored():
    assert _hae_hr_events_to_rows(["строка", None, 7], USER) == []


def test_source_stable_between_exports():
    first = _hae_hr_events_to_rows([FULL], USER)[0]["source"]
    second = _hae_hr_events_to_rows([FULL], USER)[0]["source"]
    assert first == second == "hae_hrn_2026-08-31T11:21:00+03:00"


def test_three_morning_events_parsed():
    """Ровно тот случай, ради которого канал и делается: три эпизода за утро."""
    events = [
        dict(FULL, start="2026-08-31 11:21:00 +0300", end="2026-08-31 11:31:00 +0300", maxHeartRate=110),
        dict(FULL, start="2026-08-31 11:39:00 +0300", end="2026-08-31 11:49:00 +0300", maxHeartRate=105),
        dict(FULL, start="2026-08-31 12:29:00 +0300", end="2026-08-31 12:39:00 +0300", maxHeartRate=107),
    ]
    rows = _hae_hr_events_to_rows(events, USER)
    assert [r["max_bpm"] for r in rows] == [110, 105, 107]
    assert len({r["source"] for r in rows}) == 3


def test_long_type_truncated():
    rec = {"start": "2026-08-31 11:21:00 +0300", "type": "т" * 100}
    assert len(_hae_hr_events_to_rows([rec], USER)[0]["event_type"]) == 32
