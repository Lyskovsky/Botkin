"""Справочник проверенных продуктов (#255) — нормализация и матчинг.

Слой между LLM-распознаванием и БД: нормализует имена продуктов и матчит
items из ответа LLM на записи verified_products (точные КБЖУ с этикетки).

Хранение — database.models.VerifiedProduct, CRUD — database.crud.
"""

import re

# Ё → Е и схлопывание любых разделителей: «Solvie  Protein-Barre» и
# «solvie protein barre» должны давать один ключ.
_NON_WORD_RE = re.compile(r"[^\w]+", flags=re.UNICODE)


def normalize_product_name(name: str) -> str:
    """Каноническая форма имени продукта для точного матчинга.

    lower + ё→е + все не-буквенно-цифровые последовательности → один пробел.
    Единая точка нормализации: и запись (name_norm в БД), и поиск обязаны
    проходить через неё, иначе матчинг молча развалится.
    """
    if not name:
        return ""
    s = name.lower().replace("ё", "е")
    s = _NON_WORD_RE.sub(" ", s)
    return s.strip()


def _build_lookup(products) -> dict:
    """name_norm/alias_norm → VerifiedProduct.

    products отсортированы «личные первыми» (get_verified_products), поэтому
    setdefault сохраняет приоритет личной записи при коллизии имён с общей.
    """
    lookup: dict = {}
    for p in products:
        keys = [p.name_norm]
        for alias in p.aliases or []:
            keys.append(normalize_product_name(alias))
        if p.brand:
            # LLM часто возвращает имя без бренда или бренд+имя — кроем оба варианта
            keys.append(normalize_product_name(f"{p.brand} {p.name}"))
        for key in keys:
            if key:
                lookup.setdefault(key, p)
    return lookup


def apply_verified(item: dict, product) -> None:
    """Пересчитывает КБЖУ item из справочных *_per_100g по весу.

    Вес не распознан → берём portion_g с этикетки (или 100 г) и проставляем
    его в item. Мутирует item in-place — так же, как enrich_items_with_fiber.
    """
    from core.food.fiber_table import _item_weight

    weight = _item_weight(item)
    if weight <= 0:
        weight = product.portion_g or 100.0
        item["weight_g"] = weight

    factor = weight / 100.0
    item["calories"] = round(product.calories_per_100g * factor, 1)
    item["protein"] = round(product.protein_per_100g * factor, 1)
    item["fats"] = round(product.fats_per_100g * factor, 1)
    item["carbs"] = round(product.carbs_per_100g * factor, 1)
    if product.fiber_per_100g is not None:
        # fiber > 0 не перетрётся enrich_items_with_fiber (он идемпотентен)
        item["fiber"] = round(product.fiber_per_100g * factor, 1)


#: Сколько продуктов подмешиваем в промпт распознавания (топ по times_used).
PROMPT_BLOCK_LIMIT = 20
#: Потолок длины блока — защита от разбухания промпта.
PROMPT_BLOCK_MAX_CHARS = 2500


def build_known_products_block(user_id: int, db=None, limit: int = PROMPT_BLOCK_LIMIT) -> str:
    """Компактный блок «проверенные продукты пользователя» для system-промпта.

    Пустой справочник → "" (вызывающий код НЕ добавляет блок — промпт для
    пользователей без записей остаётся байт-в-байт прежним, и кеш базового
    SYSTEM_PROMPT не ломается).
    """
    from database import SessionLocal, get_verified_products

    own_session = db is None
    if own_session:
        db = SessionLocal()
    try:
        products = get_verified_products(db, user_id=user_id, limit=limit)
    finally:
        if own_session:
            db.close()

    if not products:
        return ""

    lines = [
        "VERIFIED PRODUCTS CATALOG (exact label data for THIS user).",
        "If a recognized item IS one of these products — USE THESE NUMBERS "
        "(scaled to the actual weight), do NOT estimate visually:",
    ]
    for p in products:
        brand = f" [{p.brand}]" if p.brand else ""
        portion = f"; portion {p.portion_g:g} g" if p.portion_g else ""
        fiber = f", fiber {p.fiber_per_100g:g}" if p.fiber_per_100g is not None else ""
        line = (
            f"- {p.name}{brand}: per 100g — {p.calories_per_100g:g} kcal, "
            f"P {p.protein_per_100g:g}, F {p.fats_per_100g:g}, C {p.carbs_per_100g:g}{fiber}{portion}"
        )
        if sum(len(x) + 1 for x in lines) + len(line) > PROMPT_BLOCK_MAX_CHARS:
            break
        lines.append(line)
    return "\n".join(lines)


def match_and_apply_verified_products(items: list, user_id: int, db=None) -> int:
    """Post-match: заменяет LLM-оценку КБЖУ точными цифрами из справочника.

    Матч — только точное совпадение нормализованного имени/алиаса/«бренд имя»
    (НЕ substring: «шоколадный батончик» не должен ловить справочный Bombbar).
    Вызывается из save_meal_to_db ДО enrich_items_with_fiber. Возвращает число
    сматченных items; у матчей инкрементится times_used (ранжирование топ-N
    в промпт-блоке). Ошибка справочника не должна ломать сохранение еды —
    вызывающий код обязан оборачивать в try/except.
    """
    from core.food.fiber_table import _item_name
    from database import SessionLocal, get_verified_products, increment_verified_product_usage

    if not items:
        return 0

    own_session = db is None
    if own_session:
        db = SessionLocal()
    try:
        products = get_verified_products(db, user_id=user_id)
        if not products:
            return 0
        lookup = _build_lookup(products)

        matched = 0
        for item in items:
            norm = normalize_product_name(_item_name(item))
            product = lookup.get(norm)
            if product is None:
                continue
            apply_verified(item, product)
            increment_verified_product_usage(db, product.id)
            matched += 1
        return matched
    finally:
        if own_session:
            db.close()
