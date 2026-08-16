"""i18n для doctor-report (#300): словарь каркаса (ru/en), разрешение языка,
LLM-перевод свободного текста.

Гибрид: каркас (заголовки/единицы/дисклеймер) — детерминированный словарь;
свободный текст пользователя (диагнозы/аллергии/лекарства) — translate_freetext.
Числа/даты/нормы/флаги ↑↓ здесь не участвуют — они форматируются в doctor_report.
"""

from __future__ import annotations

import json
import logging

import requests

from config import get_settings

logger = logging.getLogger(__name__)

_ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
_ANTHROPIC_VERSION = "2023-06-01"
# Дешёвая модель для механического перевода коротких строк.
_TRANSLATE_MODEL = "claude-haiku-4-5-20251001"
_LANG_NAMES = {"en": "English", "ru": "Russian"}

SUPPORTED_LANGS = ("ru", "en")

# Секции free-text, которые уходят в LLM-перевод (остальные — структурированы).
FREE_TEXT_SECTIONS = ("problems", "allergies", "medications")

CHROME = {
    "ru": {
        "html_lang": "ru",
        "report_title": "Отчёт о здоровье",
        "generated": "Сформирован",
        "period_prefix": "данные на",
        "patient_fallback": "Пациент",
        "self_reported": "(со слов пользователя)",
        "no_data": "Нет данных",
        "sections": {
            "problems": "Проблемы и диагнозы",
            "allergies": "Аллергии и непереносимости",
            "medications": "Лекарства и добавки",
            "results": "Результаты исследований",
            "vitals": "Витальные показатели",
            "social": "Образ жизни",
        },
        "bp_label": "АД",
        "bp_unit": "мм рт.ст.",
        "weight_label": "Вес",
        "weight_unit": "кг",
        "fat_label": "жир",
        "workouts_tpl": "Тренировок за {days} дней: {n}",
        "caption": (
            "📄 Отчёт о здоровье для врача. Сформирован автоматически из ваших данных "
            "в Botkin (wellness-сервис, не диагноз). Можно переслать врачу."
        ),
        "disclaimer": (
            "Отчёт сформирован автоматически из данных, которые пользователь вносил "
            "самостоятельно в приложение Botkin (wellness-сервис): фото/текст еды, "
            "показания устройств, ответы анкеты. Это не медицинский документ и не диагноз, "
            "часть сведений приведена со слов пользователя. Требования 152-ФЗ соблюдаются: "
            "отчёт формируется по запросу самого пользователя."
        ),
        "status_preparing": "⏳ Готовлю PDF-отчёт для врача…",
        "status_done": "✅ Готово — отчёт отправлен файлом выше, можно переслать врачу.",
        "status_failed": "❌ Не удалось сформировать отчёт. Попробуй позже.",
        "lang_hint": "Доступные языки: ru, en. Генерирую на языке по умолчанию.",
    },
    "en": {
        "html_lang": "en",
        "report_title": "Health Report",
        "generated": "Generated",
        "period_prefix": "data as of",
        "patient_fallback": "Patient",
        "self_reported": "(self-reported)",
        "no_data": "No data",
        "sections": {
            "problems": "Problems and diagnoses",
            "allergies": "Allergies and intolerances",
            "medications": "Medications and supplements",
            "results": "Lab results",
            "vitals": "Vital signs",
            "social": "Lifestyle",
        },
        "bp_label": "BP",
        "bp_unit": "mmHg",
        "weight_label": "Weight",
        "weight_unit": "kg",
        "fat_label": "body fat",
        "workouts_tpl": "Workouts in last {days} days: {n}",
        "caption": (
            "📄 Health report for your doctor. Generated automatically from your data "
            "in Botkin (a wellness service, not a diagnosis). You can forward it to a physician."
        ),
        "disclaimer": (
            "This report was generated automatically from data the user entered themselves "
            "into the Botkin app (a wellness service): food photos/text, device readings, "
            "questionnaire answers. It is not a medical document or a diagnosis; some entries "
            "are self-reported. It was produced at the user's own request."
        ),
        "status_preparing": "⏳ Preparing your PDF health report…",
        "status_done": "✅ Done — the report was sent as a file above, you can forward it to your doctor.",
        "status_failed": "❌ Could not build the report. Please try again later.",
        "lang_hint": "Available languages: ru, en. Generating in the default language.",
    },
}


def resolve_report_language(explicit: str | None, tg_language_code: str | None) -> str:
    """Разрешить язык отчёта. Явный валидный выбор перебивает всё; иначе en-код → en; иначе ru."""
    if explicit in SUPPORTED_LANGS:
        return explicit
    if tg_language_code and str(tg_language_code).lower().startswith("en"):
        return "en"
    return "ru"


# Кириллица → латиница по ICAO Doc 9303 (схема загранпаспорта РФ) — официальная,
# детерминированная, без диакритики. Для имени пациента в НЕ-русском отчёте, чтобы
# оно не оставалось кириллицей (#1). Некириллические символы — без изменений.
_RU_TO_LAT = {
    "а": "a",
    "б": "b",
    "в": "v",
    "г": "g",
    "д": "d",
    "е": "e",
    "ё": "e",
    "ж": "zh",
    "з": "z",
    "и": "i",
    "й": "i",
    "к": "k",
    "л": "l",
    "м": "m",
    "н": "n",
    "о": "o",
    "п": "p",
    "р": "r",
    "с": "s",
    "т": "t",
    "у": "u",
    "ф": "f",
    "х": "kh",
    "ц": "ts",
    "ч": "ch",
    "ш": "sh",
    "щ": "shch",
    "ъ": "ie",
    "ы": "y",
    "ь": "",
    "э": "e",
    "ю": "iu",
    "я": "ia",
}


def transliterate_ru_to_latin(text: str) -> str:
    """Транслитерация кириллицы в латиницу (ICAO Doc 9303 / загранпаспорт РФ).

    Пример: «Игорь» → «Igor», «Лысковский» → «Lyskovskii». Регистр первой буквы
    многобуквенного соответствия сохраняется (Ж → Zh). Латиница, цифры, пробелы
    и пунктуация не трогаются — латинское имя пользователя останется как есть.
    """
    out: list[str] = []
    for ch in text:
        lat = _RU_TO_LAT.get(ch.lower())
        if lat is None:
            out.append(ch)  # не кириллица — как есть
        elif ch.isupper():
            out.append(lat.capitalize())
        else:
            out.append(lat)
    return "".join(out)


def translate_freetext(items: list[str], target_lang: str) -> list[str]:
    """Перевести список пользовательских строк на target_lang одним LLM-вызовом.

    Переводятся только НАЗВАНИЯ (диагнозы/аллергии/лекарства/добавки). Числа и
    дозировки сохраняются. При любой ошибке (сеть, парсинг, рассинхрон длины) —
    graceful fallback: возвращаем исходные строки (каркас всё равно на target_lang).
    Для target_lang == "ru" LLM не вызывается.
    """
    if not items or target_lang == "ru":
        return list(items)

    lang_name = _LANG_NAMES.get(target_lang, "English")
    settings = get_settings()
    api_key = getattr(settings, "anthropic_api_key", None)
    if not api_key:
        logger.warning("translate_freetext: ANTHROPIC_API_KEY не задан — fallback на оригинал")
        return list(items)

    system = (
        f"You translate short medical terms (diagnoses, allergies, medications, "
        f"supplements) into {lang_name}. Preserve any numbers and dosages verbatim. "
        f"Return ONLY a JSON array of strings, same length and order as the input. "
        f"No prose, no markdown."
    )
    user = json.dumps(items, ensure_ascii=False)

    try:
        resp = requests.post(
            _ANTHROPIC_API_URL,
            headers={
                "x-api-key": api_key,
                "anthropic-version": _ANTHROPIC_VERSION,
                "content-type": "application/json",
            },
            json={
                "model": _TRANSLATE_MODEL,
                "max_tokens": 1024,
                "system": system,
                "messages": [{"role": "user", "content": user}],
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        text_out = "".join(
            block.get("text", "") for block in data.get("content", []) if block.get("type") == "text"
        ).strip()
        # Снять возможные ```json ... ``` обёртки.
        if text_out.startswith("```"):
            text_out = text_out.strip("`")
            text_out = text_out[text_out.find("[") :]
        parsed = json.loads(text_out)
        if isinstance(parsed, list) and len(parsed) == len(items):
            return [str(x) for x in parsed]
        logger.warning("translate_freetext: длина ответа != входа — fallback")
        return list(items)
    except Exception as e:  # noqa: BLE001 — любой сбой = fallback на оригинал
        logger.warning("translate_freetext failed (%s) — fallback на оригинал", e)
        return list(items)
