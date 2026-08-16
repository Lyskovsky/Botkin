from database.crud import merge_onboarding_lists
from database.models import User


def _add_user(db, tid, onboarding=None):
    db.add(
        User(
            telegram_id=tid,
            first_name="Тест",
            is_active=True,
            cohort="external",
            pack_name="generic",
            onboarding_data=onboarding or {},
        )
    )
    db.commit()


def test_merge_into_empty_creates_keys(test_db):
    _add_user(test_db, 111, {})
    added = merge_onboarding_lists(test_db, 111, {"allergies": ["Пыльца"], "chronic_conditions": ["Астма"]})
    assert added == {"allergies": 1, "chronic_conditions": 1}
    u = test_db.query(User).filter_by(telegram_id=111).one()
    assert u.onboarding_data["allergies"] == ["Пыльца"]
    assert u.onboarding_data["chronic_conditions"] == ["Астма"]


def test_merge_dedups_case_insensitive(test_db):
    _add_user(test_db, 222, {"allergies": ["Пыльца"]})
    added = merge_onboarding_lists(test_db, 222, {"allergies": ["пыльца", "Кошки"], "chronic_conditions": []})
    assert added == {"allergies": 1, "chronic_conditions": 0}
    u = test_db.query(User).filter_by(telegram_id=222).one()
    assert u.onboarding_data["allergies"] == ["Пыльца", "Кошки"]


def test_merge_normalizes_existing_string_to_list(test_db):
    _add_user(test_db, 333, {"allergies": "Пыльца; Кошки"})
    added = merge_onboarding_lists(test_db, 333, {"allergies": ["Собаки"], "chronic_conditions": []})
    assert added == {"allergies": 1, "chronic_conditions": 0}
    u = test_db.query(User).filter_by(telegram_id=333).one()
    assert u.onboarding_data["allergies"] == ["Пыльца", "Кошки", "Собаки"]


def test_merge_writes_to_existing_synonym_key(test_db):
    _add_user(test_db, 444, {"diagnoses": ["Гипертония"]})
    merge_onboarding_lists(test_db, 444, {"allergies": [], "chronic_conditions": ["Астма"]})
    u = test_db.query(User).filter_by(telegram_id=444).one()
    assert u.onboarding_data["diagnoses"] == ["Гипертония", "Астма"]
    assert "chronic_conditions" not in u.onboarding_data


def test_merge_missing_user_returns_empty(test_db):
    assert merge_onboarding_lists(test_db, 999, {"allergies": ["X"], "chronic_conditions": []}) == {}
