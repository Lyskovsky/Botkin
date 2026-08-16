import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "telegram-bot"))
import pytest
from unittest.mock import patch, AsyncMock, MagicMock


@pytest.mark.asyncio
@patch("handlers.onboarding.log_event")
@patch("handlers.onboarding.send_message", new_callable=AsyncMock)
@patch("handlers.onboarding.SessionLocal")
async def test_legacy_step_remapped(MockSession, mock_send, _le):
    """Юзер застрял на старом шаге 'wearables' → маппится на artifact→persona, не падает.

    ('smoking' больше НЕ ремапится — это снова реальный шаг нового флоу.)
    """
    db = MagicMock()
    MockSession.return_value = db
    user = MagicMock(
        telegram_id=999888,
        onboarding_step="wearables",
        onboarding_data={"goal": "Похудеть", "name": "И"},
        first_name="И",
        health_token=None,
        share_token=None,
    )
    db.query.return_value.filter_by.return_value.first.return_value = user
    from handlers.onboarding import process_onboarding_message

    await process_onboarding_message({"message": {"from": {"id": 999888}, "chat": {"id": 999888}, "text": "привет"}})
    assert user.onboarding_step in ("persona", "done")
    mock_send.assert_awaited()


def test_detect_missing_smoking_included_chronic_wearables_excluded():
    from handlers.onboarding import _detect_missing_steps

    db = MagicMock()
    db.query.return_value.filter_by.return_value.count.return_value = 1
    us = MagicMock(activity_level="moderate")
    db.query.return_value.filter_by.return_value.first.return_value = us
    user = MagicMock(
        first_name="И",
        birth_date=object(),
        sex="male",
        height_cm=178,
        smoking_status=None,
        onboarding_data={"goal": "Похудеть"},
    )
    missing = _detect_missing_steps(user, db)
    # курение вернулось в обязательный набор (нужно для PhenoAge)
    assert "smoking" in missing
    # chronic/wearables окончательно убраны из онбординга
    assert "chronic" not in missing and "wearables" not in missing
