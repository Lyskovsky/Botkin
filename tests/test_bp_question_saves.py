"""
Fix 2 (text.py, 04.09.2026): замер АД, присланный вместе с вопросом («170/100
пульс 70, это нормально?»), раньше НЕ сохранялся — наличие "?" отменяло запись
целиком. Тест проверяет: (а) такой замер сохраняется И вопрос уходит агенту;
(б) диапазон («давление бывает 140-120/85-70») по-прежнему НЕ сохраняется;
(в) прошедшее время («вчера было 150/90») по-прежнему НЕ сохраняется; (г) нет
двойной записи (save_bp_to_db вызывается ровно один раз для вопроса-с-замером).

Мокаем все LLM-вызовы (analyze_message, ask_agent) — реальных запросов к моделям
не делаем.
"""

import logging
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
SAVE_BP = "helpers.db_save.save_bp_to_db"


def _make_text_message(user_id: int, text: str):
    msg = MagicMock()
    msg.from_user = MagicMock()
    msg.from_user.id = user_id
    msg.text = text
    msg.photo = None
    msg.answer = AsyncMock(return_value=MagicMock())
    msg.bot = AsyncMock()
    return msg


@pytest.mark.asyncio
async def test_bp_with_question_saves_and_answers(tmp_path):
    """«170/100 пульс 70, это нормально?» — раньше "?" отменял сохранение целиком.
    Теперь: замер сохраняется детерминированно И вопрос отдельно уходит агенту."""
    from handlers.text import handle_text_message
    from services.state import state_manager

    user_id = 895657
    state_manager.clear_state(str(user_id))

    msg = _make_text_message(user_id, "170/100 пульс 70, это нормально?")

    mock_save_bp = MagicMock(return_value=True)
    mock_ask_agent = MagicMock(return_value="Это повышенное давление, стоит обратиться к врачу.")

    with (
        patch(SAVE_BP, mock_save_bp),
        patch(ASK_AGENT, mock_ask_agent),
        patch("logging.FileHandler", return_value=logging.NullHandler()),
    ):
        await handle_text_message(msg, user_id, MagicMock())

    # Замер должен быть сохранён РОВНО ОДИН РАЗ (не задвоен)
    assert mock_save_bp.call_count == 1, "Двойная запись АД — save_bp_to_db вызван не 1 раз"
    _, kwargs = mock_save_bp.call_args
    assert kwargs["systolic"] == 170
    assert kwargs["diastolic"] == 100
    assert kwargs["pulse"] == 70

    # Вопрос должен был уйти агенту ЗА ОТВЕТОМ, но БЕЗ повторного лога
    assert mock_ask_agent.called, "Вопрос пользователя должен был уйти агенту на ответ"
    agent_prompt = mock_ask_agent.call_args.args[1]
    assert "log_bp" in agent_prompt or "НЕ вызывай" in agent_prompt  # явный запрет повторной записи


@pytest.mark.asyncio
async def test_bp_range_description_not_saved(tmp_path):
    """«давление бывает 140-120/85-70» — описание диапазона, не единичный замер.
    НЕ должно сохраняться (поведение не меняется этим фиксом)."""
    from handlers.text import handle_text_message
    from services.state import state_manager

    user_id = 895658
    state_manager.clear_state(str(user_id))

    msg = _make_text_message(user_id, "давление бывает 140-120/85-70")

    mock_save_bp = MagicMock(return_value=True)
    other_result = {"type": "other", "data": {"reply": "Похоже на диапазон давления, уточни детали."}}

    with (
        patch(SAVE_BP, mock_save_bp),
        patch(LLM_ANALYZE, return_value=other_result),
        patch(ASK_AGENT, MagicMock(return_value="Понял, это диапазон.")),
        patch("logging.FileHandler", return_value=logging.NullHandler()),
    ):
        await handle_text_message(msg, user_id, MagicMock())

    mock_save_bp.assert_not_called()


@pytest.mark.asyncio
async def test_bp_past_tense_not_saved(tmp_path):
    """«вчера было 150/90» — прошедшее время, описание истории, а не текущий лог.
    НЕ должно сохраняться (поведение не меняется этим фиксом)."""
    from handlers.text import handle_text_message
    from services.state import state_manager

    user_id = 895659
    state_manager.clear_state(str(user_id))

    msg = _make_text_message(user_id, "вчера было 150/90")

    mock_save_bp = MagicMock(return_value=True)
    other_result = {"type": "other", "data": {"reply": "Посмотрю историю давления за вчера."}}

    with (
        patch(SAVE_BP, mock_save_bp),
        patch(LLM_ANALYZE, return_value=other_result),
        patch(ASK_AGENT, MagicMock(return_value="Вчера было 150/90 по твоим словам.")),
        patch("logging.FileHandler", return_value=logging.NullHandler()),
    ):
        await handle_text_message(msg, user_id, MagicMock())

    mock_save_bp.assert_not_called()
