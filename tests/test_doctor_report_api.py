"""Tests for POST /api/doctor_report — кнопка «Экспорт для врача» (#290).

Основной путь доставки: мини-апп → эндпоинт → send_doctor_report_to_chat →
PDF Telegram-документом. Проверяет: auth (initData), успех (status=sent),
проброс ошибки доставки как HTTP 502.
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

from database.models import Base


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
    from webhook import doctor_report_api

    monkeypatch.setattr(api_db, "close", lambda: None)
    monkeypatch.setattr(database, "SessionLocal", lambda: api_db)

    app = FastAPI()
    app.include_router(doctor_report_api.router)
    return app, api_db


@pytest.fixture
def client(app_and_db):
    from webhook.tg_auth import get_tg_user

    app, _ = app_and_db
    app.dependency_overrides[get_tg_user] = lambda: {"id": 895655}
    return TestClient(app)


def test_sends_report_on_success(client, monkeypatch):
    """Успешная доставка → 200 {status: sent}, вызван send с верным user_id."""
    from services import doctor_report

    seen = {}

    def _fake_send(db, user_id, **kw):
        seen["user_id"] = user_id
        return {"status": "ok", "sent": True}

    monkeypatch.setattr(doctor_report, "send_doctor_report_to_chat", _fake_send)

    r = client.post("/api/doctor_report")
    assert r.status_code == 200
    assert r.json() == {"status": "sent"}
    assert seen["user_id"] == 895655


def test_delivery_failure_returns_502(client, monkeypatch):
    """Сбой рендера/доставки → HTTP 502 (фронт покажет ошибку)."""
    from services import doctor_report

    monkeypatch.setattr(
        doctor_report,
        "send_doctor_report_to_chat",
        lambda db, user_id, **kw: {"status": "error", "error": "render-failed: boom", "sent": False},
    )

    r = client.post("/api/doctor_report")
    assert r.status_code == 502


def test_requires_auth(app_and_db):
    """Без валидного initData (get_tg_user) — не 200."""
    app, _ = app_and_db
    client = TestClient(app)
    r = client.post("/api/doctor_report")
    assert r.status_code != 200


def test_api_passes_language_to_helper(client, monkeypatch):
    """language из тела → проброшен в send как lang."""
    from services import doctor_report

    seen = {}
    monkeypatch.setattr(
        doctor_report,
        "send_doctor_report_to_chat",
        lambda db, user_id, **kw: seen.update(lang=kw.get("lang")) or {"status": "ok", "sent": True},
    )
    r = client.post("/api/doctor_report", json={"language": "en"})
    assert r.status_code == 200
    assert seen["lang"] == "en"


def test_api_invalid_language_defaults_ru(client, monkeypatch):
    """Невалидный language + нет language_code в initData → ru."""
    from services import doctor_report

    seen = {}
    monkeypatch.setattr(
        doctor_report,
        "send_doctor_report_to_chat",
        lambda db, user_id, **kw: seen.update(lang=kw.get("lang")) or {"status": "ok", "sent": True},
    )
    r = client.post("/api/doctor_report", json={"language": "fr"})
    assert r.status_code == 200
    assert seen["lang"] == "ru"
