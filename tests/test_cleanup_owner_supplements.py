"""Тесты скрипта очистки остаточного owner-протокола добавок (#251)."""

import copy

from database.models import User, UserSettings
from scripts.cleanup_owner_supplement_protocol import (
    OWNER_DEFAULT,
    _is_stamped,
    _looks_similar,
    apply_cleanup,
    collect,
)


def _default():
    """Свежая глубокая копия эталонного owner-дефолта."""
    return copy.deepcopy(OWNER_DEFAULT)


# ─── чистые функции матчинга ────────────────────────────────────────────────


def test_is_stamped_exact_match():
    assert _is_stamped(_default()) is True


def test_is_stamped_reordered_still_matches():
    # Порядок не важен — сравнение как мультимножество.
    items = _default()
    items.reverse()
    assert _is_stamped(items) is True


def test_is_stamped_missing_item_is_false():
    items = _default()[:-1]  # без Whey
    assert _is_stamped(items) is False


def test_is_stamped_extra_item_is_false():
    items = _default()
    items.append({"name": "Цинк", "slot": "evening", "dose": "15 мг"})
    assert _is_stamped(items) is False


def test_is_stamped_different_dose_is_false():
    items = _default()
    items[1] = {**items[1], "dose": "2000 IU"}  # D3 доза изменена
    assert _is_stamped(items) is False


def test_is_stamped_empty_is_false():
    assert _is_stamped([]) is False


def test_is_stamped_none_is_false():
    assert _is_stamped(None) is False


def test_is_stamped_non_dict_items_is_false():
    assert _is_stamped(["Псиллиум", "Витамин D3"]) is False


def test_looks_similar_partial_overlap_true():
    # Непустой список, пересекается с owner-позициями, но не точный матч.
    assert _looks_similar([{"name": "Псиллиум", "slot": "morning_before", "dose": "2 ч.л."}]) is True


def test_looks_similar_unrelated_false():
    assert _looks_similar([{"name": "Цинк", "slot": "evening", "dose": "15 мг"}]) is False


def test_looks_similar_empty_false():
    assert _looks_similar([]) is False


# ─── интеграция с БД (in-memory sqlite через фикстуру test_db) ───────────────


def _seed(db, telegram_id, cohort, supplements):
    db.add(User(telegram_id=telegram_id, cohort=cohort))
    db.add(UserSettings(user_id=telegram_id, supplements=supplements))


def test_collect_flags_nonowner_skips_owner(test_db):
    _seed(test_db, 1, "family", _default())  # засеян, не owner → под очистку
    _seed(test_db, 2, "owner", _default())  # засеян, но owner → пропуск
    _seed(test_db, 3, "external", _default())  # засеян, не owner → под очистку
    _seed(test_db, 4, "family", [{"name": "Цинк", "slot": "evening", "dose": "15 мг"}])  # своё
    _seed(test_db, 5, "family", [])  # пусто
    _seed(test_db, 6, "family", [{"name": "Псиллиум", "slot": "morning_before", "dose": "2 ч.л."}])  # похоже
    test_db.commit()

    report = collect(test_db)

    assert sorted(uid for uid, _ in report["stamped"]) == [1, 3]
    assert report["owner_skipped"] == [2]
    assert [uid for uid, _c, _n in report["similar"]] == [6]
    assert report["total"] == 6


def test_apply_cleanup_clears_only_targets(test_db):
    _seed(test_db, 1, "family", _default())  # цель
    _seed(test_db, 2, "owner", _default())  # не трогать
    _seed(test_db, 4, "family", [{"name": "Цинк", "slot": "evening", "dose": "15 мг"}])  # не трогать
    test_db.commit()

    n = apply_cleanup(test_db, [1])

    assert n == 1
    assert test_db.query(UserSettings).filter_by(user_id=1).one().supplements == []
    # Владелец и юзер со своим списком — без изменений.
    assert _is_stamped(test_db.query(UserSettings).filter_by(user_id=2).one().supplements) is True
    assert test_db.query(UserSettings).filter_by(user_id=4).one().supplements == [
        {"name": "Цинк", "slot": "evening", "dose": "15 мг"}
    ]


def test_apply_cleanup_is_idempotent(test_db):
    _seed(test_db, 1, "family", _default())
    test_db.commit()

    apply_cleanup(test_db, [1])
    # Повторный collect уже не видит очищенную строку.
    report = collect(test_db)
    assert report["stamped"] == []
