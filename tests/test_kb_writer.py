# tests/test_kb_writer.py
import json


def test_append_document_creates_file_if_missing(tmp_path):
    """Если kb файла нет — создаёт его с документом."""
    from core.health.kb_writer import append_document_to_kb

    kb_path = tmp_path / "kb_12345.json"
    entry = {
        "added_at": "2026-06-28",
        "file": "2026-06-28_abc.pdf",
        "extracted": {"date": "2026-04-13", "values": {"Hb": 165}},
        "user_confirmed": True,
    }
    append_document_to_kb(kb_path, entry)

    data = json.loads(kb_path.read_text(encoding="utf-8"))
    assert len(data["documents"]) == 1
    assert data["documents"][0]["file"] == "2026-06-28_abc.pdf"


def test_append_document_adds_to_existing_list(tmp_path):
    """Новый документ добавляется к уже существующим, не перезаписывает."""
    from core.health.kb_writer import append_document_to_kb

    kb_path = tmp_path / "kb_12345.json"
    existing = {"documents": [{"file": "old.pdf", "extracted": {}, "user_confirmed": True}]}
    kb_path.write_text(json.dumps(existing), encoding="utf-8")

    new_entry = {"added_at": "2026-06-28", "file": "new.pdf", "extracted": {}, "user_confirmed": True}
    append_document_to_kb(kb_path, new_entry)

    data = json.loads(kb_path.read_text(encoding="utf-8"))
    assert len(data["documents"]) == 2
    assert data["documents"][1]["file"] == "new.pdf"


def test_append_document_preserves_other_kb_sections(tmp_path):
    """blood_tests и другие секции KB не трогаются."""
    from core.health.kb_writer import append_document_to_kb

    kb_path = tmp_path / "kb_12345.json"
    existing = {
        "blood_tests": [{"date": "2025-01-01", "values": {"Hb": 150}}],
        "documents": [],
    }
    kb_path.write_text(json.dumps(existing), encoding="utf-8")

    append_document_to_kb(kb_path, {"file": "x.pdf", "extracted": {}, "user_confirmed": True})

    data = json.loads(kb_path.read_text(encoding="utf-8"))
    assert len(data["blood_tests"]) == 1
    assert data["blood_tests"][0]["values"]["Hb"] == 150
