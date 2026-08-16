"""
Pydantic-модели для ответов LLM Router.

Решают три проблемы:
1. GPT возвращает "100" (строка) вместо 100 (число) → автоматически конвертируем
2. GPT возвращает null для обязательных полей → подставляем дефолты
3. GPT возвращает -50 для калорий (баг) → заменяем на None

Использование:
    raw = json.loads(gpt_response)
    validated = parse_llm_response(raw)  # ← всё нормализовано
"""

import logging
from typing import Any, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Модели для FOOD
# ---------------------------------------------------------------------------


class FoodItem(BaseModel):
    """Один ингредиент/продукт в приёме пищи."""

    name: str = "Неизвестно"
    weight: Optional[float] = None  # граммы, None если неизвестно
    quantity: Optional[str] = None  # "1 cup", "2 слайса" — строка от GPT
    calories: Optional[float] = None
    protein: Optional[float] = None
    fats: Optional[float] = None
    carbs: Optional[float] = None
    drinks: Optional[float] = None  # стандартные дозы алкоголя (1 доза = 10г этанола)

    @field_validator("weight", "calories", "protein", "fats", "carbs", "drinks", mode="before")
    @classmethod
    def coerce_numeric(cls, v: Any) -> Optional[float]:
        """Строки → float, null/"null"/"" → None, отрицательные → None."""
        if v is None or v == "" or v == "null":
            return None
        try:
            val = float(v)
            return val if val >= 0 else None
        except (ValueError, TypeError):
            return None


class TotalNutrition(BaseModel):
    """Итоговое КБЖУ блюда (с этикетки или рецепта)."""

    calories: float = 0.0
    protein: float = 0.0
    fats: float = 0.0
    carbs: float = 0.0
    drinks: float = 0.0  # сумма стандартных доз алкоголя

    @field_validator("calories", "protein", "fats", "carbs", "drinks", mode="before")
    @classmethod
    def coerce_non_negative(cls, v: Any) -> float:
        """Строки → float, None/отрицательные → 0.0."""
        try:
            return max(0.0, float(v or 0))
        except (ValueError, TypeError):
            return 0.0


class ProductLabel(BaseModel):
    """Данные этикетки упакованного продукта (#255).

    LLM заполняет, когда на фото/в тексте есть таблица пищевой ценности
    конкретного продукта. Идёт в предложение «Запомнить продукт?» →
    справочник verified_products. Все значения per 100g.
    """

    name: str = ""
    brand: Optional[str] = None
    barcode: Optional[str] = None
    calories_per_100g: Optional[float] = None
    protein_per_100g: Optional[float] = None
    fats_per_100g: Optional[float] = None
    carbs_per_100g: Optional[float] = None
    fiber_per_100g: Optional[float] = None
    portion_g: Optional[float] = None

    @field_validator(
        "calories_per_100g",
        "protein_per_100g",
        "fats_per_100g",
        "carbs_per_100g",
        "fiber_per_100g",
        "portion_g",
        mode="before",
    )
    @classmethod
    def coerce_numeric(cls, v: Any) -> Optional[float]:
        if v is None or v == "" or v == "null":
            return None
        try:
            val = float(v)
            return val if val >= 0 else None
        except (ValueError, TypeError):
            return None


class FoodData(BaseModel):
    dish_name: str = ""
    meal_type: str = "snack"
    items: List[FoodItem] = Field(default_factory=list)
    total_nutrition: Optional[TotalNutrition] = None
    product_label: Optional[ProductLabel] = None


class FoodResponse(BaseModel):
    type: Literal["food"]
    data: FoodData


class MultiFoodData(BaseModel):
    meals: List[FoodData] = Field(default_factory=list)


class MultiFoodResponse(BaseModel):
    type: Literal["multi_food"]
    data: MultiFoodData


# ---------------------------------------------------------------------------
# Модели для WEIGHT
# ---------------------------------------------------------------------------


class WeightData(BaseModel):
    """Данные с весов/скриншота смарт-весов."""

    weight: float  # Обязательное — если null от GPT, валидация упадёт
    body_fat: Optional[float] = None
    muscle_mass: Optional[float] = None
    visceral_fat: Optional[float] = None
    water_percent: Optional[float] = None
    date: Optional[str] = None

    @field_validator("body_fat", "muscle_mass", "visceral_fat", "water_percent", mode="before")
    @classmethod
    def coerce_optional_numeric(cls, v: Any) -> Optional[float]:
        if v is None or v == "" or v == "null":
            return None
        try:
            return float(v)
        except (ValueError, TypeError):
            return None


class WeightResponse(BaseModel):
    type: Literal["weight"]
    data: WeightData


# ---------------------------------------------------------------------------
# Модели для VITAMINS
# ---------------------------------------------------------------------------


class VitaminsData(BaseModel):
    items: List[str] = Field(default_factory=list)
    action: str = "logged"


class VitaminsResponse(BaseModel):
    type: Literal["vitamins"]
    data: VitaminsData


# ---------------------------------------------------------------------------
# Главная функция
# ---------------------------------------------------------------------------


def parse_llm_response(raw: Optional[dict]) -> Optional[dict]:
    """
    Валидирует и нормализует JSON-ответ от GPT или Gemini.

    Что делает:
    - "weight": "150"  →  weight: 150.0   (строка → число)
    - "calories": null →  calories: None  (null остаётся None для items)
    - "calories": null →  calories: 0.0   (null → 0 для total_nutrition)
    - "calories": -50  →  calories: None  (отрицательное → None)
    - отсутствует "items" →  items: []    (подставляет дефолт)

    Для типов "other" и "medical" данные пропускаются как есть (нет строгой схемы).
    При ошибке валидации возвращает исходный raw (обратная совместимость).
    """
    if not raw or not isinstance(raw, dict):
        return raw

    type_ = raw.get("type", "other")

    try:
        if type_ == "food":
            return FoodResponse.model_validate(raw).model_dump()
        elif type_ == "weight":
            return WeightResponse.model_validate(raw).model_dump()
        elif type_ == "vitamins":
            return VitaminsResponse.model_validate(raw).model_dump()
        elif type_ == "multi_food":
            return MultiFoodResponse.model_validate(raw).model_dump()
        else:
            # "other", "medical" — произвольная структура, пропускаем как есть
            return raw

    except Exception as e:
        logger.warning(
            f"⚠️  LLM response validation failed (type={type_!r}): {e}. Используем исходный ответ (backward compatible)."
        )
        return raw
