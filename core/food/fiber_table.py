"""Fallback fiber content (g per 100 g) for common foods.

Used when the LLM doesn't return fiber for an item. Keys are substrings matched
against a normalized (lowercase) product name — first match wins, so order
more specific keys before generic ones.

Sources: USDA FoodData Central, Roskomstat food tables (typical values).
"""

from typing import Optional

# Ordered: more specific names first. Dish-level entries come BEFORE
# ingredient-level so that compound dish names (e.g. "Овсяное печенье")
# match the cookie value (4.0) instead of the dry-oatmeal value (10.0).
_FIBER_PER_100G = [
    # ═══ DISH-LEVEL MATCHES (highest priority) ═══
    # Soups — broth + some veg, mostly water
    # Сухарики (крутоны) — ДО "уха" ниже: "с-УХА-рики" иначе ложно матчит суп
    # (прецедент 02.07.2026, вместе с багом варёной овсянки). "уха" как
    # самостоятельное блюдо не встретилась ни разу в истории — только в
    # ложных срабатываниях на "сухарики"/"сухая".
    ("сухарик", 3.0),
    ("уха", 0.8),
    ("щи", 1.5),
    ("борщ", 2.0),
    ("солянк", 1.2),
    ("харчо", 1.0),
    ("окрошк", 1.5),
    ("куриный суп", 0.8),
    ("овощной суп", 2.0),
    ("крем-суп", 1.5),
    ("суп-пюре", 1.5),
    # Выпечка — specific first
    ("овсяное печень", 4.0),
    ("печенье овсян", 4.0),
    ("сочник", 1.0),
    ("сырник", 0.6),
    ("блинчик", 1.3),
    ("блин", 1.3),
    ("оладь", 1.2),
    ("оладушк", 1.2),
    ("пирожок", 1.5),
    ("пирог", 1.5),
    ("кулебяк", 1.5),
    ("тарт", 1.5),
    ("кекс", 1.8),
    ("маффин", 1.8),
    ("булочк", 1.5),
    ("пончик", 1.2),
    ("печенье", 2.0),
    ("вафл", 1.5),
    ("круассан", 1.8),
    ("штрудел", 2.0),
    ("чизкейк", 0.8),
    ("тирамису", 0.8),
    # Шоколад / конфеты — both word orders for Russian
    ("горький шоколад", 10.5),
    ("шоколад горьк", 10.5),
    ("тёмный шоколад", 7.0),
    ("темный шоколад", 7.0),
    ("шоколад тёмн", 7.0),
    ("шоколад темн", 7.0),
    ("молочный шоколад", 2.5),
    ("шоколад молочн", 2.5),
    ("шоколадная конфет", 2.0),
    ("шоколад", 3.0),  # generic, AFTER specific variants
    ("конфет", 1.5),
    ("халв", 4.0),
    ("пастил", 1.0),
    # Салаты — dish-specific before generic "салат" at ingredient level
    ("винегрет", 3.0),
    ("оливь", 1.5),
    ("мимоз", 0.8),
    ("греческ салат", 2.0),
    ("цезар", 1.2),
    ("сельд под шуб", 2.2),
    # Пицца / паста / лапша — after ingredient matches for шпинат/тыкв etc
    # (but still in dish section so "Пицца с ветчиной" gets 2.2 via пицц match)
    ("пицц", 2.2),
    ("лазань", 2.0),
    ("равиол", 1.5),
    ("спагетти", 1.8),
    ("феттучин", 1.8),
    ("макарон", 1.8),
    # Блюда из мяса/фарша
    ("котлет куриная", 0.5),
    ("котлет", 0.5),
    ("фрикадел", 0.5),
    ("тефтел", 0.8),
    ("голубц", 2.5),
    ("пельмен", 0.8),
    ("манты", 1.0),
    ("вареник", 1.0),
    ("хачапури", 2.0),
    ("чебурек", 1.2),
    ("беляш", 1.2),
    ("шаурм", 2.5),
    ("бургер", 2.0),
    ("сэндвич", 2.0),
    ("сендвич", 2.0),
    ("панини", 2.0),
    ("ролл суши", 1.2),
    ("ролл", 1.5),
    # Каши и гарниры готовые
    # Варёная овсяная каша ДО generic ингредиента "овсян" (10.0, сухая крупа) —
    # иначе любая "Овсяная каша готовая/варёная" получает сухую норму вместо
    # варёной (~6x переоценка). Прецедент 02.07.2026: агент сам заметил разброс
    # 18.7 г клетчатки на 187 г каши и верно предположил ошибку в таблице.
    ("овсяная каша", 1.7),
    ("каша овсян", 1.7),
    ("плов", 1.2),
    ("рисотто", 1.0),
    ("пюре картоф", 1.2),
    # Протеиновые/энергетические батончики (Bombbar обычно 3-6г)
    # ВАЖНО: глазированная линейка Bombbar "No sugar added" с инулином/пшеничными
    # волокнами содержит ~15 г клетчатки на 40 г (≈37.5 г/100г) — это НЕ старый
    # неглазированный Original (~1.8 г). Специфичный ключ должен стоять ВЫШЕ общих
    # "bombbar"/"батончик bombbar", т.к. fiber_per_100g возвращает первое совпадение.
    ("bombbar глазированн", 37.5),
    ("батончик протеиновый", 4.5),
    ("протеиновый батончик", 4.5),
    ("батончик bombbar", 4.5),
    ("bombbar", 4.5),
    ("батончик белковый", 4.5),
    ("белковый батончик", 4.5),
    ("батончик энергетическ", 3.5),
    ("энергетическ батончик", 3.5),
    ("батончик мюсли", 5.0),
    # Напитки с остатком клетчатки
    ("смузи", 2.0),
    ("фреш", 0.5),
    # Протеиновые напитки/коктейли (кефир, шейк, RTD-бутылки) — вкусовые слова
    # в названии ("Малина-Банан" и т.п.) это ароматизатор, не ингредиент, клетчатки
    # там почти нет. Ставить ДО fruit-секции ниже, иначе "малин"/"банан" во вкусе
    # ложно матчатся как ингредиент (прецедент 10.07.2026: Exponenta High-Pro
    # Малина-Банан 250г получил 16.2г клетчатки от ключа "малин" 6.5/100г, хотя
    # у протеинового кефира клетчатки нет).
    ("протеиновый кефир", 0.0),
    ("протеиновый коктейль", 0.0),
    ("протеиновый напиток", 0.0),
    ("high-pro", 0.0),
    ("exponenta", 0.0),
    # Generic fallbacks for soups/soup-adjacent dishes and pasta
    ("суп", 1.0),
    ("паст", 1.8),
    ("лапш", 1.5),
    # ═══ INGREDIENT-LEVEL MATCHES ═══
    # Legumes / beans
    ("чечевиц", 7.9),
    ("нут", 7.6),
    ("фасол", 6.4),
    ("горох", 5.5),
    ("соя", 9.3),
    # Whole grains / cereals
    ("овсян", 10.0),  # сухая крупа/хлопья; варёная каша перехватывается выше "овсяная каша"
    ("греч", 2.7),
    ("бурый рис", 1.8),
    ("коричневый рис", 1.8),
    ("дикий рис", 1.8),
    ("рис отварн", 0.4),
    ("рис варён", 0.4),
    ("белый рис", 0.4),
    ("рис", 0.4),
    ("пшён", 1.3),
    ("пшен", 1.3),
    ("перловк", 2.1),
    ("булгур", 4.5),
    ("кускус", 2.2),
    ("киноа", 2.8),
    ("киноа варён", 2.8),
    ("хлебц", 7.0),
    ("хлеб цельнозерн", 7.0),
    ("хлеб ржаной", 5.8),
    ("хлеб бородинск", 7.0),
    ("хлеб отрубной", 8.0),
    ("хлеб", 2.4),
    # Fruits
    ("авокадо", 6.7),
    ("малин", 6.5),
    ("ежевик", 5.3),
    ("груш", 3.1),
    ("яблок", 2.4),
    ("чернослив", 7.1),
    ("курага", 7.3),
    ("финик", 7.0),
    ("изюм", 3.7),
    ("апельсин", 2.4),
    ("мандарин", 1.8),
    ("лимон", 2.8),
    ("грейпфрут", 1.6),
    ("киви", 3.0),
    ("банан", 2.6),
    ("персик", 1.5),
    ("абрикос", 2.0),
    ("слив", 1.4),
    ("виноград", 0.9),
    ("черешн", 2.1),
    ("вишн", 2.1),
    ("клубник", 2.0),
    ("земляник", 2.0),
    ("черник", 2.4),
    ("голубик", 2.4),
    ("арбуз", 0.4),
    ("дын", 0.9),
    ("ананас", 1.4),
    ("манго", 1.6),
    ("хурм", 3.6),
    ("гранат", 4.0),
    # Vegetables
    ("артишок", 5.4),
    ("брокколи", 2.6),
    ("цветн капуст", 2.0),
    ("брюссельск", 3.8),
    ("кольрабы", 3.6),
    ("кольраби", 3.6),
    ("квашен капуст", 4.1),
    ("квашенн капуст", 4.1),
    ("кимчи", 1.7),
    ("капуст белокочанн", 2.5),
    ("капуст краснокочанн", 2.5),
    ("капуст пекинск", 1.2),
    ("капуст", 2.5),
    ("морковь", 2.8),
    ("свекл", 2.8),
    ("тыкв", 2.1),
    ("кабачок", 1.0),
    ("кабачк", 1.0),
    ("цукини", 1.0),
    ("баклажан", 3.0),
    ("перец сладк", 2.1),
    ("перец болгарск", 2.1),
    ("перец", 2.1),
    ("огурец", 0.5),
    ("огурц", 0.5),
    ("помидор", 1.2),
    ("томат", 1.2),
    ("редис", 1.6),
    ("редьк", 1.6),
    ("репа", 1.8),
    ("лук репчат", 1.7),
    ("лук зелён", 1.8),
    ("лук-порей", 1.8),
    ("чеснок", 2.1),
    ("шпинат", 2.2),
    ("салат", 1.3),
    ("рукол", 1.6),
    ("сельдерей", 1.6),
    ("спарж", 2.1),
    ("фенхел", 3.1),
    ("картофель", 1.8),
    ("картошк", 1.8),
    ("батат", 3.0),
    ("укроп", 2.1),
    ("петрушк", 3.3),
    ("кинза", 2.8),
    ("базилик", 1.6),
    # Nuts / seeds
    ("миндал", 12.5),
    ("фундук", 9.7),
    ("грецк орех", 6.7),
    ("кешью", 3.3),
    ("фисташк", 10.3),
    ("арахис", 8.5),
    ("орех", 7.0),
    ("семен подсолнечн", 8.6),
    ("семечк подсолнеч", 8.6),
    ("тыквенн семен", 6.0),
    ("льнян семен", 27.3),
    ("семена чиа", 34.4),
    ("чиа семен", 34.4),
    # NB: bare "чиа" is too short — matches "чиабатта" (ciabatta bread, 2g fiber).
    # Require "семен" context to avoid false positives.
    ("кунжут", 11.8),
    # Mushrooms
    ("гриб", 2.3),
    ("шампиньон", 2.3),
    ("вешенк", 2.3),
    ("лисичк", 3.8),
    # Berries / dried
    ("клюкв", 4.6),
    ("смородин", 4.3),
    ("крыжовник", 3.4),
    # Non-fiber explicit (meat/fish/dairy/fats): leave out → falls to 0 default
    # Confectionery / grains misc
    ("гречнев хлебц", 7.0),
    ("отруб", 24.0),
    # Fiber supplements (БАД) — almost pure soluble fiber.
    # Users log these as "Псиллиум", "Псиллиум (БАД)", "Psyllium husk", etc.
    # The LLM sometimes leaves fiber=0 when the name lacks a "(БАД)" hint.
    ("псиллиум", 85.0),
    ("псилиум", 85.0),  # common misspelling
    ("psyllium", 85.0),
    ("мюсли", 7.0),
    ("гранол", 6.0),
    # Каша (generic, обобщённо для варёных круп) — after specific оатсян/греч/рис/etc
    ("каша", 1.5),
]


def fiber_per_100g(name: str) -> Optional[float]:
    """Returns fiber g per 100g for a food name, or None if no match."""
    if not name:
        return None
    n = name.lower()
    for key, val in _FIBER_PER_100G:
        if key in n:
            return val
    return None


def estimate_fiber(name: str, weight_g: Optional[float]) -> float:
    """Estimate fiber grams for a given food item. Returns 0.0 if no match or no weight."""
    if not weight_g or weight_g <= 0:
        return 0.0
    per100 = fiber_per_100g(name)
    if per100 is None:
        return 0.0
    return round(per100 * weight_g / 100.0, 1)


def _item_name(it: dict) -> str:
    """Extract display name from an item dict, handling all three schemas
    (product / name / food) used across the codebase."""
    return str(it.get("product") or it.get("name") or it.get("food") or "")


def _item_weight(it: dict) -> float:
    """Extract weight in grams, handling weight_g / amount / weight field names."""
    w = it.get("weight_g")
    if w is None:
        w = it.get("amount")
    if w is None:
        w = it.get("weight")
    try:
        return float(w) if w is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def enrich_items_with_fiber(items: list) -> list:
    """Fill the `fiber` field on items that lack it, using estimate_fiber.

    Idempotent — items with existing fiber > 0 are never overwritten.
    Items with fiber = 0, None, or missing are re-estimated from name + weight.
    Mutates items in place (safe since they are fresh copies from SQLAlchemy JSONB).

    Handles all three item schemas in the codebase:
      - DB LLM/vision items: {"food": ..., "amount": ...}
      - internal meal_items: {"product": ..., "weight_g": ...}
      - supplements items:   {"name": ..., "weight_g": ..., "fiber": ...}

    Returns the same list for convenience.
    """
    for it in items:
        existing = it.get("fiber")
        if existing is not None:
            try:
                if float(existing) > 0:
                    continue
            except (TypeError, ValueError):
                pass
        estimated = estimate_fiber(_item_name(it), _item_weight(it))
        if estimated > 0:
            it["fiber"] = estimated
    return items


def sum_fiber(items: list) -> float:
    """Sum fiber across items (assumes items already enriched). Rounded to 1 decimal."""
    total = 0.0
    for it in items:
        v = it.get("fiber")
        try:
            total += float(v) if v is not None else 0.0
        except (TypeError, ValueError):
            pass
    return round(total, 1)
