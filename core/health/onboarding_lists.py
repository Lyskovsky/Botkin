"""Единый источник чтения/сплита списков онбординга (аллергии, диагнозы).

Переиспользуется отчётом для врача, merge-CRUD, превью /doc и блоком агента,
чтобы «куда пишем» = «откуда читаем» (один порядок ключей и один сплиттер).
"""

from __future__ import annotations

import re
from typing import Optional

ALLERGY_KEYS: tuple[str, ...] = ("allergies", "food_allergies", "allergy")
CONDITION_KEYS: tuple[str, ...] = ("chronic_conditions", "chronic_diagnoses", "diagnoses", "conditions")

_ITEM_SEP_RE = re.compile(r"[\n;]+|\.\s+|\.$")


def split_freetext(val: str) -> list[str]:
    """Разбить свободный текст (диагнозы/аллергии) на пункты.

    Делит по СИЛЬНЫМ разделителям: конец предложения, перенос строки, «;».
    Запятая разделителем НЕ считается (часть описания одного пункта, #7/#309).
    Точка внутри кода МКБ (J45.0) сохраняется. Если сильных разделителей нет,
    а запятые есть — fallback на split по запятой.
    """
    s = str(val).strip()
    if not s:
        return []
    parts = [p.strip(" .;") for p in _ITEM_SEP_RE.split(s)]
    parts = [p for p in parts if p]
    if len(parts) <= 1 and "," in s:
        parts = [p.strip() for p in s.split(",") if p.strip()]
    return parts


def onboarding_list(onboarding: Optional[dict], keys: tuple[str, ...]) -> list[str]:
    """Достать список значений из онбординга по первому непустому ключу.

    Значение — список (берём как есть) или свободная строка (через split_freetext).
    """
    if not onboarding:
        return []
    for key in keys:
        val = onboarding.get(key)
        if not val:
            continue
        if isinstance(val, list):
            return [str(v).strip() for v in val if str(v).strip()]
        return split_freetext(str(val))
    return []
