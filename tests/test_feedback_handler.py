import importlib.util
from pathlib import Path

# handlers/ live under telegram-bot/ (hyphen → not importable as a package); load by path.
_MOD_PATH = Path(__file__).resolve().parents[1] / "telegram-bot" / "handlers" / "feedback.py"
_spec = importlib.util.spec_from_file_location("handlers_feedback", _MOD_PATH)
handlers_feedback = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(handlers_feedback)

strip_feedback_prefix = handlers_feedback.strip_feedback_prefix
format_feedback_queue = handlers_feedback.format_feedback_queue


def test_strip_prefix_extracts_text():
    assert strip_feedback_prefix("/feedback вес неверный") == "вес неверный"


def test_strip_prefix_handles_bot_suffix():
    assert strip_feedback_prefix("/feedback@Botkin_md_bot идея X") == "идея X"


def test_strip_prefix_empty_when_no_text():
    assert strip_feedback_prefix("/feedback") == ""
    assert strip_feedback_prefix("/feedback   ") == ""


def test_format_queue_empty():
    assert "пусто" in format_feedback_queue([]).lower()


def test_format_queue_renders_rows():
    class Row:
        id = 14
        kind = "bug"
        source = "agent"
        user_id = 54246431
        text = "вес на дашборде неверный"
        agent_context = {"agent_note": "юзер указал расхождение"}

    out = format_feedback_queue([Row()])
    assert "#14" in out
    assert "bug" in out
    assert "agent" in out
    assert "вес на дашборде неверный" in out
    assert "юзер указал расхождение" in out


def test_format_queue_non_dict_agent_context_no_crash():
    class Row:
        id = 7
        kind = "unspecified"
        source = "command"
        user_id = 1
        text = "просто текст"
        agent_context = None  # не dict → без ↳-строки, без краха

    out = format_feedback_queue([Row()])
    assert "#7" in out
    assert "↳" not in out


def test_format_queue_unknown_kind_uses_default_emoji():
    class Row:
        id = 8
        kind = "weird"  # нет в _KIND_EMOJI → дефолтный 📝
        source = "command"
        user_id = 1
        text = "x"
        agent_context = None

    out = format_feedback_queue([Row()])
    assert "#8" in out
    assert "📝" in out
