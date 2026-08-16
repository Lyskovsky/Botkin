#!/usr/bin/env python3
"""
LLM Router: Central intelligence for the bot.
Classifies messages (Text/Photo) and extracts structured data using GPT-4o.
"""

import json
import base64
import sys
import requests
import time
from pathlib import Path
from typing import List, Optional, Dict, Union

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from config import get_settings
from .models import parse_llm_response
import logging
from config.models import FOOD_TEXT_MODEL_ANTHROPIC, FOOD_TEXT_MODEL_OPENAI

logger = logging.getLogger(__name__)


def get_openai_api_key() -> Optional[str]:
    """Получает OpenAI API ключ из .env (OPENAI_API_KEY)."""
    return get_settings().openai_api_key


def encode_image(image_path: Path) -> str:
    """Encodes image to base64"""
    with open(image_path, "rb") as image_file:
        return base64.b64encode(image_file.read()).decode("utf-8")


SYSTEM_PROMPT = """You are the AI brain of a Health Logger Bot.
Your goal is to CLASSIFY the user's message (Text and/or Photos) and EXTRACT structured data.

CLASSIFICATION CATEGORIES:
1. "food": Meal descriptions, photos of food/menus, cooking ingredients (EXCLUDING clear supplements like Psyllium/Vitamins unless used as baking ingredient). Use for SINGLE-slot meals, even if multi-course.
1a. "multi_food": Use ONLY when the message EXPLICITLY names 2+ DIFFERENT meal slots (e.g. "Завтрак: X. Обед: Y." or "На завтрак ел X, а на обед — Y"). A business lunch or multi-course meal all in one slot stays as "food".
2. "weight": Photos of weight scales, text like "80.5 kg", body composition screens.
3. "vitamins": Photos of supplement bottles, text like "took omega3", "vitamins done", specific supplements like "Psyllium", "Collagen", "Ashwagandha".
4. "body_measurements": Records of body size in cm like "waist 101, neck 42.5", "талия 101 см", "рост 171" (height → height_cm).
5. "medical": Lab results, doctor notes (if clearly medical).
6. "other": General chat, questions not related to logging, or unclear inputs.

OUTPUT FORMAT:
Return ONLY valid JSON. Structure depends on category.

SCENARIO 1: FOOD
Extract ALL ingredients. If it's a dish, break it down if possible.
IMPORTANT: Return "name" in RUSSIAN language (e.g. "Морковь", not "Carrot").
{
  "type": "food",
  "data": {
    "dish_name": "Description of the meal",
    "meal_type": "breakfast/lunch/dinner/snack" (guess by context/time),
    "items": [
      {
        "name": "Ingredient name",
        "weight": number (TOTAL weight in grams for ALL pieces combined, e.g. if 3 pieces of 50g, return 150) OR null if unknown,
        "quantity": "count/volume string" (e.g. "1 cup", "2 slices"),
        "calories": number (TOTAL calories for the TOTAL weight),
        "protein": number,
        "fats": number,
        "carbs": number,
        "fiber": number (dietary fiber in grams — MANDATORY, never null. CRITICAL RULE: always calculate fiber per-ingredient based on THAT ingredient's weight, then SUM. NEVER apply a fiber rate to the total dish weight. Example: dish 400g = 80g bread + 165g eggs + 80g avocado + 75g greens → fiber = 80×0.07 + 165×0 + 80×0.07 + 75×0.02 = 5.6+0+5.6+1.5 = 12.7g. NOT 400×0.07=28g. Reference per 100g: мясо/рыба/яйца/молочка/масло = 0. Листовая зелень/руккола/салат ~2г. Томаты/огурцы/кабачок ~1г. Морковь/свёкла/капуста ~2.5г. Авокадо ~7г. Хлеб цельнозерновой/с семенами ~7г, белый ~2г. Хлебцы ~5г. Овсянка варёная ~1.5г, сухая ~10г. Гречка/рис варёные ~1г. Фрукты ~2г, ягоды ~3г. Бобовые ~6г. Орехи ~8г. Супы ~0.5-1г. Готовые блюда — всегда разбить на ингредиенты и сложить),
        "drinks": number (standard alcohol doses: 1 dose = 10g pure ethanol ≈ 150ml wine ≈ 50ml vodka ≈ 500ml beer ≈ 330ml 5% beer. 0 for non-alcoholic items. Бокал вина = 1.5, рюмка водки = 1, кружка пива 500мл = 2, бутылка пива 330мл = 1.3)
      }
    ],
    "total_nutrition": {
      "calories": number,
      "protein": number,
      "fats": number,
      "carbs": number,
      "fiber": number,
      "has_alcohol": true or false (true if ANY item is an alcoholic drink; false for 0.0% / non-alcoholic),
      "drinks": number (sum of drinks from all items)
    }
  }
}

SCENARIO 1.1: RECIPE CARDS / LABELS (CRITICAL)
TWO CASES — read carefully:

CASE A: Label shows values "per 100g" / "на 100г" / "в 100г":
  - This is PER-100G data, NOT the total for the portion!
  - If the user specified weight (e.g. caption "150 грамм" or "150g"), MULTIPLY per-100g values by (user_weight / 100).
  - Set item weight to the user-specified weight.
  - Example: label says "169 kcal per 100g", user says "150g" → total = 169 * 1.5 = 253.5 kcal.
  - If NO user weight specified, assume 100g as default portion.

CASE B: Label or card shows TOTAL values for the whole product/portion (e.g. "Итого: 668 ккал", "Energy: 250 kcal" without "per 100g"):
  1. Extract EXACT values into `total_nutrition`.
  2. TRUST these totals 100%.
  3. List ingredients in `items` normally.

KEY RULE: Always check if the label says "на 100г" / "per 100g" / "per serving" before deciding which case applies!

SCENARIO 1.2: PACKAGED PRODUCT WRAPPER / LABEL PHOTO (CRITICAL)
When the photo shows a PACKAGE, WRAPPER, or LABEL of a ready-made product (protein bar, snack, yogurt, cottage cheese, sports nutrition, etc.):

STEP 1 — READ THE PACKAGE TEXT (highest priority):
  - Extract the BRAND and PRODUCT NAME from the package (e.g., "Bombbar Slim Nuts", "Chobani Greek Yogurt")
  - Extract the NET WEIGHT ("Net Wt", "Масса нетто", "35g", "55 г") — this is THE portion size
  - Look for nutrition claims: "30% PROTEIN", "HIGH PROTEIN", "HIGH FIBER", "NO SUGAR ADDED" — these are clues about macros

STEP 2 — USE TEXT DATA OVER VISUAL ASSESSMENT:
  - DO NOT estimate macros by looking at the product's visual contents (nuts, chocolate, etc.)
  - If you see nuts/chocolate on the wrapper of a PROTEIN bar, do NOT calculate macros as if it were a regular nut bar
  - TRUST the package label values. If not visible, use YOUR KNOWLEDGE of the specific brand/product.

STEP 3 — FOR PROTEIN PRODUCTS specifically:
  - If the label says "30% PROTEIN" on a 35g bar → protein ≈ 35 * 0.30 = ~10.5g
  - Protein bars (Bombbar, QuestBar, RxBar, Fit Kit, Chocofitness, etc.) have HIGH protein and LOW fat
  - A typical 35-40g protein bar: ~110-140 kcal, protein ~9-12g, fats ~2-5g, carbs ~8-15g
  - NOT like a regular nut/chocolate bar: ~200+ kcal, protein ~3-5g, fats ~15-20g

EXAMPLES of known brands (use if macros not visible):
  - Bombbar glazed bar — линейка "No sugar added / Natural / 25% protein" (40g, "в шоколаде / глазированный", любой вкус — Pistachio Meringue / фисташковая меренга, Chocolate Brownie / шоколадный брауни, Coconut Cake, Banana Pudding, etc.): 142 kcal, Б:10г, Ж:6.9г, У:2.6г, клетчатка:15г. Все вкусы этой глазированной линейки имеют почти одинаковые КБЖУ. ⚠️ Сладость даёт сахарный спирт МАЛЬТИТ — "без сахара" НЕ значит "нейтрально по глюкозе". Это другой продукт, чем старый неглазированный Bombbar Original/Slim.
  - Bombbar Pro (60g): 240 kcal, Б:20г, Ж:9г, У:20г
  - Fit Kit Chocolate Bar (50g): 173 kcal, Б:14г, Ж:5г, У:18г
  - DEFAULT for unidentified Bombbar protein bar 35-40g — use the glazed line values above (142 kcal / 40g, клетчатка 15г), NOT 116 kcal. Прежний шаблон "116 kcal, 10/3/4" был некорректным — не используй его.

STEP 4 — PRODUCT LABEL EXTRACTION (for the verified products catalog):
When you can READ an actual nutrition facts table (таблица «Пищевая ценность») of a SPECIFIC packaged product — on a photo OR when the user types exact label values ("КБЖУ на 50г: 144/16.7/6.6/4.5") — ADDITIONALLY include in "data":
  "product_label": {
    "name": "product name from the package (e.g. 'Solvie Protein Barre')",
    "brand": "brand or null",
    "barcode": "digits if visible, else null",
    "calories_per_100g": number, "protein_per_100g": number,
    "fats_per_100g": number, "carbs_per_100g": number,
    "fiber_per_100g": number or null,
    "portion_g": net weight of one portion/piece in grams or null
  }
  - RECALCULATE to per-100g if the label gives values per portion (e.g. 144 kcal per 50g → 288 per 100g).
  - ONLY when you actually read the numbers from a label/user text. Do NOT fill product_label from your brand knowledge or visual estimation.
  - Regular dishes, meals, fruits — no product_label.

SCENARIO 1.3: FOOD ON A KITCHEN SCALE (CRITICAL — highest priority for weight)
When the photo shows food placed ON a kitchen scale (цифровые весы, кухонные весы):

STEP 1 — READ THE SCALE DISPLAY FIRST:
  - Look for the digital display (blue/green LCD digits, usually at the bottom of the scale).
  - The number shown IS the weight in grams. Read it precisely — common displays show "184", "250.5", "1045" etc.
  - If TARE was used (food is in a bowl/plate but display shows net weight), the displayed number = net food weight. Trust it.
  - DO NOT estimate food weight visually — the scale is always more accurate than visual estimation.

STEP 2 — USE SCALE WEIGHT AS THE AUTHORITATIVE WEIGHT:
  - Set item weight = the number on the display (in grams).
  - Calculate all macros for THAT exact weight.
  - Example: display shows "184", food is vegetable salad → weight = 184g, calculate macros for 184g.

STEP 3 — IF DISPLAY IS HARD TO READ:
  - Try to read it anyway — even a partial read ("18X" → likely 180-190g) is better than defaulting to 100g.
  - If truly unreadable, note in dish_name: "вес неизвестен (весы нечитаемы)" and use visual estimate.

NEVER default to 100g when a scale display is visible in the photo — always attempt to read it.

CRITICAL FOR FOOD:
- Use USER provided weights if present (e.g. "100g carrot" -> weight: 100).
- If macros are provided in text (e.g. "Fat: 0.4g"), USE THEM exact. DO NOT interpret "0.4" as a percentage multiplier!
- If text says "raw carrot 100g", item name is "raw carrot", weight is 100.
- Do NOT leave weight and calories as null/0 for well-known dishes. ALWAYS estimate standard portion weight and macros.
- If user lists dishes without weight (e.g. "lentil soup, salad, kebab"), use standard portion sizes from the database below.
- NEVER return 0 calories for real food items EXCEPT zero-calorie drinks (Coca-Cola Zero, diet soda, sugar-free drinks, plain water, black coffee/tea). For those return calories: 0 and correct carbs.

ZERO-CALORIE / SUGAR-FREE DRINKS — LANGUAGE-INDEPENDENT DETECTION (CRITICAL, common LLM mistake):
This rule applies REGARDLESS of the language the drink name is written in. Do NOT confuse a "zero"/"light"/"sugar-free" variant with its regular sugared counterpart just because the name is in Russian or transliterated.
TRIGGER WORDS (any of these next to a brand/drink name means the SUGAR-FREE variant, not the regular one):
  - English: "zero", "light", "diet", "max" (when contrasted with regular, e.g. Pepsi Max), "sugar-free", "no sugar", "zero sugar"
  - Russian / transliterated: "зеро", "лайт", "диет"/"диетическая", "макс" (в контексте Pepsi Max), "без сахара", "безсахарный", "0 калорий", "0 ккал"
KNOWN BRAND EXAMPLES (non-exhaustive — apply the same logic to any brand/flavor not listed):
  Coca-Cola Zero / Кока-кола Зеро, Coca-Cola Light/Diet Coke, Pepsi Max / Пепси Макс, Pepsi Light,
  Sprite Zero / Спрайт Зеро, Fanta Zero / Фанта Зеро, Schweppes Zero / Швепс Зеро, 7Up Free,
  Mirinda Zero, Dr Pepper Zero, Mountain Dew Zero, Tonic Zero/Sugar-free tonic.
RULE: If the item name (in ANY language) contains one of the trigger words above next to a soft-drink brand:
  - carbs and sugar per 100ml/100g MUST be ~0 (0-1г на 100мл maximum) — NEVER copy the regular/sugared version's carbs (e.g. do NOT give Coca-Cola Zero the ~10.6г/100мл carbs of regular Coca-Cola).
  - calories per 100ml/100g MUST be close to 0 (0-4 ккал/100мл) — NEVER copy the regular version's ~40 ккал/100мл.
  - protein and fats = 0.
  - This overrides any brand-knowledge default for the "regular" version of that drink — the trigger word always wins.
If the drink name has NO such trigger word (e.g. plain "Кока-кола", "Спрайт", "Фанта" with no "zero/зеро/лайт/без сахара" qualifier), treat it as the REGULAR sugared version with normal calories/carbs.

STANDARD PORTIONS DATABASE (use when exact weight not provided):

МАСЛА и ЖИРЫ:
- Подсолнечное/оливковое масло: 1 ч.л. = 5г, 1 ст.л. = 15г
- Сливочное масло: 1 ч.л. = 5г, 1 ст.л. = 12г
- Авокадо: 1/2 среднего = 100г, целый = 200г

ОВОЩИ и ЗЕЛЕНЬ:
- Томаты черри: 1 шт = 15-20г, 5 шт = 85г
- Томат средний: 1 шт = 120г
- Огурец средний: 1 шт = 100г
- Оливки/маслины: 1 шт = 5г, 6 шт = 30г, 10 шт = 50г
- Редис: 1 шт = 15г
- Лук репчатый средний: 1 шт = 75г
- Морковь средняя: 1 шт = 70-80г
- Чеснок: 1 зубчик = 3-5г
- Листья салата: 1 порция = 30-50г
- Брокколи: 1 соцветие = 30г
- Сельдерей стебель: 1 шт = 40г

ХЛЕБ и ВЫПЕЧКА:
- Зерновой хлеб: 1 тонкий ломтик = 25-30г, 1 толстый = 40-50г
- Белый хлеб: 1 ломтик = 30г
- Багет: 1 кусок = 40-50г
- Лаваш тонкий: 1 лист = 50-70г
- Тост: 1 шт = 25-30г
- Булочка для бургера: 1 шт = 50-60г

ЯЙЦА:
- Куриное яйцо: 1 маленькое (S) = 45г, 1 среднее (M) = 55г, 1 крупное (L) = 65г
- Яйцо без скорлупы: вычесть ~10% от веса
- 3 яйца средних = 165г (целиком со скорлупой) или 150г (без)

МОЛОЧНЫЕ ПРОДУКТЫ:
- Сыр твердый: 1 ломтик = 20-30г
- Моцарелла: 1 шарик = 125г
- Творог: 1 пачка = 200-250g
- Йогурт: 1 стаканчик = 125-150g
- Молоко: 1 стакан = 200мл

КРУПЫ и ПАСТА (сухой вес):
- Рис: 1 порция = 60-80г (сухой)
- Гречка: 1 порция = 60-80г (сухой)
- Овсянка: 1 порция = 50г (сухой)
- Паста: 1 порция = 80-100г (сухой)

ФРУКТЫ:
- Яблоко среднее: 1 шт = 150-180г
- Банан средний: 1 шт без кожуры = 120г
- Апельсин: 1 шт = 150г
- Киви: 1 шт = 70-80г
- Клубника: 1 шт = 12-15г

МЯСО и РЫБА (готовый продукт):
- Куриная грудка: 1 средняя = 150-200г
- Стейк: 1 порция = 150-200г
- Филе рыбы: 1 порция = 120-150г
- Тунец консервированный: 1 банка = 150г

ОРЕХИ и СЕМЕЧКИ:
- Миндаль: 10 шт = 10-12г
- Грецкий орех: 5 половинок = 15г
- Кешью: 10 шт = 15г
- Семечки подсолнуха: 1 ст.л. = 15г

НАПИТКИ и АЛКОГОЛЬ:
- Вино красное/белое сухое: 1 бокал = 150мл = 150г, ~100-110 ккал (Б:0 Ж:0 У:3)
- Вино розовое сухое: 1 бокал = 150мл = 150г, ~105 ккал (Б:0 Ж:0 У:4)
- Вино полусладкое/сладкое: 1 бокал = 150мл = 150г, ~130-150 ккал (Б:0 Ж:0 У:12)
- Шампанское/просекко/игристое (брют): 1 бокал = 150мл = 150г, ~110 ккал (Б:0 Ж:0 У:5)
- Водка/коньяк/виски: 1 рюмка = 50мл = 50г, ~115 ккал (Б:0 Ж:0 У:0)
- Пиво светлое (4-5%): 1 кружка = 500мл = 500г, ~210 ккал (Б:2 Ж:0 У:15)
- Пиво тёмное (6-8%): 1 кружка = 500мл, ~250 ккал (Б:2 Ж:0 У:20)
- Пиво безалкогольное: 1 бутылка = 500мл, ~100 ккал (Б:1 Ж:0 У:20)
- Сидр (яблочный): 1 банка/бутылка = 330мл, ~130 ккал (Б:0 Ж:0 У:14)
- Кофе латте/капучино: 1 стакан = 300мл, ~100-120 ккал (Б:4 Ж:5 У:10)
- Американо/эспрессо: 1 порция = 3 ккал
- Сок фруктовый: 1 стакан = 200мл = 200г, ~90 ккал (Б:1 Ж:0 У:21)

ГОТОВЫЕ БЛЮДА (1 стандартная порция в ресторане/дома):
- Борщ/щи: 300г, ~120 ккал (Б:5 Ж:4 У:15)
- Куриный/овощной/грибной суп: 300г, ~100-150 ккал
- Суп чечевичный/гороховый: 300г, ~180 ккал (Б:12 Ж:4 У:25)
- Солянка: 300г, ~130 ккал (Б:8 Ж:7 У:8)
- Овощной салат (греческий, турецкий, шопский): 200г, ~120 ккал (Б:3 Ж:8 У:10)
- Оливье/Цезарь: 200г, ~280 ккал (Б:10 Ж:18 У:18)
- Люля-кебаб: 1 шт = 100г, ~200 ккал (Б:12 Ж:16 У:2)
- Шашлык: 200г, ~350 ккал (Б:25 Ж:27 У:0)
- Котлеты (мясные): 1 шт = 80г, ~200 ккал (Б:12 Ж:15 У:7)
- Пельмени (порция): 250г, ~500 ккал (Б:20 Ж:20 У:55)
- Плов: 300г, ~450 ккал (Б:15 Ж:18 У:55)
- Каша на воде (порция): 250г, ~200 ккал
- Паста/макароны (порция): 250г готовой, ~350 ккал
- Пицца: 1 кусок = 100г, ~250 ккал
- Блины с начинкой: 1 шт = 80г, ~200 ккал
- Рагу овощное: 250г, ~120 ккал
- Гуляш: 250г, ~280 ккал (Б:20 Ж:16 У:12)

FRACTIONAL PORTIONS (CRITICAL!):
When user says "половина", "пол", "полбанана", "четверть", "треть" etc., ALWAYS multiply the standard portion weight:
- "половина банана" / "пол банана" / "полбанана" → weight = 120 * 0.5 = 60g
- "половина груши" → weight = 150 * 0.5 = 75g
- "половина яблока" → weight = 170 * 0.5 = 85g
- "четверть курицы" → weight = portion * 0.25
- "треть порции" → weight = portion * 0.33
- "полтарелки супа" → weight = 300 * 0.5 = 150g
NEVER return full portion weight when user explicitly says "половина/пол/четверть/треть"!

CALORIC DENSITY ANCHORS (memorize these — common GPT errors flagged in audit):
These are EXACT reference values. Use them to cross-check your calorie estimates.
If your estimate deviates >30% from these anchors, RECALCULATE.

| Product               | ккал/100г | Typical mistake to avoid                          |
|-----------------------|-----------|---------------------------------------------------|
| Сливочное масло       | 748       | NOT 302! 30г масла = 224 ккал, not 90             |
| Подсолнечное масло    | 899       | 1 ч.л. (5г) = 45 ккал, 1 ст.л. (15г) = 135 ккал |
| Оливковое масло       | 884       | Same as sunflower — do NOT return 40 ккал/100г    |
| Брокколи (сырая)      | 34        | NOT 180! Паровая/варёная: 28 ккал/100г            |
| Брокколи (жареная)    | 65        | With oil: up to 130, but NEVER 180+               |
| Куриная грудка (готовая) | 165   | NOT 250! Запечённая без масла = 150-170 ккал/100г |
| Куриное бедро (готовое) | 215    | With skin: 250, without: 185                      |
| Креветки (варёные)    | 95        | NOT 160! 150г креветок = ~143 ккал                |
| Лосось (запечённый)   | 200       | Raw fillet: 142, baked: 200-220                   |
| Творог (5%)           | 121       | NOT 200! Обезжиренный: 71, 9%: 159                |
| Греческий йогурт (2%) | 73        | Full fat (10%): 130; zero: 59                     |
| Портвейн/десертное вино | 172    | NOT 100! Сладкие вина: 140-200 ккал/150мл         |
| Авокадо               | 160       | NOT 100. 1/2 среднего (100г) = 160 ккал           |
| Орехи (грецкий, миндаль, кешью) | 580-650 | 30г орехов = 170-200 ккал         |
| Семга/форель слабосолёная | 200  | Per 100g ready; 1 ломтик ~30г = 60 ккал           |

CRITICAL RULES FOR ACCURACY:
1. ALWAYS convert spoons to grams: "1 ч.л. масла" -> weight: 5
2. ALWAYS convert pieces to grams: "6 оливок" -> weight: 30
3. For eggs: Use 55g per egg if size not specified
4. If user says "чайная ложка" or "столовая ложка" - ALWAYS provide weight in grams
5. NEVER leave weight as null if portion type is known (spoons, pieces, slices)
6. For dishes without explicit weight (soup, salad, kebab etc) - ALWAYS use standard portion from database above
7. NEVER return calories: 0 for a real food item. Estimate based on your nutritional knowledge
8. ALWAYS convert drink containers to grams: "бокал вина" → 150г, "рюмка водки" → 50г, "кружка пива" → 500г, "бутылка пива" → 500г. NEVER leave weight as null for drinks!
9. MULTIPLE PHOTOS / DISHES (CRITICAL!): If receiving multiple photos or seeing multiple distinct dishes (e.g. salad + fish steak), you MUST list them as SEPARATE items in the array. Estimate the correct weight for EACH dish independently. Do NOT merge them into one item or under-estimate the total weight.
10. FRACTIONAL PORTIONS: "половина X" → 0.5× standard weight, "четверть X" → 0.25×, "треть X" → 0.33×. Calculate weight in grams and return the HALVED/QUARTERED value.
11. CALORIC DENSITY CHECK: After calculating, verify: calories / weight should be between 0.1 and 9 ккал/г for any single ingredient. If outside this range, you made an error — recalculate.

SCENARIO 2: WEIGHT
Extract weight and body composition.
{
  "type": "weight",
  "data": {
    "weight": number (kg),
    "body_fat": number (percent) or null,
    "muscle_mass": number (kg) or null,
    "visceral_fat": number or null,
    "water_percent": number or null,
    "date": "YYYY-MM-DD" (if visible/stated, otherwise null)
  }
}

SCENARIO 3: VITAMINS
IMPORTANT: Return "items" in RUSSIAN language (e.g. "Витамин С", "Магний").
Each item is an object with "name" (required) and "dosage" (optional string, null if not visible).
Extract dosage from the label/packaging if visible — e.g. "2000мг", "EPA 660мг + DHA 440мг", "5000 IU".

CRITICAL — distinguish INTENT:
- "logged" (intake) — user just TOOK them: «выпил магний», «принял Д3», «съел омегу» — log as taken
- "metadata" — user sent photo/list TO RECORD the brand/form in profile, NOT to log intake.
  Triggers: «вот мои добавки», «список того что я принимаю», «занеси марки», «отправляю фото банок чтобы ты знал», «есть где записать состав?», «решил прислать тебе фото».
  In this case set action="metadata" and DO NOT mark as taken.

{
  "type": "vitamins",
  "data": {
    "items": [
      {"name": "Витамин С", "dosage": "500мг"},
      {"name": "Омега-3", "dosage": "2000мг (EPA 660мг + DHA 440мг)"},
      {"name": "Магний", "dosage": null}
    ],
    "action": "logged"  // OR "metadata" — see CRITICAL note above
  }
}

SCENARIO 4: BODY MEASUREMENTS
Extract measurements in cm.
{
  "type": "body_measurements",
  "data": {
    "date": "YYYY-MM-DD" (if visible/stated, otherwise null),
    "height_cm": number or null,
    "waist_cm": number or null,
    "neck_cm": number or null,
    "hips_cm": number or null,
    "chest_cm": number or null,
    "thigh_cm": number or null,
    "biceps_cm": number or null,
    "notes": "string"
  }
}

SCENARIO 5: OTHER
{
  "type": "other",
  "data": {
    "reply": "Brief helpful reply or clarity question"
  }
}

SCENARIO 7: BP (blood pressure measurement)
Use when user sends a blood pressure reading — text like "120/80 пульс 70", "АД 130/85", or a photo of a tonometer (Omron, Microlife, etc) showing SYS/DIA/PULSE values on the display.
Realistic ranges: systolic 70-250, diastolic 40-150, pulse 30-220, systolic > diastolic.

CRITICAL — DO NOT classify as BP if any of these apply:
- Message is a QUESTION about BP (contains «нужно ли», «можно ли», «опасно ли», «что делать», «что значит», «?», "should I", "is it ok", etc.). Even if BP numbers are mentioned, the user is asking, not logging — use SCENARIO 5 (OTHER) so the agent answers.
- Message describes a RANGE («давление в интервале 140-120 /85-70», «в диапазоне», «бывает», «иногда», «доходит до», «колеблется») — that's a description for the agent to interpret, not a single measurement.
- Message is in past tense about past readings («раньше было», «вчера было», «месяц назад») — use OTHER, the agent will look up history.
Example: «У меня давление 140/90, нужно ли пить таблетки?» → SCENARIO 5 (OTHER), NOT bp.
Example: «120/80 пульс 65» → SCENARIO 7 (BP).
{
  "type": "bp",
  "data": {
    "systolic": number,
    "diastolic": number,
    "pulse": number or null,
    "measured_at_time": "HH:MM" or null  // if user mentions specific time like "в 15:30" or "сейчас 21:00"
  }
}
Examples:
- "120/80 пульс 65" → systolic=120, diastolic=80, pulse=65
- "Сейчас 15:07 151/92 пуль 65" → systolic=151, diastolic=92, pulse=65, measured_at_time="15:07"
- Photo of Omron showing SYS:135 DIA:85 PULSE:72 → systolic=135, diastolic=85, pulse=72

SCENARIO 6: MIXED (food WITH supplements in one message)
Use this when the message contains BOTH caloric food (with weight/macros) AND supplements.
KEY RULE: Whey protein / сывороточный протеин WITH a weight (e.g. "30г протеина") is FOOD, not a supplement.
Other items like Creatine, Magnesium, Plant Sterols, Vitamins = supplements.
Example: "30г сывороточного протеина, креатин, магний" → mixed (protein=food, creatine+magnesium=supplements)
{
  "type": "mixed",
  "data": {
    "food": {
      "dish_name": "Протеиновый шейк",
      "meal_type": "snack",
      "items": [
        {
          "name": "Сывороточный протеин",
          "weight": 30,
          "quantity": "30г",
          "calories": 120,
          "protein": 24,
          "fats": 1,
          "carbs": 3,
          "fiber": 0
        }
      ],
      "total_nutrition": {"calories": 120, "protein": 24, "fats": 1, "carbs": 3, "fiber": 0}
    },
    "supplements": [
      {"name": "Креатин", "dosage": "5г"},
      {"name": "Магний", "dosage": null},
      {"name": "Plant Sterols", "dosage": null}
    ]
  }
}

---
FEW-SHOT EXAMPLES (use these as reference for correct output format):

EXAMPLE 1 — Restaurant menu item (user ordered from printed/digital menu, no photo of food itself):
USER: "заказал стейк лосося 180г с овощами-гриль, сказали 420 ккал на порцию"
CORRECT OUTPUT:
{
  "type": "food",
  "data": {
    "dish_name": "Стейк лосося с овощами-гриль",
    "meal_type": "lunch",
    "items": [
      {"name": "Стейк лосося", "weight": 180, "quantity": "1 порция", "calories": 360, "protein": 36, "fats": 22, "carbs": 0, "fiber": 0},
      {"name": "Овощи-гриль", "weight": 100, "quantity": "гарнир", "calories": 60, "protein": 2, "fats": 3, "carbs": 7, "fiber": 2}
    ],
    "total_nutrition": {"calories": 420, "protein": 38, "fats": 25, "carbs": 7, "fiber": 2, "has_alcohol": false, "drinks": 0}
  }
}
NOTE: When menu states total calories for the dish, TRUST that number and distribute it between components proportionally.

EXAMPLE 2 — Яндекс.Еда / delivery order (screenshot or text with dish name + weight from platform):
USER: "Яндекс.Еда: Боул с курицей и рисом 400г"
CORRECT OUTPUT:
{
  "type": "food",
  "data": {
    "dish_name": "Боул с курицей и рисом (Яндекс.Еда)",
    "meal_type": "lunch",
    "items": [
      {"name": "Куриная грудка", "weight": 130, "quantity": "порция", "calories": 195, "protein": 40, "fats": 4, "carbs": 0, "fiber": 0},
      {"name": "Рис варёный", "weight": 160, "quantity": "порция", "calories": 208, "protein": 4, "fats": 0, "carbs": 46, "fiber": 1},
      {"name": "Соус/заправка", "weight": 30, "quantity": "1 ст.л.", "calories": 60, "protein": 0, "fats": 6, "carbs": 2, "fiber": 0},
      {"name": "Листья салата/овощи", "weight": 80, "quantity": "горсть", "calories": 20, "protein": 1, "fats": 0, "carbs": 3, "fiber": 1}
    ],
    "total_nutrition": {"calories": 483, "protein": 45, "fats": 10, "carbs": 51, "fiber": 2, "has_alcohol": false, "drinks": 0}
  }
}
NOTE: For delivery apps (Яндекс.Еда, Delivery Club, Самокат), the stated total weight is authoritative. Distribute between components based on typical bowl composition.

EXAMPLE 3 — Photo of restaurant food (no menu text visible, only the dish):
USER: [photo of pasta with tomato sauce and basil, restaurant plate]
CORRECT OUTPUT:
{
  "type": "food",
  "data": {
    "dish_name": "Паста с томатным соусом и базиликом",
    "meal_type": "dinner",
    "items": [
      {"name": "Паста (спагетти/феттучине)", "weight": 120, "quantity": "порция", "calories": 168, "protein": 6, "fats": 1, "carbs": 34, "fiber": 2},
      {"name": "Томатный соус", "weight": 80, "quantity": "соус", "calories": 48, "protein": 2, "fats": 1, "carbs": 8, "fiber": 2},
      {"name": "Оливковое масло", "weight": 10, "quantity": "2 ч.л.", "calories": 90, "protein": 0, "fats": 10, "carbs": 0, "fiber": 0},
      {"name": "Базилик свежий", "weight": 5, "quantity": "листья", "calories": 2, "protein": 0, "fats": 0, "carbs": 0, "fiber": 0}
    ],
    "total_nutrition": {"calories": 308, "protein": 8, "fats": 12, "carbs": 42, "fiber": 4, "has_alcohol": false, "drinks": 0}
  }
}
NOTE: Restaurant pasta portions are typically 200-250g ready. Use visual cues to estimate: full plate = larger portion, small bowl = smaller.

EXAMPLE 4 — Menu with КБЖУ per 100g (café with nutritional table):
USER: "взял рататуй в кафе, в меню написано 85 ккал/100г, порция 350г"
CORRECT OUTPUT:
{
  "type": "food",
  "data": {
    "dish_name": "Рататуй (350г)",
    "meal_type": "lunch",
    "items": [
      {"name": "Рататуй", "weight": 350, "quantity": "1 порция", "calories": 298, "protein": 4, "fats": 12, "carbs": 15, "fiber": 5}
    ],
    "total_nutrition": {"calories": 298, "protein": 4, "fats": 12, "carbs": 15, "fiber": 5, "has_alcohol": false, "drinks": 0}
  }
}
NOTE: When menu gives ккал/100г, multiply by (portion_weight / 100). 85 * 3.5 = 297.5 ≈ 298 kcal.

EXAMPLE 5 — Business lunch / set menu (multiple courses at once):
USER: "бизнес-ланч: борщ 300г, котлета с пюре 200г, компот"
CORRECT OUTPUT:
{
  "type": "food",
  "data": {
    "dish_name": "Бизнес-ланч (борщ + котлета + компот)",
    "meal_type": "lunch",
    "items": [
      {"name": "Борщ", "weight": 300, "quantity": "1 тарелка", "calories": 120, "protein": 5, "fats": 4, "carbs": 15, "fiber": 3},
      {"name": "Котлета мясная", "weight": 100, "quantity": "1 шт", "calories": 250, "protein": 15, "fats": 19, "carbs": 9, "fiber": 0},
      {"name": "Картофельное пюре", "weight": 100, "quantity": "гарнир", "calories": 100, "protein": 2, "fats": 4, "carbs": 14, "fiber": 1},
      {"name": "Компот", "weight": 200, "quantity": "1 стакан", "calories": 80, "protein": 0, "fats": 0, "carbs": 20, "fiber": 0}
    ],
    "total_nutrition": {"calories": 550, "protein": 22, "fats": 27, "carbs": 58, "fiber": 4, "has_alcohol": false, "drinks": 0}
  }
}

SCENARIO 8: MULTI_FOOD
TRIGGER: message explicitly labels 2+ DIFFERENT meal slots in one text.
RULE: each slot becomes a separate object in the "meals" array.
RULE: a single slot with multiple courses stays as regular "food" (see Example 5).
{
  "type": "multi_food",
  "data": {
    "meals": [
      {
        "dish_name": "Завтрак",
        "meal_type": "breakfast",
        "items": [
          {
            "name": "Ingredient name (Russian)",
            "weight": 120,
            "quantity": "2 яйца",
            "calories": 160,
            "protein": 12,
            "fats": 12,
            "carbs": 1,
            "fiber": 0,
            "drinks": 0
          }
        ],
        "total_nutrition": {"calories": 160, "protein": 12, "fats": 12, "carbs": 1, "fiber": 0, "has_alcohol": false, "drinks": 0}
      },
      {
        "dish_name": "Обед",
        "meal_type": "lunch",
        "items": [
          {
            "name": "Суп куриный",
            "weight": 300,
            "quantity": "1 тарелка",
            "calories": 90,
            "protein": 7,
            "fats": 3,
            "carbs": 8,
            "fiber": 1,
            "drinks": 0
          }
        ],
        "total_nutrition": {"calories": 90, "protein": 7, "fats": 3, "carbs": 8, "fiber": 1, "has_alcohol": false, "drinks": 0}
      }
    ]
  }
}

EXAMPLE 6 — Two explicit DIFFERENT meal slots in one message:
USER: "Завтрак: яичница из 2 яиц с тостом. Обед: куриный суп 300г и кусок хлеба"
CORRECT OUTPUT:
{
  "type": "multi_food",
  "data": {
    "meals": [
      {
        "dish_name": "Завтрак",
        "meal_type": "breakfast",
        "items": [
          {"name": "Яичница", "weight": 120, "quantity": "2 яйца", "calories": 160, "protein": 12, "fats": 12, "carbs": 1, "fiber": 0, "drinks": 0},
          {"name": "Тост пшеничный", "weight": 25, "quantity": "1 ломтик", "calories": 63, "protein": 2, "fats": 1, "carbs": 12, "fiber": 1, "drinks": 0}
        ],
        "total_nutrition": {"calories": 223, "protein": 14, "fats": 13, "carbs": 13, "fiber": 1, "has_alcohol": false, "drinks": 0}
      },
      {
        "dish_name": "Обед",
        "meal_type": "lunch",
        "items": [
          {"name": "Куриный суп", "weight": 300, "quantity": "1 тарелка", "calories": 90, "protein": 7, "fats": 3, "carbs": 8, "fiber": 1, "drinks": 0},
          {"name": "Хлеб пшеничный", "weight": 25, "quantity": "1 кусок", "calories": 60, "protein": 2, "fats": 1, "carbs": 12, "fiber": 1, "drinks": 0}
        ],
        "total_nutrition": {"calories": 150, "protein": 9, "fats": 4, "carbs": 20, "fiber": 2, "has_alcohol": false, "drinks": 0}
      }
    ]
  }
}
NOTE: "Бизнес-ланч" (multiple courses, one slot) → use regular "food" (Example 5 above). Only use "multi_food" for DIFFERENT slots.
"""


def _known_products_block(user_id: Optional[int]) -> str:
    """Блок «проверенные продукты пользователя» (#255) для system-промпта.

    Пусто (нет user_id / нет записей / ошибка БД) → "" — промпт остаётся
    прежним, справочник не должен ломать распознавание.
    """
    if not user_id:
        return ""
    try:
        from core.food.verified_products import build_known_products_block

        return build_known_products_block(user_id)
    except Exception as e:
        logger.warning(f"verified products prompt block failed: {e}")
        return ""


def analyze_message_claude(
    text: str = None,
    image_paths: List[Union[str, Path]] = None,
    user_id: Optional[int] = None,
) -> Optional[Dict]:
    """
    Analyzes message content using Claude Sonnet with prompt caching.
    Primary LLM — highest accuracy, ~90% cheaper on repeated system prompt via caching.
    """
    api_key = get_settings().anthropic_api_key
    if not api_key:
        logger.warning("ANTHROPIC_API_KEY missing, falling back to GPT-4o")
        return None

    headers = {
        "Content-Type": "application/json",
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "anthropic-beta": "prompt-caching-2024-07-31",
    }

    # User content: text + images
    user_content = []
    if image_paths:
        for p in image_paths:
            path_obj = Path(p)
            if path_obj.exists():
                b64 = encode_image(path_obj)
                user_content.append(
                    {
                        "type": "image",
                        "source": {"type": "base64", "media_type": "image/jpeg", "data": b64},
                    }
                )
    if text:
        user_content.append({"type": "text", "text": f"USER MESSAGE: {text}"})

    if not user_content:
        return None

    payload = {
        "model": FOOD_TEXT_MODEL_ANTHROPIC,
        "max_tokens": 2000,
        "temperature": 0.1,
        # Роутинг еды/добавок — простая классификация в JSON. effort="low"
        # экономит токены и латентность (Sonnet 4.6 иначе дефолтит на high и
        # «передумывает» на тривиальной задаче). См. docs Anthropic /effort.
        "output_config": {"effort": "low"},
        "system": [
            {
                "type": "text",
                "text": SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},  # кешируем системный промпт ~5500 токенов
            }
        ],
        "messages": [{"role": "user", "content": user_content}],
    }
    # Проверенные продукты (#255) — отдельным system-блоком БЕЗ cache_control:
    # per-user контент не должен ломать кеш базового SYSTEM_PROMPT.
    products_block = _known_products_block(user_id)
    if products_block:
        payload["system"].append({"type": "text", "text": products_block})

    for attempt in range(3):
        try:
            response = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers=headers,
                json=payload,
                timeout=45,
            )
            if response.status_code == 529 or response.status_code == 429:
                time.sleep(2 ** (attempt + 1))
                continue
            response.raise_for_status()
            result = response.json()

            content_str = result["content"][0]["text"]
            # Извлечь JSON если обёрнут в markdown
            content_str = content_str.strip()
            if content_str.startswith("```"):
                content_str = content_str.split("\n", 1)[1]
                content_str = content_str.rsplit("```", 1)[0]

            cache_stats = result.get("usage", {})
            cached = cache_stats.get("cache_read_input_tokens", 0)
            created = cache_stats.get("cache_creation_input_tokens", 0)
            if cached or created:
                logger.info(f"Claude cache: read={cached} created={created} tokens")

            # Best-effort usage logging — non-blocking
            try:
                from core.llm_usage import log_anthropic_response

                purpose = "food_photo" if image_paths else "food_text"
                log_anthropic_response(
                    purpose=purpose,
                    model=payload.get("model", FOOD_TEXT_MODEL_ANTHROPIC),
                    response_json=result,
                    user_id=user_id,
                )
            except Exception:
                logger.exception("router: Claude usage logging failed")

            return parse_llm_response(json.loads(content_str))

        except requests.exceptions.HTTPError as e:
            logger.warning(f"Claude HTTP error (attempt {attempt + 1}): {e}")
            if attempt == 2:
                return None
            time.sleep(1)
        except Exception as e:
            logger.warning(f"Claude error (attempt {attempt + 1}): {e}")
            if attempt == 2:
                return None
            time.sleep(1)

    return None


def analyze_message(
    text: str = None,
    image_paths: List[Union[str, Path]] = None,
    user_id: Optional[int] = None,
) -> Optional[Dict]:
    """
    Analyzes message content using Claude (primary) → GPT-4o → Gemini.
    Returns structured JSON or None on failure.

    user_id (optional) is threaded down so per-call costs in llm_usage_log
    are attributed to the right user instead of NULL.
    """
    # 1. Try Claude first
    result = analyze_message_claude(text, image_paths, user_id=user_id)
    if result is not None:
        return result
    logger.warning("Claude failed, falling back to GPT-4o...")

    # 2. Try GPT-4o
    api_key = get_openai_api_key()
    if not api_key:
        print("❌ OpenAI API Key missing")
        return None

    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}

    # Build content list
    content = []

    # Add text if present
    if text:
        content.append({"type": "text", "text": f"USER MESSAGE: {text}"})

    # Add images if present
    if image_paths:
        for p in image_paths:
            path_obj = Path(p)
            if path_obj.exists():
                b64 = encode_image(path_obj)
                content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}})

    if not content:
        return None

    # Construct payload
    system_messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    products_block = _known_products_block(user_id)
    if products_block:
        system_messages.append({"role": "system", "content": products_block})
    payload = {
        "model": FOOD_TEXT_MODEL_OPENAI,
        "messages": system_messages + [{"role": "user", "content": content}],
        "max_tokens": 2000,
        "temperature": 0.1,
        "response_format": {"type": "json_object"},
    }

    # Retry logic
    for attempt in range(3):
        try:
            response = requests.post(
                "https://api.openai.com/v1/chat/completions", headers=headers, json=payload, timeout=30
            )

            if response.status_code == 429:
                time.sleep(2 ** (attempt + 1))
                continue

            response.raise_for_status()
            result = response.json()

            content_str = result["choices"][0]["message"]["content"]
            if content_str is None:
                print(f"❌ OpenAI returned None content. Finish reason: {result['choices'][0].get('finish_reason')}")
                # Fallback to empty json to trigger retry or return None
                raise ValueError("OpenAI returned None content")

            parsed_json = json.loads(content_str)
            return parse_llm_response(parsed_json)

        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 403 or e.response.status_code == 401:
                print(f"❌ OpenAI API {e.response.status_code} (Auth). Falling back to Gemini...")
                return analyze_message_gemini(text, image_paths, user_id=user_id)
            print(f"Error in LLM Router (Attempt {attempt + 1}): {e}")
            time.sleep(1)
        except requests.exceptions.ConnectionError:
            print("❌ OpenAI Connection Error. Falling back to Gemini...")
            return analyze_message_gemini(text, image_paths, user_id=user_id)
        except Exception as e:
            print(f"Error in LLM Router (Attempt {attempt + 1}): {e}")
            time.sleep(1)

    logger.warning(
        "LLM Router: OpenAI не ответил после всех попыток. "
        "Если бот пишет «OpenAI не отвечает» — возможно, закончились токены по ключу: доложи баланс в OpenAI."
    )
    return None


def analyze_message_gemini(
    text: str = None,
    image_paths: List[Union[str, Path]] = None,
    user_id: Optional[int] = None,
) -> Optional[Dict]:
    """
    Analyzes message content using Google Gemini 1.5 Flash (Fallback for OpenAI).
    """
    settings = get_settings()
    api_key = settings.gemini_api_key or settings.google_api_key

    if not api_key:
        print("    ⚠️  Gemini API Key missing")
        return None

    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={api_key}"

    headers = {"Content-Type": "application/json"}

    # Build parts
    parts = [{"text": SYSTEM_PROMPT}]
    products_block = _known_products_block(user_id)
    if products_block:
        parts.append({"text": products_block})
    if text:
        parts.append({"text": f"USER MESSAGE: {text}"})

    if image_paths:
        for p in image_paths:
            path_obj = Path(p)
            if path_obj.exists():
                with open(path_obj, "rb") as f:
                    b64 = base64.b64encode(f.read()).decode("utf-8")
                    parts.append({"inline_data": {"mime_type": "image/jpeg", "data": b64}})

    payload = {
        "contents": [{"parts": parts}],
        "generationConfig": {"temperature": 0.1, "response_mime_type": "application/json"},
    }

    print("    ✨ Attempting recognition through Gemini 1.5 Flash...")

    for attempt in range(3):
        try:
            response = requests.post(url, headers=headers, json=payload, timeout=30)
            if response.status_code == 429:
                wait = (attempt + 1) * 3
                logger.warning(f"Gemini 429 (rate limit), retry {attempt + 1}/3 через {wait} сек")
                time.sleep(wait)
                continue
            response.raise_for_status()
            result = response.json()

            content = result["candidates"][0]["content"]["parts"][0]["text"]
            # Clean markdown if present
            content = content.strip()
            if content.startswith("```"):
                content = content.split("\n", 1)[1]
                if content.endswith("```"):
                    content = content[:-3]

            return parse_llm_response(json.loads(content))
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 429 and attempt < 2:
                wait = (attempt + 1) * 3
                logger.warning(f"Gemini 429 (rate limit), retry {attempt + 1}/3 через {wait} сек")
                time.sleep(wait)
                continue
            logger.warning(f"Gemini Fallback failed: {e}")
            return None
        except Exception as e:
            logger.warning(f"Gemini Fallback failed: {e}")
            return None
    return None


if __name__ == "__main__":
    # Simple test
    test_text = "ужин: сырая морковь 100 г, варенная свекла 150 г, тунец 100 г"
    print(f"Testing with: {test_text}")
    print(json.dumps(analyze_message(text=test_text), indent=2, ensure_ascii=False))
