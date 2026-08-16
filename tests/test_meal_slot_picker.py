"""Слот-пикер на карточке подтверждения фото без подписи (#181, баг 4).

Фото еды без подписи уходит в handle_menu_photo, где слот молча выводился по
времени (боул в 16:00 → «перекус»). Теперь карточка показывает ряд выбора слота,
а тап фиксирует его через meal_time = центр слота.

Patch strategy зеркалит tests/test_meal_confirmation_logging.py (lazy-import
внутри функции → патчим в модуле-источнике).
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


def _flatten(markup):
    return [btn for row in markup.inline_keyboard for btn in row]


def test_keyboard_with_slot_shows_four_slots_and_marks_default():
    from handlers.photo import _meal_confirm_keyboard

    markup = _meal_confirm_keyboard("menu", selected_slot="lunch")
    buttons = _flatten(markup)

    slot_buttons = [b for b in buttons if "set_slot" in b.callback_data]
    assert len(slot_buttons) == 4, "должны быть 4 кнопки слотов"

    labels = [b.text for b in slot_buttons]
    assert any("Завтрак" in x for x in labels)
    assert any("Обед" in x for x in labels)
    assert any("Ужин" in x for x in labels)
    assert any("Перекус" in x for x in labels)

    # Дефолт (lunch → «Обед») помечен маркером, остальные — без него.
    marked = [b.text for b in slot_buttons if b.text.startswith("🔘")]
    assert marked == ["🔘 Обед"]

    # Штатные действия на месте.
    assert any("Сохранить" in b.text for b in buttons)
    assert any(b.text.startswith("❌") for b in buttons)


def test_keyboard_without_slot_has_no_slot_row():
    from handlers.photo import _meal_confirm_keyboard

    markup = _meal_confirm_keyboard("regular")
    buttons = _flatten(markup)

    assert not any("set_slot" in b.callback_data for b in buttons), "без слота — нет слот-ряда (регресс)"
    assert any("Сохранить" in b.text for b in buttons)
    assert any("Отмена" in b.text for b in buttons)


@pytest.mark.asyncio
async def test_set_slot_updates_state_and_does_not_save():
    from handlers.photo import handle_meal_confirmation
    from handlers.callbacks import MealConfirmationCallback
    from services.state import UserState, state_manager

    user_id = "424242"
    state_manager.set_state(
        user_id,
        UserState(
            user_id=user_id,
            state="waiting_confirmation",
            data={
                "source": "photo",
                "dish_name": "Боул с киноа",
                "meal_items": [{"product": "Боул с киноа", "calories": 511}],
                "meal_totals": {"calories": 511},
                "meal_time": "16:05",
                "slot": "snack",
            },
        ),
    )

    callback = AsyncMock()
    callback.from_user = MagicMock()
    callback.from_user.id = 424242
    callback.message = AsyncMock()
    callback_data = MealConfirmationCallback(action="set_slot", meal_type="menu", slot="lunch")

    with patch(SAVE_MEAL) as mock_save:
        await handle_meal_confirmation(callback, callback_data)

    mock_save.assert_not_called()  # выбор слота ничего не сохраняет
    callback.message.edit_reply_markup.assert_awaited_once()  # карточка перерисована

    state = state_manager.get_state(user_id)
    assert state is not None, "состояние сохраняется — карточка ещё активна"
    assert state.data["slot"] == "lunch"
    assert state.data["meal_time"] == "13:00"  # центр слота «обед»

    state_manager.clear_state(user_id)


@pytest.mark.asyncio
async def test_save_after_set_slot_persists_chosen_slot_meal_time():
    from handlers.photo import handle_meal_confirmation
    from handlers.callbacks import MealConfirmationCallback
    from services.state import UserState, state_manager

    user_id = "424243"
    state_manager.set_state(
        user_id,
        UserState(
            user_id=user_id,
            state="waiting_confirmation",
            data={
                "source": "photo",
                "dish_name": "Боул с киноа",
                "meal_items": [{"product": "Боул с киноа", "calories": 511}],
                "meal_totals": {"calories": 511},
                "meal_time": "16:05",
                "slot": "snack",
            },
        ),
    )

    callback = AsyncMock()
    callback.from_user = MagicMock()
    callback.from_user.id = 424243
    callback.message = AsyncMock()
    callback.message.text = "🍽️ Боул с киноа"

    # 1. Пользователь выбирает «Обед».
    set_slot = MealConfirmationCallback(action="set_slot", meal_type="menu", slot="lunch")
    with patch(SAVE_MEAL) as mock_save:
        await handle_meal_confirmation(callback, set_slot)
    mock_save.assert_not_called()

    # 2. Затем «Сохранить» — запись должна уйти со слотом «обед» (meal_time=13:00).
    save = MealConfirmationCallback(action="save", meal_type="menu")
    with (
        patch(SAVE_MEAL, return_value=777) as mock_save,
        patch(LOG_INTERACTION),
        patch(BUDGET_LINE, return_value=""),
        patch(GET_USER_SETTINGS, return_value=None),
        patch(SESSION_LOCAL, return_value=MagicMock()),
    ):
        await handle_meal_confirmation(callback, save)

    mock_save.assert_called_once()
    saved_data = mock_save.call_args.args[0]
    assert saved_data["meal_time"] == "13:00", "сохраняем время центра выбранного слота, а не now()"

    assert state_manager.get_state(user_id) is None  # после сохранения состояние очищено
