"""
Калькуляторы кардиоваскулярного риска для дашборда:
  - SCORE2 (ESC 2021) — 10-летний риск ССЗ для Европы
  - ASCVD Lifetime Risk (AHA/ACC) — пожизненный риск

Все функции мульти-юзерные: принимают параметры, не делают I/O.
Если данных не хватает — возвращают None / структуру с явным "missing".

Публичный интерфейс — `calc_score2` и `calc_ascvd_lifetime` (возвращают dict,
который читает dashboard_generator). Внутренние шаги вынесены в именованные
`_*`-хелперы: каждый отвечает за один кусок расчёта и тестируем по отдельности.

Источники:
  SCORE2: https://academic.oup.com/eurheartj/article/42/25/2439/6297709
          (SCORE2 working group, Eur Heart J 2021;42:2439-54)
  ASCVD:  https://www.ahajournals.org/doi/10.1161/CIR.0000000000000678
          (Goff DC Jr et al. 2014 ACC/AHA Guideline; lifetime risk extrapolation)
"""

from __future__ import annotations

import math
from typing import Optional

# ─────────────────────────────────────────────────────────────────────
#  SCORE2 — 10-year fatal+nonfatal CVD risk
# ─────────────────────────────────────────────────────────────────────

# Коэффициенты SCORE2 для high-risk региона (Россия попадает сюда),
# мужчины, возраст 40-69. Источник: ESC SCORE2 supplementary table S5.
# Для женщин и low-risk регионов — другие константы (TODO для multi-user).
_SCORE2_HIGH_RISK_MALE = {
    # Линейные предикторы (бета-коэффициенты), age centered at 60, others standardized
    "age": 0.3742,
    "smoking": 0.6012,
    "sbp": 0.2777,  # per (sbp − 120) / 20 — стандартизация
    "tchol": 0.1458,  # per (tchol − 6.0) / 1.0 mmol/L
    "hdl": -0.2698,  # per (hdl − 1.3) / 0.5 mmol/L
    "smoking_age": -0.0755,  # interaction
    "sbp_age": -0.0255,
    "tchol_age": -0.0281,
    "hdl_age": 0.0426,
    # baseline survival at 10 years for high-risk male
    "S0": 0.9605,
    # Calibration scale + shape (recalibrated from baseline cohort to high-risk region)
    "scale1": 0.2436,
    "scale2": 1.0070,
}

_SCORE2_HIGH_RISK_FEMALE = {
    "age": 0.4648,
    "smoking": 0.7744,
    "sbp": 0.3131,
    "tchol": 0.1002,
    "hdl": -0.2606,
    "smoking_age": -0.1088,
    "sbp_age": -0.0277,
    "tchol_age": -0.0226,
    "hdl_age": 0.0613,
    "S0": 0.9776,
    "scale1": 0.5102,
    "scale2": 0.8851,
}

# Возрастные пороги категорий ESC: (порог_low, порог_moderate). <50 строже.
_SCORE2_THRESHOLDS_UNDER_50 = (2.5, 7.5)
_SCORE2_THRESHOLDS_50_PLUS = (5.0, 10.0)

_SCORE2_CAT_RU = {"low": "низкий", "moderate": "умеренный", "high": "высокий"}


def _score2_standardize(age: int, sbp_mmhg: float, tchol_mmolL: float, hdl_mmolL: float, smoking: bool):
    """Сырые величины → стандартизованные предикторы (как в оригинальной формуле)."""
    return {
        "age": (age - 60) / 5,
        "sbp": (sbp_mmhg - 120) / 20,
        "tchol": (tchol_mmolL - 6.0) / 1.0,
        "hdl": (hdl_mmolL - 1.3) / 0.5,
        "smoke": 1 if smoking else 0,
    }


def _score2_linear_predictor(coef: dict, z: dict) -> float:
    """Линейный предиктор (бета·предиктор + age-взаимодействия)."""
    return (
        coef["age"] * z["age"]
        + coef["smoking"] * z["smoke"]
        + coef["sbp"] * z["sbp"]
        + coef["tchol"] * z["tchol"]
        + coef["hdl"] * z["hdl"]
        + coef["smoking_age"] * z["smoke"] * z["age"]
        + coef["sbp_age"] * z["sbp"] * z["age"]
        + coef["tchol_age"] * z["tchol"] * z["age"]
        + coef["hdl_age"] * z["hdl"] * z["age"]
    )


def _score2_calibrate(lp: float, coef: dict) -> float:
    """Линейный предиктор → откалиброванный 10-летний риск в % (округл. до 0.1)."""
    raw_risk = 1 - coef["S0"] ** math.exp(lp)
    cal = 1 - math.exp(-math.exp(coef["scale1"] + coef["scale2"] * math.log(-math.log(1 - raw_risk))))
    return round(cal * 100, 1)


def _score2_category(risk_pct: float, age: int) -> tuple[str, str]:
    """Категория + цвет по возрастным порогам ESC."""
    low_thr, mod_thr = _SCORE2_THRESHOLDS_UNDER_50 if age < 50 else _SCORE2_THRESHOLDS_50_PLUS
    if risk_pct < low_thr:
        return "low", "g"
    if risk_pct < mod_thr:
        return "moderate", "y"
    return "high", "r"


def _score2_interpretation(risk_pct: float, category: str, cat_ru: str) -> str:
    text = f"10-летний риск инфаркта/инсульта = {risk_pct}%. Категория: {cat_ru}. "
    if category == "low":
        return text + "Статины сейчас не показаны, продолжай образ жизни."
    if category == "moderate":
        return text + "Если оптимизация образа жизни не снижает ApoB <0.9 / LDL <2.6 — обсудить статины."
    return text + "Статины показаны. Обсудить с кардиологом."


def calc_score2(
    *,
    age: int,
    sex: str,  # "male" | "female"
    smoking: bool,  # активный курильщик
    sbp_mmhg: float,  # систолическое давление
    tchol_mmolL: float,  # общий холестерин
    hdl_mmolL: float,  # ЛПВП
    region: str = "high",  # "low" / "moderate" / "high" / "very_high"
) -> Optional[dict]:
    """SCORE2 для людей 40-69. Для 70+ нужен SCORE2-OP (другая модель).

    Returns:
        {
            "risk_pct": float (0..100),
            "category": "low" | "moderate" | "high" | "very_high",
            "color": "g" | "y" | "o" | "r",
            "interpretation": str,
        }
        или None если данных не хватает / возраст вне диапазона.
    """
    if not all([age, sex, sbp_mmhg, tchol_mmolL, hdl_mmolL]):
        return None
    if not (40 <= age <= 69):
        # SCORE2-OP пока не реализован
        return None

    coef = _SCORE2_HIGH_RISK_MALE if sex == "male" else _SCORE2_HIGH_RISK_FEMALE
    z = _score2_standardize(age, sbp_mmhg, tchol_mmolL, hdl_mmolL, smoking)
    risk_pct = _score2_calibrate(_score2_linear_predictor(coef, z), coef)
    category, color = _score2_category(risk_pct, age)
    cat_ru = _SCORE2_CAT_RU[category]

    return {
        "risk_pct": risk_pct,
        "category": category,
        "category_ru": cat_ru,
        "color": color,
        "interpretation": _score2_interpretation(risk_pct, category, cat_ru),
        "inputs": {
            "age": age,
            "sex": sex,
            "smoking": smoking,
            "sbp": sbp_mmhg,
            "tchol": tchol_mmolL,
            "hdl": hdl_mmolL,
            "region": region,
        },
    }


# ─────────────────────────────────────────────────────────────────────
#  ASCVD Lifetime Risk (AHA/ACC)
# ─────────────────────────────────────────────────────────────────────

_ASCVD_CAT_RU = {"low": "низкий", "moderate": "умеренный", "high": "высокий", "very_high": "очень высокий"}


def _ascvd_classify_sbp(sbp: float, on_meds: bool) -> str:
    if on_meds:
        return "major"
    if sbp < 120:
        return "optimal"
    if sbp < 140:
        return "elevated"
    return "major"


def _ascvd_classify_chol(tc: float) -> str:
    if tc < 4.7:  # <180 mg/dL
        return "optimal"
    if tc < 5.2:  # 180-200
        return "not_optimal"
    if tc < 6.2:  # 200-239
        return "elevated"
    return "major"


def _ascvd_classify_hdl(hdl: float, threshold: float) -> str:
    # HDL: ниже = хуже (обратная шкала)
    if hdl >= threshold + 0.3:
        return "optimal"
    if hdl >= threshold:
        return "not_optimal"
    return "major"  # very low HDL = major


def _ascvd_base_risk(is_male: bool, n_major: int, n_elevated: int, n_not_optimal: int) -> float:
    """Базовый пожизненный риск из таблиц Lloyd-Jones JACC 2006 (baseline 50 лет)."""
    if is_male:
        if n_major >= 2:
            return 69.1
        if n_major == 1:
            return 50.0
        if n_elevated >= 1:
            return 39.6
        if n_not_optimal >= 1:
            return 36.4
        return 5.2
    if n_major >= 2:
        return 50.2
    if n_major == 1:
        return 39.1
    if n_elevated >= 1:
        return 27.5
    if n_not_optimal >= 1:
        return 27.6
    return 8.2


def _ascvd_age_adjust(risk_pct: float, age: int) -> float:
    """Возрастная коррекция базового (50-летнего) риска, округл. до 0.1.

    Порядок elif важен: age>=70 проверяется ДО age>=60, иначе ветка 60+
    поглощает весь диапазон 60-79. Фикс бага из PR #242 (Igor-Lysk).
    """
    if age < 50:
        risk_pct *= 1.05
    elif age >= 70:
        risk_pct *= 0.80
    elif age >= 60:
        risk_pct *= 0.92
    return round(risk_pct, 1)


def _ascvd_category(risk_pct: float) -> tuple[str, str]:
    if risk_pct < 20:
        return "low", "g"
    if risk_pct < 40:
        return "moderate", "y"
    if risk_pct < 60:
        return "high", "o"
    return "very_high", "r"


def _ascvd_interpretation(risk_pct: float, category: str, cat_ru: str) -> str:
    text = (
        f"Из 100 человек с твоим профилем у {risk_pct:.0f} случится инфаркт или инсульт когда-нибудь до конца жизни. "
        f"Категория: {cat_ru}. "
    )
    if category in ("low", "moderate"):
        return text + "Профилактика (диета, спорт, оптимизация ApoB) даёт максимум пользы именно сейчас."
    return text + "Раннее агрессивное вмешательство (статины + изменения образа жизни) сильно снижает этот риск."


def calc_ascvd_lifetime(
    *,
    age: int,
    sex: str,
    smoking: bool,
    sbp_mmhg: float,
    tchol_mmolL: float,
    hdl_mmolL: float,
    diabetes: bool = False,
    on_bp_meds: bool = False,
) -> Optional[dict]:
    """Пожизненный риск ASCVD (Lloyd-Jones et al. 2006 / AHA 2013).

    Упрощённая модель: классифицирует по 5 факторам риска (curated risk profile)
    и возвращает интерполированный пожизненный риск из таблиц AHA.

    Факторы риска (Lloyd-Jones major):
      - SBP ≥140 или на лекарствах
      - TChol ≥6.2 mmol/L (240 mg/dL)
      - HDL <1.0 (40 mg/dL) у муж / <1.3 (50) у жен
      - Курение
      - Диабет
    """
    if not all([age, sex, sbp_mmhg, tchol_mmolL, hdl_mmolL]):
        return None
    if not (20 <= age <= 79):
        return None

    is_male = sex == "male"
    hdl_threshold = 1.0 if is_male else 1.3

    factors = {
        "sbp": _ascvd_classify_sbp(sbp_mmhg, on_bp_meds),
        "chol": _ascvd_classify_chol(tchol_mmolL),
        "hdl": _ascvd_classify_hdl(hdl_mmolL, hdl_threshold),
        "smoking": "major" if smoking else "optimal",
        "diabetes": "major" if diabetes else "optimal",
    }
    cats = list(factors.values())
    n_major = sum(1 for c in cats if c == "major")
    n_elevated = sum(1 for c in cats if c == "elevated")
    n_not_optimal = sum(1 for c in cats if c in ("not_optimal", "elevated"))

    risk_pct = _ascvd_age_adjust(_ascvd_base_risk(is_male, n_major, n_elevated, n_not_optimal), age)
    category, color = _ascvd_category(risk_pct)
    cat_ru = _ASCVD_CAT_RU[category]

    return {
        "risk_pct": risk_pct,
        "category": category,
        "category_ru": cat_ru,
        "color": color,
        "interpretation": _ascvd_interpretation(risk_pct, category, cat_ru),
        "factors": factors,
        "n_major": n_major,
        "n_elevated": n_elevated,
        "inputs": {
            "age": age,
            "sex": sex,
            "smoking": smoking,
            "sbp": sbp_mmhg,
            "tchol": tchol_mmolL,
            "hdl": hdl_mmolL,
            "diabetes": diabetes,
            "on_bp_meds": on_bp_meds,
        },
    }
