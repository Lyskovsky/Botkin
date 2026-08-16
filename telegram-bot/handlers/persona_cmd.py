"""Команда /persona — сменить тон агента после онбординга.

Inline-кнопки 4 персон (core.personas) → callback пишет
users.onboarding_data["persona"]. build_default_agent_prompt (PR1) подхватит
новый тон на следующем ответе агента.
"""

import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from database import SessionLocal
from database.models import User
from core.personas import PERSONAS, get_persona

logger = logging.getLogger(__name__)
router = Router()

_PREFIX = "persona:"


def _persona_inline_kb() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=p.display, callback_data=f"{_PREFIX}{p.key}")] for p in PERSONAS.values()]
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(Command("persona"))
async def cmd_persona(message: Message) -> None:
    await message.answer("Каким тоном мне с тобой общаться?", reply_markup=_persona_inline_kb())


async def apply_persona_choice(telegram_user_id: int, key: str, callback: CallbackQuery) -> None:
    db = SessionLocal()
    try:
        user = db.query(User).filter_by(telegram_id=telegram_user_id).first()
        if not user:
            await callback.answer("Сначала пройди /start")
            return
        data = dict(user.onboarding_data or {})
        data["persona"] = key
        user.onboarding_data = data
        db.commit()
        persona = get_persona(key)
        await callback.answer(f"Готово: {persona.display}")
        try:
            await callback.message.edit_text(f"Теперь общаюсь в манере «{persona.display}».")
        except Exception:
            pass
    finally:
        db.close()


@router.callback_query(F.data.startswith(_PREFIX))
async def on_persona_pick(callback: CallbackQuery) -> None:
    key = callback.data[len(_PREFIX) :]
    if key not in PERSONAS:
        await callback.answer("Неизвестная персона")
        return
    await apply_persona_choice(callback.from_user.id, key, callback)
