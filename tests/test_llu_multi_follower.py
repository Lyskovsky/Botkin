"""Multi-region followers: парсинг LLU_FOLLOWERS, region→api_url, merge/изоляция follower'ов.

Primary (EU) follower видит только within-region пациентов; RU/US и пр. обслуживаются
дополнительными региональными follower'ами (env LLU_FOLLOWERS).
"""

import importlib.util
from pathlib import Path

# scripts/import/ — не пакет (import — ключевое слово), грузим по пути (как test_librelinkup_import).
_MOD_PATH = Path(__file__).resolve().parent.parent / "scripts" / "import" / "librelinkup.py"
_spec = importlib.util.spec_from_file_location("librelinkup_multi", _MOD_PATH)
llu = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(llu)


# ── _followers_from_env: парсинг env ─────────────────────────────────────────────


def test_followers_from_env_unset(monkeypatch):
    monkeypatch.delenv("LLU_FOLLOWERS", raising=False)
    assert llu._followers_from_env() == []


def test_followers_from_env_parses_and_normalizes(monkeypatch):
    monkeypatch.setenv(
        "LLU_FOLLOWERS",
        '[{"region":"ru","email":"a@x","password":"p"},'
        '{"email":"b@x","password":"q"},'
        '{"region":"US","email":"","password":"z"}]',
    )
    assert llu._followers_from_env() == [
        {"region": "RU", "email": "a@x", "password": "p"},
        {"region": "EU", "email": "b@x", "password": "q"},  # region по умолчанию EU
    ]  # запись без email отброшена


def test_followers_from_env_malformed_json(monkeypatch):
    monkeypatch.setenv("LLU_FOLLOWERS", "{not json")
    assert llu._followers_from_env() == []  # не падаем, деградируем к []


def test_followers_from_env_not_a_list(monkeypatch):
    monkeypatch.setenv("LLU_FOLLOWERS", '{"region":"RU","email":"a@x","password":"p"}')
    assert llu._followers_from_env() == []  # объект вместо списка → игнор


# ── _extra_followers: БД + env, дедуп ─────────────────────────────────────────


def test_extra_followers_db_first_then_env(monkeypatch):
    """Порядок источников: сначала БД, потом env — БД источник истины."""
    monkeypatch.setattr(llu, "_followers_from_db", lambda: [{"region": "RU", "email": "db@x", "password": "p1"}])
    monkeypatch.setattr(llu, "_followers_from_env", lambda: [{"region": "EU", "email": "env@x", "password": "p2"}])
    assert [f["email"] for f in llu._extra_followers()] == ["db@x", "env@x"]


def test_extra_followers_dedups_same_account(monkeypatch):
    """Один аккаунт в БД и в env = один логин: лишние логины ловят 476."""
    monkeypatch.setattr(llu, "_followers_from_db", lambda: [{"region": "RU", "email": "a@x", "password": "from-db"}])
    monkeypatch.setattr(llu, "_followers_from_env", lambda: [{"region": "RU", "email": "A@X ", "password": "from-env"}])
    got = llu._extra_followers()
    assert len(got) == 1
    assert got[0]["password"] == "from-db"  # БД выигрывает


def test_extra_followers_same_email_other_region_kept(monkeypatch):
    """Тот же email в другом регионе — отдельный аккаунт, не дубль."""
    monkeypatch.setattr(llu, "_followers_from_db", lambda: [{"region": "RU", "email": "a@x", "password": "p"}])
    monkeypatch.setattr(llu, "_followers_from_env", lambda: [{"region": "EU", "email": "a@x", "password": "p"}])
    assert len(llu._extra_followers()) == 2


def test_followers_from_db_no_database_url(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert llu._followers_from_db() == []


def test_followers_from_db_connect_error_degrades(monkeypatch):
    """БД недоступна → [] и работа по env, а не падение импортёра."""

    def boom(*a, **kw):
        raise RuntimeError("could not connect")

    monkeypatch.setattr(llu.psycopg2, "connect", boom)
    assert llu._followers_from_db("postgresql://nope/nope") == []


def test_followers_from_db_decrypts_and_skips_broken(monkeypatch):
    """Расшифровка на месте; неразбираемая запись пропускается, остальные живут."""
    from core.infra.secrets import encrypt_secret

    good = encrypt_secret("real-password")
    rows = [("ru", "ok@x", good), ("EU", "bad@x", "v1:not-a-real-token")]

    class _Cur:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, *a):
            pass

        def fetchall(self):
            return rows

    class _Conn:
        def cursor(self):
            return _Cur()

        def close(self):
            pass

    monkeypatch.setattr(llu.psycopg2, "connect", lambda *a, **kw: _Conn())
    got = llu._followers_from_db("postgresql://x/y")
    assert got == [{"region": "RU", "email": "ok@x", "password": "real-password"}]


def test_mask_email_hides_local_part():
    assert llu._mask_email("pohodnyalla@icloud.com") == "p***@icloud.com"
    assert "@" not in llu._mask_email("nodomain")


# ── _resolve_api_url ──────────────────────────────────────────────────────────


def test_resolve_api_url_known_region():
    from pylibrelinkup import APIUrl

    assert llu._resolve_api_url("EU") == APIUrl.EU
    assert llu._resolve_api_url(None) == APIUrl.EU  # дефолт EU
    assert llu._resolve_api_url("eu") == APIUrl.EU  # регистронезависимо


def test_resolve_api_url_unknown_region_falls_back_to_host():
    # Региона нет в enum pylibrelinkup → региональный host тем же URL-паттерном.
    assert llu._resolve_api_url("ZZ") == "https://api-zz.libreview.io"


# ── collect_rows_all: merge + изоляция сбоев ──────────────────────────────────

_RU = {"region": "RU", "email": "a@x", "password": "p"}


def test_collect_rows_all_merges_primary_and_extra(monkeypatch):
    monkeypatch.setattr(llu, "collect_rows_with_retry", lambda: {"p1": [{"ts": "t1", "value": 5.0}]})
    monkeypatch.setattr(llu, "_extra_followers", lambda: [_RU])
    monkeypatch.setattr(llu, "_get_extra_client", lambda f, reset=False: object())
    monkeypatch.setattr(llu, "collect_rows", lambda client: {"p2": [{"ts": "t2", "value": 6.0}]})

    assert set(llu.collect_rows_all().keys()) == {"p1", "p2"}


def test_collect_rows_all_isolates_failing_extra(monkeypatch):
    """Упавший на логине extra-follower не валит primary."""
    monkeypatch.setattr(llu, "collect_rows_with_retry", lambda: {"p1": [{"ts": "t1", "value": 5.0}]})
    monkeypatch.setattr(llu, "_extra_followers", lambda: [_RU])

    def boom(f, reset=False):
        raise RuntimeError("476 ban")

    monkeypatch.setattr(llu, "_get_extra_client", boom)
    assert set(llu.collect_rows_all().keys()) == {"p1"}


def test_collect_rows_all_survives_primary_cooldown(monkeypatch):
    """Primary на cooldown (476) → всё равно тянем с extra-follower'ов."""

    def primary_cooldown():
        raise llu.LoginOnCooldownError(retry_in=120)

    monkeypatch.setattr(llu, "collect_rows_with_retry", primary_cooldown)
    monkeypatch.setattr(llu, "_extra_followers", lambda: [_RU])
    monkeypatch.setattr(llu, "_get_extra_client", lambda f, reset=False: object())
    monkeypatch.setattr(llu, "collect_rows", lambda client: {"p2": [{"ts": "t2", "value": 6.0}]})

    assert set(llu.collect_rows_all().keys()) == {"p2"}


def test_collect_rows_all_retries_extra_on_stale_token(monkeypatch):
    """Протух токен extra → 400 на первом collect_rows → сброс + повтор (как у primary #162)."""
    monkeypatch.setattr(llu, "collect_rows_with_retry", lambda: {})
    monkeypatch.setattr(llu, "_extra_followers", lambda: [_RU])
    calls = {"collect": 0}

    def get_client(f, reset=False):
        return "reset" if reset else "stale"

    def collect(client):
        calls["collect"] += 1
        if client == "stale":
            raise Exception("400 Client Error: Bad Request for url: .../llu/connections")
        return {"p2": [{"ts": "t2", "value": 6.0}]}

    monkeypatch.setattr(llu, "_get_extra_client", get_client)
    monkeypatch.setattr(llu, "collect_rows", collect)

    assert set(llu.collect_rows_all().keys()) == {"p2"}
    assert calls["collect"] == 2  # stale → reset → success


def test_collect_rows_all_no_extra_followers_is_primary_only(monkeypatch):
    monkeypatch.setattr(llu, "collect_rows_with_retry", lambda: {"p1": [{"ts": "t1", "value": 5.0}]})
    monkeypatch.setattr(llu, "_extra_followers", lambda: [])
    assert set(llu.collect_rows_all().keys()) == {"p1"}


# ── _get_extra_client: токен с диска / backoff по региону ────────────────────


class _FakeAuthClient:
    token = None
    account_id_hash = None

    def __init__(self):
        self.auth_calls = 0

    def _set_token(self, t):
        self.token = t

    def _set_account_id_hash(self, h):
        self.account_id_hash = h

    def authenticate(self):
        self.auth_calls += 1
        self.token = "FRESH"
        self.account_id_hash = "FRESH_HASH"


def _isolate_extra_state(monkeypatch, tmp_path):
    """Чистое состояние extra-слоя + token-cache в tmp (не трогаем реальный data/cache)."""
    monkeypatch.setattr(llu, "_extra_clients", {})
    monkeypatch.setattr(llu, "_extra_blocked_until", {})
    monkeypatch.setattr(llu, "_extra_fail_count", {})
    monkeypatch.setattr(llu, "_extra_token_cache", lambda region: tmp_path / f"llu_token_{region.lower()}.json")


def test_get_extra_client_restores_token_without_login(monkeypatch, tmp_path):
    import json

    _isolate_extra_state(monkeypatch, tmp_path)
    (tmp_path / "llu_token_ru.json").write_text(json.dumps({"token": "T", "account_id_hash": "H"}))
    fake = _FakeAuthClient()
    monkeypatch.setattr(llu, "_new_client", lambda follower=None: fake)

    c = llu._get_extra_client(_RU)
    assert c.token == "T"  # восстановлен с диска
    assert fake.auth_calls == 0  # НЕ логинились


def test_get_extra_client_logs_in_and_persists(monkeypatch, tmp_path):
    import json

    _isolate_extra_state(monkeypatch, tmp_path)
    fake = _FakeAuthClient()
    monkeypatch.setattr(llu, "_new_client", lambda follower=None: fake)

    c = llu._get_extra_client(_RU)
    assert c.token == "FRESH" and fake.auth_calls == 1
    saved = json.loads((tmp_path / "llu_token_ru.json").read_text())
    assert saved == {"token": "FRESH", "account_id_hash": "FRESH_HASH"}


def test_get_extra_client_failed_login_sets_region_backoff(monkeypatch, tmp_path):
    import pytest

    _isolate_extra_state(monkeypatch, tmp_path)

    class _Boom(_FakeAuthClient):
        def authenticate(self):
            raise RuntimeError("476 Cloudflare ban")

    monkeypatch.setattr(llu, "_new_client", lambda follower=None: _Boom())

    with pytest.raises(RuntimeError):
        llu._get_extra_client(_RU)
    assert llu._extra_fail_count["RU"] == 1
    # Активный cooldown → следующий вызов без токена кидает LoginOnCooldownError
    with pytest.raises(llu.LoginOnCooldownError):
        llu._get_extra_client(_RU)


def test_get_extra_client_backoff_is_per_region(monkeypatch, tmp_path):
    """Cooldown RU не мешает логину US — состояние изолировано по регионам."""
    import time

    _isolate_extra_state(monkeypatch, tmp_path)
    llu._extra_blocked_until["RU"] = time.monotonic() + 999.0
    fake = _FakeAuthClient()
    monkeypatch.setattr(llu, "_new_client", lambda follower=None: fake)

    us = {"region": "US", "email": "u@x", "password": "p"}
    c = llu._get_extra_client(us)
    assert c.token == "FRESH"  # US залогинился, RU-cooldown не помешал
