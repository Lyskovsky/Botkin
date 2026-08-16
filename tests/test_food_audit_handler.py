import importlib.util
from pathlib import Path

# handlers/ live under telegram-bot/ (hyphen → not importable as a package); load by path.
_MOD_PATH = Path(__file__).resolve().parents[1] / "telegram-bot" / "handlers" / "food_audit.py"
_spec = importlib.util.spec_from_file_location("handlers_food_audit", _MOD_PATH)
handlers_food_audit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(handlers_food_audit)

parse_audit_args = handlers_food_audit.parse_audit_args
format_food_audit = handlers_food_audit.format_food_audit
_MAX_ROWS = handlers_food_audit._MAX_ROWS


class _Row:
    """Минимальный дубль FoodInteraction для рендер-тестов."""

    def __init__(self, **kw):
        self.created_at = kw.get("created_at")
        self.source = kw.get("source", "text")
        self.status = kw.get("status", "saved")
        self.raw_text = kw.get("raw_text")
        self.media_path = kw.get("media_path")
        self.recognized = kw.get("recognized")
        self.bot_reply = kw.get("bot_reply")
        self.nutrition_log_id = kw.get("nutrition_log_id")


# --- parse_audit_args ---------------------------------------------------------


def test_parse_args_defaults_to_caller():
    assert parse_audit_args("/food_audit", caller_id=555) == (555, _MAX_ROWS)


def test_parse_args_explicit_user():
    assert parse_audit_args("/food_audit 12345", caller_id=555) == (12345, _MAX_ROWS)


def test_parse_args_user_and_limit():
    assert parse_audit_args("/food_audit 12345 30", caller_id=555) == (12345, 30)


def test_parse_args_bot_suffix_no_numeric():
    # '/food_audit@botkin_dev_bot' без аргументов → цель = вызвавший
    assert parse_audit_args("/food_audit@botkin_dev_bot", caller_id=555) == (555, _MAX_ROWS)


def test_parse_args_limit_capped_at_50():
    assert parse_audit_args("/food_audit 1 999", caller_id=555) == (1, 50)


def test_parse_args_limit_floor_1():
    assert parse_audit_args("/food_audit 1 0", caller_id=555) == (1, 1)


# --- format_food_audit --------------------------------------------------------


def test_format_empty_mentions_user():
    out = format_food_audit([], user_id=777)
    assert "777" in out
    assert "нет" in out.lower()


def test_format_renders_chain_fields():
    row = _Row(
        source="text",
        status="saved",
        raw_text="банан 120г",
        recognized={"items": [{"name": "банан"}], "totals": {"calories": 107}},
        bot_reply="✅ Банан · 107 ккал",
        nutrition_log_id=42,
    )
    out = format_food_audit([row], user_id=777)
    assert "text" in out
    assert "saved" in out or "✅" in out
    assert "банан 120г" in out
    assert "42" in out  # ссылка на nutrition_log
    assert "107" in out  # распознанное


def test_format_cancelled_status_no_nutrition_link():
    row = _Row(source="photo", status="cancelled", raw_text="не то", nutrition_log_id=None)
    out = format_food_audit([row], user_id=1)
    assert "cancelled" in out or "❌" in out
    assert "не то" in out


def test_format_none_fields_no_crash():
    row = _Row(source="voice", status="saved", raw_text=None, recognized=None, bot_reply=None)
    out = format_food_audit([row], user_id=1)
    assert "voice" in out  # не упало на None-полях


def test_format_caps_rows_and_flags_truncation():
    rows = [_Row(raw_text=f"meal {i}") for i in range(_MAX_ROWS + 5)]
    out = format_food_audit(rows, user_id=1)
    # показываем не больше _MAX_ROWS цепочек, но отмечаем что показаны не все
    assert out.count("прислал") <= _MAX_ROWS
    assert "ещё" in out.lower() or "остал" in out.lower() or str(len(rows)) in out
