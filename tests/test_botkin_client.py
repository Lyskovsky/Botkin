"""Unit-тесты BotkinClient — auth-кэш, 401-повтор, 403, чистка параметров (#228)."""

import importlib.util
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parent.parent / "scripts" / "mcp" / "botkin_client.py"
_spec = importlib.util.spec_from_file_location("botkin_client", _PATH)
bc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bc)


class FakeResponse:
    def __init__(self, status_code=200, json_data=None):
        self.status_code = status_code
        self._json = json_data if json_data is not None else {}

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeSession:
    """Отдаёт заранее заготовленные ответы по очереди, пишет историю вызовов."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def post(self, url, json=None, timeout=None):
        self.calls.append(("POST", url, None, json))
        return self._responses.pop(0)

    def request(self, method, url, params=None, json=None, headers=None, timeout=None):
        self.calls.append((method, url, params, json, headers))
        return self._responses.pop(0)


JWT_OK = FakeResponse(200, {"access_token": "jwt-1", "expires_in": 3600, "scope": "rw"})


def _client(responses, **kw):
    return bc.BotkinClient("https://api.example", "pat_1_abc", session=FakeSession(responses), **kw)


# ── init ────────────────────────────────────────────────────────────────────


def test_requires_base_url():
    with pytest.raises(ValueError):
        bc.BotkinClient("", "pat")


def test_requires_pat():
    with pytest.raises(ValueError):
        bc.BotkinClient("https://api.example", "")


def test_strips_trailing_slash():
    c = bc.BotkinClient("https://api.example/", "pat", session=FakeSession([]))
    assert c.base_url == "https://api.example"


# ── auth caching / refresh ────────────────────────────────────────────────────


def test_first_request_exchanges_then_calls():
    c = _client([JWT_OK, FakeResponse(200, {"ok": True})])
    result = c.get("/api/agent/user_profile")

    assert result == {"ok": True}
    methods = [call[0] for call in c._session.calls]
    assert methods == ["POST", "GET"]  # exchange, затем сам запрос


def test_jwt_reused_within_ttl():
    c = _client([JWT_OK, FakeResponse(200, {}), FakeResponse(200, {})])
    c.get("/api/agent/user_profile")
    c.get("/api/agent/recent_bp")

    # один обмен (POST) на два GET
    assert [call[0] for call in c._session.calls].count("POST") == 1


def test_invalid_pat_raises_auth_error():
    c = _client([FakeResponse(401)])
    with pytest.raises(bc.BotkinAuthError):
        c.get("/api/agent/user_profile")


def test_expired_jwt_triggers_reexchange(monkeypatch):
    c = _client([JWT_OK, FakeResponse(200, {})], ttl_buffer_s=0)
    c.get("/api/agent/user_profile")  # exchange #1

    # перематываем время за пределы expires_in → следующий запрос переобменивает
    real_now = c._now()
    monkeypatch.setattr(c, "_now", lambda: real_now + 10_000)
    c._session._responses.extend([JWT_OK, FakeResponse(200, {})])
    c.get("/api/agent/recent_bp")

    assert [call[0] for call in c._session.calls].count("POST") == 2


# ── 401 retry / 403 ─────────────────────────────────────────────────────────


def test_401_on_request_reexchanges_and_retries_once():
    # exchange, затем 401 на запросе → сброс + повторный exchange + успешный запрос
    c = _client([JWT_OK, FakeResponse(401), JWT_OK, FakeResponse(200, {"ok": 1})])
    assert c.get("/api/agent/user_profile") == {"ok": 1}
    assert [call[0] for call in c._session.calls] == ["POST", "GET", "POST", "GET"]


def test_403_raises_auth_error():
    c = _client([JWT_OK, FakeResponse(403)])
    with pytest.raises(bc.BotkinAuthError):
        c.post("/api/agent/log_bp", {"systolic": 120, "diastolic": 80})


# ── param handling ─────────────────────────────────────────────────────────


def test_get_drops_none_params():
    c = _client([JWT_OK, FakeResponse(200, {})])
    c.get("/api/agent/weight_history", days=None, series=True)

    get_call = [call for call in c._session.calls if call[0] == "GET"][0]
    assert get_call[2] == {"series": True}  # days=None отброшен


def test_path_normalized_with_leading_slash():
    c = _client([JWT_OK, FakeResponse(200, {})])
    c.get("api/agent/user_profile")  # без ведущего слэша

    get_call = [call for call in c._session.calls if call[0] == "GET"][0]
    assert get_call[1] == "https://api.example/api/agent/user_profile"
