"""
Fix 1 (photo.py + router.py, 04.09.2026): vision не читала текст с фото упаковки
лекарства — даже когда LLM реально распознала текст (SCENARIO 5.1 "medical" в
core/llm/router.py), код в handlers/photo.py выбрасывал это распознавание и
подставлял агенту фиксированную фразу «LLM-vision не распознал…». Тест проверяет,
что распознанный текст ("reply" из router_result) теперь доходит до ask_agent.

Мокаем все LLM-вызовы (analyze_message, ask_agent) — реальных запросов к моделям
не делаем.
"""

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ── project root on sys.path ─────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent
BOT_ROOT = PROJECT_ROOT / "telegram-bot"
for p in [str(PROJECT_ROOT), str(BOT_ROOT)]:
    if p not in sys.path:
        sys.path.insert(0, p)

# Patch targets (lazy imports inside function bodies — must patch at source)
LLM_ANALYZE = "core.llm.router.analyze_message"
ASK_AGENT = "core.agent_chat.ask_agent"
ARCHIVE_PHOTO = "handlers.doc_upload.archive_photo_as_document"


def _make_photo_message(user_id: int, caption: str):
    processing_msg = AsyncMock()
    processing_msg.edit_text = AsyncMock()

    msg = AsyncMock()
    msg.from_user = MagicMock()
    msg.from_user.id = user_id
    msg.caption = caption
    msg.text = None
    msg.photo = [MagicMock(file_id="fake_file_id")]
    msg.answer = AsyncMock(return_value=processing_msg)
    msg.bot = AsyncMock()
    return msg, processing_msg


@pytest.mark.asyncio
async def test_medical_photo_reply_forwarded_to_agent(tmp_path):
    """Router возвращает type='medical' с непустым data.reply (SCENARIO 5.1) —
    код должен передать РАСПОЗНАННЫЙ ТЕКСТ агенту, а не фиксированную фразу
    «LLM-vision не распознал…»."""
    from handlers.photo import handle_description
    from services.state import state_manager
    from services.state_helpers import create_photo_state

    user_id = "895655"
    state_manager.clear_state(user_id)

    fake_photo_path = tmp_path / "photo.jpg"
    fake_photo_path.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 20)

    caption = "у тебя есть данные по этому препарату?"
    state = create_photo_state(
        user_id=user_id,
        photo_paths=[fake_photo_path],
        photo_file_ids=["fake_file_id"],
        caption=caption,
    )
    state_manager.set_state(user_id, state)

    msg, processing_msg = _make_photo_message(int(user_id), caption)

    recognized_text = "На фото упаковка «Омник», действующее вещество тамсулозин, дозировка 0.4 мг."
    medical_result = {"type": "medical", "data": {"reply": recognized_text}}

    mock_ask_agent = MagicMock(return_value="Да, есть данные — Омник (тамсулозин) применяется при аденоме простаты.")

    with (
        patch(LLM_ANALYZE, return_value=medical_result),
        patch(ARCHIVE_PHOTO, return_value="archived.jpg"),
        patch(ASK_AGENT, mock_ask_agent),
    ):
        await handle_description(msg, description=None, processing_message=processing_msg)

    assert mock_ask_agent.called, "ask_agent должен был быть вызван для нераспознанного как food/vitamins/bp фото"
    call_args = mock_ask_agent.call_args.args
    prompt_sent_to_agent = call_args[1]

    # Распознанный текст ДОЛЖЕН попасть в промпт агента
    assert "тамсулозин" in prompt_sent_to_agent or "Омник" in prompt_sent_to_agent
    # Старая фиксированная фраза-заглушка НЕ должна маскировать реально распознанный текст
    assert "не распознал на фото еду" not in prompt_sent_to_agent


@pytest.mark.asyncio
async def test_medical_photo_without_reply_keeps_stock_message(tmp_path):
    """Если router вернул type='other'/'medical' БЕЗ reply (вообще ничего не разобрал) —
    код по-прежнему честно говорит агенту, что vision не распознала фото (регресс-guard,
    чтобы фикс не начал выдумывать несуществующий reply)."""
    from handlers.photo import handle_description
    from services.state import state_manager
    from services.state_helpers import create_photo_state

    user_id = "895656"
    state_manager.clear_state(user_id)

    fake_photo_path = tmp_path / "photo2.jpg"
    fake_photo_path.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 20)

    caption = "что это?"
    state = create_photo_state(
        user_id=user_id,
        photo_paths=[fake_photo_path],
        photo_file_ids=["fake_file_id"],
        caption=caption,
    )
    state_manager.set_state(user_id, state)

    msg, processing_msg = _make_photo_message(int(user_id), caption)

    other_result = {"type": "other", "data": {"reply": ""}}
    mock_ask_agent = MagicMock(return_value="Не могу понять, что на фото — опиши текстом.")

    with (
        patch(LLM_ANALYZE, return_value=other_result),
        patch(ARCHIVE_PHOTO, return_value="archived.jpg"),
        patch(ASK_AGENT, mock_ask_agent),
    ):
        await handle_description(msg, description=None, processing_message=processing_msg)

    assert mock_ask_agent.called
    prompt_sent_to_agent = mock_ask_agent.call_args.args[1]
    assert "не распознал на фото еду" in prompt_sent_to_agent
