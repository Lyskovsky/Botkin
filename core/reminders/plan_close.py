"""Чистая логика вечернего вопроса «план доеден целиком?» (#407, задача 8).

«План» — строка nutrition_log со status='plan': еда, залогированная авансом,
уже посчитанная в итоге дня. Раз в сутки, в вечернем окне, если у пользователя
остались открытые планы на сегодня — спрашиваем один раз одним сообщением
с двумя кнопками. Зеркалит подход core/reminders/meal_reminders.py: диспетчер
(scripts/server/send_reminders.py) поставляет сюда уже посчитанные из БД факты
(now в локальной TZ, счётчик планов и ккал), а решение «пора ли спрашивать» и
тексты/клавиатура — здесь, чисто и тестируемо.
"""

from __future__ import annotations

from datetime import datetime, time

# Ключ дедупа внутри UserSettings.meal_reminder_last_sent (тот же словарь, что
# используют слоты питания и добавки — просто отдельный ключ).
PLAN_CLOSE_KEY = "__plan_close__"

# Локальное окно, когда задаём вопрос (включительно с обеих сторон).
PLAN_CLOSE_WINDOW: tuple[time, time] = (time(21, 0), time(22, 59))


def should_ask(now_local: datetime, last_sent: dict, today_iso: str) -> bool:
    """True, если локальное время в вечернем окне и сегодня ещё не спрашивали."""
    if (last_sent or {}).get(PLAN_CLOSE_KEY) == today_iso:
        return False
    start, end = PLAN_CLOSE_WINDOW
    return start <= now_local.time() <= end


def _plural_ru(n: int, one: str, few: str, many: str) -> str:
    """Русское склонение по числу: 1 запись / 2-4 записи / 0,5,11-14... записей."""
    n_abs = abs(n)
    if n_abs % 10 == 1 and n_abs % 100 != 11:
        return one
    if 2 <= n_abs % 10 <= 4 and not (12 <= n_abs % 100 <= 14):
        return few
    return many


def plans_word(n: int) -> str:
    """Склонение слова «запись» под число открытых планов."""
    return _plural_ru(n, "запись", "записи", "записей")


def build_question(plans_count: int, kcal_total: float) -> str:
    """Текст вечернего вопроса про открытые планы."""
    word = plans_word(plans_count)
    return (
        f"📋 План на сегодня ещё открыт: {plans_count} {word}, {kcal_total:.0f} ккал "
        f"(уже в итоге дня).\nДоедено целиком?"
    )


def build_keyboard(date_iso: str) -> dict:
    """reply_markup для Bot API sendMessage/editMessageText.

    callback_data собран вручную в формате, который паппит aiogram'овский
    CallbackData (prefix:field1:field2, разделитель ':') — core/ не может
    импортировать telegram-bot/handlers/callbacks.py, поэтому эквивалентность
    проверяется тестом (tests/test_plan_close.py), а не общим кодом.
    """
    return {
        "inline_keyboard": [
            [
                {"text": "✅ Да, всё", "callback_data": f"plan:all:{date_iso}"},
                {"text": "✏️ Что-то осталось", "callback_data": f"plan:edit:{date_iso}"},
            ]
        ]
    }
