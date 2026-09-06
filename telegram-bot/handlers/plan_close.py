#!/usr/bin/env python3
"""Обработчик кнопок вечернего вопроса «план на сегодня доеден целиком?» (#407).

Вопрос шлёт диспетчер scripts/server/send_reminders.py::dispatch_plan_close
(вне aiogram, через сырой Bot API sendMessage). Этот router обрабатывает только
нажатия на его две кнопки: "Да, всё" закрывает все открытые планы дня
(status='plan' -> 'eaten'), "Что-то осталось" — просит написать текстом, что
изменилось (правки идут обычным confirm-flow текстовых сообщений).
"""

import logging

from aiogram import Router
from aiogram.types import CallbackQuery

from database import SessionLocal
from database.crud import get_open_plans, set_meal_status
from handlers.callbacks import PlanCloseCallback
from handlers.photo import safe_edit_text

try:
    from core.reminders.plan_close import plans_word
except ImportError:  # pragma: no cover — защитный fallback, не должен срабатывать

    def plans_word(n: int) -> str:
        return "записей"


logger = logging.getLogger(__name__)

router = Router()


@router.callback_query(PlanCloseCallback.filter())
async def handle_plan_close(callback: CallbackQuery, callback_data: PlanCloseCallback):
    """Обработчик нажатия на кнопки вечернего вопроса про открытые планы."""
    user_id = callback.from_user.id

    try:
        if callback_data.action == "edit":
            await callback.answer()
            text = (
                "Напиши мне, что изменилось — например «минус 2 яйца», «половину творога "
                "не съела» или «остаток на завтра». Я покажу, что поменяю, и спрошу подтверждение."
            )
            await safe_edit_text(callback.message, text)
            return

        if callback_data.action != "all":
            await callback.answer()
            return

        await callback.answer()

        year, month, day = (int(x) for x in callback_data.date.split("-"))
        from datetime import date as date_cls

        for_date = date_cls(year, month, day)

        db = SessionLocal()
        try:
            open_plans = get_open_plans(db, user_id, for_date)
            if not open_plans:
                await safe_edit_text(callback.message, "Открытых планов уже нет.")
                return

            kcal_total = 0.0
            for plan in open_plans:
                totals = plan.totals or {}
                kcal_total += float(totals.get("calories", 0) or 0)
                set_meal_status(db, plan.id, user_id, "eaten")

            count = len(open_plans)
            word = plans_word(count)
            text = f"✅ План закрыт: {count} {word} · {kcal_total:.0f} ккал учтены как съеденные."
            await safe_edit_text(callback.message, text)
        finally:
            db.close()
    except Exception:
        logger.exception("Ошибка обработки PlanCloseCallback для user_id=%s", user_id)
        raise
