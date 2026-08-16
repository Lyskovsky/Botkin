# tests/integration/test_onboarding_wizard.py
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "telegram-bot"))

import pytest
from unittest.mock import patch, AsyncMock, MagicMock


def _mk_user(step, data=None, **kw):
    return MagicMock(
        telegram_id=999888,
        onboarding_step=step,
        onboarding_data=data or {},
        health_token=None,
        share_token=None,
        **kw,
    )


@pytest.mark.asyncio
@patch("handlers.onboarding.log_event")
@patch("handlers.onboarding.send_message", new_callable=AsyncMock)
@patch("handlers.onboarding.SessionLocal")
async def test_new_user_starts_at_goal_quiz(MockSession, mock_send, _le):
    db = MagicMock()
    MockSession.return_value = db
    db.query.return_value.filter_by.return_value.first.return_value = None
    from handlers.onboarding import process_onboarding_message

    await process_onboarding_message(
        {"message": {"from": {"id": 999888, "first_name": "Игорь"}, "chat": {"id": 999888}, "text": "/start"}}
    )
    created = db.add.call_args.args[0]
    assert created.onboarding_step == "goal"
    txt = mock_send.call_args.args[1].lower()
    assert "привет" in txt and "цель" in txt  # приветствие + первый вопрос


@pytest.mark.asyncio
@patch("handlers.onboarding.log_event")
@patch("handlers.onboarding.send_message", new_callable=AsyncMock)
@patch("handlers.onboarding.SessionLocal")
async def test_activity_advances_to_smoking(MockSession, mock_send, _le):
    """Активность (6/7) → последний квиз-вопрос о курении (7/7), ещё не артефакт."""
    db = MagicMock()
    MockSession.return_value = db
    user = _mk_user(
        "activity",
        {"goal": "Похудеть", "goal_pct": -15, "sex": "male", "age": 35, "height_cm": 178, "weight_kg": 80},
    )
    db.query.return_value.filter_by.return_value.first.return_value = user
    from handlers.onboarding import process_onboarding_message

    await process_onboarding_message(
        {"message": {"from": {"id": 999888}, "chat": {"id": 999888}, "text": "🏃 Умеренный 4-5/нед"}}
    )
    assert user.onboarding_step == "smoking"
    assert "куришь" in mock_send.call_args.args[1].lower()


@pytest.mark.asyncio
@patch("handlers.onboarding.log_event")
@patch("handlers.onboarding.send_message", new_callable=AsyncMock)
@patch("handlers.onboarding.SessionLocal")
async def test_smoking_step_computes_artifact_and_advances_to_persona(MockSession, mock_send, mock_le):
    db = MagicMock()
    MockSession.return_value = db
    user = _mk_user(
        "smoking",
        {
            "goal": "Похудеть",
            "goal_pct": -15,
            "sex": "male",
            "age": 35,
            "height_cm": 178,
            "weight_kg": 80,
            "activity_multiplier": 1.55,
        },
    )
    db.query.return_value.filter_by.return_value.first.return_value = user
    from handlers.onboarding import process_onboarding_message

    await process_onboarding_message({"message": {"from": {"id": 999888}, "chat": {"id": 999888}, "text": "Никогда"}})
    assert user.onboarding_step == "persona"
    assert user.smoking_status == "never"  # захвачено для PhenoAge
    joined = " ".join(c.args[1] for c in mock_send.call_args_list)
    assert "ккал/день" in joined  # артефакт цели показан
    # E3 quiz_completed и E4 goal_computed залогированы после курения
    events = [c.kwargs.get("event") or c.args[2] for c in mock_le.call_args_list]
    assert "quiz_completed" in str(events) and "goal_computed" in str(events)


def test_weight_forecast_deficit_projects_loss():
    from handlers.onboarding import _weight_forecast

    fc = _weight_forecast(goal_pct=-15, tdee=2180)
    assert fc["kg_per_week"] > 0 and fc["target_date"]  # худеет


def test_weight_forecast_zero_deficit_is_flat():
    from handlers.onboarding import _weight_forecast

    fc = _weight_forecast(goal_pct=0, tdee=2180)
    assert fc["kg_per_week"] == 0


@pytest.mark.asyncio
@patch("handlers.onboarding.log_event")
@patch("handlers.onboarding.send_message", new_callable=AsyncMock)
@patch("handlers.onboarding.SessionLocal")
async def test_persona_choice_finishes_and_sets_demo_flag(MockSession, mock_send, mock_le):
    db = MagicMock()
    MockSession.return_value = db
    user = _mk_user("persona", {"goal": "Похудеть", "name": "Игорь"}, first_name="Игорь")
    db.query.return_value.filter_by.return_value.first.return_value = user
    from handlers.onboarding import process_onboarding_message

    await process_onboarding_message(
        {"message": {"from": {"id": 999888}, "chat": {"id": 999888}, "text": "💪 Строгий тренер"}}
    )
    assert user.onboarding_step == "done"
    assert user.onboarding_data["persona"] == "strict_coach"
    assert user.onboarding_data["first_food_pending"] is True
    assert user.health_token and user.health_token.startswith("hvt_999888_")
    joined = " ".join(c.args[1] for c in mock_send.call_args_list).lower()
    assert "напиши" in joined and "ел" in joined  # демо-приглашение
    events = [str(c) for c in mock_le.call_args_list]
    assert any("persona_selected" in e for e in events)


@pytest.mark.asyncio
@patch("handlers.onboarding.log_event")
@patch("handlers.onboarding.send_message", new_callable=AsyncMock)
@patch("handlers.onboarding.SessionLocal")
async def test_persona_skip_uses_default(MockSession, mock_send, _le):
    db = MagicMock()
    MockSession.return_value = db
    user = _mk_user("persona", {"goal": "Похудеть", "name": "Игорь"}, first_name="Игорь")
    db.query.return_value.filter_by.return_value.first.return_value = user
    from handlers.onboarding import process_onboarding_message

    await process_onboarding_message(
        {"message": {"from": {"id": 999888}, "chat": {"id": 999888}, "text": "Пропустить"}}
    )
    assert user.onboarding_data["persona"] == "caring_doctor"
    assert user.onboarding_step == "done"
