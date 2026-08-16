"""Тесты i18n для doctor-report (#300): резолвинг языка, полнота словаря, LLM-перевод."""

from core.reports.biomarker_dynamics import MARKER_CONFIG
import services.report_i18n as i18n
from services.report_i18n import (
    CHROME,
    SUPPORTED_LANGS,
    resolve_report_language,
    transliterate_ru_to_latin,
)


def test_every_marker_has_en_label_and_unit():
    missing = [canon for canon, cfg in MARKER_CONFIG.items() if not cfg.get("label_en") or "unit_en" not in cfg]
    assert missing == [], f"markers без EN-подписи/единицы: {missing}"


def test_explicit_wins_over_language_code():
    assert resolve_report_language("en", "ru") == "en"
    assert resolve_report_language("ru", "en-US") == "ru"


def test_language_code_en_prefix():
    assert resolve_report_language(None, "en") == "en"
    assert resolve_report_language(None, "en-US") == "en"


def test_fallback_ru():
    assert resolve_report_language(None, None) == "ru"
    assert resolve_report_language(None, "de") == "ru"
    assert resolve_report_language("fr", None) == "ru"  # невалидный explicit + нет кода → ru


def test_invalid_explicit_falls_through_to_language_code():
    # Невалидный explicit игнорируется и решает language_code (документированная семантика).
    assert resolve_report_language("fr", "en") == "en"
    assert resolve_report_language("fr", "ru") == "ru"


def test_catalog_has_both_languages():
    assert set(CHROME) == set(SUPPORTED_LANGS) == {"ru", "en"}
    assert set(CHROME["ru"]) == set(CHROME["en"])
    assert set(CHROME["ru"]["sections"]) == set(CHROME["en"]["sections"])


def test_translate_ru_returns_input_without_llm(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("LLM не должен вызываться для ru")

    monkeypatch.setattr(i18n.requests, "post", _boom)
    items = ["Гипотиреоз", "Омега-3 — 1000 мг"]
    assert i18n.translate_freetext(items, "ru") == items


def test_translate_empty_returns_empty(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("не должен вызываться для пустого входа")

    monkeypatch.setattr(i18n.requests, "post", _boom)
    assert i18n.translate_freetext([], "en") == []


def test_translate_en_success_preserves_order(monkeypatch):
    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"content": [{"type": "text", "text": '["Hypothyroidism", "Omega-3 — 1000 mg"]'}]}

    monkeypatch.setattr(i18n, "get_settings", lambda: type("S", (), {"anthropic_api_key": "k"})())
    monkeypatch.setattr(i18n.requests, "post", lambda *a, **k: _Resp())
    out = i18n.translate_freetext(["Гипотиреоз", "Омега-3 — 1000 мг"], "en")
    assert out == ["Hypothyroidism", "Omega-3 — 1000 mg"]


def test_translate_llm_failure_falls_back_to_original(monkeypatch):
    def _fail(*a, **k):
        raise RuntimeError("network down")

    monkeypatch.setattr(i18n, "get_settings", lambda: type("S", (), {"anthropic_api_key": "k"})())
    monkeypatch.setattr(i18n.requests, "post", _fail)
    items = ["Гипотиреоз", "Аллергия на пыльцу"]
    assert i18n.translate_freetext(items, "en") == items


def test_translate_length_mismatch_falls_back(monkeypatch):
    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"content": [{"type": "text", "text": '["only one"]'}]}

    monkeypatch.setattr(i18n, "get_settings", lambda: type("S", (), {"anthropic_api_key": "k"})())
    monkeypatch.setattr(i18n.requests, "post", lambda *a, **k: _Resp())
    items = ["a", "b", "c"]
    assert i18n.translate_freetext(items, "en") == items


def test_translate_no_api_key_falls_back(monkeypatch):
    monkeypatch.setattr(i18n, "get_settings", lambda: type("S", (), {"anthropic_api_key": None})())
    items = ["Гипотиреоз"]
    assert i18n.translate_freetext(items, "en") == items


# ── Транслитерация имён для EN-отчёта (#1: имя не должно оставаться кириллицей) ──


def test_transliterate_soft_sign_omitted():
    # ь опускается: О-л-ь-г-а → Olga.
    assert transliterate_ru_to_latin("Ольга") == "Olga"


def test_transliterate_i_kratkoye():
    # й → i (ICAO Doc 9303): Д-м-и-т-р-и-й → Dmitrii.
    assert transliterate_ru_to_latin("Дмитрий") == "Dmitrii"


def test_transliterate_full_name():
    # Схема ICAO Doc 9303 / загранпаспорт РФ: й→i, ц→ts.
    assert transliterate_ru_to_latin("Сергей Кузнецов") == "Sergei Kuznetsov"


def test_transliterate_special_letters():
    # Многобуквенные соответствия с сохранением регистра первой буквы.
    assert transliterate_ru_to_latin("Жанна Щукина") == "Zhanna Shchukina"


def test_transliterate_latin_passthrough():
    # Латиница/пробелы не трогаются (у пользователя может быть латинское имя).
    assert transliterate_ru_to_latin("John Smith") == "John Smith"


def test_transliterate_preserves_digits_and_punct():
    assert transliterate_ru_to_latin("Омега 3") == "Omega 3"


def test_transliterate_empty():
    assert transliterate_ru_to_latin("") == ""
