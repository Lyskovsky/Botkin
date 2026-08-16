"""handle_meal_confirmation должен логировать food_interactions (#258).

Patch strategy (зеркалим tests/test_photo_handler.py):
  - save_meal_to_db      → lazy import inside fn → patch at source module
  - log_food_interaction → lazy import inside fn → patch at source module
  - format_budget_line   → lazy import inside fn → patch at source module
  - get_user_settings    → lazy import inside fn → patch at source module
  - database.SessionLocal → lazy import inside fn → patch at source module
"""

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
BOT_ROOT = PROJECT_ROOT / "telegram-bot"
for p in [str(PROJECT_ROOT), str(BOT_ROOT)]:
    if p not in sys.path:
        sys.path.insert(0, p)

SAVE_MEAL = "helpers.db_save.save_meal_to_db"
LOG_INTERACTION = "core.food.interaction_log.log_food_interaction"
BUDGET_LINE = "core.health.caloric_budget.format_budget_line"
GET_USER_SETTINGS = "database.crud.get_user_settings"
SESSION_LOCAL = "database.SessionLocal"


def _make_callback(user_id: int = 895655, message_text: str = "🍽️ <b>Завтрак</b>\n\nПодтверди?"):
    callback = AsyncMock()
    callback.from_user = MagicMock()
    callback.from_user.id = user_id
    callback.message = AsyncMock()
    callback.message.text = message_text
    callback.answer = AsyncMock()
    return callback


@pytest.mark.asyncio
async def test_single_meal_save_logs_food_interaction():
    from handlers.photo import handle_meal_confirmation
    from handlers.callbacks import MealConfirmationCallback
    from services.state import UserState, state_manager

    user_id = "895655"
    state_manager.set_state(
        user_id,
        UserState(
            user_id=user_id,
            state="waiting_confirmation",
            data={
                "source": "text",
                "description": "овсянка с бананом",
                "meal_items": [{"product": "Овсянка", "weight_g": 200, "calories": 300}],
                "meal_totals": {"calories": 300, "protein": 10, "fats": 5, "carbs": 50},
                "meal_name": "Завтрак",
            },
        ),
    )

    callback = _make_callback()
    callback_data = MealConfirmationCallback(action="save", meal_type="regular")

    with (
        patch(SAVE_MEAL, return_value=555),
        patch(LOG_INTERACTION) as mock_log,
        patch(BUDGET_LINE, return_value=""),
        patch(GET_USER_SETTINGS, return_value=None),
        patch(SESSION_LOCAL, return_value=MagicMock()),
    ):
        await handle_meal_confirmation(callback, callback_data)

    mock_log.assert_called_once()
    kwargs = mock_log.call_args.kwargs
    assert kwargs["user_id"] == 895655
    assert kwargs["source"] == "text"
    assert kwargs["raw_text"] == "овсянка с бананом"
    assert kwargs["recognized"]["totals"]["calories"] == 300
    assert kwargs["nutrition_log_id"] == 555
    assert kwargs["status"] == "saved"

    assert state_manager.get_state(user_id) is None


@pytest.mark.asyncio
async def test_callback_answered_before_blocking_save():
    """Регресс dev-hang 16.07.2026: спиннер коллбэка гасится ДО блокирующего
    save_meal_to_db.

    Иначе при медленной/залоченной записи в nutrition_log (напр. лок дев-БД от
    ночного sync-prod-to-dev) кнопка «✅ Сохранить» висит дольше ~15с окна
    Telegram, а синхронный save морозит весь event-loop бота для всех
    пользователей. answer() должен идти первым, результат — через edit сообщения.
    """
    from handlers.photo import handle_meal_confirmation
    from handlers.callbacks import MealConfirmationCallback
    from services.state import UserState, state_manager

    events: list[str] = []

    user_id = "895659"
    state_manager.set_state(
        user_id,
        UserState(
            user_id=user_id,
            state="waiting_confirmation",
            data={
                "source": "text",
                "description": "банан",
                "meal_items": [{"product": "Банан", "weight_g": 120, "calories": 100}],
                "meal_totals": {"calories": 100},
                "meal_name": "Перекус",
            },
        ),
    )

    callback = _make_callback(user_id=895659)
    callback.answer = AsyncMock(side_effect=lambda *a, **k: events.append("answer"))
    callback_data = MealConfirmationCallback(action="save", meal_type="regular")

    def _save(*a, **k):
        events.append("save")
        return 777

    with (
        patch(SAVE_MEAL, side_effect=_save),
        patch(LOG_INTERACTION),
        patch(BUDGET_LINE, return_value=""),
        patch(GET_USER_SETTINGS, return_value=None),
        patch(SESSION_LOCAL, return_value=MagicMock()),
    ):
        await handle_meal_confirmation(callback, callback_data)

    assert events and events[0] == "answer", f"answer() должен предшествовать save, порядок: {events}"
    assert "save" in events


@pytest.mark.asyncio
async def test_single_meal_cancel_logs_food_interaction():
    from handlers.photo import handle_meal_confirmation
    from handlers.callbacks import MealConfirmationCallback
    from services.state import UserState, state_manager

    user_id = "895656"
    state_manager.set_state(
        user_id,
        UserState(
            user_id=user_id,
            state="waiting_confirmation",
            data={
                "source": "photo",
                "description": "борщ",
                "meal_items": [{"product": "Борщ", "weight_g": 300, "calories": 250}],
                "meal_totals": {"calories": 250},
                "meal_name": "Обед",
            },
        ),
    )

    callback = _make_callback(user_id=895656)
    callback_data = MealConfirmationCallback(action="cancel", meal_type="regular")

    with patch(LOG_INTERACTION) as mock_log:
        await handle_meal_confirmation(callback, callback_data)

    mock_log.assert_called_once()
    kwargs = mock_log.call_args.kwargs
    assert kwargs["user_id"] == 895656
    assert kwargs["source"] == "photo"
    assert kwargs["status"] == "cancelled"
    assert kwargs["nutrition_log_id"] is None

    assert state_manager.get_state(user_id) is None


@pytest.mark.asyncio
async def test_multi_meal_save_logs_once_per_meal():
    from handlers.photo import handle_meal_confirmation
    from handlers.callbacks import MealConfirmationCallback
    from services.state import UserState, state_manager

    user_id = "895657"
    state_manager.set_state(
        user_id,
        UserState(
            user_id=user_id,
            state="waiting_confirmation",
            data={
                "source": "text",
                "description": "завтрак и обед",
                "multi_meals": [
                    {"meal_name": "Завтрак", "meal_items": [{"product": "Яйца"}], "meal_totals": {"calories": 200}},
                    {"meal_name": "Обед", "meal_items": [{"product": "Суп"}], "meal_totals": {"calories": 400}},
                ],
            },
        ),
    )

    callback = _make_callback(user_id=895657)
    callback_data = MealConfirmationCallback(action="save", meal_type="regular")

    with (
        patch(SAVE_MEAL, side_effect=[111, 222]),
        patch(LOG_INTERACTION) as mock_log,
    ):
        await handle_meal_confirmation(callback, callback_data)

    assert mock_log.call_count == 2
    logged_ids = {call.kwargs["nutrition_log_id"] for call in mock_log.call_args_list}
    assert logged_ids == {111, 222}
    for call in mock_log.call_args_list:
        assert call.kwargs["status"] == "saved"
        assert call.kwargs["source"] == "text"


@pytest.mark.asyncio
async def test_multi_meal_partial_failure_does_not_log_failed_meal():
    from handlers.photo import handle_meal_confirmation
    from handlers.callbacks import MealConfirmationCallback
    from services.state import UserState, state_manager

    user_id = "895658"
    state_manager.set_state(
        user_id,
        UserState(
            user_id=user_id,
            state="waiting_confirmation",
            data={
                "source": "text",
                "multi_meals": [
                    {"meal_name": "Завтрак", "meal_items": [{"product": "Яйца"}], "meal_totals": {"calories": 200}},
                    {"meal_name": "Обед", "meal_items": [{"product": "Суп"}], "meal_totals": {"calories": 400}},
                ],
            },
        ),
    )

    callback = _make_callback(user_id=895658)
    callback_data = MealConfirmationCallback(action="save", meal_type="regular")

    with (
        patch(SAVE_MEAL, side_effect=[111, None]),  # второе блюдо не сохранилось
        patch(LOG_INTERACTION) as mock_log,
    ):
        await handle_meal_confirmation(callback, callback_data)

    mock_log.assert_called_once()
    assert mock_log.call_args.kwargs["nutrition_log_id"] == 111
