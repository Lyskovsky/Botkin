import asyncio
import logging
from pathlib import Path

from aiogram import Router, F, Bot
from aiogram.types import Message

from core.infra.voice_service import voice_service
from handlers.text import _is_clearly_conversational
from services.state import state_manager, UserState

router = Router()

logger = logging.getLogger(__name__)


@router.message(F.voice)
async def handle_voice_message(message: Message, bot: Bot, user_id: int):
    """
    Обработчик голосовых сообщений.
    Скачивает файл, транскрибирует, затем маршрутизирует:
    - Явно не-еда (вопросы, медицинские темы) → BotkinClaw агент (#159)
    - Потенциальная еда → handle_description (food log)
    """
    try:
        await bot.send_chat_action(chat_id=message.chat.id, action="typing")

        file_id = message.voice.file_id
        file_info = await bot.get_file(file_id)
        file_path = file_info.file_path

        save_dir = Path("data/media/voice")
        save_dir.mkdir(parents=True, exist_ok=True)

        local_filename = f"{message.voice.file_unique_id}.ogg"
        local_path = save_dir / local_filename

        await bot.download_file(file_path, local_path)

        text = await voice_service.transcribe(local_path)

        await message.reply(f"🎤 <b>Распознано:</b>\n\n{text}", parse_mode="HTML")

        text_stripped = text.strip()
        if len(text_stripped) <= 3:
            return

        uid_str = str(message.from_user.id)

        # Быстрый путь: явно разговорный/медицинский вопрос → сразу в агент,
        # минуя food-роутер. Экономит токены и не показывает «не еда» (#159).
        if _is_clearly_conversational(text_stripped):
            await _route_voice_to_agent(message, uid_str, text_stripped)
            return

        # Потенциально еда: запускаем через handle_description.
        # caption сохраняем = транскрибированный текст — это позволяет
        # handle_description передать его в агент, если LLM-роутер вернёт «other».
        from handlers.photo import handle_description

        new_user_state = UserState(
            user_id=uid_str,
            state="waiting_description",
            data={
                "source": "voice",
                "media_path": str(local_path),
                "photo_paths": [],
                "photo_file_ids": [],
                "caption": text_stripped,  # fallback → агент при «other» (#159)
            },
        )
        state_manager.set_state(uid_str, new_user_state)

        processing_msg = await message.answer("🤖 Анализирую через ИИ: еда, время, вес, КБЖУ... ⏳")
        await handle_description(message, text_stripped, processing_message=processing_msg)

    except Exception as e:
        logger.exception(f"Error handling voice message: {e}")
        await message.reply(f"❌ Произошла ошибка при обработке голоса: {e}")


async def _route_voice_to_agent(message: Message, user_id: str, text: str) -> None:
    """Маршрутизирует транскрибированный голосовой текст в BotkinClaw агент."""
    from core.agent_chat import ask_agent
    from core.tg_markdown import md_to_html, split_markdown_for_telegram

    progress_msg = await message.answer("⏳ думаю...")
    loop = asyncio.get_running_loop()

    import time as _time

    _last_edit: dict = {"t": 0.0, "text": ""}

    def _progress(label: str) -> None:
        now = _time.monotonic()
        if now - _last_edit["t"] < 0.8:
            return
        if label == _last_edit["text"]:
            return
        _last_edit["t"] = now
        _last_edit["text"] = label
        try:
            coro = message.bot.edit_message_text(
                chat_id=progress_msg.chat.id,
                message_id=progress_msg.message_id,
                text=f"⏳ {label}…",
            )
            asyncio.run_coroutine_threadsafe(coro, loop)
        except Exception as _e:
            logger.debug(f"voice agent progress edit failed: {_e}")

    reply = await loop.run_in_executor(
        None,
        lambda: ask_agent(int(user_id), text, _progress),
    )

    if not reply:
        reply = "Хм, у меня нет внятного ответа. Попробуй переформулировать."

    chunks = split_markdown_for_telegram(reply)
    first = True
    for chunk in chunks:
        chunk_html = md_to_html(chunk)
        try:
            if first:
                await progress_msg.edit_text(chunk_html, parse_mode="HTML")
                first = False
            else:
                await message.answer(chunk_html, parse_mode="HTML")
        except Exception:
            if first:
                await progress_msg.edit_text(chunk)
                first = False
            else:
                await message.answer(chunk)
