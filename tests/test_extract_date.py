"""Тесты extract_date_from_text — относительная/явная дата в тексте еды.

Прецедент 30.05.2026: «Обед вчера: …» не распознавал дату → запись на сегодня.
"""

import importlib.util
import os
from datetime import datetime, timedelta, timezone


MSK = timezone(timedelta(hours=3))

_path = os.path.join(os.path.dirname(__file__), "..", "telegram-bot", "handlers", "text.py")
_spec = importlib.util.spec_from_file_location("tg_text_for_test", _path)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
extract_date_from_text = _mod.extract_date_from_text


def _ago(days: int) -> str:
    return (datetime.now(MSK) - timedelta(days=days)).strftime("%Y-%m-%d")


def test_vchera_midstring_keeps_prefix():
    """«Обед вчера: рыбные шарики» → вчера, без слова 'вчера', еда сохранена."""
    date_str, clean = extract_date_from_text("Обед вчера: рыбные шарики")
    assert date_str == _ago(1)
    assert "вчера" not in clean.lower()
    assert "рыбные шарики" in clean.lower()
    assert "обед" in clean.lower()  # префикс не потерян


def test_vchera_at_start():
    date_str, clean = extract_date_from_text("вчера ужин: омлет")
    assert date_str == _ago(1)
    assert "вчера" not in clean.lower()
    assert "омлет" in clean.lower()


def test_pozavchera():
    date_str, clean = extract_date_from_text("позавчера обед")
    assert date_str == _ago(2)
    assert "позавчера" not in clean.lower()


def test_month_name_still_works():
    """Регрессия: «29 мая ужин» уже работал — не сломать."""
    date_str, clean = extract_date_from_text("ужин 29 мая: салат")
    assert date_str is not None
    assert date_str.endswith("-05-29")
    assert "салат" in clean.lower()


def test_dd_mm_still_works():
    """Регрессия: DD.MM."""
    date_str, clean = extract_date_from_text("15.03 завтрак")
    assert date_str is not None
    assert date_str.endswith("-03-15")
    assert "завтрак" in clean.lower()


def test_no_date_returns_none():
    date_str, clean = extract_date_from_text("омлет 300 ккал")
    assert date_str is None
    assert clean == "омлет 300 ккал"


# Кейсы перенесены из tests/test_date_extraction.py (файл печатал ✅/❌ без
# единого assert — физически не мог упасть; удалён при аудите 11.06.2026).

import pytest  # noqa: E402


@pytest.mark.parametrize(
    "text, expected_date, expected_clean",
    [
        # Абсолютная дата ПЕРЕД названием приёма (классика)
        ("29 января: каша", "2026-01-29", "каша"),
        ("29-го января обед: суп", "2026-01-29", "обед: суп"),
        # Регрессия: название приёма ПЕРЕД датой (исходный баг)
        ("ужин 19-е апреля: сыр и тунец", "2026-04-19", "ужин сыр и тунец"),
        ("завтрак 19-го апреля: омлет", "2026-04-19", "завтрак омлет"),
        ("обед 19 апреля: суп", "2026-04-19", "обед суп"),
    ],
)
def test_absolute_dates(text, expected_date, expected_clean):
    date_str, clean = extract_date_from_text(text)
    assert date_str == expected_date
    assert clean == expected_clean


def test_english_yesterday():
    date_str, clean = extract_date_from_text("yesterday breakfast")
    assert date_str == _ago(1)
    assert clean == "breakfast"


def test_no_date_passthrough():
    assert extract_date_from_text("Просто текст") == (None, "Просто текст")
    assert extract_date_from_text("Сегодня ужин") == (None, "Сегодня ужин")


# --- Эвристика «вечером → вчера» (F-004, 02.07.2026) -------------------------


def _tz_with_local_hour(hour: int) -> timezone:
    """Фиксированный offset-tz, в котором СЕЙЧАС локально ~hour часов."""
    utc_hour = datetime.now(timezone.utc).hour
    return timezone(timedelta(hours=(hour - utc_hour) % 24))


def test_evening_marker_in_the_morning_means_yesterday():
    tz = _tz_with_local_hour(8)  # у юзера утро
    date_str, clean = extract_date_from_text("Я вечером ещё 500 мл кефира выпила", user_tz=tz)
    assert date_str == (datetime.now(tz) - timedelta(days=1)).strftime("%Y-%m-%d")
    assert "кефира" in clean


def test_evening_marker_in_the_evening_stays_today():
    tz = _tz_with_local_hour(20)  # у юзера вечер — «вечером» это сегодня
    assert extract_date_from_text("вечером съел салат", user_tz=tz) == (None, "вечером съел салат")


def test_evening_marker_with_segodnya_stays_today():
    tz = _tz_with_local_hour(8)
    text = "сегодня вечером буду есть рыбу"
    assert extract_date_from_text(text, user_tz=tz) == (None, text)
