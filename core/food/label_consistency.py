"""#409: детерминированная сверка item с этикеткой «на 100 г».

LLM иногда кладёт значения «на 100 г» в calories item при weight != 100 (284 ккал на 235 г
вместо 667). Правило: единственный item, вес w != 100, есть product_label.calories_per_100g,
calories item ≈ calories_per_100g (±5%), но отличается от per100·w/100 более чем на 15% →
пересчитать КБЖУ = per100 × w / 100 и поставить label_fixed=True.
"""

from __future__ import annotations

from typing import Optional

_MACROS = (
    ("calories", "calories_per_100g"),
    ("protein", "protein_per_100g"),
    ("fats", "fats_per_100g"),
    ("carbs", "carbs_per_100g"),
)


def reconcile_items_with_label(items: list, product_label: Optional[dict]) -> list:
    if not product_label or not items or len(items) != 1:
        return items
    per100 = product_label.get("calories_per_100g")
    it = items[0]
    w = it.get("weight") or it.get("weight_g") or it.get("amount")
    cal = it.get("calories")
    if not per100 or not w or not cal:
        return items
    w, per100, cal = float(w), float(per100), float(cal)
    if abs(w - 100) < 1:
        return items
    expected = per100 * w / 100
    looks_like_per100 = abs(cal - per100) <= 0.05 * per100
    far_from_expected = abs(cal - expected) > 0.15 * expected
    if not (looks_like_per100 and far_from_expected):
        return items
    fixed = dict(it)
    for k, k100 in _MACROS:
        v = product_label.get(k100)
        if v is not None:
            fixed[k] = round(float(v) * w / 100, 1)
    fixed["label_fixed"] = True
    return [fixed] + list(items[1:])
