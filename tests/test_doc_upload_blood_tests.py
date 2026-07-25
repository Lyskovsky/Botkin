"""/doc пишет лабораторные показатели в Postgres blood_tests (#281).

Дашборд, /phenoage и агент читают blood_tests, а не documents[] — до этой правки
загруженный через бота анализ не был виден нигде. Проверяем сам факт записи,
идемпотентность и то, что падение БД не выглядит как несохранённый документ.
"""

from contextlib import contextmanager
from unittest.mock import patch

from database.crud import get_all_blood_tests, upsert_blood_test

USER_ID = 4242
STORED = "2026-07-25_a1b2c3d4.pdf"
LAB_DOC = {"date": "2026-04-13", "laboratory": "KDL", "values": {"Hb": 148, "ferritin": 95}}


@contextmanager
def _handler_db(test_db):
    """doc_upload открывает свою сессию через SessionLocal — подменяем на тестовую."""
    with patch("handlers.doc_upload.SessionLocal", return_value=test_db):
        yield


def _save(test_db, extracted, stored_name=STORED):
    from handlers.doc_upload import _save_to_blood_tests

    with _handler_db(test_db):
        return _save_to_blood_tests(USER_ID, extracted, stored_name)


# ── crud.upsert_blood_test ───────────────────────────────────────────────────


def test_upsert_creates_then_updates(test_db):
    row = {
        "user_id": USER_ID,
        "test_date": "2026-04-13",
        "test_type": "KDL · a1b2c3d4",
        "values": {"Hb": 148.0},
        "file_path": "data/uploads/4242/2026-07-25_a1b2c3d4.pdf",
        "status": "current",
    }
    assert upsert_blood_test(test_db, row) is True
    assert upsert_blood_test(test_db, {**row, "values": {"Hb": 150.0}}) is False

    rows = get_all_blood_tests(test_db, USER_ID)
    assert len(rows) == 1
    assert rows[0].values == {"Hb": 150.0}


# ── /doc → blood_tests ───────────────────────────────────────────────────────


def test_lab_document_lands_in_blood_tests(test_db):
    note = _save(test_db, LAB_DOC)

    rows = get_all_blood_tests(test_db, USER_ID)
    assert len(rows) == 1
    assert rows[0].test_date.isoformat() == "2026-04-13"
    assert rows[0].values == {"Hb": 148.0, "ferritin": 95.0}
    assert "2026-04-13" in note


def test_reupload_of_same_file_does_not_duplicate(test_db):
    _save(test_db, LAB_DOC)
    note = _save(test_db, LAB_DOC)

    assert len(get_all_blood_tests(test_db, USER_ID)) == 1
    assert "Обновил" in note


def test_two_documents_same_date_are_separate_rows(test_db):
    _save(test_db, LAB_DOC, stored_name="2026-07-25_aaaaaaaa.pdf")
    _save(test_db, LAB_DOC, stored_name="2026-07-25_bbbbbbbb.pdf")

    assert len(get_all_blood_tests(test_db, USER_ID)) == 2


def test_document_without_date_is_not_written(test_db):
    note = _save(test_db, {"laboratory": "KDL", "values": {"Hb": 148}})

    assert get_all_blood_tests(test_db, USER_ID) == []
    assert "Дату" in note


def test_ultrasound_is_not_written(test_db):
    note = _save(test_db, {"date": "2026-04-19", "values": {"liver_right_lobe_mm": 145}})

    assert get_all_blood_tests(test_db, USER_ID) == []
    assert "Лабораторных показателей не нашёл" in note


def test_db_failure_is_reported_not_raised(test_db):
    """Документ уже в KB — упасть здесь нельзя, можно только честно сказать."""
    from handlers.doc_upload import _save_to_blood_tests

    with (
        _handler_db(test_db),
        patch("database.crud.upsert_blood_test", side_effect=RuntimeError("db down")),
    ):
        note = _save_to_blood_tests(USER_ID, LAB_DOC, STORED)

    assert "не попали в динамику" in note
