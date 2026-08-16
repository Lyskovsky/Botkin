"""Чистая логика хендлеров /connect_mcp и /my_connections (#228, переименован в #235).

Хендлеры тянут aiogram (в локальном venv его может не быть) — грузим модуль
через importlib и пропускаем весь файл, если aiogram недоступен (как и прочие
handler-тесты в CI). Тестируем только pure-функции, без Telegram и БД.
"""

import importlib.util
from datetime import datetime
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parent.parent / "telegram-bot" / "handlers" / "connect_claude.py"
_spec = importlib.util.spec_from_file_location("connect_claude_handler", _PATH)
cc = importlib.util.module_from_spec(_spec)
try:
    _spec.loader.exec_module(cc)
except ModuleNotFoundError as e:  # aiogram нет в этом окружении
    pytest.skip(f"aiogram недоступен: {e}", allow_module_level=True)


# ── parse_connect_name ─────────────────────────────────────────────────────────


def test_parse_name_present():
    assert cc.parse_connect_name("/connect_mcp мой ноут") == "мой ноут"


def test_parse_name_absent():
    assert cc.parse_connect_name("/connect_mcp") is None


def test_parse_name_empty_arg():
    assert cc.parse_connect_name("/connect_mcp    ") is None


def test_parse_name_collapses_whitespace():
    assert cc.parse_connect_name("/connect_mcp  рабочий    мак") == "рабочий мак"


def test_parse_name_truncated_to_max():
    long = "a" * 200
    assert len(cc.parse_connect_name(f"/connect_mcp {long}")) == cc.MAX_NAME_LEN


def test_parse_name_none_text():
    assert cc.parse_connect_name(None) is None


# ── scope_label ────────────────────────────────────────────────────────────────


def test_scope_label_rw():
    assert "запись" in cc.scope_label("rw")


def test_scope_label_ro():
    assert cc.scope_label("ro") == "только чтение"


# ── format_connections ──────────────────────────────────────────────────────────


def test_format_lists_name_scope_and_usage():
    pats = [
        {"id": 1, "name": "ноут", "scope": "rw", "last_used_at": datetime(2026, 6, 28)},
        {"id": 2, "name": None, "scope": "ro", "last_used_at": None},
    ]
    text = cc.format_connections(pats)

    assert "ноут" in text
    assert "чтение+запись" in text
    assert "28.06.2026" in text
    assert "(без имени)" in text
    assert "только чтение" in text
    assert "ещё не использовался" in text
