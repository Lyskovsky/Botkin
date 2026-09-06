"""
Fix 2 (text.py, 04.09.2026): замер АД, присланный вместе с вопросом («170/100
пульс 70, это нормально?»), раньше НЕ сохранялся — наличие "?" отменяло запись
целиком.

Fix 3 (text.py + core/llm/router.py, 06.09.2026): узкий фикс из Fix 2 отменил
вопросительный маркер СОВСЕМ, а не сузил его — из-за этого число в отвлечённом/
нормативном вопросе («правда ли, что 140/90 — это гипертония?») стало ошибочно
записываться как РЕАЛЬНЫЙ замер пользователя. Регэкспом «свой свежий замер» от
«числа в вопросе» не отличить — поэтому регэксп pre-check при наличии
вопросительного маркера теперь ПРОПУСКАЕТ детерминированное сохранение и отдаёт
решение LLM-роутеру (core/llm/router.py, SCENARIO 7), у которого достаточно
контекста, чтобы отличить «свой только что снятый замер» от «нормативного/
гипотетического/чужого/учебного» упоминания чисел.

Тесты проверяют ПУТЬ (сохранили/не сохранили в БД), а не только классификацию:
(а) «170/100 пульс 70, это нормально?» — свой замер + вопрос → сохраняется
    (LLM-роутер классифицирует как bp), вопрос уходит агенту без повторного лога;
(б) диапазон («давление бывает 140-120/85-70») — НЕ сохраняется;
(в) прошедшее время («вчера было 150/90») — НЕ сохраняется;
(г)-(ж) четыре варианта нормативных/гипотетических/чужих вопросов (Fix 3) —
    НЕ сохраняются, даже если в тексте есть BP-подобные числа и «?».

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
    """«170/100 пульс 70, это нормально?» — свой только что снятый замер + вопрос.

    С Fix 3 (06.09.2026) наличие "?" пропускает БЫСТРЫЙ регэксп-путь (тот не умеет
    отличить свой замер от числа в вопросе) — решение принимает LLM-роутер
    (мокаем его ответ как SCENARIO 7/bp, как и должно быть для этого сообщения).
    Замер должен сохраниться, а вопрос — уйти агенту без повторного log_bp.
    """
    from handlers.text import handle_text_message
    from services.state import state_manager

    user_id = 895657
    state_manager.clear_state(str(user_id))

    msg = _make_text_message(user_id, "170/100 пульс 70, это нормально?")

    mock_save_bp = MagicMock(return_value=True)
    mock_ask_agent = MagicMock(return_value="Это повышенное давление, стоит обратиться к врачу.")
    bp_result = {"type": "bp", "data": {"systolic": 170, "diastolic": 100, "pulse": 70}}

    with (
        patch(SAVE_BP, mock_save_bp),
        patch(LLM_ANALYZE, return_value=bp_result),
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


# ── Fix 3 (06.09.2026): нормативные/гипотетические/чужие вопросы с числами ───
# «Правда ли, что 140/90 — это уже гипертония?» и подобные — раньше (Fix 2)
# ошибочно записывались как РЕАЛЬНЫЙ замер пользователя: ни диапазона, ни
# прошедшего времени в тексте нет, а вопросительный маркер сохранению больше
# не мешал. Опаснее исходного бага: выдуманный замер молча ложится в историю
# АД, искажает динамику и попадает в отчёт для врача — не заметит никто.
#
# LLM-роутер (мокаем его ответ как SCENARIO 5/other) должен отличать такие
# сообщения от собственного свежего замера пользователя.


@pytest.mark.asyncio
async def test_bp_normative_threshold_question_not_saved():
    """«Правда ли, что 140/90 — это уже гипертония?» — вопрос про норму/порог,
    числа иллюстративные, НЕ замер пользователя. НЕ должно сохраняться."""
    from handlers.text import handle_text_message
    from services.state import state_manager

    user_id = 895660
    state_manager.clear_state(str(user_id))

    msg = _make_text_message(user_id, "Правда ли, что 140/90 — это уже гипертония?")

    mock_save_bp = MagicMock(return_value=True)
    other_result = {"type": "other", "data": {"reply": "140/90 — это уже повышенное давление (гипертония 1 степени)."}}

    with (
        patch(SAVE_BP, mock_save_bp),
        patch(LLM_ANALYZE, return_value=other_result),
        patch(ASK_AGENT, MagicMock(return_value="Да, это уже гипертония 1 степени.")),
        patch("logging.FileHandler", return_value=logging.NullHandler()),
    ):
        await handle_text_message(msg, user_id, MagicMock())

    mock_save_bp.assert_not_called()


@pytest.mark.asyncio
async def test_bp_normative_range_question_not_saved():
    """«Какое давление считается нормальным — 120/80?» — вопрос про норму,
    не отчёт о собственном замере. НЕ должно сохраняться."""
    from handlers.text import handle_text_message
    from services.state import state_manager

    user_id = 895661
    state_manager.clear_state(str(user_id))

    msg = _make_text_message(user_id, "Какое давление считается нормальным — 120/80?")

    mock_save_bp = MagicMock(return_value=True)
    other_result = {"type": "other", "data": {"reply": "120/80 считается нормальным давлением."}}

    with (
        patch(SAVE_BP, mock_save_bp),
        patch(LLM_ANALYZE, return_value=other_result),
        patch(ASK_AGENT, MagicMock(return_value="Да, 120/80 — это норма.")),
        patch("logging.FileHandler", return_value=logging.NullHandler()),
    ):
        await handle_text_message(msg, user_id, MagicMock())

    mock_save_bp.assert_not_called()


@pytest.mark.asyncio
async def test_bp_third_party_advice_question_not_saved():
    """«Таня сказала, при 160/100 надо принимать каптоприл, это так?» — чужое
    правило, а не собственный замер пользователя. НЕ должно сохраняться."""
    from handlers.text import handle_text_message
    from services.state import state_manager

    user_id = 895662
    state_manager.clear_state(str(user_id))

    msg = _make_text_message(user_id, "Таня сказала, при 160/100 надо принимать каптоприл, это так?")

    mock_save_bp = MagicMock(return_value=True)
    other_result = {
        "type": "other",
        "data": {"reply": "Это стоит обсудить с врачом, не занимайся самолечением по совету знакомых."},
    }

    with (
        patch(SAVE_BP, mock_save_bp),
        patch(LLM_ANALYZE, return_value=other_result),
        patch(ASK_AGENT, MagicMock(return_value="Лучше уточнить дозировку у врача.")),
        patch("logging.FileHandler", return_value=logging.NullHandler()),
    ):
        await handle_text_message(msg, user_id, MagicMock())

    mock_save_bp.assert_not_called()


@pytest.mark.asyncio
async def test_bp_hypothetical_emergency_threshold_not_saved():
    """«При каком давлении вызывать скорую, при 180/110?» — гипотетический
    порог, не измерение прямо сейчас. НЕ должно сохраняться."""
    from handlers.text import handle_text_message
    from services.state import state_manager

    user_id = 895663
    state_manager.clear_state(str(user_id))

    msg = _make_text_message(user_id, "При каком давлении вызывать скорую, при 180/110?")

    mock_save_bp = MagicMock(return_value=True)
    other_result = {"type": "other", "data": {"reply": "Скорую стоит вызывать при давлении от 180/110 и выше."}}

    with (
        patch(SAVE_BP, mock_save_bp),
        patch(LLM_ANALYZE, return_value=other_result),
        patch(ASK_AGENT, MagicMock(return_value="При 180/110 и выше — уже повод звонить в скорую.")),
        patch("logging.FileHandler", return_value=logging.NullHandler()),
    ):
        await handle_text_message(msg, user_id, MagicMock())

    mock_save_bp.assert_not_called()
