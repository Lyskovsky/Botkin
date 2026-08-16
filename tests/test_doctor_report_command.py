"""Tests for команда /doctor_report (#291).

Вторичный путь доставки PDF-отчёта врачу. Проверяет, что хендлер зовёт общий
helper и отвечает успехом/ошибкой в зависимости от результата доставки.
"""

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "telegram-bot"))


def _texts(answer_mock) -> list[str]:
    return [c.args[0] for c in answer_mock.call_args_list if c.args]


def _msg(text="/doctor_report", language_code="ru"):
    msg = MagicMock()
    msg.answer = AsyncMock()
    msg.text = text
    msg.from_user.language_code = language_code
    return msg


def test_parse_lang_arg_aliases():
    from handlers.commands import parse_doctor_report_lang

    assert parse_doctor_report_lang("/doctor_report en", "ru") == "en"
    assert parse_doctor_report_lang("/doctor_report english", "ru") == "en"
    assert parse_doctor_report_lang("/doctor_report английский", "ru") == "en"
    assert parse_doctor_report_lang("/doctor_report ru", "en") == "ru"
    assert parse_doctor_report_lang("/doctor_report RU", "en") == "ru"  # регистр не важен


def test_parse_lang_no_arg_uses_language_code():
    from handlers.commands import parse_doctor_report_lang

    assert parse_doctor_report_lang("/doctor_report", "en-US") == "en"
    assert parse_doctor_report_lang("/doctor_report", "ru") == "ru"
    assert parse_doctor_report_lang("/doctor_report", None) == "ru"


def test_parse_lang_unknown_arg_falls_back_to_language_code():
    from handlers.commands import parse_doctor_report_lang

    assert parse_doctor_report_lang("/doctor_report fr", "ru") == "ru"
    assert parse_doctor_report_lang("/doctor_report fr", "en") == "en"


@pytest.mark.asyncio
async def test_cmd_doctor_report_success(monkeypatch):
    """Успешная доставка → «готовлю…» + подтверждение, helper вызван с user_id и lang."""
    import database
    from handlers.commands import cmd_doctor_report
    from services import doctor_report

    seen = {}
    monkeypatch.setattr(database, "SessionLocal", lambda: MagicMock())
    monkeypatch.setattr(
        doctor_report,
        "send_doctor_report_to_chat",
        lambda db, uid, lang="ru": seen.update(uid=uid, lang=lang) or {"status": "ok", "sent": True},
    )

    msg = _msg(text="/doctor_report en", language_code="ru")
    await cmd_doctor_report(msg, user_id=895655)

    texts = _texts(msg.answer)
    assert any("Preparing" in t for t in texts)  # EN status (аргумент en перебил ru-клиент)
    assert any("was sent" in t for t in texts)
    assert seen["uid"] == 895655
    assert seen["lang"] == "en"


@pytest.mark.asyncio
async def test_cmd_doctor_report_default_ru(monkeypatch):
    """Без аргумента и с ru-клиентом → русские статусы, lang=ru."""
    import database
    from handlers.commands import cmd_doctor_report
    from services import doctor_report

    seen = {}
    monkeypatch.setattr(database, "SessionLocal", lambda: MagicMock())
    monkeypatch.setattr(
        doctor_report,
        "send_doctor_report_to_chat",
        lambda db, uid, lang="ru": seen.update(lang=lang) or {"status": "ok", "sent": True},
    )

    msg = _msg(text="/doctor_report", language_code="ru")
    await cmd_doctor_report(msg, user_id=895655)

    texts = _texts(msg.answer)
    assert any("Готовлю" in t for t in texts)
    assert any("отправлен" in t for t in texts)
    assert seen["lang"] == "ru"


@pytest.mark.asyncio
async def test_cmd_doctor_report_failure(monkeypatch):
    """Сбой доставки → сообщение об ошибке."""
    import database
    from handlers.commands import cmd_doctor_report
    from services import doctor_report

    monkeypatch.setattr(database, "SessionLocal", lambda: MagicMock())
    monkeypatch.setattr(
        doctor_report,
        "send_doctor_report_to_chat",
        lambda db, uid, lang="ru": {"status": "error", "error": "render-failed", "sent": False},
    )

    msg = _msg()
    await cmd_doctor_report(msg, user_id=895655)

    texts = _texts(msg.answer)
    assert any("Не удалось" in t for t in texts)
