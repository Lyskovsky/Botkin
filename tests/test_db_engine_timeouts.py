"""Session-таймауты движка БД — защита от бесконечного зависания под локом.

Прецедент 16.07.2026: meal-save на дев-Postgres висел ≥15с на INSERT в
nutrition_log (INSERT ждал лок без тайм-аута). `_build_connect_args` добавляет
`lock_timeout` + `idle_in_transaction_session_timeout` для psycopg2, но НЕ
`statement_timeout` (тот прибил бы длинные легитимные запросы).
"""

import database


def test_postgres_url_gets_lock_and_idle_timeouts():
    opts = database._build_connect_args("postgresql://u:p@host:5432/db")["options"]
    assert "lock_timeout=" in opts
    assert "idle_in_transaction_session_timeout=" in opts


def test_no_statement_timeout_set():
    # statement_timeout НЕ выставляется намеренно: прибил бы длинные
    # агент/дашборд-запросы на проде.
    opts = database._build_connect_args("postgresql://u:p@host:5432/db")["options"]
    assert "statement_timeout" not in opts


def test_defaults_are_5s_lock_and_15s_idle(monkeypatch):
    monkeypatch.delenv("DB_LOCK_TIMEOUT_MS", raising=False)
    monkeypatch.delenv("DB_IDLE_TX_TIMEOUT_MS", raising=False)
    opts = database._build_connect_args("postgresql://x")["options"]
    assert "lock_timeout=5000" in opts
    assert "idle_in_transaction_session_timeout=15000" in opts


def test_timeouts_configurable_via_env(monkeypatch):
    monkeypatch.setenv("DB_LOCK_TIMEOUT_MS", "1234")
    monkeypatch.setenv("DB_IDLE_TX_TIMEOUT_MS", "6789")
    opts = database._build_connect_args("postgresql://x")["options"]
    assert "lock_timeout=1234" in opts
    assert "idle_in_transaction_session_timeout=6789" in opts


def test_non_postgres_url_gets_empty_connect_args():
    # sqlite (тесты) не понимает libpq `options` → connect_args не строим.
    assert database._build_connect_args("sqlite:///:memory:") == {}
