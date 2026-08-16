import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "telegram-bot"))

import pytest
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

# /api/day определяет «сегодня» в МСК (фикс. UTC+3, см. nutrition_api.py).
# Naive date.today() (UTC) в окне 21:00–24:00 UTC отстаёт на сутки и уводит
# запрос в DB-ветку вместо sync-ветки → флак. Считаем «сегодня» в МСК.
MSK = ZoneInfo("Europe/Moscow")
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database.models import Base
from database.crud import create_nutrition_log


@pytest.fixture
def api_db():
    """Separate in-memory SQLite DB using StaticPool — safe for threaded TestClient."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    db = Session()
    try:
        yield db
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)


@pytest.fixture
def client(api_db, monkeypatch):
    """Build a FastAPI app with only the nutrition router, stub auth + SessionLocal."""
    from fastapi import FastAPI
    from webhook import nutrition_api

    # Patch close() on the session so production code's db.close() is a no-op
    monkeypatch.setattr(api_db, "close", lambda: None)
    monkeypatch.setattr(nutrition_api, "SessionLocal", lambda: api_db)
    app = FastAPI()
    app.include_router(nutrition_api.router)
    from webhook.apple_health import get_tg_user

    app.dependency_overrides[get_tg_user] = lambda: {"id": 895655}
    return TestClient(app)


def test_get_day_empty(client):
    r = client.get("/api/day?date=2026-04-17")
    assert r.status_code == 200
    body = r.json()
    assert body["date"] == "2026-04-17"
    assert body["meals"] == []
    assert body["totals_day"] == {"kcal": 0, "p": 0, "f": 0, "c": 0, "fib": 0}
    # Baseline macro goals must always be present
    assert {"kcal", "protein", "fats", "carbs", "fiber"} <= set(body["goals"].keys())
    # Activity for historical date (not today) comes from DB or None
    assert "activity_today" in body["goals"]


def test_get_day_with_meals(client, api_db):
    create_nutrition_log(
        db=api_db,
        user_id=895655,
        date=date(2026, 4, 17),
        meal_time=time(13, 0),
        meal_name="Обед",
        items=[
            {
                "product": "Курица",
                "weight_g": 100,
                "calories": 165,
                "protein": 31,
                "fats": 3.6,
                "carbs": 0,
                "fiber": 0,
            }
        ],
        totals={"calories": 165, "protein": 31, "fats": 3.6, "carbs": 0, "fiber": 0},
    )
    r = client.get("/api/day?date=2026-04-17")
    assert r.status_code == 200
    body = r.json()
    assert len(body["meals"]) == 1
    meal = body["meals"][0]
    assert meal["slot"] == "lunch"
    assert meal["meal_name"] == "Обед"
    assert meal["meal_time"] == "13:00"
    assert len(meal["items"]) == 1
    item = meal["items"][0]
    assert item == {
        "idx": 0,
        "name": "Курица",
        "weight": 100,
        "kcal": 165,
        "p": 31,
        "f": 3.6,
        "c": 0,
        "fib": 0,
    }
    assert body["totals_day"]["kcal"] == 165


def test_get_day_invalid_date(client):
    r = client.get("/api/day?date=not-a-date")
    assert r.status_code == 400


def test_get_day_user_scoped(client, api_db):
    create_nutrition_log(
        db=api_db,
        user_id=111,
        date=date(2026, 4, 17),
        meal_time=time(13, 0),
        meal_name="Обед",
        items=[
            {
                "product": "Пицца",
                "weight_g": 300,
                "calories": 800,
                "protein": 30,
                "fats": 30,
                "carbs": 90,
                "fiber": 4,
            }
        ],
        totals={"calories": 800, "protein": 30, "fats": 30, "carbs": 90, "fiber": 4},
    )
    r = client.get("/api/day?date=2026-04-17")
    assert r.json()["meals"] == []


def test_post_item_creates_new_meal(client, api_db, monkeypatch):
    def fake_process(description, **kwargs):
        return (
            [
                {
                    "product": description,
                    "weight_g": 180,
                    "calories": 220,
                    "protein": 38,
                    "fats": 6,
                    "carbs": 0,
                    "fiber": 0,
                }
            ],
            {"calories": 220, "protein": 38, "fats": 6, "carbs": 0, "fiber": 0},
        )

    from webhook import nutrition_api

    monkeypatch.setattr(nutrition_api, "process_meal_description", fake_process)

    r = client.post(
        "/api/meal/item",
        json={
            "date": "2026-04-17",
            "slot": "lunch",
            "name": "Курица грудка",
            "weight": 180,
            "source": "manual",
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["item"]["name"] == "Курица грудка"
    assert body["item"]["weight"] == 180
    assert body["item"]["kcal"] == 220
    assert "meal_id" in body

    day = client.get("/api/day?date=2026-04-17").json()
    assert len(day["meals"]) == 1
    assert day["meals"][0]["slot"] == "lunch"
    assert day["meals"][0]["meal_name"] == "Обед"
    assert day["meals"][0]["meal_time"] == "13:00"


def test_post_item_appends_to_existing_slot(client, api_db, monkeypatch):
    from webhook import nutrition_api

    monkeypatch.setattr(
        nutrition_api,
        "process_meal_description",
        lambda desc, **_: (
            [{"product": desc, "weight_g": 100, "calories": 50, "protein": 1, "fats": 0, "carbs": 12, "fiber": 0}],
            {"calories": 50, "protein": 1, "fats": 0, "carbs": 12, "fiber": 0},
        ),
    )
    create_nutrition_log(
        db=api_db,
        user_id=895655,
        date=date(2026, 4, 17),
        meal_time=time(13, 0),
        meal_name="Обед",
        items=[
            {"product": "Рис", "weight_g": 150, "calories": 195, "protein": 4.5, "fats": 1.5, "carbs": 42, "fiber": 2}
        ],
        totals={"calories": 195, "protein": 4.5, "fats": 1.5, "carbs": 42, "fiber": 2},
    )
    r = client.post(
        "/api/meal/item",
        json={
            "date": "2026-04-17",
            "slot": "lunch",
            "name": "Яблоко",
            "weight": 100,
            "source": "manual",
        },
    )
    assert r.status_code == 201
    day = client.get("/api/day?date=2026-04-17").json()
    assert len(day["meals"]) == 1
    assert len(day["meals"][0]["items"]) == 2
    assert day["meals"][0]["totals"]["kcal"] == 245


def test_post_item_bad_slot_400(client):
    r = client.post(
        "/api/meal/item",
        json={
            "date": "2026-04-17",
            "slot": "brunch",
            "name": "x",
            "weight": 100,
            "source": "manual",
        },
    )
    assert r.status_code == 400


def test_patch_item_rescales(client, api_db):
    row = create_nutrition_log(
        db=api_db,
        user_id=895655,
        date=date(2026, 4, 17),
        meal_time=time(13, 0),
        meal_name="Обед",
        items=[
            {"product": "Курица", "weight_g": 100, "calories": 165, "protein": 31, "fats": 3.6, "carbs": 0, "fiber": 0}
        ],
        totals={"calories": 165, "protein": 31, "fats": 3.6, "carbs": 0, "fiber": 0},
    )
    r = client.patch("/api/meal/item", json={"meal_id": row.id, "idx": 0, "weight": 200})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["item"]["weight"] == 200
    assert body["item"]["kcal"] == 330
    assert body["totals"]["kcal"] == 330


def test_patch_item_wrong_user_404(client, api_db):
    row = create_nutrition_log(
        db=api_db,
        user_id=111,
        date=date(2026, 4, 17),
        meal_time=time(13, 0),
        meal_name="Обед",
        items=[{"product": "X", "weight_g": 100, "calories": 100, "protein": 0, "fats": 0, "carbs": 0, "fiber": 0}],
        totals={"calories": 100, "protein": 0, "fats": 0, "carbs": 0, "fiber": 0},
    )
    r = client.patch("/api/meal/item", json={"meal_id": row.id, "idx": 0, "weight": 200})
    assert r.status_code == 404


def test_patch_meal_fields(client, api_db):
    row = create_nutrition_log(
        db=api_db,
        user_id=895655,
        date=date(2026, 4, 17),
        meal_time=time(13, 0),
        meal_name="Обед",
        items=[{"product": "X", "weight_g": 100, "calories": 100, "protein": 1, "fats": 1, "carbs": 1, "fiber": 0}],
        totals={"calories": 100, "protein": 1, "fats": 1, "carbs": 1, "fiber": 0},
    )
    r = client.patch("/api/meal", json={"meal_id": row.id, "meal_name": "Поздний обед", "meal_time": "15:30"})
    assert r.status_code == 200
    day = client.get("/api/day?date=2026-04-17").json()
    assert day["meals"][0]["meal_name"] == "Поздний обед"
    assert day["meals"][0]["meal_time"] == "15:30"
    assert day["meals"][0]["slot"] == "snack"


def test_delete_meal_item(client, api_db):
    row = create_nutrition_log(
        db=api_db,
        user_id=895655,
        date=date(2026, 4, 17),
        meal_time=time(13, 0),
        meal_name="Обед",
        items=[
            {"product": "A", "weight_g": 100, "calories": 100, "protein": 1, "fats": 1, "carbs": 1, "fiber": 0},
            {"product": "B", "weight_g": 100, "calories": 50, "protein": 0, "fats": 0, "carbs": 10, "fiber": 0},
        ],
        totals={"calories": 150, "protein": 1, "fats": 1, "carbs": 11, "fiber": 0},
    )
    r = client.delete(f"/api/meal/item?meal_id={row.id}&idx=0")
    assert r.status_code == 200
    assert r.json()["removed"]["name"] == "A"
    day = client.get("/api/day?date=2026-04-17").json()
    assert len(day["meals"][0]["items"]) == 1


def test_delete_last_item_removes_meal_api(client, api_db):
    row = create_nutrition_log(
        db=api_db,
        user_id=895655,
        date=date(2026, 4, 17),
        meal_time=time(13, 0),
        meal_name="Обед",
        items=[{"product": "A", "weight_g": 100, "calories": 100, "protein": 1, "fats": 1, "carbs": 1, "fiber": 0}],
        totals={"calories": 100, "protein": 1, "fats": 1, "carbs": 1, "fiber": 0},
    )
    r = client.delete(f"/api/meal/item?meal_id={row.id}&idx=0")
    assert r.status_code == 200
    day = client.get("/api/day?date=2026-04-17").json()
    assert day["meals"] == []


def test_delete_meal_whole(client, api_db):
    row = create_nutrition_log(
        db=api_db,
        user_id=895655,
        date=date(2026, 4, 17),
        meal_time=time(13, 0),
        meal_name="Обед",
        items=[{"product": "A", "weight_g": 100, "calories": 100, "protein": 1, "fats": 1, "carbs": 1, "fiber": 0}],
        totals={"calories": 100, "protein": 1, "fats": 1, "carbs": 1, "fiber": 0},
    )
    r = client.delete(f"/api/meal?meal_id={row.id}")
    assert r.status_code == 204
    day = client.get("/api/day?date=2026-04-17").json()
    assert day["meals"] == []


def test_get_favorites(client, api_db):
    from datetime import timedelta

    # get_recent_product_names использует окно [date.today()-90, date.today()].
    # Раньше здесь была захардкоженная date(2026, 4, 17) — она «протухла» (вышла
    # за окно к середине июля 2026) и тест стал падать по времени. Берём today
    # относительно текущей даты, чтобы seed всегда попадал в окно.
    today = date.today()
    create_nutrition_log(
        db=api_db,
        user_id=895655,
        date=today - timedelta(days=1),
        meal_time=time(9, 0),
        meal_name="Завтрак",
        items=[
            {"product": "Кофе", "weight_g": 200, "calories": 10, "protein": 0, "fats": 0, "carbs": 2, "fiber": 0},
            {"product": "Овсянка", "weight_g": 60, "calories": 240, "protein": 8, "fats": 5, "carbs": 42, "fiber": 6},
        ],
        totals={"calories": 250, "protein": 8, "fats": 5, "carbs": 44, "fiber": 6},
    )
    r = client.get("/api/favorites?limit=15")
    assert r.status_code == 200
    body = r.json()
    names = [x["name"] for x in body]
    assert "Кофе" in names
    assert "Овсянка" in names
    for rec in body:
        assert set(rec.keys()) >= {"name", "default_weight", "last_used", "per_100"}
        assert set(rec["per_100"].keys()) == {"kcal", "p", "f", "c", "fib"}


def test_get_favorites_respects_limit(client, api_db):
    r = client.get("/api/favorites?limit=0")
    assert r.status_code == 422


# ── Goals: today's activity vs historical ──────────────────────────────────
# Regression tests for 2026-04-19 fix: banner should show TODAY's actual
# Garmin activity (fresh, via sync_today_garmin), not the 14-day average —
# but for past dates we pull from the DB so old days stay stable.


def test_goals_activity_today_uses_garmin_for_today(client, monkeypatch):
    """For today's date we call sync_today_garmin, not the DB."""

    from webhook import nutrition_api

    calls = {"sync": 0, "db_lookup": 0}

    def fake_sync(user_id, for_date):
        calls["sync"] += 1
        return 487.0, "ok"

    def fake_db(*args, **kwargs):
        calls["db_lookup"] += 1
        return None

    monkeypatch.setattr(nutrition_api, "sync_today_garmin", fake_sync)
    monkeypatch.setattr(nutrition_api, "get_activity_by_date", fake_db)

    today = datetime.now(MSK).date().isoformat()
    r = client.get(f"/api/day?date={today}")
    assert r.status_code == 200
    assert r.json()["goals"]["activity_today"] == 487
    assert calls["sync"] == 1
    assert calls["db_lookup"] == 0


def test_goals_activity_today_uses_db_for_past(client, api_db, monkeypatch):
    """For historical dates we read activity_log from the DB, never Garmin."""
    from webhook import nutrition_api

    class FakeActRow:
        # Code first tries total_calories - bmr_calories; if missing, falls back to active_calories
        total_calories = None
        bmr_calories = None
        active_calories = 312.0

    def should_not_be_called(*args, **kwargs):
        raise AssertionError("sync_today_garmin must not be called for past dates")

    monkeypatch.setattr(nutrition_api, "sync_today_garmin", should_not_be_called)
    monkeypatch.setattr(nutrition_api, "get_activity_by_date", lambda db, uid, d: FakeActRow())

    # Use a date that's definitely in the past
    r = client.get("/api/day?date=2020-01-01")
    assert r.status_code == 200
    assert r.json()["goals"]["activity_today"] == 312


def test_goals_activity_today_null_when_no_data(client, monkeypatch):
    """When Garmin has no data for today, activity_today is null (not 0)."""

    from webhook import nutrition_api

    monkeypatch.setattr(nutrition_api, "sync_today_garmin", lambda uid, d: (None, "no-data"))

    r = client.get(f"/api/day?date={datetime.now(MSK).date().isoformat()}")
    assert r.status_code == 200
    assert r.json()["goals"]["activity_today"] is None


def test_goals_exposes_bmr_and_deficit_when_budget_available(client, monkeypatch):
    """When caloric budget is computed, goals must expose bmr/activity_avg/calorie_goal_pct."""
    # Patch in nutrition_goals where it's actually called from
    from webhook import nutrition_goals

    monkeypatch.setattr(
        nutrition_goals,
        "get_daily_budget",
        lambda user_id, for_date=None: {
            "consumed": 0,
            "target": 2000,
            "remaining": 2000,
            "pct": 0,
            "warn": False,
            "has_garmin": True,
            "bmr_avg": 1650,
            "activity_avg": 350,
            "calorie_goal_pct": -15,
        },
    )

    r = client.get("/api/day?date=2026-04-17")
    assert r.status_code == 200
    g = r.json()["goals"]
    assert g["kcal"] == 2000
    assert g["bmr"] == 1650
    assert g["activity_avg"] == 350
    assert g["calorie_goal_pct"] == -15
    # Macros derived from kcal
    assert g["protein"] == round(2000 * 0.30 / 4)
    assert g["fats"] == round(2000 * 0.30 / 9)
    assert g["carbs"] == round(2000 * 0.40 / 4)
    assert g["fiber"] == 30
