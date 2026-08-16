# core/health/doc_extractor.py
"""Извлечение медицинских данных из документов через Anthropic Claude."""

from __future__ import annotations

import base64
import json
import logging
from typing import Any

import httpx

from config.settings import get_settings

logger = logging.getLogger(__name__)

_MODEL = "claude-haiku-4-5-20251001"
_ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
_ANTHROPIC_VERSION = "2023-06-01"

_SYSTEM_PROMPT = """Ты — медицинский парсер. Твоя задача: извлечь структурированные данные из медицинского документа.

Верни ТОЛЬКО валидный JSON объект без markdown-обёртки. Структура:
{
  "date": "ГГГГ-ММ-ДД или null",
  "laboratory": "название лаборатории или null",
  "values": {
    "ключ": числовое_значение,
    ...
  },
  "allergies": ["строка", ...],
  "conditions": ["строка", ...]
}

Правила:
- "values" — только числовые показатели (анализы крови, биохимия, гормоны, витамины, размеры органов в УЗИ и т.д.)
- Используй короткие английские ключи: Hb, WBC, PLT, ferritin, ALT, AST, TSH, vitamin_D и т.д.
- Не включай единицы измерения в значения — только число
- "allergies" — список аллергий/непереносимостей, указанных в документе (аллергены, вещества, продукты). Строки на языке документа. Пусто [] если нет.
- "conditions" — список хронических/персистирующих диагнозов из документа, с кодом МКБ если он есть (например "Бронхиальная астма (J45.0)"). Пусто [] если нет.
- Не придумывай данных, которых нет в документе. Если чего-то нет — пустой список/пустой values."""


async def _call_anthropic(messages: list[dict]) -> dict:
    """Вызов Anthropic Messages API."""
    settings = get_settings()
    api_key = settings.anthropic_api_key
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY не настроен")

    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(
            _ANTHROPIC_API_URL,
            headers={
                "x-api-key": api_key,
                "anthropic-version": _ANTHROPIC_VERSION,
                "content-type": "application/json",
            },
            json={
                "model": _MODEL,
                "max_tokens": 1024,
                "system": _SYSTEM_PROMPT,
                "messages": messages,
            },
        )
        resp.raise_for_status()
        return resp.json()


def _build_image_message(file_bytes: bytes, mime_type: str) -> dict:
    """Собирает Anthropic-сообщение с base64-изображением."""
    b64 = base64.b64encode(file_bytes).decode("utf-8")
    if mime_type == "image/jpg":
        mime_type = "image/jpeg"
    return {
        "role": "user",
        "content": [
            {
                "type": "image",
                "source": {"type": "base64", "media_type": mime_type, "data": b64},
            },
            {"type": "text", "text": "Извлеки медицинские данные из этого документа."},
        ],
    }


def _build_text_message(file_bytes: bytes) -> dict:
    """Собирает Anthropic-сообщение из уже извлечённого текста документа (text-блок).

    Используется для PDF с текстовым слоем: текст извлекается в вызывающем коде и
    приходит сюда байтами. Раньше такой вход ошибочно паковался в image-блок с
    media_type=text/plain, который Anthropic отклоняет (см. #319)."""
    doc_text = file_bytes.decode("utf-8", errors="replace")
    return {
        "role": "user",
        "content": [
            {"type": "text", "text": doc_text},
            {"type": "text", "text": "Извлеки медицинские данные из этого документа."},
        ],
    }


def _build_pdf_message(file_bytes: bytes) -> dict:
    """Собирает Anthropic-сообщение с base64 PDF (document source)."""
    b64 = base64.b64encode(file_bytes).decode("utf-8")
    return {
        "role": "user",
        "content": [
            {
                "type": "document",
                "source": {"type": "base64", "media_type": "application/pdf", "data": b64},
            },
            {"type": "text", "text": "Извлеки медицинские данные из этого документа."},
        ],
    }


def _parse_response(response: dict) -> dict[str, Any]:
    """Парсит ответ Claude в dict. Возвращает {} при любой ошибке."""
    try:
        text = response["content"][0]["text"].strip()
        if text.startswith("```"):
            lines = text.split("\n")
            text = "\n".join(lines[1:-1]) if len(lines) > 2 else text
        data = json.loads(text)
        if not isinstance(data, dict):
            return {}
        return data
    except Exception as e:
        logger.debug("doc_extractor: не удалось распарсить ответ Claude: %s", e)
        return {}


def _as_str_list(v) -> list[str]:
    """Безопасно привести значение к списку непустых строк. Не-список → []."""
    if not isinstance(v, list):
        return []
    return [str(x).strip() for x in v if str(x).strip()]


async def extract_medical_data(file_bytes: bytes, mime_type: str) -> dict[str, Any]:
    """Извлекает медицинские данные из документа через Claude.

    Args:
        file_bytes: байты файла (PDF или изображение)
        mime_type: MIME-тип файла

    Returns:
        dict с ключами date, laboratory, values (или пустой dict если не нашёл)
    """
    try:
        if mime_type == "application/pdf":
            message = _build_pdf_message(file_bytes)
        elif mime_type == "text/plain":
            message = _build_text_message(file_bytes)
        else:
            message = _build_image_message(file_bytes, mime_type)

        response = await _call_anthropic([message])
        data = _parse_response(response)
        if data:
            data["allergies"] = _as_str_list(data.get("allergies"))
            data["conditions"] = _as_str_list(data.get("conditions"))
        return data
    except Exception as e:
        logger.error("doc_extractor: ошибка извлечения: %s", e)
        return {}
