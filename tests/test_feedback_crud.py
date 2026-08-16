from database.crud import (
    create_feedback,
    list_recent_feedback,
    is_feedback_opted_out,
)
from database.models import User, UserSettings, UserFeedback


def _mk_user(db, uid=895655, opted_out=False):
    db.add(User(telegram_id=uid, first_name="Тест"))
    db.add(UserSettings(user_id=uid, feedback_opt_out=opted_out))
    db.commit()


def test_create_feedback_writes_row(test_db):
    _mk_user(test_db)
    row = create_feedback(test_db, user_id=895655, text="вес неверный", source="command")
    assert row.id is not None
    assert row.kind == "unspecified"
    assert row.status == "new"
    assert row.source == "command"
    assert test_db.query(UserFeedback).count() == 1


def test_create_feedback_agent_kind_and_context(test_db):
    _mk_user(test_db)
    row = create_feedback(
        test_db,
        user_id=895655,
        text="научись графики сна",
        source="agent",
        kind="feature",
        agent_context={"agent_note": "нет тула графиков"},
    )
    assert row.kind == "feature"
    assert row.agent_context == {"agent_note": "нет тула графиков"}


def test_list_recent_feedback_only_new_desc(test_db):
    _mk_user(test_db)
    create_feedback(test_db, user_id=895655, text="a", source="command")
    b = create_feedback(test_db, user_id=895655, text="b", source="command")
    b.status = "done"
    test_db.commit()
    rows = list_recent_feedback(test_db, status="new", limit=10)
    assert [r.text for r in rows] == ["a"]


def test_is_feedback_opted_out_true(test_db):
    _mk_user(test_db, opted_out=True)
    assert is_feedback_opted_out(test_db, 895655) is True


def test_is_feedback_opted_out_false_default(test_db):
    _mk_user(test_db, opted_out=False)
    assert is_feedback_opted_out(test_db, 895655) is False


def test_is_feedback_opted_out_no_settings_row(test_db):
    test_db.add(User(telegram_id=111, first_name="Без настроек"))
    test_db.commit()
    # Нет строки user_settings → не считается opted-out.
    assert is_feedback_opted_out(test_db, 111) is False
