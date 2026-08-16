"""Тесты записи в БД для эндпоинтов Apple Health (v1 `/apple_health`).

Покрывает два фикса free-пути (iOS Shortcuts):
- #328 — v1 сохраняет `spo2_pct` в `activity_log.raw_data` (раньше молча терял).
- #329 — Apple basal→BMR пишется как max-за-день (растёт к полному значению
  на позднейших запусках), но Garmin-BMR не затирается.
"""

import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "telegram-bot"))

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database.models import Base
from database.crud import create_or_update_activity, get_activity_by_date
from webhook.apple_health import _resolve_apple_bmr

TEST_UID = 895655
TEST_DATE = "2026-07-19"


# ── Unit: чистый резолвер BMR (#329) ────────────────────────────────────────


def _row(bmr, source):
    return SimpleNamespace(bmr_calories=bmr, source=source)


def test_resolve_bmr_none_basal_returns_none():
    assert _resolve_apple_bmr(None, None) is None
    assert _resolve_apple_bmr(_row(1000, "apple_health_shortcut"), None) is None


def test_resolve_bmr_first_write_when_no_row():
    # Нет строки за день → пишем basal (первый утренний запуск).
    assert _resolve_apple_bmr(None, 1033.0) == 1033.0


def test_resolve_bmr_passes_through_for_apple_row():
    # Строка от Apple → отдаём basal; monotonic-max в CRUD дорастит до полного.
    row = _row(1033.0, "apple_health_shortcut")
    assert _resolve_apple_bmr(row, 1600.0) == 1600.0
    # даже если новое меньше — резолвер отдаёт basal, регрессию гасит CRUD
    assert _resolve_apple_bmr(row, 800.0) == 800.0


def test_resolve_bmr_preserves_garmin():
    # Garmin уже заполнил BMR → не трогаем (возвращаем None), даже если Apple больше.
    row = _row(1600.0, "garmin")
    assert _resolve_apple_bmr(row, 1700.0) is None
    row_slash = _row(1600.0, "garmin/connect")
    assert _resolve_apple_bmr(row_slash, 1700.0) is None


def test_resolve_bmr_writes_when_garmin_row_has_no_bmr():
    # Garmin-строка без BMR (напр. только шаги) → Apple может заполнить.
    row = _row(None, "garmin")
    assert _resolve_apple_bmr(row, 1500.0) == 1500.0


# ── Integration: v1 эндпоинт через TestClient ───────────────────────────────


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
def client(api_db, monkeypatch):
    import database
    from webhook import apple_health

    # Держим единую in-memory сессию: endpoint зовёт SessionLocal()/db.close().
    monkeypatch.setattr(api_db, "close", lambda: None)
    monkeypatch.setattr(database, "SessionLocal", lambda: api_db)
    # Глобальный токен → роутинг на _target_user_id (обходим per-user lookup).
    monkeypatch.setattr(apple_health, "APPLE_HEALTH_TOKEN", "testtok")
    monkeypatch.setattr(apple_health, "_target_user_id", TEST_UID)

    return TestClient(apple_health.app)


def _post(client, **fields):
    body = {"date": TEST_DATE, **fields}
    return client.post("/apple_health", json=body, headers={"Authorization": "Bearer testtok"})


def test_v1_saves_spo2_to_raw_data(client, api_db):
    """#328 — spo2_pct долетает до activity_log.raw_data."""
    r = _post(client, steps=1000, spo2_pct=97.5)
    assert r.status_code == 200
    row = get_activity_by_date(api_db, TEST_UID, date.fromisoformat(TEST_DATE))
    assert row is not None
    assert (row.raw_data or {}).get("spo2_pct") == 97.5


def test_v1_bmr_grows_to_daily_max(client, api_db):
    """#329 — поздний (больший) basal обновляет BMR, регрессия гасится."""
    d = date.fromisoformat(TEST_DATE)

    assert _post(client, basal_energy_kcal=1033.0).status_code == 200
    assert get_activity_by_date(api_db, TEST_UID, d).bmr_calories == 1033.0

    # Вечерний запуск — полное значение → BMR растёт.
    assert _post(client, basal_energy_kcal=1600.0).status_code == 200
    assert get_activity_by_date(api_db, TEST_UID, d).bmr_calories == 1600.0

    # Частичный повторный синк меньше — не должен утащить вниз.
    assert _post(client, basal_energy_kcal=1200.0).status_code == 200
    assert get_activity_by_date(api_db, TEST_UID, d).bmr_calories == 1600.0


def test_v1_bmr_never_overwrites_garmin(client, api_db):
    """#329 — Garmin-BMR приоритетнее Apple, даже если Apple-значение больше."""
    d = date.fromisoformat(TEST_DATE)
    create_or_update_activity(
        db=api_db,
        user_id=TEST_UID,
        date=d,
        steps=5000,
        bmr_calories=1600.0,
        source="garmin",
    )

    r = _post(client, basal_energy_kcal=1700.0)
    assert r.status_code == 200
    row = get_activity_by_date(api_db, TEST_UID, d)
    assert row.bmr_calories == 1600.0  # Garmin сохранён, не 1700
    assert row.source == "garmin"  # source-строки создателя не меняется
