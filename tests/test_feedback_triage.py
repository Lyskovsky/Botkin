"""Триаж инбокса фидбека — Фаза 2, ядро (#269).

CRUD (статус/приоритет/github) на in-memory SQLite + агент-тулы
(/list_feedback, /triage_feedback) через TestClient с admin-гейтом.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "telegram-bot"))

import pytest
from unittest.mock import MagicMock
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database.models import Base
from database import crud


@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(autocommit=False, autoflush=False, bind=engine)()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)


# ── CRUD ────────────────────────────────────────────────────────────────────


def _mk(db, text="вес неверный"):
    return crud.create_feedback(db, user_id=895655, text=text, source="command")


def test_status_done_sets_resolved_and_reopen_clears(db_session):
    row = _mk(db_session)
    assert row.status == "new" and row.resolved_at is None

    row = crud.update_feedback_status(db_session, row.id, "done")
    assert row.status == "done" and row.resolved_at is not None

    row = crud.update_feedback_status(db_session, row.id, "in_progress")
    assert row.status == "in_progress" and row.resolved_at is None


def test_status_invalid_raises(db_session):
    row = _mk(db_session)
    with pytest.raises(ValueError):
        crud.update_feedback_status(db_session, row.id, "bogus")


def test_status_not_found_returns_none(db_session):
    assert crud.update_feedback_status(db_session, 9999, "done") is None


def test_priority_valid_and_invalid(db_session):
    row = _mk(db_session)
    assert crud.set_feedback_priority(db_session, row.id, "P1").priority == "P1"
    with pytest.raises(ValueError):
        crud.set_feedback_priority(db_session, row.id, "P9")


def test_github_normalizes_leading_hash(db_session):
    row = _mk(db_session)
    assert crud.set_feedback_github(db_session, row.id, "#300").github_issue == "300"
    assert crud.set_feedback_github(db_session, row.id, "").github_issue is None


def test_list_all_statuses(db_session):
    a = _mk(db_session, "a")
    crud.update_feedback_status(db_session, a.id, "done")
    _mk(db_session, "b")  # new
    assert len(crud.list_recent_feedback(db_session, status="new")) == 1
    assert len(crud.list_recent_feedback(db_session, status=None)) == 2


# ── Агент-тулы (endpoints) ──────────────────────────────────────────────────


@pytest.fixture
def client(db_session, monkeypatch):
    from webhook import agent_tools_api
    from webhook.jwt_auth import get_agent_user, get_db

    monkeypatch.setattr(db_session, "close", lambda: None)

    app = FastAPI()
    app.include_router(agent_tools_api.router)

    mock_user = MagicMock()
    mock_user.telegram_id = 895655

    app.dependency_overrides[get_agent_user] = lambda: mock_user
    app.dependency_overrides[get_db] = lambda: db_session
    return TestClient(app)


def _set_admin(monkeypatch, value: bool):
    monkeypatch.setattr("config.users.is_admin", lambda tid: value)


def test_list_feedback_admin_ok(client, db_session, monkeypatch):
    _mk(db_session, "боул как перекус")
    _set_admin(monkeypatch, True)
    r = client.post("/api/agent/list_feedback", json={"status": "new", "limit": 10})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] == 1
    item = body["feedback"][0]
    assert item["text"] == "боул как перекус"
    assert item["status"] == "new" and "created_at" in item


def test_list_feedback_non_admin_403(client, monkeypatch):
    _set_admin(monkeypatch, False)
    r = client.post("/api/agent/list_feedback", json={})
    assert r.status_code == 403


def test_triage_feedback_admin_updates(client, db_session, monkeypatch):
    row = _mk(db_session)
    _set_admin(monkeypatch, True)
    r = client.post(
        "/api/agent/triage_feedback",
        json={"feedback_id": row.id, "status": "done", "priority": "P1", "github_issue": "#300"},
    )
    assert r.status_code == 200, r.text
    fb = r.json()["feedback"]
    assert fb["status"] == "done" and fb["priority"] == "P1" and fb["github_issue"] == "300"
    assert fb["resolved_at"] is not None


def test_triage_feedback_non_admin_403(client, db_session, monkeypatch):
    row = _mk(db_session)
    _set_admin(monkeypatch, False)
    r = client.post("/api/agent/triage_feedback", json={"feedback_id": row.id, "status": "done"})
    assert r.status_code == 403


def test_triage_feedback_not_found_404(client, monkeypatch):
    _set_admin(monkeypatch, True)
    r = client.post("/api/agent/triage_feedback", json={"feedback_id": 9999, "status": "done"})
    assert r.status_code == 404


def test_triage_feedback_bad_status_400(client, db_session, monkeypatch):
    row = _mk(db_session)
    _set_admin(monkeypatch, True)
    r = client.post("/api/agent/triage_feedback", json={"feedback_id": row.id, "status": "bogus"})
    assert r.status_code == 400


def test_triage_invalid_priority_does_not_commit_status(client, db_session, monkeypatch):
    # Атомарность: невалидный priority после валидного status не должен оставить
    # частичное изменение (пре-валидация до мутаций).
    row = _mk(db_session)
    _set_admin(monkeypatch, True)
    r = client.post(
        "/api/agent/triage_feedback",
        json={"feedback_id": row.id, "status": "done", "priority": "P9"},
    )
    assert r.status_code == 400
    db_session.expire_all()
    fresh = crud.get_feedback(db_session, row.id)
    assert fresh.status == "new" and fresh.resolved_at is None


def test_triage_github_too_long_400(client, db_session, monkeypatch):
    row = _mk(db_session)
    _set_admin(monkeypatch, True)
    r = client.post(
        "/api/agent/triage_feedback",
        json={"feedback_id": row.id, "github_issue": "x" * 100},
    )
    assert r.status_code == 400


# ── Фаза 3: уведомление автора при разборе (#188) ────────────────────────────


@pytest.fixture
def sent_messages(monkeypatch):
    """Перехват отправки в Telegram — возвращает список (chat_id, text)."""
    from webhook import agent_tools_api

    box = []

    async def fake_send(chat_id, text):
        box.append((chat_id, text))
        return True

    monkeypatch.setattr(agent_tools_api, "_send_feedback_notification", fake_send)
    return box


def test_done_notifies_author(client, db_session, monkeypatch, sent_messages):
    row = _mk(db_session, "вес на дашборде неверный")
    _set_admin(monkeypatch, True)
    r = client.post("/api/agent/triage_feedback", json={"feedback_id": row.id, "status": "done"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["notified"] is True and body.get("notify_skipped") is None
    assert len(sent_messages) == 1
    chat_id, text = sent_messages[0]
    assert chat_id == row.user_id
    assert "разобрал" in text.lower()
    db_session.expire_all()
    assert crud.get_feedback(db_session, row.id).notified_at is not None


def test_second_done_does_not_renotify(client, db_session, monkeypatch, sent_messages):
    row = _mk(db_session)
    _set_admin(monkeypatch, True)
    client.post("/api/agent/triage_feedback", json={"feedback_id": row.id, "status": "done"})
    r2 = client.post("/api/agent/triage_feedback", json={"feedback_id": row.id, "status": "done"})
    assert r2.json()["notified"] is False
    assert r2.json()["notify_skipped"] == "already_notified"
    assert len(sent_messages) == 1  # второй done не шлёт заново


def test_opt_out_user_not_notified(client, db_session, monkeypatch, sent_messages):
    from database.models import UserSettings

    row = _mk(db_session)
    db_session.add(UserSettings(user_id=row.user_id, feedback_opt_out=True))
    db_session.commit()
    _set_admin(monkeypatch, True)
    r = client.post("/api/agent/triage_feedback", json={"feedback_id": row.id, "status": "done"})
    body = r.json()
    assert body["notified"] is False and body["notify_skipped"] == "opt_out"
    assert len(sent_messages) == 0
    db_session.expire_all()
    assert crud.get_feedback(db_session, row.id).notified_at is None  # не штампуем, opt-out


def test_custom_notify_text_sent_and_stamps(client, db_session, monkeypatch, sent_messages):
    # question можно закрыть человеческим ответом даже без смены статуса.
    row = crud.create_feedback(
        db_session, user_id=895655, text="как считается клетчатка?", source="command", kind="question"
    )
    _set_admin(monkeypatch, True)
    r = client.post(
        "/api/agent/triage_feedback",
        json={"feedback_id": row.id, "notify_text": "Клетчатка — из таблицы USDA."},
    )
    assert r.status_code == 200, r.text
    assert r.json()["notified"] is True
    assert sent_messages == [(row.user_id, "Клетчатка — из таблицы USDA.")]
    db_session.expire_all()
    assert crud.get_feedback(db_session, row.id).notified_at is not None


def test_in_progress_does_not_notify(client, db_session, monkeypatch, sent_messages):
    row = _mk(db_session)
    _set_admin(monkeypatch, True)
    r = client.post("/api/agent/triage_feedback", json={"feedback_id": row.id, "status": "in_progress"})
    assert r.json()["notified"] is False and r.json().get("notify_skipped") is None
    assert len(sent_messages) == 0


def test_send_failure_leaves_unnotified(client, db_session, monkeypatch):
    from webhook import agent_tools_api

    async def failing_send(chat_id, text):
        return False

    monkeypatch.setattr(agent_tools_api, "_send_feedback_notification", failing_send)
    row = _mk(db_session)
    _set_admin(monkeypatch, True)
    r = client.post("/api/agent/triage_feedback", json={"feedback_id": row.id, "status": "done"})
    body = r.json()
    assert body["notified"] is False and body["notify_skipped"] == "send_failed"
    db_session.expire_all()
    # не штампуем при неудаче отправки → следующий триаж повторит попытку
    assert crud.get_feedback(db_session, row.id).notified_at is None
