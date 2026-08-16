"""Инбокс обратной связи (#188, Фаза 1). /feedback (юзер) + /feedback_queue (admin)."""

import logging

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from config.users import is_admin

logger = logging.getLogger(__name__)
router = Router()

_KIND_EMOJI = {"bug": "🐞", "feature": "💡", "question": "❓", "unspecified": "📝"}


def strip_feedback_prefix(raw: str) -> str:
    """Убрать '/feedback' (и '@botname') из начала сообщения, вернуть чистый текст."""
    parts = raw.strip().split(maxsplit=1)
    if not parts:
        return ""
    return parts[1].strip() if len(parts) > 1 else ""


def format_feedback_queue(rows: list) -> str:
    """Отрендерить очередь для админа."""
    if not rows:
        return "🆕 В очереди фидбека пусто."
    lines = ["🆕 Очередь фидбека (последние новые):", ""]
    for r in rows:
        emoji = _KIND_EMOJI.get(r.kind, "📝")
        lines.append(f"#{r.id} · {emoji} {r.kind} · [{r.source}]")
        lines.append(f"{r.user_id}: {r.text}")
        note = (r.agent_context or {}).get("agent_note") if isinstance(r.agent_context, dict) else None
        if note:
            lines.append(f"↳ {note}")
        lines.append("")
    return "\n".join(lines).strip()


@router.message(Command("feedback"))
async def cmd_feedback(message: Message) -> None:
    text = strip_feedback_prefix(message.text or "")
    if not text:
        await message.answer(
            "Напиши после команды, что не так или что хочешь улучшить.\n"
            "Например: /feedback вес на дашборде показывает неверно"
        )
        return

    from database import SessionLocal
    from database.crud import create_feedback, is_feedback_opted_out

    user_id = message.from_user.id
    db = SessionLocal()
    try:
        if is_feedback_opted_out(db, user_id):
            await message.answer("Спасибо! ✅")
            return
        create_feedback(db, user_id=user_id, text=text, source="command")
    except Exception as e:
        db.rollback()
        logger.error(f"feedback: не смог сохранить: {e}")
        await message.answer("❌ Не получилось сохранить фидбек, попробуй ещё раз позже.")
        return
    finally:
        db.close()
    await message.answer("Спасибо, передал разработчикам ✅ Вернёмся, когда разберём.")


@router.message(Command("feedback_queue"))
async def cmd_feedback_queue(message: Message) -> None:
    if not is_admin(message.from_user.id):
        await message.answer("Команда доступна только администраторам.")
        return

    from database import SessionLocal
    from database.crud import list_recent_feedback

    db = SessionLocal()
    try:
        rows = list_recent_feedback(db, status="new", limit=20)
    except Exception as e:
        logger.error(f"feedback_queue: не смог получить очередь: {e}")
        await message.answer("❌ Не смог получить очередь фидбека.")
        return
    finally:
        db.close()
    # parse_mode=None: очередь содержит пользовательский текст (r.text/agent_note), а бот
    # работает с глобальным parse_mode=HTML — без этого символ '<' в фидбеке ломает разметку
    # (400 → админ не видит очередь) или инъектит HTML. Форматирование очереди не нужно.
    await message.answer(format_feedback_queue(rows), parse_mode=None)
