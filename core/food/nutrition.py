#!/usr/bin/env python3
"""
Сервис для расчёта КБЖУ с интеграцией новых модулей извлечения весов
"""

import html
import os
import logging
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# Паттерн для определения алкогольных напитков в названиях продуктов
_ALCOHOL_RE = re.compile(
    r"(?<![а-яА-ЯёЁa-zA-Z])(вин[оаеуи]|шампанское|шампанского|шампанским|виски|whisky|whiskey|"
    r"ром(?![а-яА-ЯёЁ])|джин|gin|водк[аиу]|просекко|prosecco|пив[оаеу]|пива|"
    r"beer|коньяк|коньяка|текил[аеуыой]|ликер|ликёр|бренди|brandy|портвейн|саке|sake|"
    r"рислинг|chardonnay|мерло|каберне|совиньон|игристое|негрони|"
    r"бокал\s+\w+\s+вин|бокал\s+вин)",
    re.IGNORECASE,
)
_NON_ALCOHOL_RE = re.compile(r"0[,.]0\s*%?|безалкогол|non.?alcohol", re.IGNORECASE)


def detect_alcohol(meal_items: List[Dict]) -> bool:
    """Возвращает True если хотя бы один продукт является алкоголем."""
    for item in meal_items:
        name = str(item.get("product") or item.get("name") or "")
        if _ALCOHOL_RE.search(name) and not _NON_ALCOHOL_RE.search(name):
            return True
    return False


# Issue #115: оценка веса порции по типу блюда вместо слепого дефолта 200г.
# Ключевые слова подбираются по подстроке в нижнем регистре; порядок проверки —
# от более специфичных категорий к общим. Веса — медианные порции «на глаз».
DEFAULT_PORTION_WEIGHT_G = 200.0  # неизвестное блюдо
BOWL_PORTION_WEIGHT_G = 330.0  # боул / поке / салат-как-блюдо / тарелка
SOUP_PORTION_WEIGHT_G = 300.0  # суп
SIDE_DISH_PORTION_WEIGHT_G = 150.0  # гарнир / каша

_BOWL_KEYWORDS = ("боул", "bowl", "поке", "poke", "тарелк", "салат-боул")
_SOUP_KEYWORDS = ("суп", "soup", "борщ", "щи", "солянк", "крем-суп", "бульон")
_SIDE_DISH_KEYWORDS = ("гарнир", "каша", "каши", "овсянк")  # «каши» (мн.ч.) ≠ подстрока «каша»


def estimate_default_weight(dish_name: str) -> float:
    """Оценивает вес порции по типу блюда (issue #115).

    Возвращает медианную порцию по ключевым словам в названии. Если тип не
    распознан — DEFAULT_PORTION_WEIGHT_G (200г, прежнее поведение).
    Используется только как последний fallback, когда вес из vision/regex/LLM
    отсутствует.
    """
    # Порядок: от специфичного к общему. «тарелка каши» / «боул-каша» должны
    # дать вес боула (330), а не гарнира (150) — поэтому bowl проверяется раньше
    # side_dish; суп первым (солянка/борщ объёмнее гарнира).
    name = (dish_name or "").lower()
    if any(kw in name for kw in _SOUP_KEYWORDS):
        return SOUP_PORTION_WEIGHT_G
    if any(kw in name for kw in _BOWL_KEYWORDS):
        return BOWL_PORTION_WEIGHT_G
    if any(kw in name for kw in _SIDE_DISH_KEYWORDS):
        return SIDE_DISH_PORTION_WEIGHT_G
    return DEFAULT_PORTION_WEIGHT_G


try:
    import requests
except ImportError:
    requests = None

# Пытаемся импортировать утилиту для ключа
try:
    from .chatgpt_vision import get_openai_api_key
except ImportError:
    # Фолбек
    def get_openai_api_key():
        return os.getenv("OPENAI_API_KEY")


# Импортируем новые модули
from .description_parser import (
    parse_meal_description as parse_meal_description_new,
    apply_portion_multiplier,
    normalize_product_name,
    is_zero_calorie_drink,
)

# Импортируем поиск продуктов
try:
    from .product_search import find_product, search_product_online, load_products_db
except ImportError:
    # Если модуль не найден, создаем заглушки
    def find_product(name: str) -> Optional[Dict]:
        return None

    def search_product_online(name: str) -> Optional[Dict]:
        return None

    def load_products_db() -> Dict:
        return {}


logger = logging.getLogger(__name__)

# Загружаем базу продуктов
PRODUCTS_DB = load_products_db()


def parse_meal_description(
    description: str,
    photo_paths: Optional[List[Path]] = None,
    api_key: Optional[str] = None,
    portion_multiplier: float = 1.0,
) -> List[Dict]:
    """
    Парсит описание блюда и извлекает информацию о продуктах.
    Использует новый улучшенный парсер с поддержкой нескольких фото.

    Args:
        description: Описание блюда
        photo_paths: Список путей к фото (опционально)
        api_key: API ключ Google Cloud Vision (опционально)
        portion_multiplier: Множитель порции (например, 0.5 для половины)

    Returns:
        Список продуктов с весами и информацией:
        [{'name': 'куриное филе', 'weight': 192.8, 'source': 'photo'}, ...]
    """
    # Получаем API ключ используя единую функцию
    if not api_key:
        try:
            from .api_key_loader import get_google_vision_api_key

            api_key = get_google_vision_api_key()
        except ImportError:
            # Fallback: старая логика
            api_key = os.getenv("GOOGLE_VISION_API_KEY")
            if not api_key:
                key_file = Path(__file__).parent.parent / ".google_vision_api_key"
                if not key_file.exists():
                    family_docs_key = Path.home() / "FamilyDocs" / ".google_vision_api_key"
                    if family_docs_key.exists():
                        key_file = family_docs_key
                if key_file.exists():
                    try:
                        api_key = key_file.read_text().strip()
                    except Exception:
                        pass

    # Используем новый парсер
    products = parse_meal_description_new(description, photo_paths, api_key)

    # Применяем множитель порции
    if portion_multiplier != 1.0:
        products = apply_portion_multiplier(products, portion_multiplier)

    return products


def calculate_nutrition(
    product_name: str, weight_g: float, description: str = "", basis: str = None
) -> Dict[str, float]:
    """
    Рассчитывает КБЖУ для продукта с учётом basis (cooked/dry/raw).

    Args:
        product_name: Название продукта
        weight_g: Вес в граммах
        description: Полное описание (для определения basis)
        basis: Basis продукта (cooked/dry/raw) - если не указан, определяется автоматически

    Returns:
        Словарь с КБЖУ: {'calories': 120, 'protein': 23, 'fats': 3, 'carbs': 0, 'basis': 'cooked'}
    """
    from .description_parser import determine_product_basis

    # Определяем basis, если не указан
    if not basis:
        basis = determine_product_basis(product_name, description)

    # Нормализуем название с учётом basis
    normalized_name = normalize_product_name(product_name, basis)

    # Если basis = ambiguous, пробуем сначала cooked (Rule A)

    # Если basis = ambiguous, пробуем сначала cooked (Rule A)
    if basis == "ambiguous":
        # Пробуем сначала как готовое
        normalized_cooked = normalize_product_name(product_name, "cooked")
        product = find_product(normalized_cooked)
        if product:
            basis = "cooked"
            normalized_name = normalized_cooked
        else:
            # Если не найдено готовое, пробуем сухое
            normalized_dry = normalize_product_name(product_name, "dry")
            product = find_product(normalized_dry)
            if product:
                basis = "dry"
                normalized_name = normalized_dry
    else:
        # Ищем продукт в базе
        product = find_product(normalized_name)

    if not product:
        # Пробуем поиск в интернете
        logger.info(f"Продукт '{normalized_name}' не найден в базе, ищу в интернете...")
        product = search_product_online(normalized_name)

    if not product:
        logger.warning(f"Продукт '{normalized_name}' не найден, использую средние значения")
        # Используем средние значения для конкретных продуктов
        default_values = {
            "яйцо": {"calories_per_100g": 143, "protein_per_100g": 12.6, "fats_per_100g": 9.5, "carbs_per_100g": 0.7},
            "лук": {"calories_per_100g": 47, "protein_per_100g": 1.4, "fats_per_100g": 0.0, "carbs_per_100g": 10.4},
            "томат": {"calories_per_100g": 18, "protein_per_100g": 0.9, "fats_per_100g": 0.2, "carbs_per_100g": 3.9},
            "сыр": {"calories_per_100g": 363, "protein_per_100g": 23.0, "fats_per_100g": 30.0, "carbs_per_100g": 0.0},
            "сливочное масло": {
                "calories_per_100g": 748,
                "protein_per_100g": 0.5,
                "fats_per_100g": 82.5,
                "carbs_per_100g": 0.8,
            },
            "морковь": {"calories_per_100g": 41, "protein_per_100g": 0.9, "fats_per_100g": 0.2, "carbs_per_100g": 9.6},
            "свекла": {"calories_per_100g": 43, "protein_per_100g": 1.6, "fats_per_100g": 0.2, "carbs_per_100g": 9.6},
        }

        # Проверяем, есть ли значения для этого продукта (используем точное совпадение слов)
        import re

        product_lower = normalized_name.lower()

        # Токены продукта
        product_tokens = set(re.findall(r"\w+", product_lower))

        for key, values in default_values.items():
            # Если ключ содержится как отдельное слово в названии продукта
            # ИЛИ если название продукта содержится как отдельное слово в ключе (редко, но бывает)
            key_tokens = set(re.findall(r"\w+", key.lower()))

            # Проверка 1: Ключ - это одно из слов продукта (например "сыр" в "сыр российский")
            # Но НЕ "сыр" в "сырая морковь"

            if any(k_token in product_tokens for k_token in key_tokens):
                # Дополнительная проверка: для коротких ключей типа "сыр", "лук" требуем осторожности
                # "сырая" -> tokens: ["сырая"]. "сыр" -> tokens: ["сыр"]. No match. Correct.
                product = values
                logger.info(f"Использованы значения по умолчанию для '{normalized_name}': {product} (match: {key})")
                break

            # Compatibility fallback (if needed? Maybe safer to just rely on product search if not exact match)
            # Let's try restrictive matching first.

        # Если не нашли - используем общие средние значения
        if not product:
            product = {
                "calories_per_100g": 100,
                "protein_per_100g": 10.0,
                "fats_per_100g": 5.0,
                "carbs_per_100g": 15.0,
            }

    # Рассчитываем КБЖУ
    multiplier = weight_g / 100.0

    result = {
        "calories": round(product.get("calories_per_100g", 0) * multiplier, 1),
        "protein": round(product.get("protein_per_100g", 0) * multiplier, 1),
        "fats": round(product.get("fats_per_100g", 0) * multiplier, 1),
        "carbs": round(product.get("carbs_per_100g", 0) * multiplier, 1),
        "basis": basis,
    }

    # Логируем, если использован cooked вместо dry (важно для отладки)
    if basis == "cooked" and (
        "пшён" in product_name.lower() or "греч" in product_name.lower() or "рис" in product_name.lower()
    ):
        logger.info(
            f"✅ Использован готовый продукт '{normalized_name}' (basis=cooked) вместо сухого для '{product_name}'"
        )

    return result


def check_kcal_consistency(items: List[Dict], threshold: float = 0.25) -> List[Dict]:
    """Find items where stated calories deviate from macro-derived calories
    (4·P + 9·F + 4·C) by more than `threshold` (relative to stated kcal).

    Skips alcoholic items (alcohol = 7 kcal/g, not captured by macro formula).
    Skips items with stated_kcal < 30 or macro_kcal < 20 (noise floor).

    Returns a list of warning dicts: [{name, stated, macro, diff}, ...]
    Caller can attach to meal_totals['kcal_warnings'] and surface to user.
    """
    warnings = []
    for it in items:
        # Skip alcoholic — formula doesn't account for ethanol (7 kcal/g).
        if it.get("is_alcohol") or it.get("alcohol"):
            continue
        try:
            stated = float(it.get("calories") or 0)
            p = float(it.get("protein") or 0)
            f = float(it.get("fats") or 0)
            c = float(it.get("carbs") or 0)
        except (TypeError, ValueError):
            continue
        macro = 4 * p + 9 * f + 4 * c
        if stated < 30 or macro < 20:
            continue
        if abs(stated - macro) / max(stated, 1) > threshold:
            warnings.append(
                {
                    "name": it.get("product") or it.get("name") or it.get("food") or "?",
                    "stated": round(stated, 1),
                    "macro": round(macro, 1),
                    "diff": round(stated - macro, 1),
                }
            )
    return warnings


# Issue #115: порог плотности для салатов/боулов. Зелёный салат с заправкой
# редко плотнее ~180 ккал/100г; выше — вероятна ошибка веса или жирная заправка.
SALAD_DENSITY_THRESHOLD = 180.0
_SALAD_LIKE_KEYWORDS = ("салат", "овощи", "овощн", "боул", "bowl", "зелен", "poke", "поке")


def check_density_sanity(items: List[Dict], threshold: float = SALAD_DENSITY_THRESHOLD) -> List[Dict]:
    """Flag salad/veggie/bowl items whose calorie density is implausibly high.

    For each item with weight>0 and a salad-like name, if calories/weight*100
    exceeds `threshold`, emit a warning {name, density, weight}. Does NOT block
    saving — the estimate is recorded as-is, the warning is surfaced to the user.
    """
    warnings = []
    for it in items:
        name = str(it.get("product") or it.get("name") or it.get("food") or "")
        if not any(kw in name.lower() for kw in _SALAD_LIKE_KEYWORDS):
            continue
        try:
            weight = float(it.get("weight_g") or it.get("weight") or 0)
            calories = float(it.get("calories") or 0)
        except (TypeError, ValueError):
            continue
        if weight <= 0 or calories < 0:
            continue
        density = calories / weight * 100
        if density > threshold:
            warnings.append({"name": name, "density": round(density, 1), "weight": round(weight, 1)})
    return warnings


def _format_kcal_mismatch_line(w: Dict) -> str:
    sign = "+" if w["diff"] > 0 else ""
    name = html.escape(str(w["name"]))  # имя из vision/LLM → HTML-сообщение
    return f"• {name}: записано {int(w['stated'])} ккал, по БЖУ ≈ {int(w['macro'])} ({sign}{int(w['diff'])})"


def _format_density_line(w: Dict) -> str:
    name = html.escape(str(w["name"]))  # имя из vision/LLM → HTML-сообщение
    return f"❓ {name}: калорийнее обычного салата ({w['density']:.0f} ккал/100г) — проверь вес/заправку"


def format_kcal_warning(meal_totals: Dict) -> str:
    """Build a user-facing warning string from meal_totals['kcal_warnings'].
    Handles two warning shapes: kcal↔БЖУ mismatch ({stated,macro,diff}) and
    density sanity ({density,weight}, issue #115). Returns empty string if none."""
    warns = meal_totals.get("kcal_warnings") if isinstance(meal_totals, dict) else None
    if not warns:
        return ""
    # Заголовок по составу: density-флаги — это не расхождение ккал↔БЖУ (issue #115).
    has_mismatch = any("diff" in w for w in warns)
    has_density = any("density" in w for w in warns)
    if has_mismatch and has_density:
        header = "\n\n⚠️ <b>Проверь данные о блюде</b> (расхождение ккал/БЖУ и необычная плотность):"
    elif has_mismatch:
        header = "\n\n⚠️ <b>Расхождение ккал и БЖУ</b> (возможна ошибка распознавания):"
    else:
        header = "\n\n⚠️ <b>Необычная калорийность блюда</b> (проверь вес/заправку):"
    lines = [header]
    for w in warns[:3]:  # cap at 3 to keep the message short
        if "density" in w:
            lines.append(_format_density_line(w))
        else:
            lines.append(_format_kcal_mismatch_line(w))
    if len(warns) > 3:
        lines.append(f"… и ещё {len(warns) - 3}")
    lines.append("Проверь значения перед сохранением.")
    return "\n".join(lines)


def _clean_log_name(name: str) -> str:
    """Убирает переводы строк/возвраты каретки из имени перед логированием —
    защита от log injection (имя приходит из vision/LLM). Обрезает до 80 симв."""
    return str(name).replace("\n", " ").replace("\r", " ")[:80]


def _collect_meal_warnings(meal_items: List[Dict], has_alcohol: bool) -> List[Dict]:
    """Собирает все предупреждения по блюду в один список (issue #115).

    Объединяет kcal↔БЖУ-расхождения (только для безалкогольных — формула не
    учитывает этанол) и флаги завышенной плотности салатов/боулов. Логирует
    каждое. Не затирает: density-флаги добавляются к kcal-флагам.
    """
    warnings: List[Dict] = []
    if not has_alcohol:
        for w in check_kcal_consistency(meal_items):
            safe_name = _clean_log_name(w["name"])
            logger.warning(
                f"⚠️ kcal mismatch for '{safe_name}': stated {w['stated']} vs macro {w['macro']} (diff {w['diff']:+})"
            )
            warnings.append(w)
    for w in check_density_sanity(meal_items):
        safe_name = _clean_log_name(w["name"])
        logger.warning(f"❓ high density for '{safe_name}': {w['density']} kcal/100g at {w['weight']}g")
        warnings.append(w)
    return warnings


def calculate_meal_totals(meal_items: List[Dict]) -> Dict[str, float]:
    """
    Рассчитывает общие КБЖУ для всех продуктов в блюде.

    Args:
        meal_items: Список продуктов с КБЖУ

    Returns:
        Словарь с общими КБЖУ
    """
    totals = {
        "calories": 0.0,
        "protein": 0.0,
        "fats": 0.0,
        "carbs": 0.0,
        "fiber": 0.0,
        "drinks": 0.0,
    }

    for item in meal_items:
        totals["calories"] += item.get("calories", 0)
        totals["protein"] += item.get("protein", 0)
        totals["fats"] += item.get("fats", 0)
        totals["carbs"] += item.get("carbs", 0)
        totals["fiber"] += item.get("fiber", 0) or 0
        totals["drinks"] += item.get("drinks", 0) or 0

    # Округляем
    for key in totals:
        totals[key] = round(totals[key], 1)

    return totals


def process_meal_description(
    description: str,
    photo_paths: Optional[List[Path]] = None,
    portion_multiplier: float = 1.0,
    api_key: Optional[str] = None,
) -> Tuple[List[Dict], Dict[str, float]]:
    """
    Полный цикл обработки описания блюда:
    1. Парсит описание и извлекает продукты
    2. Рассчитывает КБЖУ для каждого продукта
    3. Возвращает список продуктов и общие КБЖУ

    Args:
        description: Описание блюда
        photo_paths: Список путей к фото (опционально)
        portion_multiplier: Множитель порции
        api_key: API ключ Google Cloud Vision (опционально)

    Returns:
        Кортеж (meal_items, meal_totals):
        - meal_items: список продуктов с КБЖУ
        - meal_totals: общие КБЖУ
    """
    # Парсим описание
    products = parse_meal_description(description, photo_paths, api_key, portion_multiplier)

    # 1. СНАЧАЛА проверяем явные итоги в описании (MAX PRIORITY)
    from .description_parser import extract_explicit_totals

    explicit_totals = extract_explicit_totals(description)

    # Рассчитываем КБЖУ для каждого продукта
    meal_items = []

    # Флаг, что мы используем ручные итоги
    using_manual_totals = False

    # Если есть явные итоги - используем их для общих значений
    if explicit_totals:
        logger.info(f"✅ Найдены явные КБЖУ в описании: {explicit_totals}")
        using_manual_totals = True

    for product in products:
        # Если это меню с уже готовыми КБЖУ - используем их
        if product.get("source") == "menu_ocr" and product.get("menu_data"):
            menu_data = product.get("menu_data", {})
            print(
                f"    ✅ Используем КБЖУ из меню: {menu_data.get('calories')} ккал, "
                f"Б: {menu_data.get('protein')}г, Ж: {menu_data.get('fats')}г, У: {menu_data.get('carbs')}г"
            )
            meal_items.append(
                {
                    "product": product["name"],
                    "weight_g": product.get("weight"),
                    "weight_source": "menu",
                    "weight_estimated": False,
                    "calories": menu_data.get("calories", 0),
                    "protein": menu_data.get("protein", 0),
                    "fats": menu_data.get("fats", 0),
                    "carbs": menu_data.get("carbs", 0),
                    "source": "menu_ocr",
                }
            )
        # Если есть готовые КБЖУ напрямую в продукте (из parse_meal_description)
        # Проверяем source='menu_ocr' ИЛИ наличие calories без weight (это меню)
        elif product.get("calories") is not None and (
            product.get("source") == "menu_ocr" or (product.get("weight") is None and product.get("calories", 0) > 0)
        ):
            print(
                f"    ✅ Используем КБЖУ из меню (прямо в продукте): {product.get('calories')} ккал, "
                f"Б: {product.get('protein')}г, Ж: {product.get('fats')}г, У: {product.get('carbs')}г"
            )
            meal_items.append(
                {
                    "product": product["name"],
                    "weight_g": product.get("weight"),
                    "weight_source": "menu",
                    "weight_estimated": False,
                    "calories": product.get("calories", 0),
                    "protein": product.get("protein", 0),
                    "fats": product.get("fats", 0),
                    "carbs": product.get("carbs", 0),
                    "source": "menu_ocr",
                }
            )
        elif product.get("weight") and product["weight"] > 0:
            # Передаём description для определения basis
            nutrition = calculate_nutrition(
                product["name"],
                product["weight"],
                description=description,
                basis=product.get("basis"),  # Если basis уже определён в продукте
            )

            meal_items.append(
                {
                    "product": product["name"],
                    "weight_g": round(product["weight"], 2),
                    "weight_source": product.get("source", "unknown"),
                    "weight_estimated": product.get("source") in ["portion_estimate", "default_portion"],
                    "calories": nutrition["calories"],
                    "protein": nutrition["protein"],
                    "fats": nutrition["fats"],
                    "carbs": nutrition["carbs"],
                    "basis": nutrition.get("basis", "raw"),  # Сохраняем basis
                    "source": "local_db",  # или 'online_search' если искали в интернете
                }
            )
        elif product.get("source") == "description_simple":
            # Для простых описаний (кофе, чай) используем стандартную порцию
            # Например, кофе - 250мл, чай - 250мл
            default_weights = {
                "кофе": 250,  # мл
                "чай": 250,  # мл
                "сок": 200,  # мл
                "вода": 250,  # мл
                "молоко": 250,  # мл
            }
            product_name_lower = product["name"].lower()
            default_weight = default_weights.get(product_name_lower, 100)

            nutrition = calculate_nutrition(product["name"], default_weight)

            meal_items.append(
                {
                    "product": product["name"],
                    "weight_g": default_weight,
                    "weight_source": "default_portion",
                    "weight_estimated": True,
                    "calories": nutrition["calories"],
                    "protein": nutrition["protein"],
                    "fats": nutrition["fats"],
                    "carbs": nutrition["carbs"],
                    "source": "local_db",
                }
            )

    # Fiber fallback for items that lack it (menu_ocr / local_db paths don't set fiber).
    from .fiber_table import estimate_fiber

    for mi in meal_items:
        if mi.get("fiber") is None:
            mi["fiber"] = estimate_fiber(mi.get("product") or "", mi.get("weight_g"))

    # Рассчитываем общие КБЖУ
    meal_totals = calculate_meal_totals(meal_items)

    # Если найдены явные итоги - заменяем общие значения
    if using_manual_totals and explicit_totals:
        if "calories" in explicit_totals:
            meal_totals["calories"] = explicit_totals["calories"]
        if "protein" in explicit_totals:
            meal_totals["protein"] = explicit_totals["protein"]
        if "fats" in explicit_totals:
            meal_totals["fats"] = explicit_totals["fats"]
        if "carbs" in explicit_totals:
            meal_totals["carbs"] = explicit_totals["carbs"]

    meal_totals["has_alcohol"] = detect_alcohol(meal_items)
    # Если drinks не посчитан из items (старый код) — оставляем 0
    if "drinks" not in meal_totals:
        meal_totals["drinks"] = 0.0
    # Sanity-check: ккал↔БЖУ + плотность салатов (issue #115). Если LLM
    # галлюцинирует — записываем предупреждение для последующего показа юзеру.
    warns = _collect_meal_warnings(meal_items, bool(meal_totals.get("has_alcohol")))
    if warns:
        meal_totals["kcal_warnings"] = warns
    return meal_items, meal_totals


# (process_llm_food_data определена ниже в этом файле)


def _stem(token: str) -> str:
    """Грубый стем для сравнения русских слов в разных падежах: первые 5 букв (4 для коротких)."""
    t = token.lower().strip("().,;:!?-")
    return t[:5] if len(t) >= 6 else t[:4]


def _stems_overlap(a: str, b: str) -> bool:
    sa = {_stem(t) for t in a.split() if len(t) > 3}
    sb = {_stem(t) for t in b.split() if len(t) > 3}
    return bool(sa & sb)


def process_llm_food_data(llm_data: Dict, description: str = None) -> Tuple[List[Dict], Dict[str, float]]:
    """
    Converts LLM Router 'food' data into internal meal structure.
    Calculates macros for items if LLM didn't provide them.

    Args:
        llm_data: Dict from llm_router.analyze_message (must be type='food')
        description: Original text description (optional, used for regex fallback)

    Returns:
        (meal_items, meal_totals)
    """
    if not llm_data or llm_data.get("type") != "food":
        return [], {"calories": 0, "protein": 0, "fats": 0, "carbs": 0}

    data = llm_data.get("data", {})
    items = data.get("items", [])
    meal_items = []

    # Pre-calculate regex items if description is available
    regex_items_map = {}
    if description:
        try:
            from .description_parser import extract_products_from_description, normalize_product_name

            regex_products = extract_products_from_description(description)
            for p in regex_products:
                if p.get("weight"):
                    # Map normalized name to weight
                    # Use simple normalization
                    n_name = normalize_product_name(p["name"])
                    regex_items_map[n_name] = p["weight"]
                    # Also map raw name just in case
                    regex_items_map[p["name"].lower()] = p["weight"]
        except Exception as e:
            print(f"Error in regex fallback: {e}")

    for item in items:
        logger.info(f"🔍 Processing LLM item: {item}")
        name = item.get("name", "Unknown")
        weight = item.get("weight")

        # Приоритет: вес из описания пользователя (regex). LLM мог ошибиться или default перезаписать.
        regex_matched = False
        if description:
            from .description_parser import normalize_product_name

            n_name = normalize_product_name(name)
            regex_weight = None
            if n_name in regex_items_map:
                regex_weight = regex_items_map[n_name]
            elif name.lower() in regex_items_map:
                regex_weight = regex_items_map[name.lower()]
            if not regex_weight:
                for r_name, r_weight in regex_items_map.items():
                    if len(r_name) > 3 and len(name) > 3 and _stems_overlap(name.lower(), r_name):
                        regex_weight = r_weight
                        break
            if not regex_weight and weight:
                # Имя не пересекается даже по стемам, но LLM-вес совпадает с явно
                # указанным пользователем весом одного из продуктов в тексте — доверяем.
                for r_weight in regex_items_map.values():
                    if abs(r_weight - weight) < 0.01:
                        regex_weight = r_weight
                        break
            if regex_weight is not None:
                weight = regex_weight
                regex_matched = True

        # Только если пользователь НЕ указал вес явно — используем default для бутылок/порций
        # Иначе "100г пельменей" перезаписывалось бы на 250г стандартной порции
        if not regex_matched:
            from .description_parser import get_default_unit_weight

            default_weight = get_default_unit_weight(name)
            if default_weight > 0 and (not weight or weight < default_weight * 0.5):
                # Если LLM уже дал вес и калории — масштабируем макросы под новый
                # (дефолтный) вес порции, чтобы не потерять КБЖУ-консистентность
                # (issue #408: раньше вес менялся, а калории — нет).
                if weight and weight > 0 and item.get("calories") is not None:
                    scale = default_weight / weight
                    item = dict(item)  # копия, не мутируем исходный dict из llm_data
                    for macro in ("calories", "protein", "fats", "carbs"):
                        val = item.get(macro)
                        if val is not None:
                            item[macro] = round(float(val) * scale, 1)
                weight = default_weight

        # Fallback: если regex не сработал — default_weight уже применили выше

        # Custom Logic: Prioritize Database Lookup over LLM macros
        # Даже если LLM вернул макросы, проверяем базу данных, так как там могут быть
        # более точные/любимые продукты пользователя (например, Bombbar)
        from .description_parser import normalize_product_name

        # Нормализуем имя для поиска
        norm_name = normalize_product_name(name)

        # Пытаемся распарсить quantity если нет веса
        if (not weight or weight == 0) and item.get("quantity"):
            qty_str = str(item.get("quantity")).lower().strip()
            import re

            # Миллилитры (1 мл = 1 г приближенно для напитков)
            ml_match = re.search(r"(\d+(?:[.,]\d+)?)\s*(?:мл|ml|milliliter)", qty_str)
            if ml_match:
                try:
                    weight = float(ml_match.group(1).replace(",", "."))
                    weight_src = "quantity_ml"
                except ValueError:
                    pass

            # Литры
            l_match = re.search(r"(\d+(?:[.,]\d+)?)\s*(?:л|l|liter)", qty_str)
            if l_match:
                try:
                    weight = float(l_match.group(1).replace(",", ".")) * 1000
                    weight_src = "quantity_l"
                except ValueError:
                    pass

            # Граммы
            g_match = re.search(r"(\d+(?:[.,]\d+)?)\s*(?:г|g|gram)", qty_str)
            if g_match:
                try:
                    weight = float(g_match.group(1).replace(",", "."))
                    weight_src = "quantity_g"
                except ValueError:
                    pass

            # Килограммы
            kg_match = re.search(r"(\d+(?:[.,]\d+)?)\s*(?:кг|kg|kilogram)", qty_str)
            if kg_match:
                try:
                    weight = float(kg_match.group(1).replace(",", ".")) * 1000
                    weight_src = "quantity_kg"
                except ValueError:
                    pass

        # Приоритет КБЖУ (исправлено 22.03.2026):
        # 1. LLM вернул калории > 0 → пользователь указал КБЖУ явно → НЕ ТРОГАТЬ
        # 2. LLM НЕ вернул калории → искать в локальной БД (Bombbar, любимые продукты)
        # 3. Нет ни LLM, ни БД → оценка по весу
        #
        # Раньше БД всегда перезаписывала LLM — это ломало случай когда пользователь
        # вводил "Кускус 59г: 200 ккал" и получал 66 ккал из справочника.

        llm_has_calories = item.get("calories") is not None and item.get("calories") > 0

        if not llm_has_calories:
            # LLM не дал калорий → ищем в БД (случай "Bombbar" или "батончик протеиновый")
            db_product = find_product(norm_name)
            if not db_product:
                db_product = find_product(name)

            if db_product:
                logger.info(f"✅ Product found in DB (LLM had no calories): {name}")

                final_weight = weight
                weight_src = "llm"

                if not final_weight:
                    final_weight = db_product.get("weight_g")
                    if final_weight:
                        weight_src = "db_default"
                    else:
                        from .description_parser import get_default_unit_weight

                        try:
                            qty = float(item.get("quantity", 1))
                        except (ValueError, TypeError):
                            qty = 1.0
                        if not qty or qty <= 0:
                            qty = 1.0

                        unit_weight = get_default_unit_weight(name)
                        if unit_weight > 0:
                            final_weight = unit_weight * qty
                            weight_src = "estimate"

                if not final_weight:
                    logger.warning(f"Product {name} found in DB but no weight. Using 100g default.")
                    final_weight = 100.0
                    weight_src = "default_100g"

                multiplier = final_weight / 100.0

                meal_items.append(
                    {
                        "product": db_product.get("name", name),
                        "weight_g": final_weight,
                        "weight_source": weight_src,
                        "calories": round(db_product.get("calories_per_100g", 0) * multiplier, 1),
                        "protein": round(db_product.get("protein_per_100g", 0) * multiplier, 1),
                        "fats": round(db_product.get("fats_per_100g", 0) * multiplier, 1),
                        "carbs": round(db_product.get("carbs_per_100g", 0) * multiplier, 1),
                        "source": "local_db",
                        "note": db_product.get("note"),
                    }
                )
                continue
        else:
            logger.info(
                f"📋 LLM provided calories for '{name}': {item.get('calories')} kcal — using as-is (user input priority)"
            )

        # ORIGINAL LOGIC
        has_macros = item.get("calories") is not None and item.get("calories") > 0

        # Защита от галлюцинаций LLM (максимум ~900 ккал/100г для чистого жира)
        if has_macros and weight and weight > 0:
            cal_per_100g = (item["calories"] / weight) * 100
            if cal_per_100g > 1000:
                logger.warning(
                    f"⚠️ LLM сгаллюцинировал макросы для '{name}'! {item['calories']} ккал на {weight}г ({cal_per_100g} ккал/100г). Игнорируем."
                )
                has_macros = False

        if has_macros:
            # Если вес не указан, но есть калории - пробуем оценить вес по названию (для отображения)
            final_weight = weight
            weight_src = "llm"

            if not final_weight:
                from .description_parser import get_default_unit_weight

                # Если LLM вернул quantity, используем его
                try:
                    qty = float(item.get("quantity", 1))
                except (ValueError, TypeError):
                    qty = 1.0

                # Если quantity=0 или None, считаем 1 (для штучных товаров)
                if not qty or qty <= 0:
                    qty = 1.0

                unit_weight = get_default_unit_weight(name)
                if unit_weight > 0:
                    final_weight = unit_weight * qty
                    weight_src = "estimate"

            # Напитки без калорий: LLM часто путает с обычной колой
            if is_zero_calorie_drink(name):
                cal, prot, fat, carb = 0.0, 0.0, 0.0, 0.0
                src = "llm_router_zero_drink"
            else:
                cal = float(item.get("calories") or 0)
                prot = float(item.get("protein") or 0)
                fat = float(item.get("fats") or 0)
                carb = float(item.get("carbs") or 0)
                src = "llm_router"

            meal_items.append(
                {
                    "product": name,
                    "weight_g": final_weight,
                    "weight_source": weight_src,
                    "calories": cal,
                    "protein": prot,
                    "fats": fat,
                    "carbs": carb,
                    "source": src,
                }
            )
        elif weight and weight > 0:
            # Calculate using local DB (или 0 для напитков без калорий)
            if is_zero_calorie_drink(name):
                meal_items.append(
                    {
                        "product": name,
                        "weight_g": weight,
                        "weight_source": "llm",
                        "calories": 0.0,
                        "protein": 0.0,
                        "fats": 0.0,
                        "carbs": 0.0,
                        "source": "llm_router_zero_drink",
                    }
                )
            else:
                nutrition = calculate_nutrition(name, weight)
                cal = nutrition["calories"]
                prot = nutrition["protein"]
                fat = nutrition["fats"]
                carb = nutrition["carbs"]
                # БД не знает блюдо — даём грубую оценку (~100–150 ккал/100г для салатов/готовых блюд)
                if cal == 0 and not is_zero_calorie_drink(name):
                    cal = round(weight * 0.12, 1)  # ~120 ккал/100г
                    prot = round(weight * 0.03, 1)
                    fat = round(weight * 0.06, 1)
                    carb = round(weight * 0.12, 1)
                    nutrition = {**nutrition, "calories": cal, "protein": prot, "fats": fat, "carbs": carb}
                meal_items.append(
                    {
                        "product": name,
                        "weight_g": weight,
                        "weight_source": "llm",
                        "calories": nutrition["calories"],
                        "protein": nutrition["protein"],
                        "fats": nutrition["fats"],
                        "carbs": nutrition["carbs"],
                        "basis": nutrition.get("basis", "raw"),
                        "source": "local_db",
                    }
                )
        else:
            # 3. No weight, no macros. Try to estimate!
            from .description_parser import get_default_unit_weight

            try:
                qty = float(item.get("quantity", 1))
            except (ValueError, TypeError):
                qty = 1.0

            if not qty or qty <= 0:
                qty = 1.0

            # Напитки без калорий — не применять грубую оценку
            if is_zero_calorie_drink(name):
                unit_weight = get_default_unit_weight(name) or 330.0  # банка
                estimated_weight = unit_weight * qty
                meal_items.append(
                    {
                        "product": name,
                        "weight_g": estimated_weight,
                        "weight_source": "estimate",
                        "weight_estimated": True,
                        "calories": 0.0,
                        "protein": 0.0,
                        "fats": 0.0,
                        "carbs": 0.0,
                        "source": "llm_router_zero_drink",
                    }
                )
                continue

            unit_weight = get_default_unit_weight(name)

            if unit_weight > 0:
                estimated_weight = unit_weight * qty
                nutrition = calculate_nutrition(name, estimated_weight)

                # Если продукта нет в локальной БД, попробуем данные LLM
                cal = nutrition["calories"]
                prot = nutrition["protein"]
                fat = nutrition["fats"]
                carb = nutrition["carbs"]
                source = "local_db_estimate"

                if cal == 0:
                    # Локальная БД не знает этот продукт — сделаем грубую оценку
                    # (~150 ккал на 100г для среднего готового блюда)
                    cal = round(estimated_weight * 1.5, 1)
                    prot = round(estimated_weight * 0.08, 1)
                    fat = round(estimated_weight * 0.06, 1)
                    carb = round(estimated_weight * 0.12, 1)
                    source = "rough_estimate"
                    logger.info(f"⚠️ Rough estimate for '{name}' ({estimated_weight}g): {cal} kcal")

                meal_items.append(
                    {
                        "product": name,
                        "weight_g": estimated_weight,
                        "weight_source": "estimate",
                        "weight_estimated": True,
                        "calories": cal,
                        "protein": prot,
                        "fats": fat,
                        "carbs": carb,
                        "source": source,
                    }
                )
            else:
                # Совсем не знаем вес — оцениваем порцию по типу блюда (issue #115),
                # а не берём слепые 200г. Срабатывает только когда веса реально нет
                # (вес из vision/regex/LLM приоритетнее и обработан выше по ветке).
                estimated_weight = estimate_default_weight(name)
                cal = round(estimated_weight * 1.5, 1)
                prot = round(estimated_weight * 0.08, 1)
                fat = round(estimated_weight * 0.06, 1)
                carb = round(estimated_weight * 0.12, 1)
                logger.info(f"⚠️ Default portion estimate for '{name}': {estimated_weight}g, {cal} kcal")

                meal_items.append(
                    {
                        "product": name,
                        "weight_g": estimated_weight,
                        "weight_source": "default_portion",
                        "weight_estimated": True,
                        "calories": cal,
                        "protein": prot,
                        "fats": fat,
                        "carbs": carb,
                        "source": "rough_estimate",
                    }
                )

    # Backfill fiber on each meal item:
    #  1) from matching LLM item (if LLM returned fiber), or
    #  2) from static fiber_table fallback by product name + weight.
    # meal_items were built in the same order as input items — safe to zip.
    from .fiber_table import estimate_fiber

    for mi, llm_it in zip(meal_items, items):
        if mi.get("fiber") is not None:
            continue
        llm_fiber = llm_it.get("fiber")
        if llm_fiber is not None:
            try:
                mi["fiber"] = float(llm_fiber)
                continue
            except (TypeError, ValueError):
                pass
        mi["fiber"] = estimate_fiber(mi.get("product") or "", mi.get("weight_g"))

    # Check for explicit total_nutrition from LLM (Recipe Cards/Labels)
    total_nutrition = data.get("total_nutrition")

    # Calculate totals from items first (as baseline)
    computed_totals = calculate_meal_totals(meal_items)

    # ТОЛЬКО для рецептурных карточек (1 продукт с явной этикеткой питания),
    # НЕ для списков блюд (где LLM может вернуть total_nutrition только для последнего продукта)
    dish_name = data.get("dish_name", "")
    first_product = meal_items[0].get("product", "") if meal_items else ""
    if total_nutrition and (total_nutrition.get("calories") or 0) > 0:
        if len(meal_items) == 1 and is_zero_calorie_drink(dish_name or first_product):
            return meal_items, {
                "calories": 0.0,
                "protein": 0.0,
                "fats": 0.0,
                "carbs": 0.0,
                "drinks": 0.0,
                "has_alcohol": False,
            }
        if len(meal_items) == 1:
            # Один продукт с явной этикеткой - используем total_nutrition
            logger.info(f"✅ Using explicit total nutrition from LLM (Recipe/Label): {total_nutrition}")
            return meal_items, {
                "calories": float(total_nutrition.get("calories", 0)),
                "protein": float(total_nutrition.get("protein", 0)),
                "fats": float(total_nutrition.get("fats", 0)),
                "carbs": float(total_nutrition.get("carbs", 0)),
                "fiber": float(total_nutrition.get("fiber", 0) or 0),
                "drinks": float(total_nutrition.get("drinks", 0) or 0),
                "has_alcohol": detect_alcohol(meal_items),
            }
        else:
            # Список блюд - игнорируем total_nutrition, используем вычисленную сумму
            logger.info(
                f"⚠️  Ignoring total_nutrition for multi-item meal (len={len(meal_items)}), using computed totals: {computed_totals}"
            )

    # Sanity-check ккал↔БЖУ + плотность салатов (см. process_meal_description, issue #115).
    has_alcohol = bool(computed_totals.get("has_alcohol")) or detect_alcohol(meal_items)
    warns = _collect_meal_warnings(meal_items, has_alcohol)
    if warns:
        computed_totals["kcal_warnings"] = warns
    return meal_items, computed_totals
