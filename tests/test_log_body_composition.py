"""Тесты POST /log_body_composition — запись состава тела с внешних весов.

Зачем эндпоинт: полный состав тела (мышцы/вода/кости/висцеральный жир) не приходит
через Apple Health — в HealthKit нет таких типов. Исторически вес с весов лился с
мака владельца через ssh + psql суперюзером (`scripts/import/zepp_csv.py`), что
требует доступа к прод-серверу. Этот эндпоинт даёт тот же результат по HTTPS с
PAT-токеном: RLS изолирует пользователя, доступ к серверу не нужен.

Ключ апсерта — точный (user_id, measured_at): Withings это device-источник, а по
#170 device-синки с реальными intraday-таймстампами НЕ дедупятся по календарному
дню (в отличие от ручного ввода через upsert_manual_weight).

Тесты бьют по РЕАЛЬНОЙ in-memory SQLite (не мокают db.execute), поэтому проверяется
именно состояние таблицы weights, а не факт вызова.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "telegram-bot"))

from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database.models import Base, User, Weight

OWNER = 895655
OTHER_USER = 111222333


@pytest.fixture
def db_session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    # autoflush=False — ровно как прод-сессия (database/__init__.py). С дефолтным
    # True тесты не увидели бы, что в батче с двумя одинаковыми measured_at
    # SELECT не находит только что добавленную строку и commit падает по UNIQUE.
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    session = Session()
    session.add(User(telegram_id=OWNER, first_name="Sasha", jwt_secret="s", is_active=True))
    session.add(User(telegram_id=OTHER_USER, first_name="Andrey", jwt_secret="s", is_active=True))
    session.commit()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)


def _mock_user(telegram_id=OWNER):
    u = MagicMock()
    u.telegram_id = telegram_id
    u.timezone = "Europe/Moscow"
    u.onboarding_data = {}
    return u


@pytest.fixture
def client(db_session, monkeypatch):
    from webhook import agent_tools_api
    from webhook.jwt_auth import get_agent_user, get_db

    monkeypatch.setattr(db_session, "close", lambda: None)
    monkeypatch.setattr(agent_tools_api, "get_db", lambda: iter([db_session]))

    app = FastAPI()
    app.include_router(agent_tools_api.router)
    app.dependency_overrides[get_agent_user] = lambda: _mock_user()
    app.dependency_overrides[get_db] = lambda: db_session
    return TestClient(app)


def _rows(db):
    db.expire_all()
    return db.query(Weight).order_by(Weight.measured_at).all()


# ── Happy path ────────────────────────────────────────────────────────────────


def test_single_measurement_written_with_all_fields(client, db_session):
    """Полный состав тела уезжает в weights целиком, включая поля которых нет в HealthKit."""
    r = client.post(
        "/api/agent/log_body_composition",
        json={
            "source": "withings",
            "measurements": [
                {
                    "measured_at": "2026-08-01T07:15:00+00:00",
                    "weight": 82.4,
                    "body_fat": 21.3,
                    "muscle_mass": 60.1,
                    "water": 55.2,
                    "bone_mass": 3.4,
                    "visceral_fat": 9.0,
                    "bmi": 26.1,
                }
            ],
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ok"
    assert body["inserted"] == 1
    assert body["updated"] == 0

    rows = _rows(db_session)
    assert len(rows) == 1
    w = rows[0]
    assert w.user_id == OWNER
    assert w.weight == 82.4
    assert w.body_fat == 21.3
    assert w.muscle_mass == 60.1
    assert w.water == 55.2
    assert w.bone_mass == 3.4
    assert w.bmi == 26.1
    assert w.source == "withings"


def test_batch_of_measurements_all_written(client, db_session):
    """Историю за много дней можно залить одним запросом, а не по замеру за раз."""
    measurements = [
        {"measured_at": f"2026-07-{day:02d}T07:00:00+00:00", "weight": 80.0 + day * 0.1} for day in range(1, 11)
    ]
    r = client.post(
        "/api/agent/log_body_composition",
        json={"source": "withings", "measurements": measurements},
    )
    assert r.status_code == 200, r.text
    assert r.json()["inserted"] == 10
    assert len(_rows(db_session)) == 10


def test_visceral_fat_float_rounded_to_int(client, db_session):
    """Колонка visceral_fat — INTEGER, а Withings отдаёт float. Округляем, не падаем."""
    r = client.post(
        "/api/agent/log_body_composition",
        json={"measurements": [{"measured_at": "2026-08-01T07:00:00+00:00", "weight": 82.0, "visceral_fat": 8.6}]},
    )
    assert r.status_code == 200, r.text
    assert _rows(db_session)[0].visceral_fat == 9


def test_source_defaults_when_not_given(client, db_session):
    """Без явного source запись всё равно атрибутирована — молчаливый NULL хуже."""
    r = client.post(
        "/api/agent/log_body_composition",
        json={"measurements": [{"measured_at": "2026-08-01T07:00:00+00:00", "weight": 82.0}]},
    )
    assert r.status_code == 200, r.text
    assert _rows(db_session)[0].source == "agent_api"


# ── Upsert / COALESCE ────────────────────────────────────────────────────────


def test_repost_same_timestamp_updates_not_duplicates(client, db_session):
    """Повторный прогон импорта не плодит дубли — ключ (user_id, measured_at)."""
    payload = {
        "source": "withings",
        "measurements": [{"measured_at": "2026-08-01T07:00:00+00:00", "weight": 82.0, "body_fat": 21.0}],
    }
    first = client.post("/api/agent/log_body_composition", json=payload)
    assert first.status_code == 200, first.text
    assert first.json()["inserted"] == 1

    payload["measurements"][0]["weight"] = 81.5
    second = client.post("/api/agent/log_body_composition", json=payload)
    assert second.status_code == 200, second.text
    assert second.json()["inserted"] == 0
    assert second.json()["updated"] == 1

    rows = _rows(db_session)
    assert len(rows) == 1
    assert rows[0].weight == 81.5


def test_missing_field_does_not_wipe_existing_value(client, db_session):
    """COALESCE-семантика: канал с частичными данными не затирает уже известное.

    Прецедент в CLAUDE.md: HAE приносит только вес/жир/безжировую массу, Withings —
    полный состав. Апсерт одного канала не должен обнулять поля другого.
    """
    client.post(
        "/api/agent/log_body_composition",
        json={
            "source": "withings",
            "measurements": [
                {
                    "measured_at": "2026-08-01T07:00:00+00:00",
                    "weight": 82.0,
                    "muscle_mass": 60.0,
                    "bone_mass": 3.4,
                }
            ],
        },
    )
    # Второй канал знает только вес — muscle_mass/bone_mass приходят пустыми
    r = client.post(
        "/api/agent/log_body_composition",
        json={
            "source": "hae",
            "measurements": [{"measured_at": "2026-08-01T07:00:00+00:00", "weight": 82.2}],
        },
    )
    assert r.status_code == 200, r.text

    w = _rows(db_session)[0]
    assert w.weight == 82.2, "новый вес должен примениться"
    assert w.muscle_mass == 60.0, "мышечная масса не должна обнулиться"
    assert w.bone_mass == 3.4, "костная масса не должна обнулиться"


# ── Изоляция пользователя ────────────────────────────────────────────────────


def test_writes_go_to_authenticated_user_only(client, db_session):
    """user_id берётся из токена. Чужой user_id в теле не должен ничего менять."""
    r = client.post(
        "/api/agent/log_body_composition",
        json={"measurements": [{"measured_at": "2026-08-01T07:00:00+00:00", "weight": 82.0, "user_id": OTHER_USER}]},
    )
    # Лишнее поле Pydantic молча игнорирует → ожидаем именно 200, а не «200 или 422»
    assert r.status_code == 200, r.text
    assert all(w.user_id == OWNER for w in _rows(db_session))
    assert not db_session.query(Weight).filter(Weight.user_id == OTHER_USER).all()


def test_ro_token_forbidden(db_session, monkeypatch):
    """ro-токен (которым делятся с врачом) не должен уметь писать вес."""
    from webhook import agent_tools_api
    from webhook.jwt_auth import get_agent_user, get_db

    monkeypatch.setattr(db_session, "close", lambda: None)
    monkeypatch.setattr(agent_tools_api, "get_db", lambda: iter([db_session]))

    app = FastAPI()
    app.include_router(agent_tools_api.router)

    # Аннотация Request обязательна: без неё FastAPI считает параметр query-полем
    async def _ro_user(request: Request):
        request.state.agent_scope = "ro"
        return _mock_user()

    app.dependency_overrides[get_agent_user] = _ro_user
    app.dependency_overrides[get_db] = lambda: db_session

    r = TestClient(app).post(
        "/api/agent/log_body_composition",
        json={"measurements": [{"measured_at": "2026-08-01T07:00:00+00:00", "weight": 82.0}]},
    )
    assert r.status_code == 403, r.text
    assert not _rows(db_session)


# ── Валидация входа ──────────────────────────────────────────────────────────


def test_bad_measured_at_returns_400_and_writes_nothing(client, db_session):
    """Невалидная дата — явная ошибка, а не молча пропущенный замер."""
    r = client.post(
        "/api/agent/log_body_composition",
        json={"measurements": [{"measured_at": "01.08.2026", "weight": 82.0}]},
    )
    assert r.status_code in (400, 422), r.text
    assert not _rows(db_session)


def test_absurd_weight_rejected(client, db_session):
    """Вес 900 кг — почти наверняка ошибка единиц измерения (фунты/граммы)."""
    r = client.post(
        "/api/agent/log_body_composition",
        json={"measurements": [{"measured_at": "2026-08-01T07:00:00+00:00", "weight": 900.0}]},
    )
    assert r.status_code == 422, r.text
    assert not _rows(db_session)


def test_empty_measurements_rejected(client, db_session):
    """Пустой батч — ошибка клиента, а не «успешно записали ничего»."""
    r = client.post("/api/agent/log_body_composition", json={"measurements": []})
    assert r.status_code == 422, r.text


def test_partial_batch_is_atomic(client, db_session):
    """Если один замер в батче битый — не должно остаться половины записанного."""
    r = client.post(
        "/api/agent/log_body_composition",
        json={
            "measurements": [
                {"measured_at": "2026-08-01T07:00:00+00:00", "weight": 82.0},
                {"measured_at": "не-дата", "weight": 82.5},
            ]
        },
    )
    assert r.status_code in (400, 422), r.text
    assert not _rows(db_session), "битый батч не должен записываться частично"


def test_naive_timestamp_rejected(client, db_session):
    """Время без офсета — отказ, а не догадки.

    Ключ идемпотентности — measured_at. Naive-строку Postgres трактует по session
    TimeZone (нигде не зафиксирован), поэтому один момент, присланный то с офсетом
    то без, дал бы два ряда вместо одного. Лучше явная ошибка, чем тихий дубль.
    """
    r = client.post(
        "/api/agent/log_body_composition",
        json={"measurements": [{"measured_at": "2026-08-01T07:00:00", "weight": 82.0}]},
    )
    assert r.status_code == 400, r.text
    assert "часовой пояс" in r.text or "офсет" in r.text
    assert not _rows(db_session)


def test_same_instant_different_offsets_is_one_row(client, db_session):
    """10:00+03:00 и 07:00Z — один момент, значит одна строка, а не две.

    Регрессия: без нормализации в UTC клиент, непоследовательный в сериализации
    (например перешёл с локального времени на Z), задваивал бы историю.
    """
    client.post(
        "/api/agent/log_body_composition",
        json={"source": "withings", "measurements": [{"measured_at": "2026-08-01T07:00:00+00:00", "weight": 82.0}]},
    )
    r = client.post(
        "/api/agent/log_body_composition",
        json={"source": "withings", "measurements": [{"measured_at": "2026-08-01T10:00:00+03:00", "weight": 81.7}]},
    )
    assert r.status_code == 200, r.text
    assert r.json()["updated"] == 1, "тот же момент должен обновить, а не вставить"

    rows = _rows(db_session)
    assert len(rows) == 1, f"один момент — одна строка, получили {len(rows)}"
    assert rows[0].weight == 81.7


def test_duplicate_timestamp_within_one_batch_does_not_crash(client, db_session):
    """Два замера с одним measured_at в ОДНОМ батче — последний выигрывает, без 500.

    Прод-сессия работает с autoflush=False, поэтому без flush после db.add()
    SELECT не видел только что добавленную строку и commit падал по
    UNIQUE(user_id, measured_at) — сырым 500 вместо внятного ответа.
    """
    r = client.post(
        "/api/agent/log_body_composition",
        json={
            "source": "withings",
            "measurements": [
                {"measured_at": "2026-08-01T07:00:00+00:00", "weight": 82.0},
                {"measured_at": "2026-08-01T07:00:00+00:00", "weight": 81.4},
            ],
        },
    )
    assert r.status_code == 200, r.text

    rows = _rows(db_session)
    assert len(rows) == 1, f"ожидали одну строку, получили {len(rows)}"
    assert rows[0].weight == 81.4, "должен примениться последний замер батча"


def test_manual_source_rejected(client, db_session):
    """source='manual'/'llm_text' зарезервирован за ручным вводом (#170).

    Ручной апсерт дедупит по календарному дню — device-замер под таким source
    он позже подменил бы, потеряв реальный замер с весов.
    """
    for source in ("manual", "llm_text"):
        r = client.post(
            "/api/agent/log_body_composition",
            json={
                "source": source,
                "measurements": [{"measured_at": "2026-08-01T07:00:00+00:00", "weight": 82.0}],
            },
        )
        assert r.status_code == 422, f"{source}: {r.text}"
    assert not _rows(db_session)


@pytest.mark.parametrize("measured_at", ["1990-05-01T07:00:00+00:00", "2099-01-01T07:00:00+00:00"])
def test_implausible_date_rejected(client, db_session, measured_at):
    """Замер из прошлого века или далёкого будущего молча перекосил бы агрегаты."""
    r = client.post(
        "/api/agent/log_body_composition",
        json={"measurements": [{"measured_at": measured_at, "weight": 82.0}]},
    )
    assert r.status_code == 422, r.text
    assert not _rows(db_session)
