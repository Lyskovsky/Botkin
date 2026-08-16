# tests/test_onboarding_deeplink.py
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "telegram-bot"))

import pytest
from unittest.mock import patch, AsyncMock, MagicMock


def _payload(text):
    return {"message": {"from": {"id": 555, "first_name": "Coachy"}, "chat": {"id": 555}, "text": text}}


@pytest.mark.asyncio
@patch("handlers.onboarding.log_event")
@patch("handlers.onboarding.send_message", new_callable=AsyncMock)
@patch("handlers.onboarding.SessionLocal")
async def test_start_without_payload_is_b2c(MockSession, mock_send, _le):
    db = MagicMock()
    MockSession.return_value = db
    db.query.return_value.filter_by.return_value.first.return_value = None
    from handlers.onboarding import process_onboarding_message

    await process_onboarding_message(_payload("/start"))
    created = db.add.call_args.args[0]
    assert created.onboarding_data.get("track") == "b2c"


@pytest.mark.asyncio
@patch("handlers.onboarding_coach.start_coach_onboarding", new_callable=AsyncMock)
@patch("handlers.onboarding.send_message", new_callable=AsyncMock)
@patch("handlers.onboarding.SessionLocal")
async def test_start_coach_routes_to_b2b_stub(MockSession, mock_send, mock_coach):
    db = MagicMock()
    MockSession.return_value = db
    db.query.return_value.filter_by.return_value.first.return_value = None
    from handlers.onboarding import process_onboarding_message

    await process_onboarding_message(_payload("/start coach"))
    mock_coach.assert_awaited_once()


@pytest.mark.asyncio
@patch("handlers.onboarding.log_event")
@patch("handlers.onboarding.send_message", new_callable=AsyncMock)
@patch("handlers.onboarding.SessionLocal")
async def test_start_stores_source_attribution(MockSession, mock_send, _le):
    db = MagicMock()
    MockSession.return_value = db
    db.query.return_value.filter_by.return_value.first.return_value = None
    from handlers.onboarding import process_onboarding_message

    await process_onboarding_message(_payload("/start gpt4tg_promoA"))
    created = db.add.call_args.args[0]
    assert created.onboarding_data.get("source") == "gpt4tg_promoA"
    assert created.onboarding_data.get("track") == "b2c"
