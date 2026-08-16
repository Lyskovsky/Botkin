# tests/test_kb_schema.py
import math
from core.health.kb_schema import to_canonical, CANONICAL, reverse_index


def test_alexander_camelcase_keys_map():
    canon, warnings = to_canonical({"LDL": 3.1, "ALT": 22, "TSH": 2.0})
    assert canon["LDL"] == 3.1
    assert canon["ALT"] == 22
    assert canon["TSH"] == 2.0
    assert warnings == []


def test_dima_snake_with_units_map_same_unit():
    canon, _ = to_canonical({"ldl_mmol_l": 3.1, "glucose_mmol_l": 5.0, "creatinine_umol_l": 90, "alt_u_l": 22})
    assert canon["LDL"] == 3.1
    assert canon["glucose"] == 5.0
    assert canon["creatinine"] == 90
    assert canon["ALT"] == 22


def test_case_insensitive():
    canon, _ = to_canonical({"alt": 30})
    assert canon["ALT"] == 30


def test_unit_conversion_insulin_pmol_to_uiu():
    # 100 pmol/L ÷ 6.945 ≈ 14.4 µIU/mL
    canon, _ = to_canonical({"insulin_pmol_l": 100.0})
    assert math.isclose(canon["insulin"], 100.0 / 6.945, rel_tol=1e-6)


def test_unit_conversion_folate_pth_b12():
    canon, _ = to_canonical({"folate_nmol_l": 20.0, "pth_pmol_l": 5.0, "vitamin_b12_pmol_l": 300.0})
    assert math.isclose(canon["folic_acid"], 20.0 / 2.266, rel_tol=1e-6)
    assert math.isclose(canon["PTH_intact"], 5.0 * 9.434, rel_tol=1e-6)
    assert math.isclose(canon["vitamin_B12"], 300.0 * 1.355, rel_tol=1e-6)


def test_albumin_pct_not_collapsed_into_albumin_g_l():
    # albumin_pct is a DIFFERENT marker (electrophoresis %), must NOT map to albumin_g_l
    canon, warnings = to_canonical({"albumin_pct": 60.0, "albumin_g_l": 42.0})
    assert canon["albumin_g_l"] == 42.0
    assert "albumin_pct" not in canon
    assert "albumin_pct" not in reverse_index()  # not a registered alias
    assert any("albumin_pct" in w for w in warnings)


def test_unknown_unit_alias_skipped_with_warning():
    canon, warnings = to_canonical({"insulin_some_weird_unit": 5.0})
    assert "insulin_some_weird_unit" not in canon
    assert canon == {}
    assert any("insulin_some_weird_unit" in w for w in warnings)


def test_passthrough_unmapped():
    canon, _ = to_canonical({"made_up_marker_zzz": 3.0}, passthrough_unmapped=True)
    assert canon["made_up_marker_zzz"] == 3.0  # passed through untouched


def test_no_passthrough_drops_unmapped():
    canon, _ = to_canonical({"made_up_marker_zzz": 3.0})
    assert "made_up_marker_zzz" not in canon


def test_collision_same_value_no_warning():
    canon, warnings = to_canonical({"LDL": 3.1, "ldl": 3.1})
    assert canon["LDL"] == 3.1
    assert warnings == []


def test_collision_different_value_warns():
    canon, warnings = to_canonical({"LDL": 3.1, "ldl": 9.9})
    assert any("LDL" in w for w in warnings)


def test_reverse_index_has_no_duplicate_aliases():
    # Invariant: each lowercased alias belongs to exactly one canonical key.
    seen = {}
    for canon_key, marker in CANONICAL.items():
        for alias in marker.aliases:
            a = alias.lower()
            assert a not in seen, f"duplicate alias {alias!r}: {seen.get(a)} vs {canon_key}"
            seen[a] = canon_key


def test_non_numeric_value_skipped():
    canon, _ = to_canonical({"LDL": "n/a"})
    assert "LDL" not in canon


def test_extended_markers_dima_explicit_units_map():
    canon, _ = to_canonical(
        {
            "hct_pct": 47.3,
            "mch_pg": 29.9,
            "mchc_g_l": 345,
            "total_protein_g_l": 67.8,
            "fibrinogen_g_l": 2.3,
            "aptt_sec": 25.2,
            "dihydrotestosterone_pg_ml": 582,
            "anti_tpo_iu_ml": 0.22,
            "vitamin_a_ug_ml": 0.584,
            "amylase_pancreatic_u_l": 14,
            "testosterone_free_pmol_l": 99.5,
            "neutrophils_seg_pct": 42,
            "monocytes_pct": 8,
        }
    )
    assert canon["HCT"] == 47.3
    assert canon["MCH"] == 29.9
    assert canon["testosterone_free"] == 99.5
    assert canon["DHT"] == 582
    assert canon["neutrophils"] == 42
    assert canon["amylase"] == 14


def test_ambiguous_bare_keys_not_mapped():
    # bare HCT (fraction), bare neutrophils (absolute), Alexander free_testosterone (wrong scale)
    # must NOT be mapped — guarded against silent mis-scaling.
    canon, _ = to_canonical({"HCT": 0.451, "neutrophils": 3.03, "free_testosterone": 19.4})
    assert "HCT" not in canon
    assert "neutrophils" not in canon
    assert canon == {}


# ── US-единицы (issue #95: панель Маккаби в g/dL · mg/dL) ────────────────────


def test_alkp_alias_maps_to_alp():
    # ALKP (Maccabi) — щелочная фосфатаза, должна маппиться в ALP.
    canon, _ = to_canonical({"ALKP": 55})
    assert canon["ALP"] == 55


def test_us_conversion_via_unit_system_param():
    # US-панель: g/dL · mg/dL → метрика. unit_system передан явно.
    canon, _ = to_canonical(
        {"albumin": 5.1, "glucose": 86, "creatinine": 0.97, "iron": 109, "LDL": 127.4},
        unit_system="US",
    )
    assert math.isclose(canon["albumin_g_l"], 51.0, rel_tol=1e-6)  # 5.1 g/dL → 51 g/L (×10)
    assert math.isclose(canon["glucose"], 86 / 18.0156, rel_tol=1e-6)  # mg/dL → ммоль/л
    assert math.isclose(canon["creatinine"], 0.97 * 88.42, rel_tol=1e-6)  # mg/dL → мкмоль/л
    assert math.isclose(canon["iron"], 109 * 0.1791, rel_tol=1e-6)  # µg/dL → мкмоль/л
    assert math.isclose(canon["LDL"], 127.4 / 38.67, rel_tol=1e-6)  # mg/dL → ммоль/л


def test_us_conversion_via_values_carrier_key():
    # _unit_system в самом values (как доезжает из Postgres JSONB) включает конверсию;
    # сам служебный ключ не попадает в canon.
    canon, _ = to_canonical({"_unit_system": "US", "albumin": 5.1})
    assert math.isclose(canon["albumin_g_l"], 51.0, rel_tol=1e-6)
    assert "_unit_system" not in canon


def test_metric_unchanged_without_unit_system():
    # Анти-регресс: без признака US ничего не конвертируется (метрический KB).
    canon, _ = to_canonical({"albumin_g_l": 42.0, "glucose": 5.3, "creatinine": 92})
    assert canon["albumin_g_l"] == 42.0
    assert canon["glucose"] == 5.3
    assert canon["creatinine"] == 92


def test_us_same_unit_analyte_unchanged():
    # Аналиты с одинаковой единицей в US и метрике (Ед/л, %) под US не меняются.
    canon, _ = to_canonical(
        {"_unit_system": "US", "ALKP": 55, "ALT": 15},
    )
    assert canon["ALP"] == 55
    assert canon["ALT"] == 15


def test_explicit_unit_system_overrides_carrier():
    # Явный параметр важнее ключа в values.
    canon, _ = to_canonical({"_unit_system": "US", "albumin_g_l": 42.0}, unit_system="metric")
    assert canon["albumin_g_l"] == 42.0


# --- Hb/MCHC: US-конверсия по флагу + read-time g/dL-guard (#295) ---


def test_us_conversion_hemoglobin_mchc():
    # US-панель: Hb/MCHC в g/dL → г/л (×10) через US_TO_METRIC.
    canon, _ = to_canonical({"_unit_system": "US", "hemoglobin": 15.5, "MCHC": 35.1})
    assert math.isclose(canon["Hb"], 155.0, rel_tol=1e-6)
    assert math.isclose(canon["MCHC"], 351.0, rel_tol=1e-6)


def test_gdl_guard_hemoglobin_without_unit_system():
    # maccabi-панель без _unit_system="US": Hb 15.5 физиологически = g/dL → ×10 на чтении.
    canon, warnings = to_canonical({"hemoglobin": 15.5})
    assert math.isclose(canon["Hb"], 155.0, rel_tol=1e-6)
    assert any("Hb" in w for w in warnings)  # трансформация не молча


def test_gdl_guard_mchc_without_unit_system():
    canon, warnings = to_canonical({"MCHC": 35.1})
    assert math.isclose(canon["MCHC"], 351.0, rel_tol=1e-6)
    assert any("MCHC" in w for w in warnings)


def test_metric_hemoglobin_not_touched():
    # Анти-регресс: нормальный метрический Hb (3-значный г/л) не трогаем.
    canon, warnings = to_canonical({"Hb": 155})
    assert canon["Hb"] == 155
    assert not any("Hb" in w for w in warnings)


def test_us_hemoglobin_not_double_converted():
    # US ×10 уже дал 155 → guard (порог 30) не срабатывает повторно.
    canon, _ = to_canonical({"_unit_system": "US", "hemoglobin": 15.5})
    assert math.isclose(canon["Hb"], 155.0, rel_tol=1e-6)


def test_gdl_guard_boundary_hemoglobin_low_metric():
    # Hb 130 г/л (нижняя граница нормы) — метрика, guard не трогает.
    canon, _ = to_canonical({"Hb": 130})
    assert canon["Hb"] == 130
