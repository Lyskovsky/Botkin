"""Роутинг короткого ответа после вопроса агента → агенту, не парсеру (#198).

Два детектора:
  - `_looks_like_short_value` (handlers/text.py) — чистая функция над текстом.
  - `agent_last_turn_was_question` (core/agent_chat.py) — читает последний
    assistant-ход из agent_conversations; патчим SessionLocal (мок строки).
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
BOT_ROOT = PROJECT_ROOT / "telegram-bot"
for p in [str(PROJECT_ROOT), str(BOT_ROOT)]:
    if p not in sys.path:
        sys.path.insert(0, p)


# ── _looks_like_short_value ─────────────────────────────────────────────────


def test_short_value_positive():
    from handlers.text import _looks_like_short_value

    for t in ["54", "72.5", "72,5 кг", "80 kg", "120/80", "130/85/70", "5000 ме", "2 таб"]:
        assert _looks_like_short_value(t) is True, t


def test_short_value_negative():
    from handlers.text import _looks_like_short_value

    for t in [
        "съел борщ",
        "какой у меня вес?",  # вопрос
        "2024",  # год (>3 цифр в одиночном числе)
        "54 капсулы витамина д утром натощак",  # длинный текст
        "",
        "давление в норме?",
    ]:
        assert _looks_like_short_value(t) is False, t


# ── agent_last_turn_was_question ────────────────────────────────────────────


def _patch_last_row(content, source, created_at):
    """Патч SessionLocal → fake db, чей execute().fetchone() отдаёт заданную строку."""
    fake_db = MagicMock()
    fake_db.execute.return_value.fetchone.return_value = (content, source, created_at)
    return patch("core.agent_chat.SessionLocal", return_value=fake_db)


def _now(minutes_ago=0):
    return datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)


def test_last_turn_is_recent_agent_question_true():
    from core.agent_chat import agent_last_turn_was_question

    content = [{"type": "text", "text": "Сколько весишь?"}]
    with _patch_last_row(content, "botkinclaw", _now(2)):
        assert agent_last_turn_was_question(895655) is True


def test_last_turn_not_question_false():
    from core.agent_chat import agent_last_turn_was_question

    content = [{"type": "text", "text": "🩺 АД: 120/80. Записано."}]
    with _patch_last_row(content, "botkinclaw", _now(1)):
        assert agent_last_turn_was_question(895655) is False


def test_parser_confirmation_source_false():
    from core.agent_chat import agent_last_turn_was_question

    # Даже если кончается «?», парсер-инъекция (не реальный ход агента) → False.
    content = [{"type": "text", "text": "Записал. Что-то ещё?"}]
    with _patch_last_row(content, "bp_fast_handler", _now(1)):
        assert agent_last_turn_was_question(895655) is False


def test_stale_question_false():
    from core.agent_chat import agent_last_turn_was_question

    content = [{"type": "text", "text": "Сколько весишь?"}]
    with _patch_last_row(content, "botkinclaw", _now(30)):  # >10 мин
        assert agent_last_turn_was_question(895655) is False


def test_no_history_false():
    from core.agent_chat import agent_last_turn_was_question

    fake_db = MagicMock()
    fake_db.execute.return_value.fetchone.return_value = None
    with patch("core.agent_chat.SessionLocal", return_value=fake_db):
        assert agent_last_turn_was_question(895655) is False


def test_custom_window_respected():
    from core.agent_chat import agent_last_turn_was_question

    content = [{"type": "text", "text": "Какое давление?"}]
    with _patch_last_row(content, None, _now(20)):  # source NULL = легаси реальный ход
        assert agent_last_turn_was_question(895655, within_minutes=30) is True
        assert agent_last_turn_was_question(895655, within_minutes=10) is False


def test_question_with_trailing_emoji_true():
    """#198: бот кончает вопрос эмодзи после «?» («Какой вес записать? 😊») —
    прежний endswith('?') давал False, короткий ответ уходил в парсер."""
    from core.agent_chat import agent_last_turn_was_question

    for txt in ["Какой вес записать? 😊", "Сколько весишь?😊", "Сколько? )", "Какой вес? "]:
        content = [{"type": "text", "text": txt}]
        with _patch_last_row(content, "botkinclaw", _now(1)):
            assert agent_last_turn_was_question(895655) is True, txt


def test_statement_after_question_false():
    """Вопрос в середине, а хвост — утверждение (кейс расчёта калорий: агент
    спросил про целевой вес, но закончил «…обновлю настройки в системе.») →
    не считаем хвостовым вопросом."""
    from core.agent_chat import agent_last_turn_was_question

    for txt in ["Целевой вес 54.9? Обновлю настройки в системе.", "Готово? Напиши число"]:
        content = [{"type": "text", "text": txt}]
        with _patch_last_row(content, "botkinclaw", _now(1)):
            assert agent_last_turn_was_question(895655) is False, txt
