from unittest.mock import patch, MagicMock

from core.agent_chat import _log_first_question


@patch("core.agent_chat.log_event")
def test_logs_e6_once_for_real_user(mock_le):
    db = MagicMock()
    _log_first_question(db, user_id=42, is_e2e=False)
    assert mock_le.call_args.kwargs.get("event") == "first_agent_question"
    assert mock_le.call_args.kwargs.get("once") is True


@patch("core.agent_chat.log_event")
def test_skips_e6_in_e2e(mock_le):
    db = MagicMock()
    _log_first_question(db, user_id=42, is_e2e=True)
    mock_le.assert_not_called()
