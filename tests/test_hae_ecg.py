"""Тесты парсера ЭКГ Health Auto Export.

ЭКГ — отдельный тип экспорта, HAE присылает её в `data.ecg[]` собственным POST.
Apple отдаёт метаданные плюс ~15 000 точек вольтажа на 30-секундную запись;
`_hae_ecg_to_rows` оставляет только метаданные, сырой сигнал в базу не идёт.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "telegram-bot"))

from webhook.apple_health import _hae_ecg_to_rows  # noqa: E402

USER = 836757955

FULL = {
    "start": "2026-08-20 10:15:00 +0300",
    "end": "2026-08-20 10:15:30 +0300",
    "classification": "Sinus Rhythm",
    "averageHeartRate": {"qty": 72, "units": "bpm"},
    "samplingFrequency": {"qty": 512.4, "units": "Hz"},
    "numberOfVoltageMeasurements": 15360,
    "voltageMeasurements": [{"voltage": 0.1}, {"voltage": 0.2}],
    "symptoms": ["Skipped Heartbeat"],
    "device": "Apple Watch",
}


def test_empty_list_returns_empty():
    assert _hae_ecg_to_rows([], USER) == []


def test_full_record_parsed():
    row = _hae_ecg_to_rows([FULL], USER)[0]

    assert row["user_id"] == USER
    assert row["recorded_at"].isoformat() == "2026-08-20T10:15:00+03:00"
    assert row["classification"] == "Sinus Rhythm"
    assert row["average_heart_rate"] == 72
    assert row["duration_sec"] == 30
    assert row["sampling_hz"] == 512.4
    assert row["voltage_samples"] == 15360
    assert row["symptoms"] == "Skipped Heartbeat"
    assert row["device"] == "Apple Watch"


def test_raw_signal_is_not_stored():
    """Сырые вольтажи не должны попасть в строку — только их количество."""
    row = _hae_ecg_to_rows([FULL], USER)[0]

    assert "voltageMeasurements" not in row
    assert not any("voltage" in k and k != "voltage_samples" for k in row)


def test_samples_counted_from_array_when_no_counter():
    rec = {k: v for k, v in FULL.items() if k != "numberOfVoltageMeasurements"}
    assert _hae_ecg_to_rows([rec], USER)[0]["voltage_samples"] == 2


def test_duration_from_interval_when_missing():
    rec = {
        "start": "2026-08-20 10:15:00 +0300",
        "end": "2026-08-20 10:15:45 +0300",
        "classification": "sinusRhythm",
    }
    assert _hae_ecg_to_rows([rec], USER)[0]["duration_sec"] == 45


def test_duration_in_minutes_converted_to_seconds():
    rec = {"start": "2026-08-20 10:15:00 +0300", "duration": {"qty": 0.5, "units": "min"}}
    assert _hae_ecg_to_rows([rec], USER)[0]["duration_sec"] == 30


def test_bare_duration_treated_as_seconds():
    rec = {"start": "2026-08-20 10:15:00 +0300", "duration": 30}
    assert _hae_ecg_to_rows([rec], USER)[0]["duration_sec"] == 30


def test_classification_kept_verbatim():
    """Написание не нормализуем: список значений расширяется с watchOS."""
    for raw in ("sinusRhythm", "Sinus Rhythm", "atrialFibrillation", "Что-то новое"):
        rec = {"start": "2026-08-20 10:15:00 +0300", "classification": raw}
        assert _hae_ecg_to_rows([rec], USER)[0]["classification"] == raw


def test_alternative_field_names():
    rec = {
        "startDate": "2026-08-20 10:15:00 +0300",
        "rhythmClassification": "highHeartRate",
        "avgHeartRate": 121,
        "samplingRate": 512,
    }
    row = _hae_ecg_to_rows([rec], USER)[0]
    assert (row["classification"], row["average_heart_rate"], row["sampling_hz"]) == ("highHeartRate", 121, 512.0)


def test_symptoms_none_string_is_empty():
    """HAE пишет 'None' строкой, когда симптомов не было — это не симптом."""
    for raw in ("None", "none", "", []):
        rec = {"start": "2026-08-20 10:15:00 +0300", "symptoms": raw}
        assert _hae_ecg_to_rows([rec], USER)[0]["symptoms"] is None


def test_symptoms_list_joined():
    rec = {"start": "2026-08-20 10:15:00 +0300", "symptoms": ["Chest Tightness", "Dizziness"]}
    assert _hae_ecg_to_rows([rec], USER)[0]["symptoms"] == "Chest Tightness, Dizziness"


def test_missing_optionals_are_none():
    rec = {"start": "2026-08-20 10:15:00 +0300"}
    row = _hae_ecg_to_rows([rec], USER)[0]
    assert row["classification"] is None
    assert row["average_heart_rate"] is None
    assert row["duration_sec"] is None
    assert row["sampling_hz"] is None
    assert row["voltage_samples"] is None
    assert row["symptoms"] is None
    assert row["device"] is None


def test_record_without_start_skipped():
    assert _hae_ecg_to_rows([{"classification": "sinusRhythm"}], USER) == []
    assert _hae_ecg_to_rows([{"start": "не дата"}], USER) == []


def test_non_dict_entries_ignored():
    assert _hae_ecg_to_rows(["строка", None, 42], USER) == []


def test_source_from_id_when_present():
    rec = dict(FULL, id="ABC-123")
    assert _hae_ecg_to_rows([rec], USER)[0]["source"] == "hae_ecg_ABC-123"


def test_source_falls_back_to_start_and_is_stable():
    """Без id ключ дедупа строится из времени старта и одинаков между экспортами."""
    first = _hae_ecg_to_rows([FULL], USER)[0]["source"]
    second = _hae_ecg_to_rows([FULL], USER)[0]["source"]
    assert first == second == "hae_ecg_2026-08-20T10:15:00+03:00"


def test_long_classification_truncated():
    rec = {"start": "2026-08-20 10:15:00 +0300", "classification": "x" * 200}
    assert len(_hae_ecg_to_rows([rec], USER)[0]["classification"]) == 64


def test_several_records_parsed():
    second = dict(FULL, start="2026-08-21 09:00:00 +0300", end="2026-08-21 09:00:30 +0300")
    rows = _hae_ecg_to_rows([FULL, second], USER)
    assert len(rows) == 2
    assert rows[0]["source"] != rows[1]["source"]
