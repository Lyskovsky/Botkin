"""Unit-тесты CRUD для Personal Access Token (MCP-коннектор Claude Desktop, #228)."""

import pytest

from database.crud import (
    create_user,
    create_pat,
    get_active_pat_by_token,
    list_pats,
    revoke_pat,
)

OWNER = 895655
OTHER = 111


@pytest.fixture
def owner(test_db):
    return create_user(db=test_db, telegram_id=OWNER, first_name="Owner")


def test_create_pat_default_scope_is_rw(test_db, owner):
    pat = create_pat(test_db, OWNER, name="Мой ноут")

    assert pat.id is not None
    assert pat.token.startswith(f"pat_{OWNER}_")
    assert pat.scope == "rw"
    assert pat.name == "Мой ноут"
    assert pat.user_id == OWNER
    assert pat.created_by_user == OWNER  # self-service по умолчанию
    assert pat.revoked_at is None
    assert pat.is_active is True


def test_create_pat_tokens_are_unique(test_db, owner):
    first = create_pat(test_db, OWNER)
    second = create_pat(test_db, OWNER)

    assert first.token != second.token


def test_create_pat_ro_scope(test_db, owner):
    pat = create_pat(test_db, OWNER, name="Психолог", scope="ro")

    assert pat.scope == "ro"


def test_create_pat_rejects_invalid_scope(test_db, owner):
    with pytest.raises(ValueError):
        create_pat(test_db, OWNER, scope="admin")


def test_create_pat_rejects_unknown_user(test_db):
    with pytest.raises(ValueError):
        create_pat(test_db, OTHER)


def test_create_pat_explicit_created_by(test_db, owner):
    pat = create_pat(test_db, OWNER, created_by=OTHER)

    assert pat.user_id == OWNER
    assert pat.created_by_user == OTHER


def test_get_active_pat_by_token_finds_and_marks_used(test_db, owner):
    pat = create_pat(test_db, OWNER)
    assert pat.last_used_at is None

    found = get_active_pat_by_token(test_db, pat.token)

    assert found is not None
    assert found.id == pat.id
    assert found.last_used_at is not None  # факт использования зафиксирован


def test_get_active_pat_by_token_missing_returns_none(test_db, owner):
    assert get_active_pat_by_token(test_db, "pat_895655_deadbeef") is None


def test_get_active_pat_by_token_empty_returns_none(test_db, owner):
    assert get_active_pat_by_token(test_db, "") is None


def test_revoke_pat_makes_token_inactive(test_db, owner):
    pat = create_pat(test_db, OWNER)

    assert revoke_pat(test_db, OWNER, pat.id) is True
    # отозванный токен больше не находится публичным lookup'ом
    assert get_active_pat_by_token(test_db, pat.token) is None


def test_revoke_pat_is_scoped_to_owner(test_db, owner):
    """Чужой пользователь не может отозвать токен владельца."""
    create_user(db=test_db, telegram_id=OTHER, first_name="Other")
    pat = create_pat(test_db, OWNER)

    assert revoke_pat(test_db, OTHER, pat.id) is False
    assert get_active_pat_by_token(test_db, pat.token) is not None  # всё ещё активен


def test_revoke_pat_already_revoked_returns_false(test_db, owner):
    pat = create_pat(test_db, OWNER)
    assert revoke_pat(test_db, OWNER, pat.id) is True
    assert revoke_pat(test_db, OWNER, pat.id) is False


def test_list_pats_excludes_revoked_by_default(test_db, owner):
    active = create_pat(test_db, OWNER, name="active")
    revoked = create_pat(test_db, OWNER, name="revoked")
    revoke_pat(test_db, OWNER, revoked.id)

    listed = list_pats(test_db, OWNER)

    assert [p.id for p in listed] == [active.id]


def test_list_pats_include_revoked(test_db, owner):
    create_pat(test_db, OWNER)
    revoked = create_pat(test_db, OWNER)
    revoke_pat(test_db, OWNER, revoked.id)

    assert len(list_pats(test_db, OWNER, include_revoked=True)) == 2


def test_list_pats_is_scoped_to_owner(test_db, owner):
    create_user(db=test_db, telegram_id=OTHER, first_name="Other")
    create_pat(test_db, OWNER)
    create_pat(test_db, OTHER)

    assert len(list_pats(test_db, OWNER)) == 1
    assert all(p.user_id == OWNER for p in list_pats(test_db, OWNER))
