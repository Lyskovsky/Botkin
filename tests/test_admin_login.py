"""Тесты формы логина админки (#226).

Заменяем браузерный вход HTTP Basic (нативный popup) на форму логина + сессию.
Basic Auth остаётся принимаемым для `/admin/api/*` (fallback для curl/скриптов/
второго админа) — это явное решение по задаче.

RED → GREEN: тесты написаны до реализации роутов `/admin/login` и `/admin/logout`.
"""

import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "telegram-bot"))

ADMIN_PW = "s3cret-pass"


@pytest.fixture
def admin(monkeypatch):
    """Модуль admin с заданным ADMIN_PASSWORD и чистым стором троттлинга."""
    from webhook import admin as admin_mod

    monkeypatch.setenv("ADMIN_PASSWORD", ADMIN_PW)
    monkeypatch.setenv("ADMIN_USERNAME", "admin")
    # сбросить in-memory троттлинг между тестами
    if hasattr(admin_mod, "_login_attempts"):
        admin_mod._login_attempts.clear()
    return admin_mod


@pytest.fixture
def client(admin):
    app = FastAPI()
    app.include_router(admin.router)
    # follow_redirects=False — хотим проверять сами 303-редиректы
    return TestClient(app, follow_redirects=False)


# ─── _is_authed (unit) ──────────────────────────────────────────────────────


def test_is_authed_rejects_no_creds_no_cookie(admin):
    """Без Basic-кредов и без cookie → не авторизован."""

    class _Req:
        cookies: dict = {}

    assert admin._is_authed(None, _Req()) is False


def test_is_authed_accepts_correct_basic(admin):
    """Basic fallback: верные креды → авторизован (не ломаем curl/скрипты)."""
    from fastapi.security import HTTPBasicCredentials

    class _Req:
        cookies: dict = {}

    creds = HTTPBasicCredentials(username="admin", password=ADMIN_PW)
    assert admin._is_authed(creds, _Req()) is True


def test_is_authed_rejects_wrong_password(admin):
    from fastapi.security import HTTPBasicCredentials

    class _Req:
        cookies: dict = {}

    creds = HTTPBasicCredentials(username="admin", password="nope")
    assert admin._is_authed(creds, _Req()) is False


def test_is_authed_accepts_valid_cookie(admin):
    """Валидная подписанная cookie → авторизован без Basic."""

    class _Req:
        cookies = {admin.ADMIN_COOKIE_NAME: admin._expected_cookie_token()}

    assert admin._is_authed(None, _Req()) is True


# ─── Редирект-гард HTML-страницы ────────────────────────────────────────────


def test_index_without_session_redirects_to_login(client, admin):
    """Главная без сессии → 303 на /admin/login (НЕ 401, НЕ нативный popup)."""
    r = client.get("/admin/")
    assert r.status_code == 303
    assert r.headers["location"] == "/admin/login"
    # критично: никакого Basic-challenge, иначе браузер покажет popup
    assert "www-authenticate" not in {k.lower() for k in r.headers}


def test_index_with_valid_cookie_serves_html(client, admin):
    """С валидной cookie главная отдаёт HTML админки (200)."""
    client.cookies.set(admin.ADMIN_COOKIE_NAME, admin._expected_cookie_token())
    r = client.get("/admin/")
    assert r.status_code == 200
    assert "Botkin Admin" in r.text


# ─── GET /admin/login ───────────────────────────────────────────────────────


def test_login_page_renders_form(client):
    r = client.get("/admin/login")
    assert r.status_code == 200
    assert 'type="password"' in r.text
    assert "/admin/login" in r.text  # form action


def test_login_page_redirects_if_already_authed(client, admin):
    client.cookies.set(admin.ADMIN_COOKIE_NAME, admin._expected_cookie_token())
    r = client.get("/admin/login")
    assert r.status_code == 303
    assert r.headers["location"] == "/admin/"


# ─── POST /admin/login ──────────────────────────────────────────────────────


def test_login_wrong_password_shows_error_no_cookie(client, admin):
    r = client.post("/admin/login", data={"password": "wrong"})
    assert r.status_code in (200, 401)
    # cookie сессии НЕ ставится
    assert admin.ADMIN_COOKIE_NAME not in r.cookies
    # детали не утекают
    assert ADMIN_PW not in r.text


def test_login_correct_password_sets_cookie_and_redirects(client, admin):
    r = client.post("/admin/login", data={"password": ADMIN_PW})
    assert r.status_code == 303
    assert r.headers["location"] == "/admin/"
    set_cookie = r.headers.get("set-cookie", "")
    assert admin.ADMIN_COOKIE_NAME in set_cookie
    assert admin._expected_cookie_token() in set_cookie


# ─── Троттлинг ──────────────────────────────────────────────────────────────


def test_login_throttles_after_max_attempts(client, admin):
    """После LOGIN_MAX_ATTEMPTS неудачных попыток вход залочен (429)."""
    last = None
    for _ in range(admin.LOGIN_MAX_ATTEMPTS + 1):
        last = client.post("/admin/login", data={"password": "wrong"})
    assert last.status_code == 429
    # даже верный пароль во время локаута не пускает
    blocked = client.post("/admin/login", data={"password": ADMIN_PW})
    assert blocked.status_code == 429


# ─── Logout ─────────────────────────────────────────────────────────────────


def test_logout_clears_cookie_and_redirects(client, admin):
    client.cookies.set(admin.ADMIN_COOKIE_NAME, admin._expected_cookie_token())
    r = client.get("/admin/logout")
    assert r.status_code == 303
    assert r.headers["location"] == "/admin/login"
    set_cookie = r.headers.get("set-cookie", "")
    # cookie очищается (Max-Age=0 / expires в прошлом)
    assert admin.ADMIN_COOKIE_NAME in set_cookie
    assert "max-age=0" in set_cookie.lower() or "expires=" in set_cookie.lower()


# ─── Basic Auth fallback на API цел ─────────────────────────────────────────


def test_api_still_challenges_basic_when_unauthed(client):
    """API без авторизации по-прежнему отвечает 401 — программный путь (Basic)
    сохранён, форма логина его не подменяет."""
    r = client.get("/admin/api/users")
    assert r.status_code == 401
