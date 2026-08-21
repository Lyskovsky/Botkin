"""Табличные тесты _hae_to_daily_payloads — ночной HAE-канал Apple Health всей семьи.

Аудит 11.06.2026: 80% веток парсера были без тестов, при том что баги тут уже
чинились дважды (int-коэрция, MJ/kJ). Фиксируем поведение всех конверсий.
Чисто in-memory, без БД.
"""

import logging
import sys
from pathlib import Path

import pytest

TG_BOT = Path(__file__).resolve().parent.parent / "telegram-bot"
if str(TG_BOT) not in sys.path:
    sys.path.insert(0, str(TG_BOT))

from webhook.apple_health import _hae_to_daily_payloads

D = "2026-06-01"


def _metric(name, units, **rec):
    rec.setdefault("date", f"{D} 00:00:00 +0300")
    return {"name": name, "units": units, "data": [rec]}


def _parse_one(*metrics):
    out = _hae_to_daily_payloads(list(metrics))
    assert D in out, f"дата {D} не распознана: {list(out)}"
    return out[D]


# ── Энергия: эвристика HAE-бага «МДж под видом kJ» ───────────────────────────


@pytest.mark.parametrize(
    "units, qty, expected_kcal",
    [
        # HAE-баг: units="kJ", но реально МДж (значение < 100) → ×239.006
        ("kJ", 5.858, 1400.1),
        ("kJ", 99.0, 23661.6),  # граница: 99 < 100 → трактуем как MJ
        # Настоящие килоджоули (≥100) → /4.184
        ("kJ", 100.0, 23.9),
        ("kJ", 5858.0, 1400.1),
        # Явные МДж
        ("MJ", 5.858, 1400.1),
        # kcal — как есть
        ("kcal", 450.0, 450.0),
    ],
)
def test_active_energy_units(units, qty, expected_kcal):
    p = _parse_one(_metric("active_energy", units, qty=qty))
    assert p.active_energy_kcal == pytest.approx(expected_kcal, abs=0.2)


def test_basal_energy_same_heuristic():
    p = _parse_one(_metric("basal_energy_burned", "kJ", qty=7.2))
    assert p.basal_energy_kcal == pytest.approx(7.2 * 239.006, abs=0.5)


# ── Дистанция: метры / мили / километры ──────────────────────────────────────


@pytest.mark.parametrize(
    "units, qty, expected_km",
    [
        ("m", 11158.0, 11.158),
        ("mi", 5.0, 8.047),
        ("km", 11.158, 11.158),
    ],
)
def test_walking_distance_units(units, qty, expected_km):
    p = _parse_one(_metric("walking_running_distance", units, qty=qty))
    assert p.distance_walking_km == pytest.approx(expected_km, abs=0.001)


# ── Сон: все 4 формата HAE ───────────────────────────────────────────────────


def test_sleep_totalsleep_format_with_stages():
    """Основной формат Apple Watch (summarize=ON): totalSleep + стадии."""
    p = _parse_one(_metric("sleep_analysis", "hr", totalSleep=7.73, deep=1.2, rem=1.8, core=4.5, awake=0.4))
    assert p.sleep_hours == 7.73
    assert p.sleep_deep_h == 1.2
    assert p.sleep_rem_h == 1.8
    assert p.sleep_core_h == 4.5
    assert p.sleep_awake_h == 0.4


def test_sleep_legacy_asleep_field():
    p = _parse_one(_metric("sleep_analysis", "hr", Asleep=6.5, InBed=8.0))
    assert p.sleep_hours == 6.5


def test_sleep_value_style_asleep_counted_inbed_skipped():
    """value-стиль: Asleep учитывается, InBed — нет (это не сон)."""
    asleep = {
        "name": "sleep_analysis",
        "units": "hr",
        "data": [{"date": f"{D} 00:00:00 +0300", "qty": 6.9, "value": "Asleep"}],
    }
    inbed = {
        "name": "sleep_analysis",
        "units": "hr",
        "data": [{"date": f"{D} 00:00:00 +0300", "qty": 8.4, "value": "InBed"}],
    }
    out = _hae_to_daily_payloads([asleep, inbed])
    assert out[D].sleep_hours == 6.9


def test_sleep_start_end_fallback():
    """summarize=OFF: часы считаются из startDate/endDate."""
    p = _parse_one(
        _metric(
            "sleep_analysis",
            "hr",
            startDate="2026-06-01 23:30:00 +0300",
            endDate="2026-06-02 07:00:00 +0300",
        )
    )
    assert p.sleep_hours == 7.5


def test_sleep_under_30min_ignored():
    """Микро-сон <0.5ч (артефакт) не пишется."""
    p = _parse_one(_metric("sleep_analysis", "hr", totalSleep=0.3))
    assert p.sleep_hours is None


# ── Давление: оба формата ────────────────────────────────────────────────────


def test_bp_combined_record():
    p = _parse_one(_metric("blood_pressure", "mmHg", systolic=128.4, diastolic=83.6))
    assert p.blood_pressure_systolic == 128
    assert p.blood_pressure_diastolic == 84


def test_bp_separate_metrics():
    p = _parse_one(
        _metric("blood_pressure_systolic", "mmHg", qty=119.0),
        _metric("blood_pressure_diastolic", "mmHg", qty=76.0),
    )
    assert p.blood_pressure_systolic == 119
    assert p.blood_pressure_diastolic == 76


# ── Тело и походка ───────────────────────────────────────────────────────────


def test_weight_and_body_fat():
    p = _parse_one(
        _metric("weight_body_mass", "kg", qty=82.456),
        _metric("body_fat_percentage", "%", qty=27.43),
    )
    assert p.weight_kg == 82.46
    assert p.body_fat_pct == 27.4


def test_double_support_not_multiplied():
    """Регрессия: HAE шлёт *_percentage уже в %, не во фракции — не умножать ×100."""
    p = _parse_one(_metric("walking_double_support_percentage", "%", qty=29.5))
    assert p.walking_double_support_pct == 29.5


def test_walking_speed_ms_to_kmh():
    p = _parse_one(_metric("walking_speed", "m/s", qty=1.39))
    assert p.walking_speed_km_h == pytest.approx(5.0, abs=0.01)


def test_hrv_rounded_to_int():
    p = _parse_one(_metric("heart_rate_variability_sdnn", "ms", qty=46.7))
    assert p.hrv == 47


# ── Устойчивость к мусору ────────────────────────────────────────────────────


def test_malformed_dates_skipped():
    """Записи без валидной даты молча пропускаются, не роняя парс."""
    bad = {"name": "step_count", "units": "count", "data": [{"date": "garbage", "qty": 100}, {"qty": 200}]}
    ok = _metric("step_count", "count", qty=12000)
    out = _hae_to_daily_payloads([bad, ok])
    assert list(out) == [D]
    assert out[D].steps == 12000


def test_multiple_days_grouped():
    m1 = _metric("step_count", "count", qty=10000)
    m2 = {"name": "step_count", "units": "count", "data": [{"date": "2026-06-02 00:00:00 +0300", "qty": 8000}]}
    out = _hae_to_daily_payloads([m1, m2])
    assert out["2026-06-01"].steps == 10000
    assert out["2026-06-02"].steps == 8000


# ── Кумулятивные метрики: суммирование по дню (регресс «3 шага в сутки») ──────
#
# HAE шлёт день ОДНОЙ записью только при «Суммировать: ON» + группировке «День».
# Если настройка сбита или экспорт идёт интервалами, за день приходит несколько
# записей. Раньше каждая следующая перезатирала предыдущую, и в БД оседал
# последний огрызок: реальные ~6000 шагов превращались в 3–6 (данные 11–12.08.2026).


def _metric_multi(name, units, qtys):
    """Одна метрика, несколько внутридневных записей (интервалы одного дня)."""
    return {
        "name": name,
        "units": units,
        "data": [{"date": f"{D} {h:02d}:00:00 +0300", "qty": q} for h, q in enumerate(qtys)],
    }


def test_steps_summed_across_intervals():
    (payload,) = _hae_to_daily_payloads([_metric_multi("step_count", "count", [4000, 2000, 3])]).values()
    assert payload.steps == 6003  # не 3 — последняя запись больше не затирает сумму


def test_distance_summed_across_intervals():
    (payload,) = _hae_to_daily_payloads([_metric_multi("walking_running_distance", "km", [3.0, 1.5, 0.004])]).values()
    assert payload.distance_walking_km == 4.504


def test_flights_and_energy_summed_across_intervals():
    metrics = [
        _metric_multi("flights_climbed", "count", [5, 3]),
        _metric_multi("active_energy", "kcal", [200.0, 150.5]),
    ]
    (payload,) = _hae_to_daily_payloads(metrics).values()
    assert payload.flights_climbed == 8
    assert payload.active_energy_kcal == 350.5


def test_single_daily_record_unchanged():
    """Штатный режим (одна запись за день) не должен измениться от суммирования."""
    (payload,) = _hae_to_daily_payloads([_metric("step_count", "count", qty=8432)]).values()
    assert payload.steps == 8432


def test_state_metrics_not_summed():
    """Пульс — состояние, а не счётчик: суммировать нельзя, берём последнее значение."""
    metrics = [
        {
            "name": "heart_rate",
            "units": "count/min",
            "data": [
                {"date": f"{D} 08:00:00 +0300", "Avg": 70, "Min": 55, "Max": 120},
                {"date": f"{D} 20:00:00 +0300", "Avg": 75, "Min": 60, "Max": 130},
            ],
        }
    ]
    (payload,) = _hae_to_daily_payloads(metrics).values()
    assert payload.heart_rate_avg == 75  # не 145


# ── энергия: конверсия один раз к суточной сумме (баг 16–21.08.2026) ──────────


def _energy_metric(name, units, values, day="2026-08-21"):
    return [
        {
            "name": name,
            "units": units,
            "data": [{"date": f"{day} 00:0{i % 10}:00", "qty": v} for i, v in enumerate(values)],
        }
    ]


def test_minute_intervals_in_kj_not_multiplied_as_mj():
    """1440 минутных кусков по ~6,1 кДж = 2101 ккал, а не 2 млн.

    Регресс, из-за которого bmr_calories 16–21.08.2026 стал миллионами: эвристика
    «<100 при kJ = это МДж» применялась к КАЖДОМУ куску.
    """

    payloads = _hae_to_daily_payloads(_energy_metric("basal_energy_burned", "kJ", [6.104] * 1440))
    kcal = payloads["2026-08-21"].basal_energy_kcal
    assert 2000 < kcal < 2200, kcal


def test_daily_aggregate_mislabelled_as_kj_still_treated_as_mj():
    """Обратная совместимость: одна суточная запись 5,858 «kJ» = 1400 ккал."""

    payloads = _hae_to_daily_payloads(_energy_metric("basal_energy_burned", "kJ", [5.858]))
    assert payloads["2026-08-21"].basal_energy_kcal == pytest.approx(1400.1, abs=1.0)


def test_energy_in_kcal_passes_through():

    payloads = _hae_to_daily_payloads(_energy_metric("active_energy", "kcal", [200, 150, 90]))
    assert payloads["2026-08-21"].active_energy_kcal == pytest.approx(440.0)


def test_active_and_basal_are_summed_independently():

    metrics = _energy_metric("active_energy", "kcal", [100, 100]) + _energy_metric(
        "basal_energy_burned", "kcal", [900, 900]
    )
    p = _hae_to_daily_payloads(metrics)["2026-08-21"]
    assert (p.active_energy_kcal, p.basal_energy_kcal) == (200.0, 1800.0)


def test_service_keys_do_not_leak_into_payload():
    """Служебные ключи суммирования не должны попасть в AppleHealthPayload."""

    p = _hae_to_daily_payloads(_energy_metric("basal_energy_burned", "kJ", [6.1] * 10))["2026-08-21"]
    assert not hasattr(p, "_basal_raw")


# ── пробелы, найденные ревью #384 ─────────────────────────────────────────────


def test_partial_day_small_kj_sum_not_inflated_as_mj(caplog):
    """Частичный день: 3 записи по 7 кДж = 21 кДж ≈ 5 ккал, а НЕ 5019.

    Порог «<100 = это МДж» осмыслен только для суточного агрегата (одна точка).
    При неполной синхронизации сумма честно мала — умножать её на 239 нельзя.
    """

    with caplog.at_level(logging.WARNING):
        payloads = _hae_to_daily_payloads(_energy_metric("basal_energy_burned", "kJ", [7.0, 7.0, 7.0]))

    assert payloads["2026-08-21"].basal_energy_kcal == pytest.approx(5.0, abs=0.1)
    # Тихо подменять число нельзя — спорный случай должен быть виден в логах.
    assert any("частичный день" in r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING)


def test_single_small_kj_record_still_treated_as_mj():
    """Обратная совместимость: ОДНА запись <100 кДж — это мислейбл МДж (баг HAE)."""

    payloads = _hae_to_daily_payloads(_energy_metric("basal_energy_burned", "kJ", [5.858]))
    assert payloads["2026-08-21"].basal_energy_kcal == pytest.approx(1400.1, abs=1.0)


def test_mixed_units_in_one_day_converted_per_unit():
    """kcal и kJ в одном дне: каждая группа конвертируется своим правилом.

    Регресс #384: сырые qty с разными units складывались в одно число ДО
    конверсии — 1000 kcal + 4184 kJ давали 1239 ккал вместо ~2000, молча.
    """

    metrics = _energy_metric("active_energy", "kcal", [1000.0]) + _energy_metric("active_energy", "kJ", [4184.0])
    assert _hae_to_daily_payloads(metrics)["2026-08-21"].active_energy_kcal == pytest.approx(2000.0, abs=1.0)
