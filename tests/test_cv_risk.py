"""Тесты для core/health/cv_risk.py (SCORE2 + ASCVD lifetime).

Закрывают дыру тестируемости клинических калькуляторов риска (модуль был без
тестов) И страхуют behavior-preserving рефактор «расчленить нутро». Снимки
пиннят ТЕКУЩИЙ вывод (включая квирк age≥70 в ASCVD — он фиксируется отдельной
задачей, не этим рефактором). Property-тесты ловят регрессии в монотонности и
классификации без привязки к точным числам.
"""

import pytest

from core.health.cv_risk import calc_score2, calc_ascvd_lifetime


# базовые «здоровые» входы, от которых отклоняемся в property-тестах
S2_BASE = dict(age=50, sex="male", smoking=False, sbp_mmhg=120, tchol_mmolL=5.0, hdl_mmolL=1.4)
ASCVD_BASE = dict(age=55, sex="male", smoking=False, sbp_mmhg=115, tchol_mmolL=4.5, hdl_mmolL=1.4)


# ── SCORE2: характеризующие снапшоты (текущее поведение) ───────────────────────
@pytest.mark.parametrize(
    "kw,expected",
    [
        (
            dict(age=50, sex="male", smoking=True, sbp_mmhg=140, tchol_mmolL=6.3, hdl_mmolL=1.3),
            {"risk_pct": 7.2, "category": "moderate", "color": "y"},
        ),
        (
            dict(age=45, sex="male", smoking=False, sbp_mmhg=120, tchol_mmolL=5.0, hdl_mmolL=1.5),
            {"risk_pct": 1.1, "category": "low", "color": "g"},
        ),
        (
            dict(age=60, sex="female", smoking=False, sbp_mmhg=130, tchol_mmolL=5.5, hdl_mmolL=1.6),
            {"risk_pct": 5.4, "category": "moderate", "color": "y"},
        ),
    ],
)
def test_score2_snapshot(kw, expected):
    r = calc_score2(**kw)
    assert r is not None
    assert {k: r[k] for k in expected} == expected


@pytest.mark.parametrize("age", [39, 70, 80])
def test_score2_none_outside_age_range(age):
    # SCORE2 определён только для 40-69 (70+ → SCORE2-OP, не реализован)
    assert calc_score2(**{**S2_BASE, "age": age}) is None


@pytest.mark.parametrize("field", ["age", "sbp_mmhg", "tchol_mmolL", "hdl_mmolL"])
def test_score2_none_on_missing_input(field):
    assert calc_score2(**{**S2_BASE, field: 0}) is None


def test_score2_smoking_increases_risk():
    base = calc_score2(**S2_BASE)["risk_pct"]
    smoker = calc_score2(**{**S2_BASE, "smoking": True})["risk_pct"]
    assert smoker > base


@pytest.mark.parametrize("field,worse", [("sbp_mmhg", 160), ("tchol_mmolL", 7.0), ("age", 65)])
def test_score2_risk_monotonic_up(field, worse):
    base = calc_score2(**S2_BASE)["risk_pct"]
    assert calc_score2(**{**S2_BASE, field: worse})["risk_pct"] > base


def test_score2_higher_hdl_lowers_risk():
    base = calc_score2(**S2_BASE)["risk_pct"]
    assert calc_score2(**{**S2_BASE, "hdl_mmolL": 2.0})["risk_pct"] < base


def test_score2_category_thresholds_depend_on_age():
    # <50 и ≥50 используют РАЗНЫЕ пороги категорий (ESC): один и тот же профиль
    # риска может дать разные категории по разные стороны 50 лет. Проверяем, что
    # функция возвращает валидную категорию/цвет в обоих случаях.
    young = calc_score2(**{**S2_BASE, "age": 45})
    old = calc_score2(**{**S2_BASE, "age": 55})
    assert young["category"] in {"low", "moderate", "high"}
    assert old["category"] in {"low", "moderate", "high"}
    assert young["color"] in {"g", "y", "r"} and old["color"] in {"g", "y", "r"}


def test_score2_return_shape():
    r = calc_score2(**S2_BASE)
    assert set(r) >= {"risk_pct", "category", "category_ru", "color", "interpretation", "inputs"}


# ── ASCVD lifetime: характеризующие снапшоты ───────────────────────────────────
@pytest.mark.parametrize(
    "kw,expected",
    [
        (
            dict(age=55, sex="male", smoking=False, sbp_mmhg=115, tchol_mmolL=4.5, hdl_mmolL=1.4),
            {"risk_pct": 5.2, "category": "low", "color": "g"},
        ),
        (
            dict(
                age=55,
                sex="male",
                smoking=True,
                sbp_mmhg=145,
                tchol_mmolL=6.3,
                hdl_mmolL=0.9,
                diabetes=True,
                on_bp_meds=True,
            ),
            {"risk_pct": 69.1, "category": "very_high", "color": "r"},
        ),
        (
            dict(age=45, sex="female", smoking=True, sbp_mmhg=118, tchol_mmolL=4.5, hdl_mmolL=1.6),
            {"risk_pct": 41.1, "category": "high", "color": "o"},
        ),
        # age=72: 1 major (курение)=50.0 × 0.80 = 40.0. Фикс бага PR #242 (Igor-Lysk).
        (
            dict(age=72, sex="male", smoking=True, sbp_mmhg=118, tchol_mmolL=4.5, hdl_mmolL=1.4),
            {"risk_pct": 40.0, "category": "high", "color": "o"},
        ),
    ],
)
def test_ascvd_snapshot(kw, expected):
    r = calc_ascvd_lifetime(**kw)
    assert r is not None
    assert {k: r[k] for k in expected} == expected


@pytest.mark.parametrize("age", [19, 80])
def test_ascvd_none_outside_age_range(age):
    assert calc_ascvd_lifetime(**{**ASCVD_BASE, "age": age}) is None


@pytest.mark.parametrize("field", ["age", "sbp_mmhg", "tchol_mmolL", "hdl_mmolL"])
def test_ascvd_none_on_missing_input(field):
    assert calc_ascvd_lifetime(**{**ASCVD_BASE, field: 0}) is None


def test_ascvd_more_major_factors_increase_risk():
    optimal = calc_ascvd_lifetime(**ASCVD_BASE)["risk_pct"]
    one_major = calc_ascvd_lifetime(**{**ASCVD_BASE, "smoking": True})["risk_pct"]
    two_major = calc_ascvd_lifetime(**{**ASCVD_BASE, "smoking": True, "diabetes": True})["risk_pct"]
    assert optimal < one_major <= two_major


def test_ascvd_on_bp_meds_classifies_sbp_major():
    r = calc_ascvd_lifetime(**{**ASCVD_BASE, "on_bp_meds": True})
    assert r["factors"]["sbp"] == "major"
    assert r["n_major"] >= 1


@pytest.mark.parametrize(
    "risk_inputs,expected_color",
    [
        (ASCVD_BASE, "g"),  # optimal → low/g
        (
            dict(
                age=55,
                sex="male",
                smoking=True,
                sbp_mmhg=145,
                tchol_mmolL=6.3,
                hdl_mmolL=0.9,
                diabetes=True,
                on_bp_meds=True,
            ),
            "r",
        ),  # very_high → r
    ],
)
def test_ascvd_color_matches_category(risk_inputs, expected_color):
    assert calc_ascvd_lifetime(**risk_inputs)["color"] == expected_color


def test_ascvd_return_shape():
    r = calc_ascvd_lifetime(**ASCVD_BASE)
    assert set(r) >= {
        "risk_pct",
        "category",
        "category_ru",
        "color",
        "interpretation",
        "factors",
        "n_major",
        "n_elevated",
        "inputs",
    }
