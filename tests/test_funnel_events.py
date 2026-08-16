# NOTE: task template originally called `SessionLocal()` from `database` directly,
# but that factory is bound to the real (dummy DATABASE_URL) Postgres engine — every
# other test in this repo patches `database.SessionLocal` to an in-memory SQLite
# session first (see test_dashboard_url_api.py, test_verified_products_autofill.py).
# log_event() takes `db` as a plain parameter, so we just reuse the shared `test_db`
# fixture from tests/conftest.py (in-memory SQLite, tables auto-created from Base
# metadata — FunnelEvent's table included automatically) instead of touching
# database.SessionLocal at all.
from database.models import FunnelEvent, log_event


def test_log_event_writes_row(test_db):
    db = test_db
    log_event(db, user_id=111, event="onboarding_started", track="b2c", source="promoA")
    db.commit()
    rows = db.query(FunnelEvent).filter_by(user_id=111).all()
    assert len(rows) == 1
    assert rows[0].event == "onboarding_started"
    assert rows[0].track == "b2c"
    assert rows[0].source == "promoA"


def test_log_event_once_is_idempotent(test_db):
    db = test_db
    log_event(db, user_id=222, event="first_food_logged", once=True)
    db.commit()
    log_event(db, user_id=222, event="first_food_logged", once=True)
    db.commit()
    rows = db.query(FunnelEvent).filter_by(user_id=222, event="first_food_logged").all()
    assert len(rows) == 1


def test_log_event_meta_roundtrip(test_db):
    db = test_db
    log_event(db, user_id=333, event="goal_computed", meta={"goal_kcal": 1850})
    db.commit()
    row = db.query(FunnelEvent).filter_by(user_id=333).first()
    assert row.meta["goal_kcal"] == 1850


def test_once_duplicate_preserves_sibling_pending_writes(test_db):
    db = test_db
    log_event(db, user_id=444, event="first_food_logged", once=True)
    db.commit()
    # new pending sibling + duplicate once-event in the same uncommitted unit
    log_event(db, user_id=444, event="quiz_completed")  # sibling, pending
    log_event(db, user_id=444, event="first_food_logged", once=True)  # duplicate → must NOT discard sibling
    db.commit()
    assert db.query(FunnelEvent).filter_by(user_id=444, event="quiz_completed").count() == 1
    assert db.query(FunnelEvent).filter_by(user_id=444, event="first_food_logged").count() == 1
