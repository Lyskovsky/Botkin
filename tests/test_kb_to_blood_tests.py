# tests/test_kb_to_blood_tests.py
"""_extract_rows: импорт KB-секций в Postgres blood_tests (чистая функция, без SSH/БД).

Фокус issue #95: секция biochemistry должна импортироваться, а US-записи —
нести признак единиц _unit_system, чтобы to_canonical сконвертировал на чтении.
"""

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "kb_to_blood_tests", ROOT / "scripts" / "import" / "kb_to_blood_tests.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mod = _load_module()


def test_biochemistry_section_is_extracted():
    kb = {
        "biochemistry": [
            {"date": "2026-06-09", "lab": "maccabi", "units": "US (mg/dl)", "values": {"albumin": 5.1, "ALKP": 55}},
        ]
    }
    rows = list(mod._extract_rows(kb, 42))
    assert len(rows) == 1
    assert rows[0]["user_id"] == 42
    assert rows[0]["test_type"] == "biochemistry"
    assert rows[0]["test_date"] == "2026-06-09"


def test_us_entry_carries_unit_system_flag():
    kb = {"biochemistry": [{"date": "2026-06-09", "units": "US (mg/dl)", "values": {"albumin": 5.1}}]}
    rows = list(mod._extract_rows(kb, 42))
    assert rows[0]["values"]["_unit_system"] == "US"


def test_metric_entry_has_no_unit_system_flag():
    # Запись без поля units (метрическая, напр. gnokb) — признак не инжектится.
    kb = {"biochemistry": [{"date": "2023-06-29", "lab": "gnokb", "values": {"creatinine": 87, "glucose": 4.84}}]}
    rows = list(mod._extract_rows(kb, 42))
    assert "_unit_system" not in rows[0]["values"]


def test_extracted_us_row_canonizes_to_metric():
    # End-to-end: извлечённая US-строка → to_canonical → метрика (read-time pipeline).
    from core.health.kb_schema import to_canonical

    kb = {"biochemistry": [{"date": "2026-06-09", "units": "US (mg/dl)", "values": {"albumin": 5.1, "ALKP": 55}}]}
    rows = list(mod._extract_rows(kb, 42))
    canon, _ = to_canonical(rows[0]["values"])
    assert abs(canon["albumin_g_l"] - 51.0) < 1e-6
    assert canon["ALP"] == 55


def test_units_less_gdl_panel_detected_as_us():
    # maccabi CBC: единицы заданы инлайновыми _ref, поля units нет; hemoglobin в g/dL (#295).
    kb = {
        "blood_tests": [
            {
                "date": "2026-06-09",
                "lab": "maccabi",
                "values": {"hemoglobin": 15.5, "hemoglobin_ref": "13.5-18", "MCHC": 35.1, "hematocrit": 44.1},
            }
        ]
    }
    rows = list(mod._extract_rows(kb, 42))
    assert rows[0]["values"]["_unit_system"] == "US"


def test_metric_cbc_not_flagged_us():
    # Метрический CBC (Hb 3-значный г/л, без units) — не помечаем.
    kb = {"blood_tests": [{"date": "2026-06-09", "values": {"hemoglobin": 155, "hematocrit": 44}}]}
    rows = list(mod._extract_rows(kb, 42))
    assert "_unit_system" not in rows[0]["values"]
