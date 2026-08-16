# tests/test_doc_extractor.py
import json
import pytest
from unittest.mock import AsyncMock, patch

from core.health import doc_extractor


@pytest.mark.asyncio
async def test_extract_returns_dict_on_success():
    """Экстрактор возвращает dict с date/laboratory/values при валидном ответе Claude."""
    mock_response = {
        "content": [
            {
                "type": "text",
                "text": json.dumps({"date": "2026-04-13", "laboratory": "KDL", "values": {"Hb": 165, "ferritin": 112}}),
            }
        ]
    }
    with patch("core.health.doc_extractor._call_anthropic", new_callable=AsyncMock) as mock_call:
        mock_call.return_value = mock_response
        from core.health.doc_extractor import extract_medical_data

        result = await extract_medical_data(b"fake-pdf-bytes", "application/pdf")
    assert result["date"] == "2026-04-13"
    assert result["values"]["Hb"] == 165


@pytest.mark.asyncio
async def test_extract_returns_empty_dict_when_no_values_found():
    """Экстрактор возвращает {} если Claude ничего не нашёл."""
    mock_response = {"content": [{"type": "text", "text": "{}"}]}
    with patch("core.health.doc_extractor._call_anthropic", new_callable=AsyncMock) as mock_call:
        mock_call.return_value = mock_response
        from core.health.doc_extractor import extract_medical_data

        result = await extract_medical_data(b"fake-bytes", "image/jpeg")
    assert result == {}


@pytest.mark.asyncio
async def test_extract_handles_malformed_json_gracefully():
    """Если Claude вернул не-JSON — возвращаем {}, не падаем."""
    mock_response = {"content": [{"type": "text", "text": "Я не нашёл ничего в документе."}]}
    with patch("core.health.doc_extractor._call_anthropic", new_callable=AsyncMock) as mock_call:
        mock_call.return_value = mock_response
        from core.health.doc_extractor import extract_medical_data

        result = await extract_medical_data(b"fake-bytes", "image/jpeg")
    assert result == {}


def _fake_response(payload: dict) -> dict:
    return {"content": [{"text": json.dumps(payload, ensure_ascii=False)}]}


@pytest.mark.asyncio
async def test_extract_returns_allergies_and_conditions():
    payload = {
        "date": "2026-04-13",
        "laboratory": None,
        "values": {},
        "allergies": ["пыльца", "кошки"],
        "conditions": ["Бронхиальная астма (J45.0)"],
    }
    with patch.object(doc_extractor, "_call_anthropic", new=AsyncMock(return_value=_fake_response(payload))):
        out = await doc_extractor.extract_medical_data(b"x", "image/png")
    assert out["allergies"] == ["пыльца", "кошки"]
    assert out["conditions"] == ["Бронхиальная астма (J45.0)"]


@pytest.mark.asyncio
async def test_extract_missing_qualitative_defaults_to_empty_lists():
    payload = {"date": None, "laboratory": None, "values": {"Hb": 155}}
    with patch.object(doc_extractor, "_call_anthropic", new=AsyncMock(return_value=_fake_response(payload))):
        out = await doc_extractor.extract_medical_data(b"x", "image/png")
    assert out["allergies"] == []
    assert out["conditions"] == []
    assert out["values"] == {"Hb": 155}


@pytest.mark.asyncio
async def test_extract_coerces_nonlist_qualitative_to_empty():
    payload = {"values": {}, "allergies": "пыльца", "conditions": None}
    with patch.object(doc_extractor, "_call_anthropic", new=AsyncMock(return_value=_fake_response(payload))):
        out = await doc_extractor.extract_medical_data(b"x", "image/png")
    assert out["allergies"] == []
    assert out["conditions"] == []


@pytest.mark.asyncio
async def test_text_plain_builds_text_block_not_image():
    """text/plain (текстовый слой PDF) уходит как text-блок, а не image с битым media_type."""
    doc_text = "Заключение: аллергия на амоксициллин. Гастрит (K29.5)."
    mock = AsyncMock(return_value=_fake_response({"values": {}}))
    with patch.object(doc_extractor, "_call_anthropic", new=mock):
        await doc_extractor.extract_medical_data(doc_text.encode(), "text/plain")

    messages = mock.call_args.args[0]
    content = messages[0]["content"]
    # Есть text-блок с самим текстом документа
    assert any(b.get("type") == "text" and doc_text in b.get("text", "") for b in content)
    # И НЕТ image-блока с невалидным media_type text/plain
    assert not any(b.get("type") == "image" for b in content)


@pytest.mark.asyncio
async def test_text_plain_extracts_values():
    """Путь text/plain доходит до парсинга ответа и возвращает извлечённые данные."""
    payload = {"date": "2026-07-10", "values": {"Hb": 155}, "allergies": ["амоксициллин"], "conditions": []}
    with patch.object(doc_extractor, "_call_anthropic", new=AsyncMock(return_value=_fake_response(payload))):
        out = await doc_extractor.extract_medical_data(b"any text bytes", "text/plain")
    assert out["values"] == {"Hb": 155}
    assert out["allergies"] == ["амоксициллин"]
