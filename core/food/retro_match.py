"""Ретро-ре-матч КБЖУ исторических записей питания по verified_products (#257).

Детерминированное ядро: берёт items записи ``nutrition_log`` и справочник
проверенных продуктов (#255), матчит **по имени** (та же нормализация, что и в
онлайн-пути — ``core.food.verified_products``), и для совпавших пересчитывает
КБЖУ из этикеточных ``*_per_100g`` по весу. Возвращает ПРЕДЛОЖЕНИЕ правки
(новые items + пересчитанные totals + список изменений), НЕ мутируя вход и НЕ
трогая БД — запись делает вызывающий скрипт под ``--apply``.

Отличия от онлайн ``match_and_apply_verified_products``:
- работает на копии (исторические записи неизменны до явного apply);
- НЕ инкрементит ``times_used`` (ретро-проход ≠ новое использование продукта,
  иначе исказит ранжирование топ-N в промпт-блоке);
- порог значимости (``epsilon``) — не «чинит» дребезг округления → идемпотентность.

LLM-vision повторное распознавание фото и сиротский backfill photo_paths —
вне ядра (вынесены в follow-up, см. issue #257).
"""

import copy
from dataclasses import dataclass
from typing import List, Optional

from core.food.fiber_table import _item_name
from core.food.nutrition import calculate_meal_totals
from core.food.verified_products import _build_lookup, apply_verified, normalize_product_name

#: Минимальная разница по нутриенту, чтобы считать это реальной правкой (не округление).
DEFAULT_EPSILON = 0.5

#: Нутриенты, по которым сравниваем старое/новое значение item.
_TRACKED_FIELDS = ("calories", "protein", "fats", "carbs", "fiber")


@dataclass
class FieldChange:
    """Одно изменение нутриента у item'а: было → стало."""

    item_name: str
    field: str
    old: float
    new: float


@dataclass
class RetroFix:
    """Предложенная правка одной записи nutrition_log."""

    new_items: List[dict]
    new_totals: dict
    changes: List[FieldChange]
    matched_count: int  # сколько items сматчилось со справочником


def _num(value) -> float:
    """None/пусто → 0.0; иначе float."""
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def plan_record_fix(items: list, products: list, *, epsilon: float = DEFAULT_EPSILON) -> Optional[RetroFix]:
    """Посчитать предложение правки КБЖУ для items одной записи.

    Возвращает ``RetroFix`` только если хотя бы один нутриент реально меняется
    (сверх ``epsilon``); иначе ``None`` (нечего чинить — идемпотентность).
    Вход не мутируется: изменённые items — глубокие копии, несовпавшие —
    исходные объекты.
    """
    if not items or not products:
        return None

    lookup = _build_lookup(products)

    new_items: List[dict] = []
    changes: List[FieldChange] = []
    matched = 0
    changed_any = False

    for item in items:
        product = lookup.get(normalize_product_name(_item_name(item)))
        if product is None:
            new_items.append(item)
            continue

        matched += 1
        candidate = copy.deepcopy(item)
        apply_verified(candidate, product)

        item_changes = [
            FieldChange(_item_name(item), field, _num(item.get(field)), _num(candidate.get(field)))
            for field in _TRACKED_FIELDS
            if abs(_num(candidate.get(field)) - _num(item.get(field))) > epsilon
        ]
        if item_changes:
            changed_any = True
            changes.extend(item_changes)
            new_items.append(candidate)
        else:
            new_items.append(item)

    if not changed_any:
        return None

    return RetroFix(
        new_items=new_items,
        new_totals=calculate_meal_totals(new_items),
        changes=changes,
        matched_count=matched,
    )
