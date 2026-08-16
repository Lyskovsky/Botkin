"""Тесты справочника проверенных продуктов (#255): нормализация + CRUD."""

from core.food.verified_products import normalize_product_name
from database.crud import (
    find_verified_product,
    get_verified_products,
    increment_verified_product_usage,
    upsert_verified_product,
)
from database.models import User

USER_ID = 111
OTHER_USER_ID = 222


def _make_user(db, telegram_id):
    user = User(telegram_id=telegram_id, first_name=f"user{telegram_id}")
    db.add(user)
    db.commit()
    return user


def _solvie_kwargs(**overrides):
    kwargs = dict(
        name="Solvie Protein Barre",
        name_norm=normalize_product_name("Solvie Protein Barre"),
        calories_per_100g=288.0,
        protein_per_100g=33.4,
        fats_per_100g=13.2,
        carbs_per_100g=9.0,
        fiber_per_100g=22.8,
        portion_g=50.0,
        brand="Solvie",
        barcode="4673728135932",
        source="manual",
    )
    kwargs.update(overrides)
    return kwargs


# ── normalize_product_name ───────────────────────────────────────────────────


def test_normalize_lowercases_and_collapses_separators():
    assert normalize_product_name("Solvie  Protein-Barre") == "solvie protein barre"
    assert normalize_product_name("solvie protein barre") == "solvie protein barre"


def test_normalize_yo_and_punctuation():
    assert normalize_product_name("Творог «Савушкин», 5%") == normalize_product_name("творог савушкин 5")


def test_normalize_empty_and_none_like():
    assert normalize_product_name("") == ""
    assert normalize_product_name("   ") == ""


# ── upsert ───────────────────────────────────────────────────────────────────


def test_upsert_creates_product(test_db):
    _make_user(test_db, USER_ID)
    p = upsert_verified_product(test_db, user_id=USER_ID, **_solvie_kwargs())
    assert p.id is not None
    assert p.name_norm == "solvie protein barre"
    assert p.times_used == 0


def test_upsert_updates_existing_without_duplicate(test_db):
    _make_user(test_db, USER_ID)
    upsert_verified_product(test_db, user_id=USER_ID, **_solvie_kwargs())
    p2 = upsert_verified_product(test_db, user_id=USER_ID, **_solvie_kwargs(calories_per_100g=300.0))
    rows = get_verified_products(test_db, user_id=USER_ID)
    assert len(rows) == 1
    assert p2.calories_per_100g == 300.0


def test_upsert_global_scope_is_separate_from_user(test_db):
    _make_user(test_db, USER_ID)
    upsert_verified_product(test_db, user_id=USER_ID, **_solvie_kwargs())
    upsert_verified_product(test_db, user_id=None, **_solvie_kwargs(calories_per_100g=290.0))
    rows = get_verified_products(test_db, user_id=USER_ID)
    assert len(rows) == 2


# ── find: приоритет личной записи над общей ──────────────────────────────────


def test_find_prefers_user_record_over_global(test_db):
    _make_user(test_db, USER_ID)
    upsert_verified_product(test_db, user_id=None, **_solvie_kwargs(calories_per_100g=290.0))
    upsert_verified_product(test_db, user_id=USER_ID, **_solvie_kwargs(calories_per_100g=288.0))

    found = find_verified_product(test_db, USER_ID, "solvie protein barre")
    assert found is not None
    assert found.user_id == USER_ID
    assert found.calories_per_100g == 288.0


def test_find_falls_back_to_global(test_db):
    _make_user(test_db, USER_ID)
    upsert_verified_product(test_db, user_id=None, **_solvie_kwargs())
    found = find_verified_product(test_db, USER_ID, "solvie protein barre")
    assert found is not None
    assert found.user_id is None


def test_find_does_not_see_other_users_records(test_db):
    _make_user(test_db, USER_ID)
    _make_user(test_db, OTHER_USER_ID)
    upsert_verified_product(test_db, user_id=OTHER_USER_ID, **_solvie_kwargs())
    assert find_verified_product(test_db, USER_ID, "solvie protein barre") is None


def test_find_unknown_returns_none(test_db):
    _make_user(test_db, USER_ID)
    assert find_verified_product(test_db, USER_ID, "неизвестный продукт") is None


# ── листинг и usage ──────────────────────────────────────────────────────────


def test_get_verified_products_orders_personal_first_then_by_usage(test_db):
    _make_user(test_db, USER_ID)
    upsert_verified_product(test_db, user_id=None, **_solvie_kwargs(name="Global Bar", name_norm="global bar"))
    low = upsert_verified_product(test_db, user_id=USER_ID, **_solvie_kwargs(name="Rare Bar", name_norm="rare bar"))
    top = upsert_verified_product(test_db, user_id=USER_ID, **_solvie_kwargs(name="Fav Bar", name_norm="fav bar"))
    for _ in range(3):
        increment_verified_product_usage(test_db, top.id)
    increment_verified_product_usage(test_db, low.id)

    rows = get_verified_products(test_db, user_id=USER_ID)
    assert [r.name for r in rows] == ["Fav Bar", "Rare Bar", "Global Bar"]


def test_increment_usage(test_db):
    _make_user(test_db, USER_ID)
    p = upsert_verified_product(test_db, user_id=USER_ID, **_solvie_kwargs())
    increment_verified_product_usage(test_db, p.id)
    test_db.refresh(p)
    assert p.times_used == 1
