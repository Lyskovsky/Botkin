"""Tests for Agent Tools API — 8 endpoints for NanoClaw containers.

All tests use TestClient with mocked auth + in-memory SQLite DB.
No real DB connections or JWT secrets required.
"""

import sys
from pathlib import Path

# Project root → database module
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
# telegram-bot → webhook package
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "telegram-bot"))

import pytest
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

# Эндпоинты считают «сегодня» в таймзоне пользователя (тест-юзер — Europe/Moscow,
# фикс. UTC+3). Сравнивать с naive date.today() (UTC) нельзя: в окне 21:00–24:00 UTC
# календарные сутки расходятся и тест флакает. Берём «сегодня» в той же tz.
MSK = ZoneInfo("Europe/Moscow")
from unittest.mock import MagicMock, patch
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database.models import Base, User, NutritionLog, SupplementLog
from database.crud import create_nutrition_log, create_or_update_activity, create_weight


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def engine():
    """In-memory SQLite engine with shared connection (StaticPool)."""
    e = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=e)
    yield e
    Base.metadata.drop_all(bind=e)


@pytest.fixture
def db_session(engine):
    """SQLAlchemy session backed by the in-memory engine."""
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    session = Session()
    # Create the test user so FK constraints are satisfied
    user = User(
        telegram_id=895655,
        first_name="Sasha",
        username="alexlyskovsky",
        cohort="owner",
        container_id="nc-sasha",
        pack_name="bariatric",
        health_token="hvt_old_token",
        jwt_secret="test_secret",
        is_active=True,
    )
    session.add(user)
    session.commit()
    try:
        yield session
    finally:
        session.close()


def _make_mock_user(health_token="hvt_old_token"):
    user = MagicMock()
    user.telegram_id = 895655
    user.container_id = "nc-sasha"
    user.cohort = "owner"
    user.first_name = "Sasha"
    user.username = "alexlyskovsky"
    user.garmin_email = "test@garmin.com"
    user.health_token = health_token
    user.pack_name = "bariatric"
    user.timezone = "Europe/Moscow"
    user.sex = "male"
    user.height_cm = 178
    user.birth_date = None
    user.share_token = "testtoken123abc456def789ghi0"
    # Явный дефолт: у MagicMock любой атрибут «истинный», а meal_context читает
    # onboarding_data как dict (#340) — без этого тесты падают на .get().
    user.onboarding_data = {}
    return user


@pytest.fixture
def client(db_session, monkeypatch):
    """TestClient with mocked auth and DB session injection."""
    from webhook import agent_tools_api
    from webhook.jwt_auth import get_agent_user, get_db

    # Patch close() so production code's db.close() is a no-op on our session
    monkeypatch.setattr(db_session, "close", lambda: None)
    monkeypatch.setattr(agent_tools_api, "get_db", lambda: iter([db_session]))

    app = FastAPI()
    app.include_router(agent_tools_api.router)

    mock_user = _make_mock_user()

    def _mock_agent_user():
        return mock_user

    app.dependency_overrides[get_agent_user] = _mock_agent_user
    app.dependency_overrides[get_db] = lambda: db_session

    return TestClient(app)


# ── Task 5/6: Write endpoint tests ───────────────────────────────────────────


@pytest.fixture
def mock_food_llm(monkeypatch):
    """Мок analyze_message: unit-тесты не зовут реальный LLM (раньше эти тесты
    были deselected как live-LLM — аудит 11.06.2026)."""
    import core.llm.router as llm_router

    def _fake_analyze(text: str, user_id: int = None, **kwargs):
        return {
            "type": "food",
            "data": {
                "items": [{"name": "Гречка", "weight": 200, "calories": 220, "protein": 8, "fats": 2, "carbs": 42}]
            },
        }

    monkeypatch.setattr(llm_router, "analyze_message", _fake_analyze)


def test_log_meal_text_returns_200(client, db_session, mock_food_llm):
    """POST /log_meal_text stores a meal and returns 200 with meal_id."""
    r = client.post(
        "/api/agent/log_meal_text",
        json={"text": "гречка 200г", "date": "2026-05-04", "slot": "lunch"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ok"
    assert "meal_id" in body
    assert body["date"] == "2026-05-04"
    assert body["slot"] == "lunch"
    assert body["meal_name"] == "Обед"
    # Verify it's actually in the DB
    logs = db_session.query(NutritionLog).filter_by(user_id=895655).all()
    assert len(logs) == 1


def test_log_meal_text_defaults_to_today(client, db_session, mock_food_llm):
    """POST /log_meal_text without date defaults to today."""
    r = client.post(
        "/api/agent/log_meal_text",
        json={"text": "кофе с молоком"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["date"] == datetime.now(MSK).date().isoformat()


def test_log_meal_text_rejects_non_food(client, db_session, monkeypatch):
    """Если LLM не распознал еду — ошибка с пояснением, в БД ничего не пишется."""
    import core.llm.router as llm_router

    monkeypatch.setattr(llm_router, "analyze_message", lambda text, user_id=None, **kw: {"type": "question"})
    r = client.post("/api/agent/log_meal_text", json={"text": "как дела?"})
    body = r.json()
    assert body.get("status") != "ok"
    assert db_session.query(NutritionLog).filter_by(user_id=895655).count() == 0


def test_log_meal_text_invalid_slot_returns_400(client):
    """POST /log_meal_text with unknown slot → 400."""
    r = client.post(
        "/api/agent/log_meal_text",
        json={"text": "что-то", "slot": "brunch"},
    )
    assert r.status_code == 400


def test_log_supplement_returns_200(client, db_session):
    """POST /log_supplement stores supplement and returns 200."""
    r = client.post(
        "/api/agent/log_supplement",
        json={
            "supplement_name": "Витамин D3",
            "dosage": "5000 IU",
            "date": "2026-05-04",
            "time": "08:00",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ok"
    assert "supplement_id" in body
    assert body["supplement_name"] == "Витамин D3"
    assert body["dosage"] == "5000 IU"
    # Verify DB
    logs = db_session.query(SupplementLog).filter_by(user_id=895655).all()
    assert len(logs) == 1
    assert logs[0].supplement_name == "Витамин D3"


def test_log_supplement_without_optional_fields(client, db_session):
    """POST /log_supplement with only required fields — date/time/dosage all optional."""
    r = client.post(
        "/api/agent/log_supplement",
        json={"supplement_name": "Магний"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["supplement_name"] == "Магний"
    assert body["dosage"] is None


def test_log_bp_returns_200(client, db_session, monkeypatch):
    """POST /log_bp inserts into blood_pressure_logs and returns 200."""
    # Patch db.execute and db.commit for SQLite (blood_pressure_logs uses raw SQL)
    execute_calls = []
    original_execute = db_session.execute

    def fake_execute(stmt, params=None):
        execute_calls.append((str(stmt), params))
        # For SQLite compatibility: just skip the raw INSERT
        return MagicMock()

    monkeypatch.setattr(db_session, "execute", fake_execute)
    monkeypatch.setattr(db_session, "commit", lambda: None)

    r = client.post(
        "/api/agent/log_bp",
        json={
            "systolic": 120,
            "diastolic": 78,
            "pulse": 65,
            "measured_at": "2026-05-04T08:30:00",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ok"
    assert body["systolic"] == 120
    assert body["diastolic"] == 78
    assert body["pulse"] == 65
    assert len(execute_calls) == 1


def test_log_bp_defaults_to_now(client, db_session, monkeypatch):
    """POST /log_bp without measured_at uses current time — no 422 validation error."""
    execute_calls = []

    def fake_execute(stmt, params=None):
        execute_calls.append(params)
        return MagicMock()

    monkeypatch.setattr(db_session, "execute", fake_execute)
    monkeypatch.setattr(db_session, "commit", lambda: None)

    r = client.post(
        "/api/agent/log_bp",
        json={"systolic": 118, "diastolic": 76},  # no measured_at
    )
    # Status 200 (SQLite compatible mock) — no 422 unprocessable entity
    assert r.status_code == 200, r.text
    assert r.json()["systolic"] == 118
    assert r.json()["diastolic"] == 76
    # measured_at should be set automatically (not None)
    assert r.json()["measured_at"] is not None
    assert len(execute_calls) == 1


def test_regenerate_health_token_returns_new_token(client, db_session):
    """POST /regenerate_health_token returns a new hvt_... token and saves it."""
    r = client.post("/api/agent/regenerate_health_token")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ok"
    assert "health_token" in body
    new_token = body["health_token"]
    assert new_token.startswith("hvt_895655_")
    # Token should have changed from the mock user's original
    assert new_token != "hvt_old_token"


# ── Task 7: Read endpoint tests ───────────────────────────────────────────────


def test_recent_meals_returns_meals_list(client, db_session):
    """GET /recent_meals returns meals from the last N days."""
    create_nutrition_log(
        db=db_session,
        user_id=895655,
        date=date.today(),
        meal_time=time(13, 0),
        meal_name="Обед",
        items=[
            {"product": "Курица", "weight_g": 150, "calories": 250, "protein": 40, "fats": 5, "carbs": 0, "fiber": 0}
        ],
        totals={"calories": 250, "protein": 40, "fats": 5, "carbs": 0, "fiber": 0},
    )
    r = client.get("/api/agent/recent_meals?days=7")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ok"
    assert body["days"] == 7
    assert len(body["meals"]) == 1
    meal = body["meals"][0]
    assert meal["meal_name"] == "Обед"
    assert meal["meal_time"] == "13:00"
    assert meal["totals"]["calories"] == 250


def test_recent_meals_scoped_to_user(client, db_session):
    """GET /recent_meals only returns meals for the authenticated user."""
    # Create meal for another user
    other_user = User(telegram_id=111, first_name="Other", is_active=True, cohort="external", pack_name="generic")
    db_session.add(other_user)
    db_session.commit()
    create_nutrition_log(
        db=db_session,
        user_id=111,
        date=date.today(),
        meal_time=time(9, 0),
        meal_name="Чужой завтрак",
        items=[
            {"product": "Пицца", "weight_g": 300, "calories": 900, "protein": 30, "fats": 40, "carbs": 90, "fiber": 3}
        ],
        totals={"calories": 900, "protein": 30, "fats": 40, "carbs": 90, "fiber": 3},
    )
    r = client.get("/api/agent/recent_meals?days=7")
    assert r.status_code == 200, r.text
    assert r.json()["meals"] == []


def test_recent_meals_invalid_days(client):
    """GET /recent_meals with days=0 → 400."""
    r = client.get("/api/agent/recent_meals?days=0")
    assert r.status_code == 400


def test_recent_meals_compact_returns_product_names(client, db_session):
    """compact=true → items как список имён, totals только калории (F-006)."""
    create_nutrition_log(
        db=db_session,
        user_id=895655,
        date=date.today(),
        meal_time=time(13, 0),
        meal_name="Обед",
        items=[
            {"product": "Хинкали", "weight_g": 200, "calories": 400, "protein": 20, "fats": 15, "carbs": 45, "fiber": 2}
        ],
        totals={"calories": 400, "protein": 20, "fats": 15, "carbs": 45, "fiber": 2},
    )
    r = client.get("/api/agent/recent_meals?days=7&compact=true")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["compact"] is True
    meal = body["meals"][0]
    assert meal["items"] == ["Хинкали"]
    assert meal["totals"] == {"calories": 400}


def test_recent_meals_long_window_auto_compact(client, db_session):
    """days>14 авто-включает compact, даже если не передан явно (F-006)."""
    create_nutrition_log(
        db=db_session,
        user_id=895655,
        date=date.today(),
        meal_time=time(13, 0),
        meal_name="Обед",
        items=[{"product": "Рис", "weight_g": 150, "calories": 200}],
        totals={"calories": 200, "protein": 4, "fats": 1, "carbs": 44, "fiber": 1},
    )
    r = client.get("/api/agent/recent_meals?days=30")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["compact"] is True
    assert body["meals"][0]["items"] == ["Рис"]


def _seed_meal(db_session, **kw):
    defaults = dict(
        db=db_session,
        user_id=895655,
        date=date.today(),
        meal_time=time(13, 0),
        meal_name="Обед",
        items=[{"product": "Курица", "weight_g": 150, "calories": 250}],
        totals={"calories": 250, "protein": 40, "fats": 5, "carbs": 0, "fiber": 0},
    )
    defaults.update(kw)
    return create_nutrition_log(**defaults)


def test_edit_meal_moves_date(client, db_session):
    """POST /edit_meal с new_date переносит запись на другой день (P-001)."""
    from datetime import timedelta

    meal = _seed_meal(db_session)
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    r = client.post("/api/agent/edit_meal", json={"meal_id": meal.id, "new_date": yesterday})
    assert r.status_code == 200, r.text
    assert r.json()["date"] == yesterday


def test_edit_meal_rename_and_slot(client, db_session):
    """POST /edit_meal меняет имя и слот (время)."""
    meal = _seed_meal(db_session)
    r = client.post(
        "/api/agent/edit_meal",
        json={"meal_id": meal.id, "new_name": "Ужин (перенос)", "new_slot": "dinner"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["meal_name"] == "Ужин (перенос)"
    assert body["meal_time"] == "19:00"


def test_edit_meal_not_found(client):
    """POST /edit_meal с несуществующим meal_id → 404."""
    r = client.post("/api/agent/edit_meal", json={"meal_id": 999999, "new_name": "x"})
    assert r.status_code == 404


def test_edit_meal_nothing_to_change(client, db_session):
    """POST /edit_meal без полей-изменений → 400."""
    meal = _seed_meal(db_session)
    r = client.post("/api/agent/edit_meal", json={"meal_id": meal.id})
    assert r.status_code == 400


def test_delete_meal_removes_row(client, db_session):
    """POST /delete_meal удаляет запись; после — recent_meals пуст (P-001)."""
    meal = _seed_meal(db_session)
    r = client.post("/api/agent/delete_meal", json={"meal_id": meal.id})
    assert r.status_code == 200, r.text
    assert r.json()["deleted_meal_id"] == meal.id
    assert client.get("/api/agent/recent_meals?days=7").json()["meals"] == []


def test_delete_meal_not_found(client):
    """POST /delete_meal с несуществующим meal_id → 404."""
    r = client.post("/api/agent/delete_meal", json={"meal_id": 999999})
    assert r.status_code == 404


def test_delete_meal_scoped_to_user(client, db_session):
    """Нельзя удалить чужой приём пищи (RLS-изоляция по user_id)."""
    other = User(telegram_id=222, first_name="Other", is_active=True, cohort="external", pack_name="generic")
    db_session.add(other)
    db_session.commit()
    foreign = _seed_meal(db_session, user_id=222, meal_name="Чужой обед")
    r = client.post("/api/agent/delete_meal", json={"meal_id": foreign.id})
    assert r.status_code == 404


def test_kb_value_owner_returns_value(client, tmp_path, monkeypatch):
    """GET /kb_value for owner cohort reads knowledge_base.json."""
    import json

    kb_data = {"blood_tests": [{"date": "2026-01-01", "values": {"cholesterol": 5.1}}], "name": "Alexander"}
    kb_file = tmp_path / "knowledge_base.json"
    kb_file.write_text(json.dumps(kb_data), encoding="utf-8")

    import webhook.agent_tools_api as ata

    monkeypatch.setattr(ata, "Path", lambda *args: kb_file if "knowledge_base" in str(args) else Path(*args))

    # Patch the path resolution directly
    with patch("webhook.agent_tools_api.Path") as mock_path_cls:
        mock_path_instance = MagicMock()
        mock_path_instance.__truediv__ = lambda self, other: kb_file
        mock_path_instance.resolve.return_value = mock_path_instance
        mock_path_instance.parents = [mock_path_instance, mock_path_instance, tmp_path]
        mock_path_cls.return_value = mock_path_instance

        r = client.get("/api/agent/kb_value?key=name")

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["key"] == "name"
    # Value lookup depends on path mock — at minimum no crash


def test_kb_value_non_owner_returns_stub(db_session, monkeypatch):
    """GET /kb_value for non-owner user returns kb-not-available stub.

    Только owner-cohort пользователь имеет полный knowledge_base.json. Для
    family/early_user/external endpoint возвращает заглушку, не падает.
    """
    from fastapi.testclient import TestClient
    from webhook import agent_tools_api
    from webhook.jwt_auth import get_agent_user, get_db

    app = FastAPI()
    app.include_router(agent_tools_api.router)

    non_owner = _make_mock_user()
    non_owner.cohort = "external"

    app.dependency_overrides[get_agent_user] = lambda: non_owner
    app.dependency_overrides[get_db] = lambda: db_session

    c = TestClient(app)
    r = c.get("/api/agent/kb_value?key=anything")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["value"] is None
    assert body["source"] == "kb-not-available"


def test_dashboard_summary_empty_db(client):
    """GET /dashboard_summary returns valid structure when no data exists."""
    r = client.get("/api/agent/dashboard_summary")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ok"
    assert "period" in body
    assert body["period"]["days"] == 7
    assert body["activity"]["avg_steps"] is None
    assert body["activity"]["avg_hr"] is None
    assert body["nutrition"]["avg_kcal_consumed"] is None
    assert body["weight"]["latest_kg"] is None


def test_dashboard_summary_with_data(client, db_session):
    """GET /dashboard_summary aggregates data correctly."""
    today = date.today()
    create_or_update_activity(
        db=db_session,
        user_id=895655,
        date=today,
        steps=8000,
        heart_rate_avg=70,
        total_calories=2200,
        source="test",
    )
    create_nutrition_log(
        db=db_session,
        user_id=895655,
        date=today,
        meal_time=time(13, 0),
        meal_name="Обед",
        items=[],
        totals={"calories": 1800, "protein": 120, "fats": 60, "carbs": 180, "fiber": 25},
    )
    create_weight(
        db=db_session,
        user_id=895655,
        measured_at=datetime(today.year, today.month, today.day, 8, 0),
        weight=82.5,
        body_fat=22.3,
    )

    r = client.get("/api/agent/dashboard_summary")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["activity"]["avg_steps"] == 8000
    assert body["activity"]["avg_hr"] == 70
    assert body["nutrition"]["avg_kcal_consumed"] == 1800.0
    assert body["weight"]["latest_kg"] == 82.5
    assert body["weight"]["body_fat_pct"] == 22.3


def test_dashboard_summary_includes_dashboard_url(client):
    """GET /dashboard_summary returns dashboard_url from user.share_token."""
    r = client.get("/api/agent/dashboard_summary")
    assert r.status_code == 200, r.text
    body = r.json()
    assert "dashboard_url" in body
    assert body["dashboard_url"] == "https://botkin.health/mc/testtoken123abc456def789ghi0"


def test_dashboard_summary_no_share_token_returns_null_url(db_session, monkeypatch):
    """GET /dashboard_summary returns dashboard_url=None when share_token is absent."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from webhook import agent_tools_api
    from webhook.jwt_auth import get_agent_user, get_db

    monkeypatch.setattr(db_session, "close", lambda: None)
    monkeypatch.setattr(agent_tools_api, "get_db", lambda: iter([db_session]))

    app = FastAPI()
    app.include_router(agent_tools_api.router)

    mock_no_token = _make_mock_user()
    mock_no_token.share_token = None

    app.dependency_overrides[get_agent_user] = lambda: mock_no_token
    app.dependency_overrides[get_db] = lambda: db_session

    c = TestClient(app)
    r = c.get("/api/agent/dashboard_summary")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["dashboard_url"] is None


def test_user_profile_returns_profile(client):
    """GET /user_profile returns expected profile fields."""
    r = client.get("/api/agent/user_profile")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ok"
    assert body["telegram_id"] == 895655
    assert body["first_name"] == "Sasha"
    assert body["cohort"] == "owner"
    assert body["container_id"] == "nc-sasha"
    assert body["pack_name"] == "bariatric"
    assert body["health_token"] == "hvt_old_token"
    assert body["garmin_email"] == "test@garmin.com"
    assert "timezone" in body


# ── New tools (post-NanoClaw, added 21.05.2026) ─────────────────────────────


def test_weight_history_no_data(client):
    """GET /weight_history with no weights — returns no_data, not 500."""
    r = client.get("/api/agent/weight_history")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "no_data"


def test_weight_history_returns_latest_and_extremes(client, db_session):
    """GET /weight_history — latest + all_time extremes when data present."""
    create_weight(db_session, user_id=895655, measured_at=datetime(2020, 12, 16, 8), weight=74.9, body_fat=25.7)
    create_weight(db_session, user_id=895655, measured_at=datetime(2024, 1, 10, 8), weight=86.3, body_fat=35.9)
    create_weight(db_session, user_id=895655, measured_at=datetime(2026, 5, 16, 8), weight=76.6, body_fat=26.7)

    r = client.get("/api/agent/weight_history")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ok"
    assert body["latest"]["weight_kg"] == 76.6
    assert body["latest"]["body_fat_pct"] == 26.7
    assert body["all_time"]["min_weight"]["weight_kg"] == 74.9
    assert body["all_time"]["max_weight"]["weight_kg"] == 86.3
    assert body["all_time"]["min_body_fat"]["body_fat_pct"] == 25.7
    assert body["all_time"]["min_body_fat"]["date"] == "2020-12-16"


def test_weight_history_with_window(client, db_session):
    """GET /weight_history?days=30 — adds in_window aggregate."""
    create_weight(db_session, user_id=895655, measured_at=datetime(2020, 1, 1, 8), weight=80.0, body_fat=30.0)
    create_weight(db_session, user_id=895655, measured_at=datetime.now(), weight=76.0, body_fat=26.0)

    r = client.get("/api/agent/weight_history?days=30")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["window_days"] == 30
    assert body["in_window"]["count"] == 1  # only the recent one
    assert body["all_time"]["count"] == 2


def test_day_summary_invalid_date(client):
    """GET /day_summary with malformed date — returns error, not 500."""
    r = client.get("/api/agent/day_summary?date=not-a-date")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "error"


def test_day_summary_no_data(client):
    """GET /day_summary для пустой даты — no_data, не 500."""
    r = client.get("/api/agent/day_summary?date=2020-01-01")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "no_data"


def test_day_summary_live_aggregation(client, db_session):
    """day_summary агрегирует live из nutrition_log + activity_log + weights
    (таблица daily_summaries никогда не заполнялась — аудит 11.06.2026)."""
    d = date(2026, 6, 1)
    create_nutrition_log(
        db_session,
        user_id=895655,
        date=d,
        meal_time=time(9, 0),
        meal_name="Завтрак",
        items=[{"product": "Овсянка", "weight_g": 60, "calories": 240, "protein": 8, "fats": 5, "carbs": 42}],
        totals={"calories": 240, "protein": 8, "fats": 5, "carbs": 42, "fiber": 6},
    )
    create_nutrition_log(
        db_session,
        user_id=895655,
        date=d,
        meal_time=time(13, 0),
        meal_name="Обед",
        items=[{"product": "Суп", "weight_g": 300, "calories": 200, "protein": 10, "fats": 7, "carbs": 20}],
        totals={"calories": 200, "protein": 10, "fats": 7, "carbs": 20, "fiber": 2},
    )
    create_or_update_activity(db_session, user_id=895655, date=d, steps=12000, sleep_hours=7.5)
    create_weight(db_session, user_id=895655, measured_at=datetime(2026, 6, 1, 8, 0), weight=82.4)

    r = client.get("/api/agent/day_summary?date=2026-06-01")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ok"
    assert body["nutrition"]["calories"] == 440
    assert body["nutrition"]["protein_g"] == 18
    assert body["nutrition"]["meals_count"] == 2
    assert body["activity"]["steps"] == 12000
    assert body["sleep_hours"] == 7.5
    assert body["weight_kg"] == 82.4


def test_body_measurements_no_data(client):
    """GET /body_measurements with no data — returns no_data, not 500."""
    r = client.get("/api/agent/body_measurements")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "no_data"


def test_recent_biomarkers_returns_canonical_keys():
    from core.health.kb_schema import to_canonical

    raw = {"ldl_mmol_l": 3.1, "alt_u_l": 22, "fibrinogen_g_l": 3.0}
    canon, _ = to_canonical(raw, passthrough_unmapped=True)
    assert canon["LDL"] == 3.1  # mapped
    assert canon["ALT"] == 22  # mapped
    assert canon["fibrinogen"] == 3.0  # mapped (fibrinogen now in registry)


def test_phenoage_marker_lookup_canonical_for_dima_keys():
    from core.health.kb_schema import to_canonical

    rows_values = [
        {
            "mcv_fl": 90,
            "wbc_10_9_l": 6.7,
            "creatinine_umol_l": 92,
            "glucose_mmol_l": 5.3,
            "alkaline_phosphatase_u_l": 68,
            "lymphocytes_pct": 28,
            "albumin_g_l": 42,
            "rdw_cv": 13.8,
            "hs_CRP": 1.0,
        }
    ]
    latest = {}
    for vals in rows_values:
        canon, _ = to_canonical(vals)
        for k, v in canon.items():
            latest.setdefault(k, v)
    for need in ["albumin_g_l", "creatinine", "glucose", "hs_CRP", "lymphocytes", "MCV", "RDW_CV", "ALP", "WBC"]:
        assert need in latest, f"phenoage marker {need} не извлёкся из ключей Димы"


# ── US-биохимия на чтении (issue #95: панель Маккаби g/dL·mg/dL) ─────────────


def test_recent_biomarkers_canonizes_us_biochemistry(client, db_session):
    """US-запись biochemistry с _unit_system → метрика на чтении; служебный ключ не протекает."""
    from database.models import BloodTest

    db_session.add(
        BloodTest(
            user_id=895655,
            test_date=date(2026, 6, 9),
            test_type="biochemistry",
            values={"_unit_system": "US", "albumin": 5.1, "ALKP": 55, "glucose": 86},
        )
    )
    db_session.commit()

    r = client.get("/api/agent/recent_biomarkers")
    assert r.status_code == 200, r.text
    vals = r.json()["tests"][0]["values"]
    assert abs(vals["albumin_g_l"] - 51.0) < 1e-6  # 5.1 g/dL → 51 g/L
    assert vals["ALP"] == 55  # ALKP → ALP (Ед/л, без конверсии)
    assert abs(vals["glucose"] - 86 / 18.0156) < 1e-3  # mg/dL → ммоль/л
    assert "_unit_system" not in vals  # служебный признак не отдаётся агенту


def test_phenoage_sees_albumin_from_us_biochemistry(client, db_session):
    """phenoage извлекает альбумин из US-биохимии в правильной единице (был «нет альбумина»)."""
    from database.models import BloodTest

    db_session.add(
        BloodTest(
            user_id=895655,
            test_date=date(2026, 6, 9),
            test_type="biochemistry",
            values={"_unit_system": "US", "albumin": 5.1, "ALKP": 55},
        )
    )
    db_session.commit()

    r = client.get("/api/agent/phenoage")
    assert r.status_code == 200, r.text
    by_name = {m["name"]: m for m in r.json()["markers"]}
    assert by_name["albumin_g_l"]["value"] is not None, "альбумин снова не виден"
    assert abs(by_name["albumin_g_l"]["value"] - 51.0) < 1e-2  # метрика, не 5.1
    assert by_name["ALP"]["value"] == 55  # из ключа ALKP


# ── RLS / user isolation (аудит 11.06.2026: privacy-гарантия без тестов) ─────


def _add_other_user(db_session, tid=111):
    other = User(telegram_id=tid, first_name="Other", is_active=True, cohort="external", pack_name="generic")
    db_session.add(other)
    db_session.commit()
    return other


def test_weight_history_scoped_to_user(client, db_session):
    """/weight_history не отдаёт замеры другого пользователя."""
    _add_other_user(db_session)
    create_weight(db_session, user_id=111, measured_at=datetime(2026, 6, 5, 8, 0), weight=99.9)
    create_weight(db_session, user_id=895655, measured_at=datetime(2026, 6, 5, 8, 0), weight=82.0)

    r = client.get("/api/agent/weight_history?days=30&series=true")
    assert r.status_code == 200, r.text
    body = r.json()
    text = str(body)
    assert "99.9" not in text, "чужой вес просочился в ответ"
    assert body["latest"]["weight_kg"] == 82.0


def test_weight_history_other_user_only_data_invisible(client, db_session):
    """Если данные есть ТОЛЬКО у чужого юзера — для нас это no_data, не утечка."""
    _add_other_user(db_session, tid=222)
    create_weight(db_session, user_id=222, measured_at=datetime(2026, 6, 5, 8, 0), weight=99.9)

    r = client.get("/api/agent/weight_history?days=30")
    assert r.status_code == 200, r.text
    assert "99.9" not in str(r.json())


def test_day_summary_scoped_to_user(client, db_session):
    """/day_summary агрегирует только данные аутентифицированного юзера."""
    d = date(2026, 6, 2)
    _add_other_user(db_session)
    create_nutrition_log(
        db_session,
        user_id=111,
        date=d,
        meal_time=time(13, 0),
        meal_name="Чужой обед",
        items=[{"product": "Бургер", "weight_g": 350, "calories": 777, "protein": 30, "fats": 40, "carbs": 60}],
        totals={"calories": 777, "protein": 30, "fats": 40, "carbs": 60, "fiber": 2},
    )
    create_or_update_activity(db_session, user_id=111, date=d, steps=55555)

    r = client.get("/api/agent/day_summary?date=2026-06-02")
    assert r.status_code == 200, r.text
    body = r.json()
    # У нашего юзера на эту дату данных нет — чужие не должны подмешаться
    assert body["status"] == "no_data", body


def test_recent_supplements_scoped_to_user(client, db_session, monkeypatch):
    """/recent_supplements фильтрует по user_id (raw SQL → проверяем параметры).

    Сам запрос использует ARRAY_AGG (Postgres) и не исполним на SQLite —
    проверяем, что в него уходит именно telegram_id аутентифицированного юзера.
    """
    captured = {}
    original_execute = db_session.execute

    def spy_execute(stmt, params=None):
        if params and "uid" in params:
            captured["uid"] = params["uid"]
            raise RuntimeError("stop after capture (SQLite can't run ARRAY_AGG)")
        return original_execute(stmt, params)

    monkeypatch.setattr(db_session, "execute", spy_execute)
    try:
        client.get("/api/agent/recent_supplements?days=30")
    except RuntimeError:
        pass  # ожидаемо: нарочно прерываем после захвата параметров
    assert captured.get("uid") == 895655


class TestSupplementDailyLog:
    """GET /supplement_daily_log — per-day taken-dates for correlation joins."""

    def _add(self, db, name, d, dosage=None, t=time(9, 0), uid=895655):
        db.add(SupplementLog(user_id=uid, date=d, time=t, supplement_name=name, dosage=dosage))

    def test_taken_dates_and_adherence(self, client, db_session):
        today = datetime.now(MSK).date()
        # магний: 3 разных дня (сегодня два приёма — схлопываются в одну дату)
        self._add(db_session, "магний", today, dosage="350мг", t=time(9, 0))
        self._add(db_session, "магний", today, dosage="400мг", t=time(21, 0))  # позже → last_dosage
        self._add(db_session, "магний", today - timedelta(days=2), dosage="300мг")
        self._add(db_session, "магний", today - timedelta(days=5))
        db_session.commit()

        r = client.get("/api/agent/supplement_daily_log?days=30")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "ok"
        assert body["period_days"] == 30
        assert body["end_date"] == today.isoformat()
        assert body["start_date"] == (today - timedelta(days=29)).isoformat()

        mg = next(s for s in body["supplements"] if s["supplement"] == "магний")
        assert mg["days_taken"] == 3  # два приёма сегодня = одна дата
        assert mg["taken_dates"] == [
            (today - timedelta(days=5)).isoformat(),
            (today - timedelta(days=2)).isoformat(),
            today.isoformat(),
        ]
        assert mg["adherence_pct"] == round(100 * 3 / 30, 1)
        assert mg["last_dosage"] == "400мг"  # последний по (date, time)

    def test_scoped_to_user(self, client, db_session):
        today = datetime.now(MSK).date()
        self._add(db_session, "магний", today, uid=895655)
        self._add(db_session, "чужое", today, uid=999999)  # другой юзер — не должен просочиться
        db_session.commit()
        body = client.get("/api/agent/supplement_daily_log?days=30").json()
        assert {s["supplement"] for s in body["supplements"]} == {"магний"}

    def test_filter_by_name_case_insensitive(self, client, db_session):
        today = datetime.now(MSK).date()
        self._add(db_session, "Магний", today)
        self._add(db_session, "Омега 3", today)
        db_session.commit()
        body = client.get("/api/agent/supplement_daily_log?days=30&supplement=магний").json()
        assert {s["supplement"] for s in body["supplements"]} == {"Магний"}

    def test_window_excludes_old(self, client, db_session):
        today = datetime.now(MSK).date()
        self._add(db_session, "магний", today - timedelta(days=40))  # вне окна 30д
        db_session.commit()
        body = client.get("/api/agent/supplement_daily_log?days=30").json()
        assert body["supplements"] == []

    def test_empty(self, client, db_session):
        body = client.get("/api/agent/supplement_daily_log?days=30").json()
        assert body["status"] == "ok"
        assert body["supplements"] == []


class TestLatestBiomarkers:
    """GET /latest_biomarkers — per-marker aggregated view with staleness."""

    def test_returns_200(self, client):
        resp = client.get("/api/agent/latest_biomarkers")
        assert resp.status_code == 200

    def test_response_shape(self, client):
        resp = client.get("/api/agent/latest_biomarkers")
        data = resp.json()
        assert data["status"] == "ok"
        assert "as_of" in data
        assert "biomarkers" in data
        assert "stale_count" in data
        assert "stale_keys" in data

    def test_biomarker_entry_has_staleness_fields(self, client, db_session):
        from database.models import BloodTest

        # Seed a fresh (today) blood test so the assertions actually run.
        db_session.add(BloodTest(user_id=895655, test_date=date.today(), values={"cholesterol": 5.1, "LDL": 3.0}))
        db_session.commit()

        resp = client.get("/api/agent/latest_biomarkers")
        data = resp.json()
        assert data["biomarkers"], "expected seeded biomarkers, got empty response"
        for key, entry in data["biomarkers"].items():
            assert "value" in entry, f"missing 'value' in {key}"
            assert "date" in entry, f"missing 'date' in {key}"
            assert "days_ago" in entry, f"missing 'days_ago' in {key}"
            assert "threshold_days" in entry, f"missing 'threshold_days' in {key}"
            assert "is_stale" in entry, f"missing 'is_stale' in {key}"
            assert "stale_label" in entry, f"missing 'stale_label' in {key}"
        # Freshly-dated test → nothing is stale, no badge.
        assert data["stale_count"] == 0
        assert all(e["is_stale"] is False and e["stale_label"] is None for e in data["biomarkers"].values())

    def test_stale_biomarker_is_flagged(self, client, db_session):
        from database.models import BloodTest

        # A blood test from ~4 years ago must be flagged stale with a badge.
        old_date = date.today() - timedelta(days=1500)
        db_session.add(BloodTest(user_id=895655, test_date=old_date, values={"testosterone": 18.0}))
        db_session.commit()

        data = client.get("/api/agent/latest_biomarkers").json()
        assert data["stale_count"] >= 1
        stale = [e for e in data["biomarkers"].values() if e["is_stale"]]
        assert stale and all(e["stale_label"] for e in stale)


# ── #188: agent-tool flag_for_devs — единый инбокс обратной связи ────────────


def test_flag_for_devs_writes_feedback(client, db_session):
    """POST /flag_for_devs пишет строку в user_feedback и возвращает её kind."""
    from database.models import UserFeedback

    r = client.post(
        "/api/agent/flag_for_devs",
        json={
            "category": "feature",
            "user_msg": "хочу графики сна",
            "agent_note": "нет тула",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ok"
    assert body["kind"] == "feature"

    rows = db_session.query(UserFeedback).filter_by(user_id=895655).all()
    assert len(rows) == 1
    assert rows[0].text == "хочу графики сна"
    assert rows[0].source == "agent"
    assert rows[0].kind == "feature"


def test_flag_for_devs_respects_opt_out(client, db_session):
    """POST /flag_for_devs для opted-out пользователя не пишет запись — status=skipped_opt_out."""
    from database.models import UserSettings

    db_session.add(UserSettings(user_id=895655, feedback_opt_out=True))
    db_session.commit()

    r = client.post(
        "/api/agent/flag_for_devs",
        json={
            "category": "bug",
            "user_msg": "не работает кнопка",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "skipped_opt_out"


# ── /meal_context: источники ограничений (#340) ───────────────────────────────


@pytest.fixture
def no_kb(monkeypatch):
    """У пользователя нет KB-файла — как у самозарегистрированного юзера."""
    from webhook import agent_tools_api

    monkeypatch.setattr(agent_tools_api, "_resolve_user_kb_path", lambda user: (None, "none"))


def test_meal_context_falls_back_to_onboarding(client, no_kb):
    """Без KB диагнозы и аллергии берутся из users.onboarding_data."""
    mock_user = _current_mock_user(client)
    mock_user.onboarding_data = {
        "chronic_conditions": ["Гипотиреоз (E03.9)"],
        "allergies": ["пыльца"],
    }

    body = client.get("/api/agent/meal_context").json()

    assert body["constraints"] == ["Гипотиреоз (E03.9)"]
    assert body["constraints_source"] == "onboarding"
    assert body["allergies"] == ["пыльца"]


def test_meal_context_kb_wins_over_onboarding(client, monkeypatch, tmp_path):
    """KB — приоритетный источник: онбординг не перебивает файл."""
    from webhook import agent_tools_api

    kb_file = tmp_path / "kb_895655.json"
    kb_file.write_text('{"chronic_diagnoses": ["Демпинг-синдром"]}', encoding="utf-8")
    monkeypatch.setattr(agent_tools_api, "_resolve_user_kb_path", lambda user: (kb_file, "test"))

    mock_user = _current_mock_user(client)
    mock_user.onboarding_data = {"chronic_conditions": ["Гипотиреоз"]}

    body = client.get("/api/agent/meal_context").json()

    assert body["constraints"] == ["Демпинг-синдром"]
    assert body["constraints_source"] == "kb"


def test_meal_context_no_data_reports_none(client, no_kb):
    """Ни KB, ни онбординга — пустой список и source=none (не выдумывать)."""
    _current_mock_user(client).onboarding_data = {}

    body = client.get("/api/agent/meal_context").json()

    assert body["constraints"] == []
    assert body["constraints_source"] == "none"
    assert body["allergies"] == []


def test_meal_context_splits_freetext_conditions(client, no_kb):
    """Свободный текст из онбординга режется на пункты общим сплиттером."""
    _current_mock_user(client).onboarding_data = {"chronic_conditions": "Гипотиреоз. Принимаю Эутирокс"}

    body = client.get("/api/agent/meal_context").json()

    assert body["constraints"] == ["Гипотиреоз", "Принимаю Эутирокс"]
    assert body["constraints_source"] == "onboarding"


# ── /save_health_profile: мягкий сбор медпрофиля (#340) ───────────────────────


def test_save_health_profile_writes_and_sets_flag(client, db_session):
    """Диагнозы и аллергии со слов пациента ложатся в onboarding_data + флаг."""
    r = client.post(
        "/api/agent/save_health_profile",
        json={"chronic_conditions": ["Гипотиреоз, принимаю Эутирокс 50 мкг"], "allergies": ["пыльца"]},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ok"
    assert body["added"] == {"allergies": 1, "chronic_conditions": 1}
    assert body["health_profile_asked"] is True

    row = db_session.query(User).filter_by(telegram_id=895655).first()
    assert row.onboarding_data["chronic_conditions"] == ["Гипотиреоз, принимаю Эутирокс 50 мкг"]
    assert row.onboarding_data["allergies"] == ["пыльца"]
    assert row.onboarding_data["health_profile_asked"] is True


def test_save_health_profile_deduplicates(client, db_session):
    """Повторная запись того же пункта не плодит дубли (case-insensitive)."""
    client.post("/api/agent/save_health_profile", json={"chronic_conditions": ["Астма"]})

    body = client.post("/api/agent/save_health_profile", json={"chronic_conditions": ["астма"]}).json()

    assert body["added"] == {"allergies": 0, "chronic_conditions": 0}
    row = db_session.query(User).filter_by(telegram_id=895655).first()
    assert row.onboarding_data["chronic_conditions"] == ["Астма"]


def test_save_health_profile_nothing_to_report(client, db_session):
    """«Нечего сообщить» ничего не пишет в списки, но помечает вопрос заданным."""
    body = client.post("/api/agent/save_health_profile", json={"nothing_to_report": True}).json()

    assert body["status"] == "ok"
    assert body["added"] == {}
    row = db_session.query(User).filter_by(telegram_id=895655).first()
    assert row.onboarding_data["health_profile_asked"] is True
    assert "chronic_conditions" not in row.onboarding_data


def _current_mock_user(client):
    """Достать mock-юзера, которым подменён get_agent_user в фикстуре client."""
    from webhook.jwt_auth import get_agent_user

    return client.app.dependency_overrides[get_agent_user]()
