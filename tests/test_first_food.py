import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "telegram-bot"))

import pytest
from unittest.mock import patch, AsyncMock, MagicMock


@pytest.mark.asyncio
@patch("handlers.first_food.log_event")
@patch("handlers.first_food.SessionLocal")
async def test_celebrates_and_clears_flag_when_pending(MockSession, mock_le):
    db = MagicMock()
    MockSession.return_value = db
    user = MagicMock(telegram_id=5, onboarding_data={"first_food_pending": True})
    db.query.return_value.filter_by.return_value.first.return_value = user
    from handlers.first_food import record_first_food

    message = MagicMock(answer=AsyncMock())
    await record_first_food(5, message)

    # E5 залогирован (once)
    assert any(c.kwargs.get("event") == "first_food_logged" for c in mock_le.call_args_list)
    # флаг снят
    assert user.onboarding_data.get("first_food_pending") is not True
    # празднующая строка отправлена
    message.answer.assert_awaited()
    assert "команд" in message.answer.call_args.args[0].lower()


@pytest.mark.asyncio
@patch("handlers.first_food.log_event")
@patch("handlers.first_food.SessionLocal")
async def test_logs_e5_but_no_message_when_not_pending(MockSession, mock_le):
    db = MagicMock()
    MockSession.return_value = db
    user = MagicMock(telegram_id=6, onboarding_data={})  # флага нет
    db.query.return_value.filter_by.return_value.first.return_value = user
    from handlers.first_food import record_first_food

    message = MagicMock(answer=AsyncMock())
    await record_first_food(6, message)

    # E5 всё равно логируется (это событие активации, once)
    assert any(c.kwargs.get("event") == "first_food_logged" for c in mock_le.call_args_list)
    # но празднования нет
    message.answer.assert_not_awaited()


@pytest.mark.asyncio
@patch("handlers.first_food.asyncio.to_thread", new_callable=AsyncMock)
async def test_db_work_offloaded_to_thread(mock_to_thread):
    """Синхронный DB-путь идёт через asyncio.to_thread — не блокирует event loop
    (прецедент 16.07.2026: под локом Postgres прямой db.commit подвесил бы loop)."""
    mock_to_thread.return_value = False  # не pending → без празднования
    from handlers.first_food import record_first_food, _record_first_food_sync

    message = MagicMock(answer=AsyncMock())
    await record_first_food(9, message)

    mock_to_thread.assert_awaited_once()
    # именно синхронный helper уходит в тред, с telegram_user_id
    assert mock_to_thread.await_args.args[0] is _record_first_food_sync
    assert mock_to_thread.await_args.args[1] == 9
    message.answer.assert_not_awaited()


@pytest.mark.asyncio
@patch("handlers.first_food.asyncio.to_thread", new_callable=AsyncMock)
async def test_celebrates_when_thread_reports_pending(mock_to_thread):
    """Если синхронный helper вернул pending=True — шлём празднующую строку."""
    mock_to_thread.return_value = True
    from handlers.first_food import record_first_food

    message = MagicMock(answer=AsyncMock())
    await record_first_food(10, message)

    message.answer.assert_awaited_once()
    assert "команд" in message.answer.call_args.args[0].lower()


@pytest.mark.asyncio
async def test_photo_save_calls_record_first_food(monkeypatch):
    """После сохранения приёма пищи handle_meal_confirmation зовёт record_first_food."""
    import handlers.photo as photo

    called = {}

    async def fake_record(uid, message):
        called["uid"] = uid

    monkeypatch.setattr(photo, "record_first_food", fake_record, raising=False)
    # Прямой вызов helper-обёртки, добавленной в photo.py:
    msg = object()
    await photo._maybe_record_first_food(777, msg)
    assert called["uid"] == 777
