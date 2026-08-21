"""CRUD follower-аккаунтов CGM (#381): шифрование, обновление, отзыв, изоляция владельцев."""

import pytest

from core.infra.secrets import decrypt_secret, is_encrypted
from database.crud import (
    create_cgm_follower,
    create_user,
    list_cgm_followers,
    revoke_cgm_follower,
)

OWNER = 836757955
OTHER = 111

PASSWORD = "follower-pass-123"


@pytest.fixture
def owner(test_db):
    return create_user(db=test_db, telegram_id=OWNER, first_name="Owner")


@pytest.fixture
def other(test_db):
    return create_user(db=test_db, telegram_id=OTHER, first_name="Other")


def test_password_stored_encrypted(test_db, owner):
    f = create_cgm_follower(test_db, OWNER, region="RU", email="a@icloud.com", password=PASSWORD)

    assert f.password_enc != PASSWORD
    assert PASSWORD not in f.password_enc
    assert is_encrypted(f.password_enc)
    assert decrypt_secret(f.password_enc) == PASSWORD


def test_region_uppercased_and_email_lowercased(test_db, owner):
    f = create_cgm_follower(test_db, OWNER, region="ru", email="  MiXeD@Icloud.COM ", password=PASSWORD)

    assert f.region == "RU"
    assert f.email == "mixed@icloud.com"


def test_repeat_same_account_updates_password_not_duplicates(test_db, owner):
    first = create_cgm_follower(test_db, OWNER, region="RU", email="a@x.ru", password="old")
    second = create_cgm_follower(test_db, OWNER, region="RU", email="a@x.ru", password="new")

    assert second.id == first.id  # та же запись, не вторая
    assert decrypt_secret(second.password_enc) == "new"
    assert len(list_cgm_followers(test_db, OWNER)) == 1


def test_repeat_reactivates_revoked(test_db, owner):
    f = create_cgm_follower(test_db, OWNER, region="RU", email="a@x.ru", password="p")
    assert revoke_cgm_follower(test_db, OWNER, f.id) is True

    again = create_cgm_follower(test_db, OWNER, region="RU", email="a@x.ru", password="p2")
    assert again.revoked_at is None
    assert len(list_cgm_followers(test_db, OWNER)) == 1


def test_cannot_take_over_someone_elses_account(test_db, owner, other):
    create_cgm_follower(test_db, OWNER, region="RU", email="shared@x.ru", password="p")

    with pytest.raises(ValueError):
        create_cgm_follower(test_db, OTHER, region="RU", email="shared@x.ru", password="hijack")


def test_same_email_other_region_is_separate_account(test_db, owner):
    create_cgm_follower(test_db, OWNER, region="RU", email="a@x.ru", password="p")
    create_cgm_follower(test_db, OWNER, region="EU", email="a@x.ru", password="p")

    assert len(list_cgm_followers(test_db, OWNER)) == 2


def test_list_excludes_revoked_by_default(test_db, owner):
    f = create_cgm_follower(test_db, OWNER, region="RU", email="a@x.ru", password="p")
    revoke_cgm_follower(test_db, OWNER, f.id)

    assert list_cgm_followers(test_db, OWNER) == []
    assert len(list_cgm_followers(test_db, OWNER, include_revoked=True)) == 1


def test_list_is_scoped_to_owner(test_db, owner, other):
    create_cgm_follower(test_db, OWNER, region="RU", email="mine@x.ru", password="p")

    assert list_cgm_followers(test_db, OTHER) == []


def test_revoke_foreign_follower_returns_false(test_db, owner, other):
    f = create_cgm_follower(test_db, OWNER, region="RU", email="a@x.ru", password="p")

    assert revoke_cgm_follower(test_db, OTHER, f.id) is False
    assert len(list_cgm_followers(test_db, OWNER)) == 1  # чужой отзыв не сработал


def test_revoke_twice_returns_false(test_db, owner):
    f = create_cgm_follower(test_db, OWNER, region="RU", email="a@x.ru", password="p")

    assert revoke_cgm_follower(test_db, OWNER, f.id) is True
    assert revoke_cgm_follower(test_db, OWNER, f.id) is False


def test_unknown_user_rejected(test_db):
    with pytest.raises(ValueError):
        create_cgm_follower(test_db, 999999, region="RU", email="a@x.ru", password="p")


def test_empty_fields_rejected(test_db, owner):
    with pytest.raises(ValueError):
        create_cgm_follower(test_db, OWNER, region="RU", email="", password="p")
    with pytest.raises(ValueError):
        create_cgm_follower(test_db, OWNER, region="RU", email="a@x.ru", password="")


# ── login_ok → last_ok_at (ревью #382) ────────────────────────────────────────


def test_login_ok_sets_last_ok_at(test_db, owner):
    """/connect_cgm проверяет логин живьём — /my_connections не должен потом
    писать «ещё не логинился» у рабочего аккаунта."""
    f = create_cgm_follower(test_db, OWNER, region="RU", email="a@x.ru", password="p", login_ok=True)

    assert f.last_ok_at is not None


def test_without_login_ok_stays_empty(test_db, owner):
    f = create_cgm_follower(test_db, OWNER, region="RU", email="a@x.ru", password="p")

    assert f.last_ok_at is None


def test_login_ok_updates_existing_record(test_db, owner):
    create_cgm_follower(test_db, OWNER, region="RU", email="a@x.ru", password="p")
    again = create_cgm_follower(test_db, OWNER, region="RU", email="a@x.ru", password="p2", login_ok=True)

    assert again.last_ok_at is not None
