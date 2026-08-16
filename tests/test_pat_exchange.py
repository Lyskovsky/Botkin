"""HTTP-тесты PAT→JWT exchange и enforcement scope ro/rw (#228).

Используют реальный get_agent_user (не мок) — чтобы проверить полный путь:
exchange выдаёт JWT с нужным scope, require_agent_scope блокирует ro на write.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "telegram-bot"))

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database.models import Base, User
from database.crud import create_pat, revoke_pat

OWNER = 895655


@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    session = Session()
    session.add(
        User(
            telegram_id=OWNER,
            first_name="Sasha",
            container_id="nc-sasha",
            jwt_secret="test_secret",
            is_active=True,
        )
    )
    session.commit()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)


@pytest.fixture(autouse=True)
def _patch_rls_and_limiter(monkeypatch):
    """SET LOCAL app.user_id не работает на sqlite → no-op; счётчик лимита сбрасываем."""
    import database.crud as crud
    from webhook import agent_tools_api

    monkeypatch.setattr(crud, "set_user_session_var", lambda db, uid: None)
    agent_tools_api._exchange_limiter.reset()


@pytest.fixture
def client(db_session):
    from webhook import agent_tools_api
    from webhook.jwt_auth import get_db

    app = FastAPI()
    app.include_router(agent_tools_api.router)
    app.dependency_overrides[get_db] = lambda: db_session
    return TestClient(app)


def _exchange(client, pat_token):
    return client.post("/api/agent/exchange_pat_for_jwt", json={"pat": pat_token})


# ── exchange ──────────────────────────────────────────────────────────────────


def test_exchange_valid_pat_returns_jwt(client, db_session):
    pat = create_pat(db_session, OWNER, name="ноут", scope="rw")

    resp = _exchange(client, pat.token)

    assert resp.status_code == 200
    body = resp.json()
    assert body["token_type"] == "Bearer"
    assert body["scope"] == "rw"
    assert body["access_token"]
    assert body["expires_in"] > 0


def test_exchange_echoes_ro_scope(client, db_session):
    pat = create_pat(db_session, OWNER, scope="ro")

    assert _exchange(client, pat.token).json()["scope"] == "ro"


def test_exchange_invalid_token_401(client):
    assert _exchange(client, "pat_895655_nope").status_code == 401


def test_exchange_revoked_token_401(client, db_session):
    pat = create_pat(db_session, OWNER)
    revoke_pat(db_session, OWNER, pat.id)

    assert _exchange(client, pat.token).status_code == 401


def test_exchange_rate_limited_after_10(client, db_session):
    pat = create_pat(db_session, OWNER)

    statuses = [_exchange(client, pat.token).status_code for _ in range(11)]

    assert statuses[:10] == [200] * 10
    assert statuses[10] == 429


# ── scope enforcement ──────────────────────────────────────────────────────────


def _auth(client, pat_token):
    token = _exchange(client, pat_token).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def test_ro_token_blocked_on_write_endpoint(client, db_session):
    ro = create_pat(db_session, OWNER, scope="ro")

    resp = client.post("/api/agent/regenerate_health_token", headers=_auth(client, ro.token))

    assert resp.status_code == 403


def test_rw_token_allowed_on_write_endpoint(client, db_session):
    rw = create_pat(db_session, OWNER, scope="rw")

    resp = client.post("/api/agent/regenerate_health_token", headers=_auth(client, rw.token))

    assert resp.status_code == 200
    assert resp.json()["health_token"].startswith(f"hvt_{OWNER}_")


def test_ro_token_allowed_on_read_endpoint(client, db_session):
    """ro-токен врача должен читать данные — на read-эндпоинте scope не блокирует."""
    ro = create_pat(db_session, OWNER, scope="ro")

    resp = client.get("/api/agent/user_settings", headers=_auth(client, ro.token))

    assert resp.status_code not in (401, 403)
