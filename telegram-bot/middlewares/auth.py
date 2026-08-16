"""
Authorization middleware for Telegram bot.

Open registration: any Telegram user can sign up via /start.
Blocked users (is_active=False) are rejected. Admin can block via /block.
"""

from typing import Callable, Dict, Any, Awaitable
from aiogram import BaseMiddleware
from aiogram.types import Message, TelegramObject
import sys
import logging
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

logger = logging.getLogger(__name__)


class AuthMiddleware(BaseMiddleware):
    """
    Middleware for open-registration bot.

    - Any user can start; ensure_user_exists() registers them on first message.
    - Blocked users (is_active=False) are rejected with a friendly message.
    - Passes user_id / username / first_name to all handlers.
    """

    async def __call__(
        self, handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]], event: Message, data: Dict[str, Any]
    ) -> Any:
        # Skip non-message updates (inline queries, etc.)
        if not isinstance(event, Message):
            return await handler(event, data)

        user_id = event.from_user.id
        username = event.from_user.username or ""
        first_name = event.from_user.first_name or "User"

        # Register user if new, update last_active if existing.
        # Returns User ORM object with is_active field.
        try:
            from database import SessionLocal
            from database.crud import ensure_user_exists

            db = SessionLocal()
            try:
                user = ensure_user_exists(db, telegram_id=user_id, username=username, first_name=first_name)
                is_active = user.is_active
            finally:
                db.close()
        except Exception as e:
            logger.error(f"AuthMiddleware DB error for user {user_id}: {e}", exc_info=True)
            await event.answer("⚠️ Временная ошибка сервера. Попробуй ещё раз через минуту.")
            return

        if not is_active:
            await event.answer(
                "🚫 Ваш аккаунт заблокирован.\nЕсли вы считаете, что это ошибка — напишите администратору."
            )
            return

        # Pass user info to all handlers
        data["user_id"] = user_id
        data["username"] = username
        data["first_name"] = first_name

        return await handler(event, data)
