import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "telegram-bot"))

import pytest
from unittest.mock import patch, AsyncMock, MagicMock


def test_persona_keyboard_lists_four_personas():
    from handlers.persona_cmd import _persona_inline_kb

    kb = _persona_inline_kb()
    flat = [b.text for row in kb.inline_keyboard for b in row]
    assert len(flat) == 4


@pytest.mark.asyncio
@patch("handlers.persona_cmd.SessionLocal")
async def test_callback_sets_persona(MockSession):
    db = MagicMock()
    MockSession.return_value = db
    user = MagicMock(telegram_id=9, onboarding_data={})
    db.query.return_value.filter_by.return_value.first.return_value = user
    from handlers.persona_cmd import apply_persona_choice

    callback = MagicMock(answer=AsyncMock(), message=MagicMock(edit_text=AsyncMock()))
    await apply_persona_choice(9, "strict_coach", callback)
    assert user.onboarding_data["persona"] == "strict_coach"
