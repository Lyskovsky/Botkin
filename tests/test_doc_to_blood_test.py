"""build_blood_test_row: extracted из /doc → строка Postgres blood_tests (#281).

Чистая функция, без БД и сети. Проверяем три вещи, ради которых она есть:
  1. в values уезжают СЫРЫЕ ключи (канонизация — на чтении, как во всём репо);
  2. нелабораторные документы (УЗИ, заключение) в blood_tests не попадают;
  3. дата не выдумывается — нет даты в документе, нет строки.
"""

from core.health.doc_to_blood_test import build_blood_test_row

STORED = "2026-07-25_a1b2c3d4.pdf"


def _build(extracted, stored_name=STORED, user_id=42):
    return build_blood_test_row(extracted, stored_name=stored_name, user_id=user_id)


# ── коэрция значений ─────────────────────────────────────────────────────────


def test_strips_units_from_string_value():
    """«165 г/л» → 165.0: экстрактор обещает числа, но регулярно шлёт строки с единицей."""
    res = _build({"date": "2026-04-13", "values": {"Hb": "165 г/л"}})
    assert res.row is not None
    assert res.row["values"]["Hb"] == 165.0


def test_comma_decimal_separator():
    res = _build({"date": "2026-04-13", "values": {"LDL": "3,42 ммоль/л"}})
    assert res.row["values"]["LDL"] == 3.42


def test_value_with_second_number_is_rejected():
    """«120/80» — не биомаркер, а давление: молча взять 120 нельзя."""
    res = _build({"date": "2026-04-13", "values": {"Hb": 148, "blood_pressure": "120/80"}})
    assert "blood_pressure" not in res.row["values"]
    assert any("blood_pressure" in w for w in res.warnings)


def test_non_numeric_value_is_dropped_with_warning():
    """«<0.5» — не число; додумывать 0.5 или 0 нельзя."""
    res = _build({"date": "2026-04-13", "values": {"Hb": 148, "CRP": "<0.5"}})
    assert "CRP" not in res.row["values"]
    assert any("CRP" in w for w in res.warnings)


def test_bool_is_not_a_number():
    res = _build({"date": "2026-04-13", "values": {"Hb": 148, "fasting": True}})
    assert "fasting" not in res.row["values"]


# ── что попадает в строку ────────────────────────────────────────────────────


def test_row_shape_for_lab_document():
    res = _build(
        {"date": "2026-04-13", "laboratory": "KDL", "values": {"Hb": 148, "ferritin": 95, "LDL": 3.1}},
        user_id=895655,
    )
    assert res.reason == "ok"
    assert res.marker_count == 3
    row = res.row
    assert row["user_id"] == 895655
    assert row["test_date"] == "2026-04-13"
    assert row["status"] == "current"
    assert row["file_path"] == "data/uploads/895655/2026-07-25_a1b2c3d4.pdf"
    assert row["values"] == {"Hb": 148.0, "ferritin": 95.0, "LDL": 3.1}


def test_values_stay_raw_not_canonical():
    """Канонизация — на чтении. Сырой ключ hemoglobin не переименовывается в Hb."""
    res = _build({"date": "2026-04-13", "values": {"hemoglobin": 148}})
    assert "hemoglobin" in res.row["values"]
    assert "Hb" not in res.row["values"]


def test_test_type_carries_lab_and_file_hash():
    res = _build({"date": "2026-04-13", "laboratory": "Хеликс", "values": {"Hb": 148}})
    assert "Хеликс" in res.row["test_type"]
    assert "a1b2c3d4" in res.row["test_type"]


def test_test_type_without_lab_has_default_label():
    res = _build({"date": "2026-04-13", "values": {"Hb": 148}})
    assert res.row["test_type"].startswith("документ")
    assert "a1b2c3d4" in res.row["test_type"]


def test_test_type_fits_column_and_keeps_hash():
    """test_type — VARCHAR(100). Обрезаем название лаборатории, но не хэш."""
    res = _build({"date": "2026-04-13", "laboratory": "Л" * 300, "values": {"Hb": 148}})
    assert len(res.row["test_type"]) <= 100
    assert res.row["test_type"].endswith("a1b2c3d4")


def test_same_file_same_key_twice():
    """Идемпотентность: повторная загрузка того же файла даёт тот же ключ upsert'а."""
    extracted = {"date": "2026-04-13", "laboratory": "KDL", "values": {"Hb": 148}}
    first, second = _build(extracted).row, _build(extracted).row
    assert (first["user_id"], first["test_date"], first["test_type"]) == (
        second["user_id"],
        second["test_date"],
        second["test_type"],
    )


def test_different_files_same_date_differ_by_type():
    extracted = {"date": "2026-04-13", "laboratory": "KDL", "values": {"Hb": 148}}
    a = _build(extracted, stored_name="2026-07-25_aaaaaaaa.pdf").row
    b = _build(extracted, stored_name="2026-07-25_bbbbbbbb.pdf").row
    assert a["test_type"] != b["test_type"]


# ── US-панели ────────────────────────────────────────────────────────────────


def test_us_panel_is_marked_for_read_time_conversion():
    """Hb=15.5 физиологически возможен только в g/dL → метим _unit_system, чтобы
    to_canonical сконвертировал на чтении (issue #95/#295)."""
    res = _build({"date": "2026-06-09", "laboratory": "Maccabi", "values": {"Hb": 15.5, "albumin": 5.1}})
    assert res.row["values"]["_unit_system"] == "US"


def test_metric_panel_is_not_marked():
    res = _build({"date": "2026-04-13", "values": {"Hb": 148}})
    assert "_unit_system" not in res.row["values"]


# ── когда строку писать нельзя ───────────────────────────────────────────────


def test_missing_date_yields_no_row():
    res = _build({"laboratory": "KDL", "values": {"Hb": 148}})
    assert res.row is None
    assert res.reason == "no_date"


def test_unparseable_date_yields_no_row():
    res = _build({"date": "апрель 2026", "values": {"Hb": 148}})
    assert res.row is None
    assert res.reason == "no_date"


def test_ultrasound_document_is_not_a_lab_row():
    """УЗИ: числа есть, но ни одного канонического биомаркера — в blood_tests не пишем."""
    res = _build({"date": "2026-04-19", "values": {"liver_right_lobe_mm": 145, "kidney_left_mm": 102}})
    assert res.row is None
    assert res.reason == "not_lab"


def test_document_without_values_yields_no_row():
    res = _build({"date": "2026-04-13", "values": {}, "conditions": ["Астма (J45.0)"]})
    assert res.row is None
    assert res.reason == "no_values"


def test_empty_extracted_yields_no_row():
    res = _build({})
    assert res.row is None
    assert res.reason == "no_values"
