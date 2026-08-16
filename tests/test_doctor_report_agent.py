"""Tests for agent-тул generate_doctor_report (#291).

Вторичный путь доставки PDF-отчёта врачу через BotkinClaw. Проверяет:
- эндпоинт /doctor_report вызывает общий helper с telegram_id пользователя;
- тул объявлен в TOOLS и в label-map;
- _call_tool диспетчит имя на POST /doctor_report.
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

from database.models import Base, User


class _User:
    telegram_id = 895655
    first_name = "Тест"


@pytest.fixture
def db_session():
    e = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=e)
    Session = sessionmaker(autocommit=False, autoflush=False, bind=e)
    db = Session()
    db.add(User(telegram_id=895655, first_name="Тест", is_active=True, cohort="owner", pack_name="generic"))
    db.commit()
    try:
        yield db
    finally:
        db.close()
        Base.metadata.drop_all(bind=e)


@pytest.fixture
def client(db_session):
    from webhook import agent_tools_api
    from webhook.jwt_auth import get_agent_user, get_db

    app = FastAPI()
    app.include_router(agent_tools_api.router)
    app.dependency_overrides[get_agent_user] = lambda: _User()
    app.dependency_overrides[get_db] = lambda: db_session
    return TestClient(app)


def test_endpoint_calls_helper_with_user_id(client, monkeypatch):
    """POST /doctor_report → вызывает send_doctor_report_to_chat(db, telegram_id)."""
    from services import doctor_report

    seen = {}

    def _fake(db, user_id, **kw):
        seen["user_id"] = user_id
        return {"status": "ok", "sent": True}

    monkeypatch.setattr(doctor_report, "send_doctor_report_to_chat", _fake)

    r = client.post("/api/agent/doctor_report")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "sent": True}
    assert seen["user_id"] == 895655


def test_endpoint_passes_language(client, monkeypatch):
    """POST /doctor_report c language=en → send вызван с lang='en'."""
    from services import doctor_report

    seen = {}

    def _fake(db, user_id, **kw):
        seen["lang"] = kw.get("lang")
        return {"status": "ok", "sent": True}

    monkeypatch.setattr(doctor_report, "send_doctor_report_to_chat", _fake)

    r = client.post("/api/agent/doctor_report", json={"language": "en"})
    assert r.status_code == 200
    assert seen["lang"] == "en"


def test_endpoint_default_ru(client, monkeypatch):
    """POST /doctor_report без тела → lang='ru'."""
    from services import doctor_report

    seen = {}
    monkeypatch.setattr(
        doctor_report,
        "send_doctor_report_to_chat",
        lambda db, user_id, **kw: seen.update(lang=kw.get("lang")) or {"status": "ok", "sent": True},
    )

    r = client.post("/api/agent/doctor_report")
    assert r.status_code == 200
    assert seen["lang"] == "ru"


def test_call_tool_passes_language(monkeypatch):
    """_call_tool прокидывает language в тело POST."""
    import core.agent_chat as ac

    captured = {}

    class _Resp:
        ok = True
        text = '{"status":"ok","sent":true}'

    def _fake_post(url, headers=None, timeout=None, **kw):
        captured["json"] = kw.get("json")
        return _Resp()

    monkeypatch.setattr(ac.requests, "post", _fake_post)
    ac._call_tool("generate_doctor_report", {"language": "en"}, "faketoken")
    assert captured["json"] == {"language": "en"}


def test_tool_declared_in_botkinclaw():
    """generate_doctor_report есть в TOOLS и в label-map прогресса."""
    from core.agent_chat import TOOLS, _TOOL_PROGRESS_LABEL

    names = [t["name"] for t in TOOLS]
    assert "generate_doctor_report" in names
    assert "generate_doctor_report" in _TOOL_PROGRESS_LABEL


def test_call_tool_dispatches_to_doctor_report(monkeypatch):
    """_call_tool('generate_doctor_report') шлёт POST на /doctor_report."""
    import core.agent_chat as ac

    captured = {}

    class _Resp:
        ok = True
        text = '{"status":"ok","sent":true}'

    def _fake_post(url, headers=None, timeout=None, **kw):
        captured["url"] = url
        captured["timeout"] = timeout
        return _Resp()

    monkeypatch.setattr(ac.requests, "post", _fake_post)
    out = ac._call_tool("generate_doctor_report", {}, "faketoken")
    assert captured["url"].endswith("/doctor_report")
    assert out == '{"status":"ok","sent":true}'
