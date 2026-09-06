"""Тесты парсера CSV-экспорта LibreView (scripts/import/libreview_csv.py).

Парсер чистый (без БД) — проверяем форматы, единицы, порядок день/месяц, TZ→UTC,
фильтрацию не-глюкозных записей. import_libreview_csv (с psycopg2) тут не тестируем.

scripts/import/ — не пакет (import зарезервирован), грузим модуль через importlib
(как core/health/glucose_runtime.py).
"""

import importlib.util
import sys
from datetime import timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

_PATH = Path(__file__).resolve().parent.parent / "scripts" / "import" / "libreview_csv.py"
_spec = importlib.util.spec_from_file_location("libreview_csv_test", _PATH)
_mod = importlib.util.module_from_spec(_spec)
sys.modules["libreview_csv_test"] = _mod
_spec.loader.exec_module(_mod)

parse_libreview_csv = _mod.parse_libreview_csv
LibreViewParseError = _mod.LibreViewParseError

MSK = ZoneInfo("Europe/Moscow")


def _csv(header_unit="mmol/L", rows=None):
    lines = [
        "Имя пациента,Тестова Тест",
        f"Device,Serial Number,Device Timestamp,Record Type,Historic Glucose {header_unit},"
        f"Scan Glucose {header_unit},Notes",
    ]
    lines += rows or []
    return "\n".join(lines) + "\n"


def test_parses_historic_and_scan_mmol():
    content = _csv(
        rows=[
            "FreeStyle Libre 3,A1,15-06-2026 08:00,0,5.4,,",
            "FreeStyle Libre 3,A1,15-06-2026 08:15,0,5.6,,",
            "FreeStyle Libre 3,A1,15-06-2026 08:20,1,,6.1,",
        ]
    )
    rows, meta = parse_libreview_csv(content, MSK)
    assert meta["unit"] == "mmol"
    assert meta["glucose_points"] == 3
    assert [r["value"] for r in rows] == [5.4, 5.6, 6.1]
    # 08:00 МСК = 05:00 UTC
    assert rows[0]["ts"].astimezone(timezone.utc).hour == 5


def test_mgdl_converted_to_mmol():
    content = _csv(
        header_unit="mg/dL",
        rows=["FreeStyle Libre 3,A1,15-06-2026 08:00,0,90,,"],  # 90 mg/dL ≈ 5.0 mmol/L
    )
    rows, meta = parse_libreview_csv(content, MSK)
    assert meta["unit"] == "mgdl"
    assert rows[0]["value"] == pytest.approx(5.0, abs=0.05)


def test_skips_non_glucose_records():
    content = _csv(
        rows=[
            "FreeStyle Libre 3,A1,15-06-2026 08:00,0,5.4,,",
            "FreeStyle Libre 3,A1,15-06-2026 08:05,4,,,",  # инсулин
            "FreeStyle Libre 3,A1,15-06-2026 08:10,5,,,",  # углеводы
            "FreeStyle Libre 3,A1,15-06-2026 08:15,6,,,заметка",  # note
        ]
    )
    rows, meta = parse_libreview_csv(content, MSK)
    assert meta["glucose_points"] == 1
    assert meta["skipped_non_glucose"] == 3


def test_detect_day_first_when_day_gt_12():
    content = _csv(rows=["FreeStyle Libre 3,A1,25-06-2026 08:00,0,5.4,,"])
    rows, meta = parse_libreview_csv(content, MSK)
    assert meta["day_first"] is True
    assert rows[0]["ts"].astimezone(MSK).month == 6
    assert rows[0]["ts"].astimezone(MSK).day == 25


def test_detect_month_first_when_second_gt_12():
    content = _csv(rows=["FreeStyle Libre 3,A1,06-25-2026 08:00,0,5.4,,"])
    rows, meta = parse_libreview_csv(content, MSK)
    assert meta["day_first"] is False
    assert rows[0]["ts"].astimezone(MSK).month == 6
    assert rows[0]["ts"].astimezone(MSK).day == 25


def test_dedup_by_ts():
    content = _csv(
        rows=[
            "FreeStyle Libre 3,A1,15-06-2026 08:00,0,5.4,,",
            "FreeStyle Libre 3,A1,15-06-2026 08:00,1,,6.0,",
        ]
    )
    rows, meta = parse_libreview_csv(content, MSK)
    assert meta["glucose_points"] == 1


def test_not_libreview_raises():
    with pytest.raises(LibreViewParseError):
        parse_libreview_csv("foo,bar\n1,2\n", MSK)


def test_empty_data_returns_empty():
    content = _csv(rows=[])
    rows, meta = parse_libreview_csv(content, MSK)
    assert rows == []
    assert meta["glucose_points"] == 0
    assert meta["first_ts"] is None


def test_russian_localized_headers():
    """Аккаунт RU-локали: заголовки на русском — должны распознаться (язык не важен)."""
    content = (
        "Имя пациента,Тестова Тест\n"
        "Прибор,Серийный номер,Метка времени устройства,Тип записи,"
        "Глюкоза за прошлый период ммоль/л,Глюкоза при сканировании ммоль/л,Заметки\n"
        "FreeStyle Libre 3,A1,15-06-2026 08:00,0,5.4,,\n"
        "FreeStyle Libre 3,A1,15-06-2026 08:20,1,,6.1,\n"
    )
    rows, meta = parse_libreview_csv(content, MSK)
    assert meta["unit"] == "mmol"
    assert meta["glucose_points"] == 2
    assert [r["value"] for r in rows] == [5.4, 6.1]


def test_russian_mgdl_unit():
    content = (
        "Имя пациента,Ника\n"
        "Прибор,Серийный номер,Метка времени устройства,Тип записи,"
        "Глюкоза за прошлый период мг/дл,Глюкоза при сканировании мг/дл\n"
        "FreeStyle Libre 3,A1,15-06-2026 08:00,0,90,\n"
    )
    rows, meta = parse_libreview_csv(content, MSK)
    assert meta["unit"] == "mgdl"
    assert rows[0]["value"] == pytest.approx(5.0, abs=0.05)


def test_semicolon_delimiter_and_comma_decimal():
    content = (
        "Имя пациента;Ника\n"
        "Device;Serial Number;Device Timestamp;Record Type;Historic Glucose mmol/L;Scan Glucose mmol/L\n"
        "FreeStyle Libre 3;A1;15-06-2026 08:00;0;5,4;\n"
    )
    rows, meta = parse_libreview_csv(content, MSK)
    assert rows[0]["value"] == 5.4


def test_russian_retrospective_historic_header():
    """Реальный RU-экспорт Abbott: historic = «Ретроспективные данные по глюкозе ммоль/л».

    Регрессия (живой файл Андрея): матч только «за прошлый период» НЕ ловил «ретроспективные»
    → все historic-замеры (Record Type 0) отбрасывались, оставались одни сканы (~11% точек).
    """
    content = (
        "Данные по уровню глюкозы,Дата формирования отчета,03-05-2026 20:52 UTC,Автор,Тест\n"
        "Устройство,Серийный номер,Отметка времени устройства,Тип записи,"
        "Ретроспективные данные по глюкозе ммоль/л,Сканирование уровня глюкозы ммоль/л,Примечания\n"
        'FreeStyle Libre 2,A1,12-12-2025 16:39,0,"5,1",,\n'
        'FreeStyle Libre 2,A1,12-12-2025 16:54,0,"5,2",,\n'
        'FreeStyle Libre 2,A1,12-12-2025 17:00,1,,"4,7",\n'
        "FreeStyle Libre 2,A1,12-12-2025 17:05,6,,,заметка\n"
    )
    rows, meta = parse_libreview_csv(content, MSK)
    assert meta["unit"] == "mmol"
    assert meta["glucose_points"] == 3  # 2 historic + 1 scan, НЕ 1
    assert meta["skipped_non_glucose"] == 1  # только заметка
    assert [r["value"] for r in rows] == [5.1, 5.2, 4.7]
