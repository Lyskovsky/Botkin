"""Регресс на 04d7ec5 (#322): декоратор @router.callback_query(...) не должен
«съезжать» с handle_meal_confirmation на внутреннюю обёртку _maybe_record_first_food.

Баг: при вставке обёртки первым-food-празднования декоратор оказался над
_maybe_record_first_food, а handle_meal_confirmation остался без регистрации.
Итог — нажатие «Сохранить» вело в обёртку, aiogram передавал CallbackQuery в
позиционный telegram_user_id, message отсутствовал → TypeError до callback.answer,
еда не сохранялась (симптом «зависания» meal-save 16.07).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "telegram-bot"))


def _registered_callbacks(observer):
    return [h.callback for h in observer.handlers]


def test_meal_confirmation_button_routes_to_save_handler():
    import handlers.photo as photo

    callbacks = _registered_callbacks(photo.router.callback_query)

    assert photo.handle_meal_confirmation in callbacks, (
        "handle_meal_confirmation не зарегистрирован как callback_query-хендлер (декоратор съехал)"
    )


def test_first_food_wrapper_is_not_a_handler():
    import handlers.photo as photo

    callbacks = _registered_callbacks(photo.router.callback_query)

    assert photo._maybe_record_first_food not in callbacks, (
        "_maybe_record_first_food — внутренний хелпер, он не должен быть зарегистрирован как хендлер"
    )
