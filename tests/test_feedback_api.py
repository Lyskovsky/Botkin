"""Tests for POST /api/feedback — кнопка фидбека в мини-аппе (#271).

Третий канал захвата инбокса #188 (после /feedback command и agent flag_for_devs).
Проверяет: запись source='webapp', валидацию текста/kind, opt-out-гейт, auth (initData).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "telegram-bot"))

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database.models import Base, UserFeedback, UserSettings


@pytest.fixture
def engine():
    eng = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=eng)
    yield eng
    Base.metadata.drop_all(bind=eng)


@pytest.fixture
def api_db(engine):
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    db = Session()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture
def app_and_db(api_db, monkeypatch):
    import database
    from webhook import feedback_api

    # Endpoint calls SessionLocal() then db.close(); keep the test session alive.
    monkeypatch.setattr(api_db, "close", lambda: None)
    monkeypatch.setattr(database, "SessionLocal", lambda: api_db)

    app = FastAPI()
    app.include_router(feedback_api.router)
    return app, api_db


@pytest.fixture
def client(app_and_db):
    from webhook.tg_auth import get_tg_user

    app, _ = app_and_db
    app.dependency_overrides[get_tg_user] = lambda: {"id": 895655}
    return TestClient(app)


def test_creates_webapp_feedback(client, app_and_db):
    _, db = app_and_db
    r = client.post("/api/feedback", json={"text": "Кнопка не работает", "kind": "bug"})
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    rows = db.query(UserFeedback).all()
    assert len(rows) == 1
    assert rows[0].user_id == 895655
    assert rows[0].source == "webapp"
    assert rows[0].kind == "bug"
    assert rows[0].text == "Кнопка не работает"


def test_kind_defaults_to_unspecified(client, app_and_db):
    _, db = app_and_db
    r = client.post("/api/feedback", json={"text": "просто идея"})
    assert r.status_code == 200
    assert db.query(UserFeedback).one().kind == "unspecified"


def test_text_is_stripped(client, app_and_db):
    _, db = app_and_db
    client.post("/api/feedback", json={"text": "  хвостатый  ", "kind": "feature"})
    assert db.query(UserFeedback).one().text == "хвостатый"


def test_empty_text_rejected(client, app_and_db):
    _, db = app_and_db
    r = client.post("/api/feedback", json={"text": "   ", "kind": "bug"})
    assert r.status_code == 422
    assert db.query(UserFeedback).count() == 0


def test_invalid_kind_rejected(client, app_and_db):
    _, db = app_and_db
    r = client.post("/api/feedback", json={"text": "x", "kind": "spam"})
    assert r.status_code == 422
    assert db.query(UserFeedback).count() == 0


def test_opt_out_user_not_stored(client, app_and_db):
    _, db = app_and_db
    db.add(UserSettings(user_id=895655, feedback_opt_out=True))
    db.commit()
    r = client.post("/api/feedback", json={"text": "не хочу", "kind": "bug"})
    assert r.status_code == 200
    assert r.json()["status"] == "opted_out"
    assert db.query(UserFeedback).count() == 0


def test_requires_auth(app_and_db):
    """Без override get_tg_user реальная зависимость отвергает запрос."""
    app, _ = app_and_db
    c = TestClient(app)
    # нет заголовка Authorization → 422 (required Header)
    assert c.post("/api/feedback", json={"text": "x"}).status_code == 422
    # битый initData → 403 (HMAC mismatch)
    r = c.post("/api/feedback", json={"text": "x"}, headers={"Authorization": "tma deadbeef"})
    assert r.status_code == 403
