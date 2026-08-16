"""Characterization + invariant tests for core/reports/biomarker_dynamics.

Написаны как защита рефактора «консолидация MARKER_CONFIG на kb_schema»
(убрать параллельный мост `_KB_CANON_TO_LOCAL`). Утверждения сформулированы
через label/unit/ref — инвариантно к тому, какими ключами (локальными lower
или kb-каноном) индексируется MARKER_CONFIG, поэтому остаются зелёными и до,
и после рефактора. Плюс инвариант: ключи MARKER_CONFIG ⊆ kb_schema.CANONICAL
(это и есть гарантия отсутствия 4-го параллельного реестра).
"""

import json

import pytest

from core.reports.biomarker_dynamics import (
    MARKER_CONFIG,
    PRIORITY_ORDER,
    _collect_series,
    resolve_marker_key,
    render_single_marker_png,
)


def _label(panel_key):
    return MARKER_CONFIG[panel_key]["label"]


def _labels_of_series(series):
    return {_label(k): pts for k, pts in series.items()}


# ── resolve_marker_key: вход → ожидаемый label панели ──────────────────────────
# Покрывает canon-ключ, kb-алиас, русские синонимы, label, и merge (hs_CRP→СРБ).
RESOLVE_CASES = [
    ("glucose", "Глюкоза"),
    ("Глюкоза", "Глюкоза"),
    ("сахар", "Глюкоза"),
    ("сахар крови", "Глюкоза"),
    ("LDL", "ЛПНП"),
    ("ldl_mmol_l", "ЛПНП"),
    ("лпнп", "ЛПНП"),
    ("плохой холестерин", "ЛПНП"),
    ("HbA1c", "HbA1c"),
    ("гликированный", "HbA1c"),
    ("ferritin", "Ферритин"),
    ("железо", "Железо"),
    ("hs_CRP", "СРБ"),  # high-sensitivity CRP сливается в панель СРБ
    ("СРБ", "СРБ"),
    ("витамин д", "Витамин D"),
    ("vit d", "Витамин D"),
    ("psa_total", "ПСА"),
    ("гематокрит", "Гематокрит"),
]


@pytest.mark.parametrize("query,expected_label", RESOLVE_CASES)
def test_resolve_marker_key_maps_to_expected_panel(query, expected_label):
    key = resolve_marker_key(query)
    assert key is not None, f"{query!r} should resolve"
    assert _label(key) == expected_label


@pytest.mark.parametrize("junk", ["", "   ", "не маркер", "xyzzy", "печень"])
def test_resolve_marker_key_returns_none_for_unknown(junk):
    assert resolve_marker_key(junk) is None


# ── _collect_series: канонизация + merge через kb_schema ───────────────────────
def test_collect_series_groups_aliases_and_merges_hs_crp():
    kb = {
        "blood_tests": [
            {"date": "2024-01-01", "values": {"glucose": 5.0, "LDL": 3.2, "CRP": 3.0}},
            {"date": "2024-06-01", "values": {"glucose_mmol_l": 5.5, "ldl_mmol_l": 2.8, "hs_CRP": 1.5}},
        ],
        "vitamins": [{"date": "2024-03-01", "values": {"vitamin_D": 35}}],
    }
    labels = _labels_of_series(_collect_series(kb))

    # разные написания одного маркера сведены в одну серию
    assert len(labels["Глюкоза"]) == 2
    assert [v for _, v in labels["Глюкоза"]] == [5.0, 5.5]  # отсортировано по дате
    assert len(labels["ЛПНП"]) == 2
    # CRP (date1) и hs_CRP (date2) попадают в ОДНУ панель «СРБ» (merge)
    assert len(labels["СРБ"]) == 2
    assert len(labels["Витамин D"]) == 1


def test_collect_series_averages_same_date_duplicates():
    # один маркер в двух секциях за одну дату → среднее
    kb = {
        "blood_tests": [{"date": "2024-01-01", "values": {"glucose": 5.0}}],
        "biochemistry": [{"date": "2024-01-01", "values": {"glucose": 6.0}}],
    }
    labels = _labels_of_series(_collect_series(kb))
    assert labels["Глюкоза"] == [("2024-01-01", 5.5)]


# ── render: PNG для валидного, dict-ошибка для неизвестного ────────────────────
def test_render_single_marker_returns_png_bytes(tmp_path):
    kb = {
        "blood_tests": [
            {"date": "2024-01-01", "values": {"glucose": 5.0}},
            {"date": "2024-06-01", "values": {"glucose": 5.4}},
        ]
    }
    p = tmp_path / "kb.json"
    p.write_text(json.dumps(kb), encoding="utf-8")
    out = render_single_marker_png(p, "глюкоза")
    assert isinstance(out, bytes) and out[:4] == b"\x89PNG"


def test_render_single_marker_unknown_returns_error(tmp_path):
    p = tmp_path / "kb.json"
    p.write_text(json.dumps({"blood_tests": []}), encoding="utf-8")
    out = render_single_marker_png(p, "xyzzy")
    assert isinstance(out, dict) and out["error"] == "unknown-marker"
    assert "available" in out and out["available"]


# ── ИНВАРИАНТ консолидации: никакого 4-го реестра ──────────────────────────────
def test_marker_config_keys_are_kb_schema_canonical():
    """Каждый ключ панели — каноническое имя из kb_schema (а не свой namespace)."""
    from core.health.kb_schema import CANONICAL

    unknown = [k for k in MARKER_CONFIG if k not in CANONICAL]
    assert not unknown, f"ключи MARKER_CONFIG вне kb_schema.CANONICAL: {unknown}"


def test_priority_order_matches_marker_config():
    assert set(PRIORITY_ORDER) == set(MARKER_CONFIG)
