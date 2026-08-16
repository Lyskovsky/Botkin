#!/usr/bin/env python3
"""
Модели данных для системы управления состоянием.
Используют Pydantic для типизации и валидации.
"""

from pydantic import BaseModel, ConfigDict, Field, model_validator
from typing import Optional, List, Dict, Any


class MenuData(BaseModel):
    """
    Данные о распознанном меню/блюде из изображения.

    КРИТИЧНО: Эти данные НЕ ДОЛЖНЫ теряться при пересоздании состояния!
    """

    dish_name: str = Field(..., description="Название блюда")
    calories: float = Field(..., ge=0, description="Калории (ккал)")
    protein: float = Field(..., ge=0, description="Белки (г)")
    fats: float = Field(..., ge=0, description="Жиры (г")
    carbs: float = Field(..., ge=0, description="Углеводы (г)")
    weight: Optional[int] = Field(None, ge=0, description="Вес порции (г)")
    nutrition_per_100g: Optional[Dict[str, float]] = Field(None, description="КБЖУ на 100г")
    components: Optional[List[Dict[str, Any]]] = Field(None, description="Компоненты блюда")
    source: Optional[str] = Field(None, description="Источник распознавания: chatgpt_vision, gemini_vision, ocr")
    raw_text: Optional[str] = Field(None, description="Сырой текст из OCR")
    nutrition_not_found: Optional[bool] = Field(False, description="Флаг что КБЖУ не найдено")

    class Config:
        json_schema_extra = {
            "example": {
                "dish_name": "Рыбные кебабы с гарниром",
                "calories": 500.0,
                "protein": 33.0,
                "fats": 13.0,
                "carbs": 61.0,
                "weight": 415,
                "source": "chatgpt_vision",
            }
        }


class PhotoStateData(BaseModel):
    """
    Данные состояния при обработке фото.

    Используется в состоянии 'waiting_description'.
    """

    photo_paths: List[str] = Field(..., description="Пути к фото на диске")
    photo_file_ids: List[str] = Field(..., description="Telegram file IDs")
    caption: str = Field(default="", description="Подпись пользователя")
    menu_data: Optional[MenuData] = Field(
        None, description="КРИТИЧНО: Распознанное КБЖУ - НЕ ТЕРЯТЬ при пересоздании состояния!"
    )

    class Config:
        json_schema_extra = {
            "example": {
                "photo_paths": ["/app/data/photos/user123_20240209.jpg"],
                "photo_file_ids": ["AgACAgIAAxkBAAI..."],
                "caption": "ужин",
                "menu_data": {
                    "dish_name": "Салат Цезарь",
                    "calories": 350.0,
                    "protein": 25.0,
                    "fats": 18.0,
                    "carbs": 20.0,
                },
            }
        }


class MealStateData(BaseModel):
    """
    Данные состояния meal-flow (приём пищи ожидает подтверждения).

    Используется в состоянии 'waiting_confirmation' — как одиночный приём
    пищи (meal_items/meal_totals на верхнем уровне), так и multi_meals-
    контейнер (#53, несколько приёмов в одном сообщении: meal_items/
    meal_totals вложены в каждый элемент multi_meals, а не на верхнем
    уровне — отсюда model_validator ниже вместо req на уровне поля).

    extra="forbid" — ловит опечатку в имени ключа (например "photo_path"
    вместо "photo_paths", #256) в момент создания состояния, а не тихой
    потерей данных при чтении в save_meal_to_db() (#258). Для элементов
    внутри multi_meals та же защита достигается прогоном каждого элемента
    через build_meal_state_data() отдельно (см. state_helpers.py).

    NB: модель намеренно объединяет поля из разных флоу (фото-OCR, меню,
    текст) в одну "плоскую" схему — не строгий per-flow контракт. Опечатка,
    совпавшая по имени с полем другого флоу (например написать "date" там,
    где этот флоу его не использует), пройдёт валидацию молча. Если это
    станет проблемой — разделить на per-flow модели.
    """

    model_config = ConfigDict(extra="forbid")

    meal_items: Optional[List[Dict[str, Any]]] = Field(None, description="Позиции приёма пищи")
    meal_totals: Optional[Dict[str, Any]] = Field(
        None, description="КБЖУ итого (значения не только float — kcal_warnings это list[dict], #279)"
    )
    multi_meals: Optional[List[Dict[str, Any]]] = Field(
        None, description="Несколько приёмов пищи в одном сообщении (#53)"
    )
    meal_name: Optional[str] = Field(None, description="Название приёма пищи")
    dish_name: Optional[str] = Field(None, description="Название блюда (menu-флоу)")
    meal_time: Optional[str] = Field(None, description="Время приёма пищи HH:MM")
    slot: Optional[str] = Field(
        None, description="Выбранный слот для фото без подписи (#181): breakfast/lunch/snack/dinner"
    )
    description: Optional[str] = Field(None, description="Исходное описание/подпись пользователя")
    source: Optional[str] = Field(None, description="Источник: text, photo, ocr_db_lookup")
    date: Optional[str] = Field(None, description="Дата приёма пищи YYYY-MM-DD")
    photo_paths: List[str] = Field(default_factory=list, description="Пути к фото на диске")
    portion_multiplier: Optional[float] = Field(None, description="Deprecated")
    menu_ocr: Optional[bool] = Field(None, description="Флаг что это меню, распознанное по фото")
    product_label: Optional[Dict[str, Any]] = Field(
        None, description="Этикетка упакованного продукта (#255) → предложение «Запомнить продукт»"
    )

    @model_validator(mode="after")
    def _require_meal_or_multi_meals(self) -> "MealStateData":
        if self.multi_meals is None and (self.meal_items is None or self.meal_totals is None):
            raise ValueError("meal_items and meal_totals are required when multi_meals is not set")
        return self


# Вспомогательные функции для конвертации
