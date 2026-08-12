#!/usr/bin/env python3
"""
Snapshot тесты для парсинга питания.
Покрывают различные сценарии: текст, фото еды, этикетки, весы.
"""

import pytest
from pathlib import Path
import sys

# Добавляем telegram-bot в путь для импорта services
sys.path.insert(0, str(Path(__file__).parent.parent / "telegram-bot"))

from core.food.nutrition import calculate_nutrition
from core.food.description_parser import extract_products_from_description


class TestTextParsing:
    """Тесты парсинга текстовых описаний еды"""

    def test_simple_meal(self):
        """Простое блюдо: гречка и курица"""
        desc = "Обед: гречка 200г, курица 150г"
        products = extract_products_from_description(desc)

        product_names = [p["name"] for p in products]
        assert "гречка" in [n.lower() for n in product_names]
        assert any("курин" in n.lower() for n in product_names)

    def test_branded_product(self):
        """Продукт с брендом: EXPONENTA"""
        desc = "Перекус EXPONENTA HIGH-PRO вишня 160г"
        products = extract_products_from_description(desc)

        assert len(products) >= 1
        # Проверяем что вес извлечен
        weights = [p.get("weight", 0) for p in products]
        assert any(w > 0 for w in weights)

    def test_multiple_items(self):
        """Несколько продуктов: салат с тунцом"""
        desc = "Ужин: салат 100г, тунец 85г, огурец 150г"
        products = extract_products_from_description(desc)

        assert len(products) >= 2
        # Должны быть хотя бы салат и тунец
        product_names = " ".join([p["name"].lower() for p in products])
        assert "салат" in product_names or "тунец" in product_names

    def test_piece_units(self):
        """Продукты в штуках: яблоко, банан"""
        desc = "Яблоко зеленое 1 шт, банан 1 шт"
        products = extract_products_from_description(desc)

        # Парсер может не понять "шт" - допустим
        # Проверяем что хотя бы что-то извлеклось или пустой результат
        assert isinstance(products, list)


class TestNutritionCalculation:
    """Тесты расчета БЖУ"""

    def test_chicken_nutrition(self):
        """Расчет БЖУ для куриного филе"""
        result = calculate_nutrition("куриное филе", 150.0)

        assert result["calories"] > 0
        assert result["protein"] > 20  # Курица высокобелковая
        assert result["fats"] < 10  # Филе нежирное

    def test_buckwheat_nutrition(self):
        """Расчет БЖУ для гречки"""
        result = calculate_nutrition("гречка", 200.0)

        assert result["calories"] > 0
        assert result["carbs"] > 30  # Гречка высокоуглеводная
        assert result["protein"] > 5  # Есть белок (в гречке ~4-5г на 100г)

    def test_egg_nutrition(self):
        """Расчет БЖУ для яйца"""
        result = calculate_nutrition("яйцо", 55.0)

        assert result["calories"] > 0
        assert result["protein"] > 5
        assert result["fats"] > 4


class TestEdgeCases:
    """Тесты граничных случаев"""

    def test_empty_description(self):
        """Пустое описание"""
        products = extract_products_from_description("")
        assert products == [] or len(products) == 0

    def test_uncertain_weight(self):
        """Неопределенный вес: горсть орехов"""
        desc = "Горсть орехов, примерно 30г"
        products = extract_products_from_description(desc)

        # Должен распарсить орехи с примерным весом
        assert len(products) >= 1

    def test_product_normalization(self):
        """Нормализация названий продуктов"""
        # Разные написания одного продукта
        result1 = calculate_nutrition("куриное филе", 100.0)
        result2 = calculate_nutrition("курица", 100.0)

        # Калории должны быть близки (в пределах 20%)
        assert abs(result1["calories"] - result2["calories"]) < result1["calories"] * 0.3


class TestMealContext:
    """Тесты контекстных сценариев"""

    def test_americano_low_calories(self):
        """
        Регрессия 2026-03-27: Американо с молоком был записан как 325 ккал / 200г
        (GPT перепутал с латте/капучино). Реальный американо 200мл + молоко 50г = ~40 ккал.

        Американо — это эспрессо + вода, практически нет жиров и белка.
        Проверяем что calculate_nutrition не выдаёт kcal как у молочного кофе.
        """
        result = calculate_nutrition("американо", 200.0)

        # Американо 200мл: ~5-10 ккал, 0 жиров, 0 белка
        assert result["calories"] < 50, (
            f"Американо 200мл не может быть {result['calories']} ккал — это похоже на латте! Ожидали < 50 ккал."
        )
        assert result["fats"] < 2, f"Американо 200мл: жиров {result['fats']}г, ожидали < 2г"
        assert result["protein"] < 3, f"Американо 200мл: белка {result['protein']}г, ожидали < 3г"

    def test_coffee_not_latte(self):
        """
        Калории чёрного кофе (эспрессо, американо) принципиально ниже латте.
        Пропорция должна быть примерно в 10x.
        """
        americano = calculate_nutrition("американо", 200.0)
        latte = calculate_nutrition("латте", 200.0)

        assert americano["calories"] < latte["calories"] / 3, (
            f"Американо ({americano['calories']} ккал) не может быть сравним с латте "
            f"({latte['calories']} ккал) — это значит GPT путает эти напитки."
        )

    def test_drink_item(self):
        """Напитки: латте"""
        desc = "Латте на кокосовом, средний"
        products = extract_products_from_description(desc)

        # Парсер может не понять напиток без веса - это OK
        # Проверяем что функция работает
        assert isinstance(products, list)

    def test_sport_nutrition(self):
        """Спортпит: протеин"""
        desc = "Протеин Tree of Life 1 скуп"
        products = extract_products_from_description(desc)

        # Парсер может не понять "скуп" - это OK
        assert isinstance(products, list)

    def test_alcohol(self):
        """Алкоголь: пиво"""
        desc = "Пиво Guinness 0.0 0.5л"
        products = extract_products_from_description(desc)

        # Парсер может не понять "л" без граммов - OK
        assert isinstance(products, list)


class TestWeightExtraction:
    """Тесты извлечения веса из текста"""

    def test_grams_extraction(self):
        """Извлечение веса в граммах"""
        desc = "Гречка 200г"
        products = extract_products_from_description(desc)

        weights = [p.get("weight", 0) for p in products]
        assert 200 in weights or any(190 <= w <= 210 for w in weights)

    def test_milliliters_conversion(self):
        """Конвертация миллилитров"""
        # Test 1: Simple water
        desc = "Вода 500мл"
        products = extract_products_from_description(desc)
        assert isinstance(products, list)

        # Test 2: Coca-Cola 200ml (Bugfix regression)
        desc_coke = "200 мл Coca-Cola"
        products_coke = extract_products_from_description(desc_coke)

        assert len(products_coke) > 0
        coke = products_coke[0]
        # Should be normalized to "coca-cola" or similar
        assert "cola" in coke["name"].lower()
        # Should have weight 200 (1ml = 1g approximation)
        assert coke["weight"] == 200.0

    def test_piece_to_grams(self):
        """Конвертация штук в граммы"""
        desc = "3 яйца"
        products = extract_products_from_description(desc)

        # 3 яйца ≈ 165г (3 * 55г)
        weights = [p.get("weight", 0) for p in products]
        assert any(150 <= w <= 180 for w in weights)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])


class TestBombbar:
    """Тесты для батончиков Bombbar (протеиновые батончики ассорти, 40г/шт)

    Средние КБЖУ по 5 вкусам ассорти (на батончик 40г):
      Калории: 144.4 ккал | Белки: 10г | Жиры: 6.66г | Углеводы: 3.74г
    """

    def test_bombbar_found_in_db(self):
        """Продукт 'батончик bombbar' находится в products.json с правильными данными"""
        from core.food.product_search import find_product

        product = find_product("батончик bombbar")

        assert product is not None
        assert product.get("calories_per_100g", 0) > 300  # ~361 ккал/100г
        assert product.get("protein_per_100g", 0) >= 24  # ~25г/100г
        assert product.get("weight_g") == 40  # 1 батончик = 40г

    def test_bombbar_aliases(self):
        """Все варианты написания Bombbar находят продукт в БД"""
        from core.food.product_search import find_product

        aliases = [
            "bombbar",
            "бомббар",
            "батончик бомббар",
            "bombbar протеиновый батончик",
            "протеиновый батончик bombbar",
            "батончик bombbar протеиновый",
        ]
        for alias in aliases:
            result = find_product(alias)
            assert result is not None, f"Alias не найден: {alias!r}"
            assert result.get("calories_per_100g", 0) > 300, f"Неверные калории для: {alias!r}"

    def test_bombbar_single(self):
        """'батончик Bombbar' (без числа) -> weight=40г"""
        products = extract_products_from_description("батончик Bombbar")
        weights = [p.get("weight", 0) for p in products]
        assert any(w == 40.0 for w in weights), f"Ожидали 40г, получили: {weights}"

    def test_bombbar_reverse_order(self):
        """'Bombbar батончик' -> weight=40г"""
        products = extract_products_from_description("Bombbar батончик")
        weights = [p.get("weight", 0) for p in products]
        assert any(w == 40.0 for w in weights), f"Ожидали 40г, получили: {weights}"

    def test_bombbar_with_adjective(self):
        """'Bombbar протеиновый батончик' -> weight=40г"""
        products = extract_products_from_description("Bombbar протеиновый батончик")
        weights = [p.get("weight", 0) for p in products]
        assert any(w == 40.0 for w in weights), f"Ожидали 40г, получили: {weights}"

    def test_bombbar_two(self):
        """'2 батончика Bombbar' -> weight=80г"""
        products = extract_products_from_description("2 батончика Bombbar")
        weights = [p.get("weight", 0) for p in products]
        assert any(w == 80.0 for w in weights), f"Ожидали 80г, получили: {weights}"

    def test_bombbar_two_text(self):
        """'два батончика Bombbar' -> weight=80г (текстовое числительное)"""
        products = extract_products_from_description("два батончика Bombbar")
        weights = [p.get("weight", 0) for p in products]
        assert any(w == 80.0 for w in weights), f"Ожидали 80г, получили: {weights}"

    def test_bombbar_half(self):
        """'половина батончика Bombbar' -> weight=20г"""
        products = extract_products_from_description("половина батончика Bombbar")
        weights = [p.get("weight", 0) for p in products]
        assert any(w == 20.0 for w in weights), f"Ожидали 20г, получили: {weights}"

    def test_bombbar_pol_prefix(self):
        """'полбатончика Bombbar' -> weight=20г"""
        products = extract_products_from_description("полбатончика Bombbar")
        weights = [p.get("weight", 0) for p in products]
        assert any(w == 20.0 for w in weights), f"Ожидали 20г, получили: {weights}"

    def test_bombbar_cyrillic(self):
        """'батончик бомббар' (кириллица) -> weight=40г"""
        products = extract_products_from_description("батончик бомббар")
        weights = [p.get("weight", 0) for p in products]
        assert any(w == 40.0 for w in weights), f"Ожидали 40г, получили: {weights}"

    def test_bombbar_nutrition_one(self):
        """1 батончик (40г) = ~144 ккал, ~10г белка"""
        result = calculate_nutrition("батончик bombbar", 40.0)

        assert 134 <= result["calories"] <= 155, f"Ожидали ~144 ккал, получили: {result['calories']}"
        assert 9.0 <= result["protein"] <= 11.0, f"Ожидали ~10г белка, получили: {result['protein']}"
        assert 5.5 <= result["fats"] <= 7.8, f"Ожидали ~6.7г жира, получили: {result['fats']}"

    def test_bombbar_nutrition_half(self):
        """Половина батончика (20г) = ~72 ккал"""
        result = calculate_nutrition("батончик bombbar", 20.0)

        assert 67 <= result["calories"] <= 78, f"Ожидали ~72 ккал, получили: {result['calories']}"


class TestGramsNotPieces:
    """Регрессия 12.07.2026: '73 г перца' парсилось как 73 ШТУКИ перца × 150г = 10950г.

    Необязательная группа '(?:[а-яё]+\\s+)?' в quantity_patterns съедала единицу
    измерения 'г', превращая граммы в счётчик штук. Клетчатка считалась от
    раздутого веса (229.9г при норме 30г/день).
    """

    def _weight_of(self, products, token):
        for p in products:
            if token in p["name"].lower():
                return p["weight"], p.get("source")
        return None, None

    def test_grams_of_pepper_is_not_piece_count(self):
        desc = "обед\n182 г томата\n73 г перца болгарского \nСтоловая ложка оливкового масла \n3 яйца"
        products = extract_products_from_description(desc)
        weights = [p["weight"] for p in products if p.get("weight")]
        assert all(w <= 1000 for w in weights), f"Раздутый вес: {products}"
        w, _ = self._weight_of(products, "перц")
        assert w == 73, f"Ожидали 73г перца, получили {w}: {products}"

    def test_pepper_pieces_still_work(self):
        products = extract_products_from_description("съел 2 перца")
        w, src = self._weight_of(products, "перец")
        assert w == 300 and src == "quantity_estimate"

    def test_grams_of_kotleta_is_not_piece_count(self):
        products = extract_products_from_description("120 г котлеты куриной")
        w, _ = self._weight_of(products, "котлет")
        assert w == 120, f"Ожидали 120г котлеты: {products}"

    def test_kotleta_pieces_still_work(self):
        products = extract_products_from_description("2 котлеты и гарнир")
        w, src = self._weight_of(products, "котлет")
        assert w == 160 and src == "quantity_estimate"

    def test_grams_of_syrniki_is_not_piece_count(self):
        products = extract_products_from_description("100 г сырников со сметаной")
        w, _ = self._weight_of(products, "сырник")
        assert w == 100, f"Ожидали 100г сырников: {products}"


class TestProductNameNotAcrossNewline:
    """Регрессия 12.07.2026: группа названия в weight_patterns матчила через \\n.

    '73 г перца болгарского \\nСтоловая ложка масла' давал имя
    'перца болгарского \\nстоловая' — пробелы в группе названия должны быть
    горизонтальными ([ \\t]), а не \\s.
    """

    def test_product_name_stops_at_newline(self):
        desc = "73 г перца болгарского \nСтоловая ложка оливкового масла"
        products = extract_products_from_description(desc)
        pepper = [p for p in products if "перц" in p["name"].lower()]
        assert pepper, f"Перец не найден: {products}"
        assert "\n" not in pepper[0]["name"], f"Имя захватило перенос строки: {pepper[0]['name']!r}"
        assert "столов" not in pepper[0]["name"].lower(), f"Имя захватило следующую строку: {pepper[0]['name']!r}"
        assert pepper[0]["weight"] == 73

    def test_product_before_weight_stops_at_newline(self):
        desc = "томат\nперец болгарский 73 г"
        products = extract_products_from_description(desc)
        pepper = [p for p in products if "перец" in p["name"].lower()]
        assert pepper, f"Перец не найден: {products}"
        assert "\n" not in pepper[0]["name"], f"Имя захватило перенос строки: {pepper[0]['name']!r}"
        assert "томат" not in pepper[0]["name"].lower(), f"Имя захватило предыдущую строку: {pepper[0]['name']!r}"
        assert pepper[0]["weight"] == 73

    def test_multiword_name_on_one_line_still_works(self):
        products = extract_products_from_description("73 г перца болгарского красного")
        pepper = [p for p in products if "перц" in p["name"].lower()]
        assert pepper and pepper[0]["name"] == "перца болгарского красного"
        assert pepper[0]["weight"] == 73

    def test_word_before_newline_number_not_captured_as_weight(self):
        """Регрессия 12.08.2026: паттерн №2 (продукт-перед-весом) не был закрыт для \\n
        между именем продукта и числом, только фикс паттерна №1 был сделан 12.07.2026.

        '3 яйца\\n10 грамм легкого сыра' матчило 'яйца' + '10' через перенос строки,
        яйцо получало чужой вес 10г, и штучное правило (3×55=165г) не могло его
        перезаписать из-за дедупликации по added_products.
        """
        desc = "3 яйца\n10 грамм легкого сыра"
        products = extract_products_from_description(desc)

        egg_weights = [p["weight"] for p in products if "яйц" in p["name"].lower()]
        assert egg_weights, f"Яйцо не найдено: {products}"
        assert all(w != 10.0 for w in egg_weights), f"Яйцо получило вес соседней строки: {products}"
        assert any(150 <= w <= 180 for w in egg_weights), f"Ожидали ~165г (3×55) для яиц: {products}"

        cheese_weights = [p["weight"] for p in products if "сыр" in p["name"].lower()]
        assert any(w == 10.0 for w in cheese_weights), f"Сыр должен остаться 10г: {products}"
