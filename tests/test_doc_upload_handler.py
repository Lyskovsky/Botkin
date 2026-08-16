# tests/test_doc_upload_handler.py
import json
import re

import pytest
from unittest.mock import AsyncMock, MagicMock


def _make_message(text=None, document=None, photo=None, from_id=12345, chat_id=12345):
    msg = MagicMock()
    msg.from_user.id = from_id
    msg.chat.id = chat_id
    msg.text = text
    msg.document = document
    msg.photo = photo
    msg.answer = AsyncMock()
    msg.reply = AsyncMock()
    return msg


def test_preview_text_with_values():
    """Превью содержит найденные значения."""
    from handlers.doc_upload import _preview_text

    extracted = {
        "date": "2026-04-13",
        "laboratory": "KDL",
        "values": {"Hb": "165 г/л", "ferritin": "112 нг/мл"},
    }
    text = _preview_text(extracted)
    assert "2026-04-13" in text
    assert "KDL" in text
    assert "Hb" in text
    assert "165" in text


def test_preview_text_empty_extracted():
    """Превью для пустого extracted сообщает что числа не найдены."""
    from handlers.doc_upload import _preview_text

    text = _preview_text({})
    assert "не нашёл" in text.lower() or "не найд" in text.lower() or "архив" in text.lower()


def test_preview_text_values_only_no_date():
    """Если date отсутствует — превью не падает."""
    from handlers.doc_upload import _preview_text

    extracted = {"values": {"ALT": "24 Ед/л"}}
    text = _preview_text(extracted)
    assert "ALT" in text
    assert "24" in text


def test_stored_name_format_and_deterministic():
    """Имя файла: ГГГГ-ММ-ДД_<8hex>.<ext>, детерминировано от содержимого."""
    from handlers.doc_upload import _stored_name

    name1 = _stored_name(b"same-content", ".pdf")
    name2 = _stored_name(b"same-content", ".pdf")
    assert name1 == name2  # детерминировано
    assert name1.endswith(".pdf")
    stem = name1[:-4]
    date_part, hash_part = stem.rsplit("_", 1)
    assert re.match(r"^\d{4}-\d{2}-\d{2}$", date_part)
    assert len(hash_part) == 8


def test_stored_name_different_content():
    """Разное содержимое → разные имена."""
    from handlers.doc_upload import _stored_name

    assert _stored_name(b"aaa", ".pdf") != _stored_name(b"bbb", ".pdf")


def test_append_document_to_kb_creates_file(tmp_path, monkeypatch):
    """Если kb файла нет — создаёт его с документом."""
    import handlers.doc_upload as mod

    monkeypatch.setattr(mod, "_PROJECT_ROOT", tmp_path)
    entry = {
        "added_at": "2026-06-28",
        "file": "2026-06-28_abc12345.pdf",
        "extracted": {"values": {"Hb": "165 г/л"}},
        "user_confirmed": True,
    }
    mod.append_document_to_kb(12345, entry)

    kb_path = tmp_path / "data" / "kb" / "kb_12345.json"
    data = json.loads(kb_path.read_text(encoding="utf-8"))
    assert len(data["documents"]) == 1
    assert data["documents"][0]["file"] == "2026-06-28_abc12345.pdf"


def test_append_document_to_kb_preserves_sections(tmp_path, monkeypatch):
    """blood_tests и другие секции KB не трогаются."""
    import handlers.doc_upload as mod

    monkeypatch.setattr(mod, "_PROJECT_ROOT", tmp_path)
    kb_dir = tmp_path / "data" / "kb"
    kb_dir.mkdir(parents=True)
    (kb_dir / "kb_999.json").write_text(
        json.dumps({"blood_tests": [{"date": "2025-01-01"}], "documents": []}),
        encoding="utf-8",
    )
    mod.append_document_to_kb(
        999,
        {"file": "x.pdf", "extracted": {}, "user_confirmed": True, "added_at": "2026-06-28"},
    )

    data = json.loads((kb_dir / "kb_999.json").read_text(encoding="utf-8"))
    assert len(data["blood_tests"]) == 1
    assert len(data["documents"]) == 1


def test_preview_shows_allergies_new_vs_existing():
    from handlers.doc_upload import _preview_text

    extracted = {"values": {}, "allergies": ["Пыльца", "Кошки"], "conditions": []}
    existing = {"allergies": ["Пыльца"], "chronic_conditions": []}
    text = _preview_text(extracted, existing)
    assert "Кошки" in text
    assert "Пыльца" in text
    assert text.count("🆕") >= 1


def test_preview_conditions_only_is_not_archive():
    from handlers.doc_upload import _preview_text

    extracted = {"values": {}, "allergies": [], "conditions": ["Астма (J45.0)"]}
    text = _preview_text(extracted, {})
    assert "Астма (J45.0)" in text
    assert "не нашёл" not in text.lower()


def test_preview_truly_empty_is_archive():
    from handlers.doc_upload import _preview_text

    text = _preview_text({"values": {}, "allergies": [], "conditions": []}, {})
    assert "не нашёл" in text.lower() or "архив" in text.lower()


@pytest.mark.asyncio
async def test_cmd_doc_sets_fsm_state():
    """Команда /doc переводит в состояние DocUpload.waiting."""
    from handlers.doc_upload import cmd_doc

    msg = _make_message(text="/doc", from_id=12345)
    state = AsyncMock()
    state.set_state = AsyncMock()

    await cmd_doc(msg, state)

    state.set_state.assert_called_once()
    msg.answer.assert_called_once()


def test_read_existing_profile_reads_onboarding(test_db, monkeypatch):
    import handlers.doc_upload as mod
    from database.models import User

    test_db.add(
        User(
            telegram_id=555,
            first_name="Т",
            is_active=True,
            cohort="external",
            pack_name="generic",
            onboarding_data={"allergies": ["Пыльца"]},
        )
    )
    test_db.commit()
    monkeypatch.setattr(mod, "SessionLocal", lambda: test_db)

    prof = mod._read_existing_profile(555)
    assert prof["allergies"] == ["Пыльца"]
    assert prof["chronic_conditions"] == []


@pytest.mark.asyncio
async def test_doc_received_album_asks_to_send_one_by_one():
    """Если пришёл альбом из нескольких файлов — просим прислать по одному,
    не обрабатывая ни один из них (иначе остальные файлы молча теряются)."""
    from handlers.doc_upload import doc_received

    doc1 = MagicMock()
    doc2 = MagicMock()
    msg1 = _make_message(document=doc1)
    msg2 = _make_message(document=doc2)
    state = AsyncMock()

    await doc_received(msg1, state, album=[msg1, msg2])

    msg1.answer.assert_called_once()
    reply_text = msg1.answer.call_args[0][0]
    assert "по одному" in reply_text.lower()
    state.update_data.assert_not_called()


@pytest.mark.asyncio
async def test_doc_confirm_save_merges_into_onboarding(test_db, tmp_path, monkeypatch):
    import handlers.doc_upload as mod
    from database.models import User

    test_db.add(
        User(
            telegram_id=777,
            first_name="Т",
            is_active=True,
            cohort="external",
            pack_name="generic",
            onboarding_data={},
        )
    )
    test_db.commit()
    monkeypatch.setattr(mod, "SessionLocal", lambda: test_db)
    monkeypatch.setattr(mod, "_PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(mod, "_UPLOADS_DIR", tmp_path / "data" / "uploads")

    uploads = tmp_path / "data" / "uploads" / "777"
    uploads.mkdir(parents=True)
    pending = uploads / ".pending_2026-07-14_abcd1234.pdf"
    pending.write_bytes(b"x")

    callback = MagicMock()
    callback.data = "docup_save"
    callback.from_user.id = 777
    callback.message.edit_text = AsyncMock()
    callback.answer = AsyncMock()
    state = AsyncMock()
    state.get_data = AsyncMock(
        return_value={
            "pending": {
                "tmp_path": str(pending),
                "stored_name": "2026-07-14_abcd1234.pdf",
                "extracted": {"values": {}, "allergies": ["Пыльца"], "conditions": ["Астма"]},
            }
        }
    )
    state.update_data = AsyncMock()

    await mod.doc_confirm(callback, state)

    u = test_db.query(User).filter_by(telegram_id=777).one()
    assert u.onboarding_data["allergies"] == ["Пыльца"]
    assert u.onboarding_data["chronic_conditions"] == ["Астма"]
    final_text = callback.message.edit_text.call_args[0][0]
    assert "аллергии" in final_text.lower() and "1" in final_text
