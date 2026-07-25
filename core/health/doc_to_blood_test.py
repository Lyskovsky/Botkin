# core/health/doc_to_blood_test.py
"""Маппер: `extracted` из /doc → строка Postgres `blood_tests` (issue #281).

Зачем: `/doc` клал показатели только в `data/kb/kb_<uid>.json → documents[]`, а
дашборд-биомаркеры, `/phenoage`, `/recent_biomarkers` и `/latest_biomarkers` читают
`blood_tests`. Загруженный через бота анализ был не виден нигде, кроме ручного
`kb_value("documents.0.extracted.values...")`.

Контракт как у `scripts/import/kb_to_blood_tests.py::_extract_rows` — в `values`
уезжают **сырые** ключи, канонизация происходит на чтении через
`core.health.kb_schema.to_canonical` (унификация 01.06.2026). `to_canonical` здесь
работает только гейтом: если ни один ключ не распознан как биомаркер (УЗИ с
размерами органов, заключение врача) — строку в `blood_tests` не пишем вовсе.

Дата не выдумывается: нет `extracted["date"]` — нет строки (`reason="no_date"`).
Вызывающий код показывает пользователю причину, а документ всё равно остаётся
в `documents[]`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Any, Optional

from core.health.kb_schema import UNIT_SYSTEM_KEY, looks_like_us_units, to_canonical

# test_type — VARCHAR(100) (database/models.py::BloodTest).
_TEST_TYPE_MAX = 100
_DEFAULT_LAB = "документ"
_TYPE_SEP = " · "

# Ведущее число значения: «165 г/л» → 165, «3,42 ммоль/л» → 3.42.
_LEADING_NUMBER_RE = re.compile(r"^[+-]?\d+(?:[.,]\d+)?")
# 8 hex из имени файла (_stored_name: «ГГГГ-ММ-ДД_<8hex>.<ext>») — контент-хэш.
_FILE_HASH_RE = re.compile(r"_([0-9a-f]{8})(?:\.[^.]*)?$")


@dataclass(frozen=True)
class DocBloodTestResult:
    """Результат маппинга. `row is None` ⇒ в blood_tests не пишем, смотри `reason`.

    reason: ok | no_values | not_lab | no_date
    """

    row: Optional[dict]
    reason: str
    warnings: tuple[str, ...] = ()
    marker_count: int = 0


def _coerce_number(raw: Any) -> tuple[Optional[float], Optional[str]]:
    """Значение → float. Возвращает (число, причина отказа).

    Экстрактор просят возвращать числа, но на практике приходит и «165 г/л».
    Единицу отбрасываем (каноническая единица задаётся реестром маркеров), а вот
    значение со вторым числом («120/80» — давление, «1.5 x10^9») отвергаем: молча
    взять первое число значило бы придумать данные.
    """
    if isinstance(raw, bool):
        return None, "булево значение"
    if isinstance(raw, (int, float)):
        return float(raw), None
    if not isinstance(raw, str):
        return None, f"тип {type(raw).__name__}"

    text = raw.strip()
    match = _LEADING_NUMBER_RE.match(text)
    if match is None:
        return None, f"не число: {text[:30]!r}"
    tail = text[match.end() :]
    if any(ch.isdigit() for ch in tail):
        return None, f"неоднозначно, несколько чисел: {text[:30]!r}"
    return float(match.group().replace(",", ".")), None


def _coerce_values(values: dict) -> tuple[dict[str, float], list[str]]:
    """Сырые values документа → {ключ: float}. Непарсимые ключи отбрасываем с warning."""
    coerced: dict[str, float] = {}
    warnings: list[str] = []
    for key, raw in values.items():
        number, why = _coerce_number(raw)
        if number is None:
            warnings.append(f"{key}: пропущено ({why})")
            continue
        coerced[str(key)] = number
    return coerced, warnings


def _parse_date(raw: Any) -> Optional[str]:
    """ISO-дата документа. Всё, что не ГГГГ-ММ-ДД, — не дата (не угадываем)."""
    if not isinstance(raw, str):
        return None
    try:
        return date.fromisoformat(raw.strip()).isoformat()
    except ValueError:
        return None


def _file_hash(stored_name: str) -> str:
    """Контент-хэш из имени файла. Он же различает документы за одну дату."""
    match = _FILE_HASH_RE.search(stored_name)
    if match is not None:
        return match.group(1)
    # Имя не по формату _stored_name — берём стабильный хвост, лишь бы не пусто.
    stem = stored_name.rsplit(".", 1)[0]
    return (stem[-8:] or "document").lower()


def _build_test_type(laboratory: Any, file_hash: str) -> str:
    """«Хеликс · a1b2c3d4» — читаемо агенту и уникально по файлу.

    Хэш в ключе upsert'а даёт идемпотентность (перезалив того же файла обновляет
    строку) и разводит два разных документа за одну дату. Длинную лабораторию
    обрезаем — хэш при этом сохраняем целиком.
    """
    lab = " ".join(str(laboratory).split()) if laboratory else ""
    lab = lab or _DEFAULT_LAB
    budget = _TEST_TYPE_MAX - len(_TYPE_SEP) - len(file_hash)
    return f"{lab[:budget]}{_TYPE_SEP}{file_hash}"


def build_blood_test_row(extracted: dict, *, stored_name: str, user_id: int) -> DocBloodTestResult:
    """`extracted` из doc_extractor → строка для upsert в `blood_tests`.

    Args:
        extracted: результат `core.health.doc_extractor.extract_medical_data`
        stored_name: имя сохранённого файла («ГГГГ-ММ-ДД_<8hex>.<ext>»)
        user_id: telegram_id владельца документа
    """
    coerced, warnings = _coerce_values((extracted or {}).get("values") or {})
    if not coerced:
        return DocBloodTestResult(None, "no_values", tuple(warnings))

    canon, canon_warnings = to_canonical(coerced)
    warnings.extend(canon_warnings)
    if not canon:
        # Числа есть, но это не лабораторная панель (УЗИ, заключение) — не наш стол.
        return DocBloodTestResult(None, "not_lab", tuple(warnings))

    test_date = _parse_date(extracted.get("date"))
    if test_date is None:
        return DocBloodTestResult(None, "no_date", tuple(warnings), len(canon))

    values: dict[str, Any] = dict(coerced)
    if looks_like_us_units(values):
        # US-панель (g/dL·mg/dL): признак доедет до to_canonical на чтении (#95, #295).
        values[UNIT_SYSTEM_KEY] = "US"

    row = {
        "user_id": user_id,
        "test_date": test_date,
        "test_type": _build_test_type(extracted.get("laboratory"), _file_hash(stored_name)),
        "values": values,
        "file_path": f"data/uploads/{user_id}/{stored_name}",
        "status": "current",
    }
    return DocBloodTestResult(row, "ok", tuple(warnings), len(canon))
