#!/usr/bin/env python3
"""
Улучшенный парсер описаний блюд с поддержкой нескольких фото и весов
"""

import re
from pathlib import Path
from typing import List, Dict, Optional

# Импорт модуля извлечения весов
try:
    from core.vision.weight_extraction import extract_weights_from_photos
except ImportError:

    def extract_weights_from_photos(*args, **kwargs):
        return []


# Вес одной штуки продукта (для оценки, если вес не указан)
DEFAULT_UNIT_WEIGHTS = {
    # Фрукты
    "киви": 75,
    "мандарин": 80,
    "яблоко": 160,
    "груша": 140,
    "банан": 120,
    "апельсин": 150,
    # Яйца
    "яйцо": 55,
    # Овощи
    "томат": 100,
    "помидор": 100,
    "томат черри": 15,
    "черри": 15,
    "лук": 80,
    "картофель": 100,
    "морковь": 75,
    "огурец": 100,
    "перец": 150,
    # Мясо и полуфабрикаты
    "сосиска": 50,
    "котлета": 80,
    "голубец": 200,
    # Выпечка
    "блин": 40,
    "сырник": 50,
    "оладья": 40,
    "пельмень": 12,
    "вареник": 15,
    "хлеб": 30,  # кусок
    "сэндвич": 200,
    "сендвич": 200,
    "бутерброд": 100,
    # Напитки (объем = вес для воды/напитков)
    # ВАЖНО: НЕ добавлять 'вино', 'шампанское', 'просекко' без уточнения (бокал/бутылка)
    # т.к. это создает конфликт в get_default_unit_weight (частичное совпадение)
    "бутылка": 750,  # стандартная бутылка вина/шампанского
    "бутылка шампанского": 750,
    "бутылка вина": 750,
    "бутылка пива": 500,  # стандартная бутылка пива
    "банка": 330,  # стандартная банка пива/напитка
    "банка пива": 500,  # большая банка
    "стакан": 200,  # стандартный стакан (200 мл)
    "рюмка": 50,  # рюмка водки/коньяка
    "шот": 30,  # shot
    "чашка": 250,  # чашка кофе/чая
    "кружка": 300,  # кружка
    "бокал": 150,  # бокал вина
    # Молочные продукты
    "сырок": 45,
    # "сырник": 50,  # дубликат строки 49, удалено
    # Сладости
    "конфета": 15,
    # Спортивное питание
    "батончик bombbar": 40,  # Bombbar протеиновый батончик, 1 шт = 40г
    "батончик бомббар": 40,
    "bombbar": 40,
    # Порции (приблизительные)
    "порция": 200,  # средняя порция
    "тарелка": 250,  # тарелка супа/каши
    # Готовые блюда (1 стандартная порция)
    "суп": 300,
    "борщ": 300,
    "солянка": 300,
    "щи": 300,
    "бульон": 250,
    "салат": 200,
    "плов": 300,
    "пельмени": 250,
    "люля": 100,  # 1 штука
    "кебаб": 100,  # 1 штука
    "шашлык": 200,
    "каша": 250,
    "паста": 250,
    "пицца": 100,  # 1 кусок/слайс
    "стейк": 200,
    "рагу": 250,
    "гуляш": 250,
    "гарнир": 200,
}


def get_default_unit_weight(product_name: str) -> float:
    """Возвращает вес одной штуки продукта, если он известен"""
    if not product_name:
        return 0

    name_lower = product_name.lower().strip()

    # Прямое совпадение
    if name_lower in DEFAULT_UNIT_WEIGHTS:
        return DEFAULT_UNIT_WEIGHTS[name_lower]

    # Поиск по частичному совпадению
    # (но осторожно, чтобы "лук" не нашел "лук порей" с другим весом, хотя тут это приемлемо)
    for key, weight in DEFAULT_UNIT_WEIGHTS.items():
        if key in name_lower:
            return weight

    return 0


# Стандартные порции без веса
PORTION_WEIGHTS = {
    "кусок": 30,
    "ломтик": 20,
    "чайная ложка": 5,
    "столовая ложка": 15,
    "порция": 100,
    "стакан": 200,
    "чашка": 250,
    "бокал": 150,
    "рюмка": 50,
    "стопка": 50,
    "кружка": 300,
    "бутылка": 750,  # стандартная бутылка вина/шампанского
    "банка": 330,
}

# Нормализация названий продуктов
PRODUCT_ALIASES = {
    "куриное филе": ["куриная грудка", "курица", "филе куриное"],
    "замороженная фасоль": ["фасоль замороженная", "фасоль", "фасоль стручковая"],
    "цветная капуста": ["капуста цветная", "капуста"],
    "подсолнечное масло": ["масло подсолнечное", "масло растительное", "масло"],
    "сливочное масло": ["масло сливочное", "сливочного масла", "масло"],
    "соевый соус": ["соус соевый", "соус"],
    "яйцо": ["яйца", "яиц", "яйцо"],
    "томат": ["томат", "томатов", "помидор", "помидоров", "черри"],
    "лук": ["лук", "луковички", "луковицы"],
    "сыр": ["сыр", "сыра"],
}


def is_zero_calorie_drink(name: str) -> bool:
    """
    Определяет, является ли продукт напитком без калорий (Coca-Cola Zero, диет-кола и т.п.).
    Используется для принудительной подстановки 0 ккал/0 У при ошибках LLM.
    """
    if not name or not isinstance(name, str):
        return False
    n = name.lower().strip()
    markers = (
        "zero",
        "зера",
        "зеро",  # coke zero
        "diet ",
        "диет",
        "лайт",
        "light ",
        "без сахара",
        "sugar-free",
        "sugar free",
        "no sugar",
        "безуглеводн",
        "zero calorie",
        "0 ккал",
        "кола нулевая",
        "coca-cola zero",
        "pepsi zero",
    )
    return any(m in n for m in markers)


def _estimate_product_size(product_name: str) -> float:
    """
    Оценивает примерный размер продукта для умного сопоставления весов.
    Большие значения = больший продукт.
    """
    size_map = {
        "куриное филе": 400,
        "курица": 400,
        "куриная грудка": 400,
        "мясо": 300,
        "рыба": 200,
        "фасоль": 150,
        "капуста": 150,
        "цветная капуста": 150,
        "овощи": 100,
    }

    name_lower = product_name.lower()
    for key, size in size_map.items():
        if key in name_lower:
            return size

    return 100  # Средний размер по умолчанию


def determine_product_basis(product_name: str, description: str = "") -> str:
    """
    Определяет basis продукта: cooked, raw, dry, packaged.

    Правила:
    - Rule A: "каша", "гречка готовая", "рис", "макароны" без слова "сухой/крупа" = cooked
    - Rule B: "сухая крупа", "пшено (крупа)", "взвесил сухое", "до варки" = dry
    - Rule C: если неоднозначно - возвращает "ambiguous"

    Args:
        product_name: Название продукта
        description: Полное описание (для контекста)

    Returns:
        'cooked', 'raw', 'dry', 'packaged', или 'ambiguous'
    """
    name_lower = product_name.lower()
    desc_lower = description.lower() if description else ""
    full_context = f"{name_lower} {desc_lower}"

    # Rule B: Явные признаки сухого продукта
    dry_keywords = ["сухой", "сухая", "крупа", "в сухом виде", "до варки", "взвесил сухое", "сухое"]
    if any(keyword in full_context for keyword in dry_keywords):
        return "dry"

    # Rule A: Признаки готового блюда
    cooked_keywords = ["каша", "варёный", "варёная", "готовый", "готовая", "отварной", "отварная", "на воде"]
    if any(keyword in full_context for keyword in cooked_keywords):
        return "cooked"

    # Специфичные продукты - по умолчанию готовые
    cooked_defaults = ["пшённая каша", "гречневая каша", "рис", "макароны", "паста", "спагетти"]
    if any(default in name_lower for default in cooked_defaults):
        # Если нет явных признаков сухого - считаем готовым
        if not any(keyword in full_context for keyword in dry_keywords):
            return "cooked"

    # Rule C: Неоднозначность (например, "200г пшена" без слова каша/готовое)
    ambiguous_patterns = ["пшено", "гречка", "рис", "макароны"]
    if any(pattern in name_lower for pattern in ambiguous_patterns):
        if "каша" not in full_context and "готов" not in full_context and "сух" not in full_context:
            return "ambiguous"

    # По умолчанию - сырой продукт
    return "raw"


def normalize_product_name(name: str, basis: str = None) -> str:
    """
    Нормализует название продукта с учётом basis (cooked/dry).

    Args:
        name: Название продукта
        basis: Basis продукта (cooked/dry/raw) для правильной нормализации

    Returns:
        Нормализованное название
    """
    name = name.strip().lower()

    # Если basis = cooked, добавляем "готовая" к кашам/крупам
    if basis == "cooked":
        # Пшённая каша
        if "пшён" in name or "пшено" in name:
            if "каша" in name or "готов" in name:
                return "пшённая каша готовая"
        # Гречка
        if "греч" in name:
            if "каша" in name or "готов" in name:
                return "гречка готовая"
        # Рис
        if "рис" in name and ("каша" in name or "готов" in name or "варёный" in name):
            return "рис варёный"
        # Макароны
        if "макарон" in name or "паста" in name or "спагетти" in name:
            if "готов" in name or "варён" in name:
                return "макароны варёные"

    # Если basis = dry, добавляем "сухое"
    if basis == "dry":
        if "пшён" in name or "пшено" in name:
            return "пшено сухое"
        if "греч" in name:
            return "гречка сухая"
        if "рис" in name:
            return "рис сухой"
        if "макарон" in name or "паста" in name:
            return "макароны сухие"

    # Нормализация картошки/картофеля (разные падежи и формы)
    if "картош" in name or "картофел" in name:
        # Убираем окончания и приводим к базовой форме
        if "варен" in name or "отварн" in name or "готов" in name:
            return "картофель отварной"
        elif "жарен" in name:
            return "картофель жареный"
        elif "печен" in name or "запечен" in name:
            return "картофель запечённый"
        else:
            return "картофель"

    # Нормализация других продуктов с разными окончаниями
    # Убираем окончания типа "переку", "перекус" и т.д.
    name = re.sub(r"\s*(переку|перекус|обед|завтрак|ужин|бранч|полдник)\s*$", "", name)

    # Проверяем алиасы
    for normalized, aliases in PRODUCT_ALIASES.items():
        if name == normalized or name in aliases:
            return normalized

    return name


def extract_products_from_description(description: str) -> List[Dict[str, any]]:
    """
    Извлекает продукты из описания.

    Args:
        description: Описание блюда

    Returns:
        Список словарей с информацией о продуктах:
        [{'name': 'куриное филе', 'weight': 385.6, 'source': 'photo'}, ...]
    """
    products = []
    description_lower = description.lower()
    added_products = set()  # Для отслеживания уже добавленных продуктов

    # Сначала ищем простые названия продуктов (для случаев типа "кофе к завтраку", "кофе", "чай")
    # Паттерн: название продукта в начале строки или после предлогов
    simple_product_patterns = [
        r"^(кофе|чай|сок|вода|молоко|кефир|йогурт|смузи|напиток)",
        r"(?:к|для|с)\s+(?:завтраку|обеду|ужину|перекусу)\s+(кофе|чай|сок|вода|молоко)",
        r"^(?:это|вот)\s+(кофе|чай|сок|вода|молоко|кефир|йогурт|смузи)",
    ]

    for pattern in simple_product_patterns:
        match = re.search(pattern, description_lower)
        if match:
            product_name = match.group(1) if match.groups() else match.group(0)
            # Нормализуем название
            normalized = normalize_product_name(product_name)
            if normalized and normalized not in added_products:
                products.append({"name": normalized, "weight": None, "source": "description_simple"})
                added_products.add(normalized)
                # Если нашли простой продукт, возвращаем его
                if products:
                    return products

    # Ищем основные продукты из начала описания
    # Паттерн: "Потушил это куриное филе, замороженную фасоль и цветную капусту"
    # Останавливаемся на скобке, "на", "в конце" или новой строке
    # НО: пропускаем, если описание начинается с названия приёма пищи (например, "обед: яичница")
    meal_names_pattern = r"^(обед|завтрак|ужин|перекус|бранч|полдник|ранний\s+ужин|вечерний\s+перекус)\s*[:]"
    if re.match(meal_names_pattern, description_lower):
        # Если описание начинается с приёма пищи - пропускаем этот блок парсинга
        # и переходим к поиску продуктов с весами
        pass
    else:
        main_match = re.search(
            r"(?:тушил|потушил|приготовил|сделал)\s+это\s+(.+?)(?:\(|на\s+\d+|в\s+конце|\n|$)", description_lower
        )
        if not main_match:
            # Пробуем без "это"
            main_match = re.search(
                r"(?:тушил|потушил|приготовил|сделал)\s+(.+?)(?:\(|на\s+\d+|в\s+конце|\n|$)", description_lower
            )

        if main_match:
            main_text = main_match.group(1).strip()
            # Убираем "это" если есть в начале
            main_text = re.sub(r"^\s*это\s+", "", main_text)

            # Разбиваем на продукты по запятым и "и", но сохраняем полные названия
            # Сначала разбиваем по "и" (сохраняем "и" в результате)
            parts = re.split(r"\s+и\s+", main_text)
            all_items = []
            for part in parts:
                # Разбиваем каждую часть по запятым
                items = [i.strip() for i in part.split(",")]
                all_items.extend(items)

            # Фильтруем и нормализуем продукты
            for item in all_items:
                item = item.strip()
                # Игнорируем слишком короткие слова, служебные слова и фразы типа "был ранний ужин"
                if (
                    item
                    and len(item) > 3
                    and item not in ["это", "то", "все", "для"]
                    and not re.match(r"^(был|была|было|это|то|все|для)", item)
                    and "ужин" not in item
                    and "обед" not in item
                    and "завтрак" not in item
                ):
                    normalized = normalize_product_name(item)
                    # Проверяем, что это не часть уже добавленного продукта
                    if normalized not in added_products and len(normalized) > 3:
                        products.append({"name": normalized, "weight": None, "source": "photo_expected"})
                        added_products.add(normalized)

    # Паттерны для поиска продуктов с весами
    # 1. "продукт 150г" или "продукт 150 г" или "150 грамм продукта" или "150 граммов продукта"
    # Ограничиваем: максимум 3 слова для названия продукта, чтобы не захватывать весь текст
    # ВАЖНО: порядок паттернов важен! Сначала ищем "число + грамм + продукт", потом "продукт + число + г"
    # чтобы "178 грамм ячневой каши" не парсилось как "грамм ячневой каш" с весом 178
    weight_patterns = [
        # 1. "30 грамм сыра" или "200 мл молока" - ПЕРВЫЙ приоритет
        # restrict to horizontal whitespace to avoid matching "weight \n next_product"
        r"(\d+(?:[.,]\d+)?)\s*(?:грамм|граммов|г|g|Г|G|мл|миллилитр[а-я]*|л|литр[а-я]*)[ \t]+([а-яёa-zA-Z-]+(?:[ \t]+[а-яёa-zA-Z-]+){0,2})",
        # 2. "сыр 30г" или "кола 200 мл" - support full 'грамм' word
        # Исключаем слова, связанные с весом, из названия продукта
        # restrict to horizontal whitespace to avoid matching "product \n weight" across lines
        r"([а-яёa-zA-Z-]+(?:[ \t]+[а-яёa-zA-Z-]+){0,2})[ \t]+(\d+(?:[.,]\d+)?)\s*(?:грамм|граммов|г|g|Г|G|мл|миллилитр[а-я]*|л|литр[а-я]*)\b",
    ]

    # Список названий приёмов пищи, которые нужно исключить
    meal_names = ["обед", "завтрак", "ужин", "перекус", "бранч", "полдник", "ранний ужин", "вечерний перекус"]

    # Ищем продукты с явными весами
    # ВАЖНО: если описание начинается с приёма пищи, пропускаем первые 50 символов
    # чтобы не захватить "обед:" как продукт
    search_start = 0
    meal_prefix_match = re.match(
        r"^(обед|завтрак|ужин|перекус|бранч|полдник|ранний\s+ужин|вечерний\s+перекус)\s*[:]\s*", description_lower
    )
    if meal_prefix_match:
        # Пропускаем префикс с приёмом пищи
        search_start = meal_prefix_match.end()

    for weight_pattern in weight_patterns:
        for match in re.finditer(weight_pattern, description_lower[search_start:]):
            # Корректируем позицию совпадения
            match_start = match.start() + search_start
            match_end = match.end() + search_start

            if len(match.groups()) == 2:
                # Определяем, где продукт, а где вес
                if re.match(r"^\d", match.group(1)):
                    # "30 грамм сыра" - первая группа это вес
                    weight_str = match.group(1).replace(",", ".")
                    product_name = match.group(2).strip()
                else:
                    # "сыр 30г" - вторая группа это вес
                    product_name = match.group(1).strip()
                    weight_str = match.group(2).replace(",", ".")

                # Очищаем название продукта от лишних слов и двоеточий
                product_name = re.sub(r"\s*(и|или|с|из|для|на|в|:)\s*$", "", product_name).strip()
                product_name = re.sub(r"^[:\s]+", "", product_name).strip()  # Убираем двоеточие в начале

                # ВАЖНО: Убираем слова, связанные с весом, из названия продукта
                # Это может произойти, если regex захватил "грамм" как часть названия
                product_name = re.sub(
                    r"^(грамм|граммов|г|g|мл|миллилитр[а-я]*|л|литр[а-я]*)\s+", "", product_name, flags=re.IGNORECASE
                ).strip()
                product_name = re.sub(
                    r"\s+(грамм|граммов|г|g|мл|миллилитр[а-я]*|л|литр[а-я]*)$", "", product_name, flags=re.IGNORECASE
                ).strip()

                # Убираем фразы типа "добавь к обеду", "к ужину" и т.д.
                product_name = re.sub(r"^(?:добавь|плюс|еще|ещё)\s+", "", product_name, flags=re.IGNORECASE).strip()
                product_name = re.sub(
                    r"^(?:к|для|на)\s+(?:обед|ужин|завтрак|перекус|полдник)[у-я]*\s+",
                    "",
                    product_name,
                    flags=re.IGNORECASE,
                ).strip()

                # Пропускаем, если это название приёма пищи (проверяем точное совпадение и частичное)
                product_lower = product_name.lower()
                if product_lower in meal_names:
                    continue
                # Проверяем, не начинается ли название с приёма пищи (например, "обед: яичница")
                if any(product_lower.startswith(meal) or meal in product_lower for meal in meal_names):
                    # Если это только название приёма пищи без других слов - пропускаем
                    if len(product_lower.split()) == 1 or product_lower.split()[0] in meal_names:
                        continue

                try:
                    weight = float(weight_str)
                    normalized = normalize_product_name(product_name)
                    if normalized and len(normalized) > 2:
                        # Дополнительная проверка - не является ли это названием приёма пищи
                        normalized_lower = normalized.lower()
                        if normalized_lower not in meal_names and not any(
                            normalized_lower.startswith(meal) for meal in meal_names
                        ):
                            # Проверяем дубликаты по нормализованному названию и весу
                            # Улучшенная проверка: сравниваем базовые слова продуктов
                            is_duplicate = False
                            for existing_product in products:
                                existing_name = existing_product.get("name", "").lower()
                                existing_weight = existing_product.get("weight", 0)

                                # Проверка 1: точное совпадение названий
                                if normalized_lower == existing_name:
                                    is_duplicate = True
                                    break

                                # Проверка 2: одно название содержит другое (минимум 5 символов совпадения)
                                # Например: "ячневой каш" vs "ячневая каша" или "сайры" vs "консервированной сайры"
                                if len(normalized_lower) >= 5 and len(existing_name) >= 5:
                                    # Извлекаем базовые слова (первые 5+ символов каждого слова)
                                    norm_words = [w[:5] for w in normalized_lower.split() if len(w) >= 4]
                                    exist_words = [w[:5] for w in existing_name.split() if len(w) >= 4]

                                    # Если есть пересечение базовых слов и близкие веса
                                    if norm_words and exist_words:
                                        common_words = set(norm_words) & set(exist_words)
                                        if common_words and abs(weight - existing_weight) < 10:
                                            is_duplicate = True
                                            break

                            if not is_duplicate:
                                products.append({"name": normalized, "weight": weight, "source": "description"})
                                added_products.add(normalized)
                except ValueError:
                    continue

    # Преобразование текстовых числительных в цифры
    def replace_text_numbers(text):
        text_numbers = {
            "один": "1",
            "одна": "1",
            "одно": "1",
            "два": "2",
            "две": "2",
            "двух": "2",
            "три": "3",
            "трёх": "3",
            "трех": "3",
            "четыре": "4",
            "четырёх": "4",
            "четырех": "4",
            "пять": "5",
            "пяти": "5",
            "шесть": "6",
            "семь": "7",
            "восемь": "8",
            "девять": "9",
            "десять": "10",
            "пол": "0.5",
            "половину": "0.5",
            "половина": "0.5",
            "половинку": "0.5",
        }

        # Разбиваем слитные "пол-" формы: "полгруши" → "0.5 груши", "полбанана" → "0.5 банана"
        # Исключаем самостоятельные слова: половина, половину, половинку, полностью, положить и т.д.
        text = re.sub(r"\bпол(?!ов|ност|ожи|учи|ураз|оск)([а-яё]{3,})", r"0.5 \1", text)

        words = text.split()
        new_words = []
        for word in words:
            word_lower = word.lower()
            if word_lower in text_numbers:
                new_words.append(text_numbers[word_lower])
            else:
                new_words.append(word)
        return " ".join(new_words)

    # Предварительная обработка описания: заменяем текстовые числа
    description_lower = replace_text_numbers(description_lower)

    # Специальные продукты с количеством: яйца, томаты и т.д.
    # NOTE: используем \d+(?:[.,]\d+)? чтобы матчить дробные числа (0.5 груши → половина)
    _N = r"(\d+(?:[.,]\d+)?)"  # число (целое или дробное)
    # Число-счётчик штук: за ним НЕ должна идти единица веса/объёма.
    # Иначе "73 г перца" матчится как 73 ШТУКИ перца (группа [а-яё]+ съедает "г")
    # → 73×150г = 10950г (инцидент 12.07.2026, клетчатка 230г в дневнике).
    _N_CNT = _N + r"(?!\s*(?:г|гр|грамм[а-я]*|кг|килограмм[а-я]*|мл|миллилитр[а-я]*|л|литр[а-я]*)\b)"
    quantity_patterns = [
        (r"(\d+(?:[.,]\d+)?(?:-х|-и|-о)?)\s*(?:шт|штук|шт\.)?\s*(?:яйц|яиц)", 55, "яйцо"),
        (_N + r"\s*(?:томат|томатов|помидор|помидоров)", 100, "томат"),
        (_N + r"\s*(?:томат[а-я]*\s+)?(?:черри)", 15, "томат черри"),
        # Fruits
        (_N + r"\s*(?:киви)", 75, "киви"),
        (_N + r"\s*(?:мандарин[а-я]*)", 80, "мандарин"),
        (_N + r"\s*(?:яблок[а-я]*)", 160, "яблоко"),
        (_N + r"\s*(?:груш[а-я]*)", 140, "груша"),
        (_N + r"\s*(?:банан[а-я]*)", 120, "банан"),
        (_N + r"\s*(?:апельсин[а-я]*)", 150, "апельсин"),
        (r"маленьк[а-я]*\s*(?:луковичк|лук)", 50, "лук"),
        (r"средн[а-я]*\s*(?:луковичк|лук)", 80, "лук"),
        (r"больш[а-я]*\s*(?:луковичк|лук)", 120, "лук"),
        (
            _N_CNT + r"\s*(?:[а-яё]+\s+)?(?:перц[а-я]*|перец)",
            150,
            "перец фаршированный" if "тушон" in description_lower or "фарш" in description_lower else "перец",
        ),
        (_N_CNT + r"\s*(?:[а-яё]+\s+)?(?:голубц[а-я]*|голубец)", 200, "голубцы"),
        (_N_CNT + r"\s*(?:[а-яё]+\s+)?(?:котлет[а-я]*)", 80, "котлета"),
        (_N_CNT + r"\s*(?:[а-яё]+\s+)?(?:сосиск[а-я]*)", 50, "сосиска"),
        (_N_CNT + r"\s*(?:[а-яё]+\s+)?(?:сырник[а-яё]*)", 50, "сырник"),
        (_N_CNT + r"\s*(?:[а-яё]+\s+)?(?:оладь[а-яё]*)", 40, "оладья"),
        # Bombbar протеиновые батончики
        (
            _N + r"\s*(?:протеинов[а-я]*\s+)?батончик[а-яё]*\s+(?:bombbar|бомббар|бомбар|bomb\s*bar)",
            40,
            "батончик bombbar",
        ),
        (
            _N + r"\s*(?:bombbar|бомббар|бомбар|bomb\s*bar)\s+(?:протеинов[а-я]*\s+)?батончик[а-яё]*",
            40,
            "батончик bombbar",
        ),
    ]

    for pattern, weight_per_unit, product_name in quantity_patterns:
        for match in re.finditer(pattern, description_lower):
            count = 1  # По умолчанию 1
            if match.groups():
                count_str = match.group(1)
                # Извлекаем число из "2-х" или "2" или "0.5" (для текстовых "пол/половина")
                count_match = re.search(r"(\d+(?:[.,]\d+)?)", count_str)
                if count_match:
                    count = float(count_match.group(1).replace(",", "."))

            total_weight = weight_per_unit * count
            normalized = normalize_product_name(product_name)
            if normalized and normalized not in added_products:
                products.append({"name": normalized, "weight": total_weight, "source": "quantity_estimate"})
                added_products.add(normalized)

    # Специальная обработка Bombbar без числа
    bombbar_re = r"(?:bombbar|бомббар|бомбар|bomb\s*bar)"

    # "половина батончика Bombbar", "полбатончика Bombbar", "пол батончика bombbar"
    half_bombbar_pattern = (
        r"(?:половин[а-я]*|пол)[\s\-]?батончик[а-яё]*\s+" + bombbar_re + r"|"
        r"(?:половин[а-я]*|пол)[\s\-]?" + bombbar_re + r"\s+батончик[а-яё]*|"
        r"батончик[а-яё]*\s+" + bombbar_re + r"\s+(?:половин[а-я]*)|"
        r"" + bombbar_re + r"[\s\-]?(?:половин[а-я]*|полбатончик[а-яё]*)"
    )
    if re.search(half_bombbar_pattern, description_lower) and "батончик bombbar" not in added_products:
        products.append({"name": "батончик bombbar", "weight": 20.0, "source": "quantity_estimate"})
        added_products.add("батончик bombbar")

    # "батончик Bombbar" или "Bombbar батончик" без числа (1 шт = 40г)
    single_bombbar_pattern = (
        r"(?<!\d\s)(?<!\d)батончик[а-яё]*\s+" + bombbar_re + r"|"
        r"" + bombbar_re + r"\s+(?:протеинов[а-я]*\s+)?батончик[а-яё]*"
    )
    if re.search(single_bombbar_pattern, description_lower) and "батончик bombbar" not in added_products:
        products.append({"name": "батончик bombbar", "weight": 40.0, "source": "quantity_estimate"})
        added_products.add("батончик bombbar")

    # Порции без веса: "чайная ложка масла", "кусок хлеба"
    portion_patterns = [
        (r"(\d+)\s*чайн[а-я]*\s*ложк[а-я]*\s+([а-яё\s]+)", PORTION_WEIGHTS["чайная ложка"]),
        (r"чайн[а-я]*\s*ложк[а-я]*\s+([а-яё\s]+)", PORTION_WEIGHTS["чайная ложка"]),  # Implied 1
        (r"(\d+)\s*столов[а-я]*\s*ложк[а-я]*\s+([а-яё\s]+)", PORTION_WEIGHTS["столовая ложка"]),
        (r"столов[а-я]*\s*ложк[а-я]*\s+([а-яё\s]+)", PORTION_WEIGHTS["столовая ложка"]),  # Implied 1
        (
            r"(?:маленьк[а-я]*|небольш[а-я]*)\s*кусочк[а-я]*\s+([а-яё\s]+)",
            PORTION_WEIGHTS["кусок"] * 0.5,
        ),  # "маленький кусочек" = 15г
        (
            r"(?:маленьк[а-я]*|небольш[а-я]*)\s*кусок\s+([а-яё\s]+)",
            PORTION_WEIGHTS["кусок"] * 0.5,
        ),  # "маленький кусок" = 15г
        (r"кусок\s+([а-яё\s]+)", PORTION_WEIGHTS["кусок"]),
        (r"ломтик\s+([а-яё\s]+)", PORTION_WEIGHTS["ломтик"]),
        (r"порция\s+([а-яё\s]+)", PORTION_WEIGHTS["порция"]),
        # Новые паттерны
        (r"(\d+)\s*бокал[а-я]*\s+([а-яё\s]+)", PORTION_WEIGHTS["бокал"]),
        (r"бокал\s+([а-яё\s]+)", PORTION_WEIGHTS["бокал"]),
        (r"(\d+)\s*стакан[а-я]*\s+([а-яё\s]+)", PORTION_WEIGHTS["стакан"]),
        (r"стакан\s+([а-яё\s]+)", PORTION_WEIGHTS["стакан"]),
        (r"(\d+)\s*кружк[а-я]*\s+([а-яё\s]+)", PORTION_WEIGHTS["кружка"]),
        (r"кружка\s+([а-яё\s]+)", PORTION_WEIGHTS["кружка"]),
        (r"(\d+)\s*бутылк[а-я]*\s+([а-яё\s]+)", PORTION_WEIGHTS["бутылка"]),
        (r"бутылка\s+([а-яё\s]+)", PORTION_WEIGHTS["бутылка"]),
        (r"(\d+)\s*банк[а-я]*\s+([а-яё\s]+)", PORTION_WEIGHTS["банка"]),
        (r"банка\s+([а-яё\s]+)", PORTION_WEIGHTS["банка"]),
    ]

    # Ищем порции без веса
    for pattern, default_weight in portion_patterns:
        for match in re.finditer(pattern, description_lower):
            if len(match.groups()) == 2:
                # "2 чайных ложки масла"
                count = int(match.group(1))
                product_name = match.group(2).strip()
                weight = default_weight * count
            else:
                # "кусок хлеба" или "маленький кусок масла"
                product_name = match.group(1).strip()
                weight = default_weight

            # Очищаем название продукта от лишних слов
            product_name = re.sub(r"\s+(и|или|с|из|для|на|в)\s*$", "", product_name).strip()

            normalized = normalize_product_name(product_name)
            if normalized and normalized not in added_products and len(normalized) > 2:
                products.append({"name": normalized, "weight": weight, "source": "portion_estimate"})
                added_products.add(normalized)

    return products


def parse_meal_description(
    description: str, photo_paths: Optional[List[Path]] = None, api_key: Optional[str] = None
) -> List[Dict[str, any]]:
    """
    Парсит описание блюда и извлекает информацию о продуктах.
    Сначала пробует ChatGPT API для умного распознавания, затем fallback на regex парсер.

    Args:
        description: Описание блюда
        photo_paths: Список путей к фото (опционально)
        api_key: API ключ Google Cloud Vision (опционально)

    Returns:
        Список продуктов с весами:
        [{'name': 'куриное филе', 'weight': 385.6, 'source': 'photo'}, ...]
    """
    # ВСЕГДА пробуем распознать фото как меню, если есть фото
    # Это важно, потому что даже с описанием "кофе к завтраку" фото может быть меню с КБЖУ
    if photo_paths and len(photo_paths) == 1:
        try:
            from .menu_parser import parse_menu_photo

            menu_data = parse_menu_photo(photo_paths[0], api_key, description=description)
            if menu_data and (menu_data.get("calories", 0) > 0 or menu_data.get("components")):
                # Это меню! Возвращаем как продукт (приоритет меню над описанием)
                print(f"    ✅ Распознано меню из фото: {menu_data.get('dish_name')}")

                # Если есть разбивка по компонентам - используем её
                components = menu_data.get("components")
                if components and isinstance(components, list) and len(components) > 0:
                    print(f"    🧩 Найдено {len(components)} компонентов в меню")
                    component_products = []
                    for comp in components:
                        comp_name = comp.get("name")
                        if comp_name:
                            # Нормализуем имя
                            try:
                                deep_norm = normalize_product_name(comp_name)
                            except Exception:
                                deep_norm = comp_name

                            prod = {
                                "name": deep_norm,
                                "weight": comp.get("weight"),
                                "source": "menu_ocr_component",
                                "menu_data": menu_data,  # Ссылка на родительское меню
                            }
                            # Если у компонента есть КБЖУ, добавляем
                            if comp.get("calories"):
                                prod["calories"] = comp.get("calories")
                                prod["protein"] = comp.get("protein", 0)
                                prod["fats"] = comp.get("fats", 0)
                                prod["carbs"] = comp.get("carbs", 0)

                            component_products.append(prod)

                    if component_products:
                        return component_products

                print(
                    f"       КБЖУ из меню: {menu_data.get('calories')} ккал, "
                    f"Б: {menu_data.get('protein')}г, "
                    f"Ж: {menu_data.get('fats')}г, "
                    f"У: {menu_data.get('carbs')}г"
                )
                return [
                    {
                        "name": menu_data.get("dish_name") or description or "Блюдо из меню",
                        "weight": menu_data.get("weight"),
                        "calories": menu_data.get("calories", 0),
                        "protein": menu_data.get("protein", 0),
                        "fats": menu_data.get("fats", 0),
                        "carbs": menu_data.get("carbs", 0),
                        "source": "menu_ocr",
                        "menu_data": menu_data,  # Сохраняем полные данные меню
                    }
                ]
        except Exception as e:
            # Если не получилось распознать как меню - продолжаем обычный парсинг
            print(f"    ⚠️  Не удалось распознать как меню: {e}")
            pass

    products = []

    # 1. Сначала пробуем regex парсер
    # Это важно для приоритета жестких правил (например, 3 перца = 450г, а не то что решит AI)
    regex_products = extract_products_from_description(description)

    # Если regex нашел продукты с явным количеством (quantity_estimate) - доверяем ему больше чем AI
    # (так как мы специально прописали веса для шт/порций)
    # DISABLE OPTIMIZATION: ChatGPT is smarter. Regex missed "teaspoon of oil" because it lacked a number.
    # if regex_products and any(p.get('source') == 'quantity_estimate' for p in regex_products):
    #     print(f"    ✅ Regex нашел явные количества ({len(regex_products)} продуктов), пропускаем ChatGPT")
    #     # Но также проверяем, не пропустили ли мы что-то важное, что нашел бы ChatGPT?
    #     # В данном случае считаем, что если пользователь указал "2 перца", он хочет именно 2 перца с нашим весом
    #     return regex_products

    # 2. Пробуем ChatGPT API для обработки текстового описания (если regex не нашел quantity_estimate)
    try:
        from .chatgpt_vision import parse_text_description_with_chatgpt, get_openai_api_key

        openai_key = get_openai_api_key()
        if openai_key and description and len(description.strip()) > 2:
            # Пробуем ChatGPT для описаний длиннее 2 символов
            chatgpt_products = parse_text_description_with_chatgpt(description, openai_key)
            if chatgpt_products and len(chatgpt_products) > 0:
                # Дедупликация (дополнительная проверка)
                unique_gpt = []
                seen_gpt = set()
                for p in chatgpt_products:
                    # Нормализуем название
                    norm_name = p["name"].lower().strip()
                    # Если название из двух слов и более, пробуем также нормализовать через функцию
                    # но сохраняем оригинальное имя в продукте
                    if len(norm_name.split()) > 1:
                        # Например "картофель жареный" -> "картофель жареный"
                        # "жареная картошка" -> "картофель жареный"
                        # Это поможет убрать дубликаты с разными названиями но одним смыслом
                        try:
                            deep_norm = normalize_product_name(norm_name)
                        except Exception:
                            deep_norm = norm_name
                    else:
                        deep_norm = norm_name

                    weight_key = round(p["weight"], 1)
                    key = (deep_norm, weight_key)

                    if key not in seen_gpt:
                        seen_gpt.add(key)
                        unique_gpt.append(p)
                    else:
                        print(f"    ⚠️  Убран дубликат (в parser): {p['name']} ({p['weight']}г)")

                products = unique_gpt
                print(f"    ✅ Используем продукты из ChatGPT ({len(products)} продуктов)")
            else:
                print("    ⚠️  ChatGPT не распознал продукты, используем regex парсер...")
        else:
            if not openai_key:
                print("    ℹ️  OpenAI API ключ не найден, используем regex парсер...")
    except ImportError:
        print("    ℹ️  ChatGPT Vision модуль недоступен, используем regex парсер...")
    except Exception as e:
        print(f"    ⚠️  Ошибка при использовании ChatGPT: {e}, используем regex парсер...")

    # Fallback: извлекаем продукты из описания через regex парсер, если ChatGPT не справился
    if not products:
        products = extract_products_from_description(description)

    # Если есть фото и (упоминание весов или отсутствие веса у продуктов) - пробуем найти веса на фото
    # Эвристика: если веса нет ни у одного продукта, скорее всего он на фото
    if photo_paths and (
        "вес на фото" in description.lower() or not any(p.get("weight") and p["weight"] > 0 for p in products)
    ):
        try:
            # Сортируем веса по убыванию для сопоставления с продуктами по размеру
            # (предполагаем, что больший продукт весит больше)
            raw_weights = extract_weights_from_photos(photo_paths, api_key)
        except Exception as e:
            print(f"    ⚠️  Ошибка при извлечении весов из фото: {e}")
            raw_weights = []  # Продолжаем без весов из фото

        photo_weights = [w for w in raw_weights if w is not None]

        if photo_weights:
            # Убираем дубликаты весов (если OCR распознал одинаковые веса)
            # НО: сохраняем веса, которые близки, но не идентичны (например, 163.9 и 145.3)
            unique_weights = []
            seen_weights = set()
            for w in photo_weights:
                if w is None:
                    continue
                # Округляем до 0.1 для сравнения (чтобы 163.9 и 163.90 считались одинаковыми)
                w_rounded = round(w, 1)

                # Проверяем, не является ли это дубликатом
                is_duplicate = False
                for seen in seen_weights:
                    # Если разница меньше 0.5г - считаем дубликатом
                    if abs(w_rounded - seen) < 0.5:
                        is_duplicate = True
                        print(f"    ⚠️  Пропущен дубликат веса: {w}г (близко к {seen}г)")
                        break

                if not is_duplicate:
                    unique_weights.append(w)
                    seen_weights.add(w_rounded)

            print(f"    📊 Уникальных весов: {len(unique_weights)} из {len(photo_weights)}")

            # Находим продукты, которые ожидают вес из фото (или пришли из chatgpt без веса)
            photo_expected_products = [
                p
                for p in products
                if p["source"] == "photo_expected"
                or (p["source"] == "chatgpt" and (not p.get("weight") or p["weight"] == 0))
            ]

            # Также проверяем продукты с весом из описания - возможно, нужно обновить
            # если в описании сказано "есть вес на фото для всех компонентов"
            if "для всех" in description.lower() or "по отдельности" in description.lower():
                # Пытаемся сопоставить веса продуктам
                # Считаем "основными" продуктами те, которые извлечены из описания (или ожидаются на фото)
                # и не являются типичными "довесками" вроде масла для жарки (если их вес явно не указан)
                main_products = [
                    p
                    for p in products
                    if p["source"] in ["description", "photo_expected", "chatgpt"]
                    and p["name"] not in ["подсолнечное масло", "соевый соус", "масло", "соус"]
                ]

                print(f"    📋 Основных продуктов: {len(main_products)}")
                for i, p in enumerate(main_products):
                    print(f"      {i + 1}. {p['name']}")

                print(f"    ⚖️  Весов из фото: {len(unique_weights)}")
                for i, w in enumerate(unique_weights):
                    print(f"      {i + 1}. {w}г")

                # Умное сопоставление: сортируем веса по убыванию и сопоставляем с продуктами
                # Обычно больший продукт (курица) имеет больший вес
                sorted_weights = sorted(unique_weights, reverse=True)
                sorted_products = sorted(main_products, key=lambda p: _estimate_product_size(p["name"]), reverse=True)

                print("    🔄 Сортировка:")
                print(f"      Продукты (по размеру): {[p['name'] for p in sorted_products]}")
                print(f"      Веса (по убыванию): {sorted_weights}")

                # Создаём словарь сопоставления
                weight_map = {}
                for i, product in enumerate(sorted_products):
                    if i < len(sorted_weights):
                        weight_map[product["name"]] = sorted_weights[i]

                # Применяем веса к продуктам в исходном порядке
                for product in main_products:
                    if product["name"] in weight_map:
                        old_weight = product.get("weight")
                        product["weight"] = weight_map[product["name"]]
                        product["source"] = "photo"
                        print(f"    ✅ {product['name']}: {old_weight} -> {weight_map[product['name']]}г")
                    else:
                        print(f"    ⚠️  {product['name']}: вес не присвоен (нет фото)")
            else:
                # Обычное сопоставление: только продукты без веса
                for i, product in enumerate(photo_expected_products):
                    if i < len(unique_weights):
                        old_weight = product.get("weight")
                        product["weight"] = unique_weights[i]
                        product["source"] = "photo"
                        print(f"    ✅ {product['name']}: {old_weight} -> {unique_weights[i]}г")

    # Фильтруем продукты без веса (если не удалось извлечь)
    # и используем стандартную порцию ТОЛЬКО если это не основные продукты с фото
    for product in products:
        if product["weight"] is None:
            # Если это продукт, который должен был получить вес с фото, но не получил
            # используем стандартную порцию, но логируем это
            if product["source"] == "photo_expected":
                print(f"⚠️  Не удалось извлечь вес для {product['name']} с фото, используем стандартную порцию")
            product["weight"] = PORTION_WEIGHTS.get("порция", 100)
            product["source"] = "default_portion"

    return products


def extract_explicit_totals(text: str) -> Optional[Dict[str, float]]:
    """
    Извлекает явные итоговые значения КБЖУ из текста.
    Например: "Калории: 1200 ккал, Б: 100, Ж: 50, У: 200"

    Args:
        text: Текст описания

    Returns:
        Словарь с найденными значениями или None
    """
    if not text:
        return None

    text_lower = text.lower()
    totals = {}

    # Паттерны для калорий
    # "Калории: 810" или "ккал: 810" или "810 ккал" (если число большое)
    cal_match = re.search(r"(?:калории|ккал|energ[yi]|calork?ies?)[:\s]*(\d+(?:[.,]\d+)?)", text_lower)
    if cal_match:
        try:
            totals["calories"] = float(cal_match.group(1).replace(",", "."))
        except ValueError:
            pass

    # Паттерны для белков
    prot_match = re.search(r"(?:белки?|белок|протеин|prot[eiin]*|б)[:\s]*(\d+(?:[.,]\d+)?)", text_lower)
    if prot_match:
        try:
            totals["protein"] = float(prot_match.group(1).replace(",", "."))
        except ValueError:
            pass

    # Паттерны для жиров
    fat_match = re.search(r"(?:жиры?|жир|fat|ж)[:\s]*(\d+(?:[.,]\d+)?)", text_lower)
    if fat_match:
        try:
            totals["fats"] = float(fat_match.group(1).replace(",", "."))
        except ValueError:
            pass

    # Паттерны для углеводов
    carb_match = re.search(r"(?:углеводы?|угли|carb[ohydrates]*|у)[:\s]*(\d+(?:[.,]\d+)?)", text_lower)
    if carb_match:
        try:
            totals["carbs"] = float(carb_match.group(1).replace(",", "."))
        except ValueError:
            pass

    # Возвращаем только если нашли хотя бы калории или все БЖУ
    if "calories" in totals or ("protein" in totals and "fats" in totals and "carbs" in totals):
        return totals

    return None


def apply_portion_multiplier(products: List[Dict], multiplier: float) -> List[Dict]:
    """
    Применяет множитель порции ко всем продуктам.

    Args:
        products: Список продуктов
        multiplier: Множитель (например, 0.5 для половины)

    Returns:
        Список продуктов с обновленными весами
    """
    result = []
    for product in products:
        product_copy = product.copy()
        if product_copy.get("weight") is not None:
            product_copy["weight"] = product_copy["weight"] * multiplier

        # Also scale macros if they exist (e.g. from menu OCR)
        for macro in ["calories", "protein", "fats", "carbs"]:
            if product_copy.get(macro) is not None:
                # Ensure we handle 0 correctly
                if isinstance(product_copy[macro], (int, float)):
                    product_copy[macro] = round(product_copy[macro] * multiplier, 1)

        result.append(product_copy)
    return result


if __name__ == "__main__":
    # Тестирование
    test_description = """Потушил это куриное филе, замороженную фасоль и цветную капусту
(есть вес на фото для всех трёх компонентов по отдельности)
на 1 чайной ложке подсолнечного масла,
в конце влил 2 чайных ложки соевого соуса.
Съел половину блюда - вторую съела моя жена."""

    print("Тестирование парсера описания:")
    print(f"Описание: {test_description}\n")

    products = parse_meal_description(test_description)
    print("Извлеченные продукты:")
    for product in products:
        print(f"  - {product['name']}: {product['weight']}г (источник: {product['source']})")

    print("\nПосле применения множителя 0.5:")
    products_with_multiplier = apply_portion_multiplier(products, 0.5)
    for product in products_with_multiplier:
        print(f"  - {product['name']}: {product['weight']}г")
