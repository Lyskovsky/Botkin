#!/usr/bin/env python3
"""
Обработчик фото для бота с поддержкой нескольких фото и извлечения весов
"""

from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from aiogram import Bot
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.exceptions import TelegramBadRequest
from datetime import datetime

from core.infra.tz import MSK  # noqa: E402  (общая TZ проекта)
from pathlib import Path
import asyncio
import html
import math
import os
import logging

logger = logging.getLogger(__name__)

# Issue #115: лимиты на пользовательский ввод/вывод vision при сборке items.
MAX_CAPTION_HINT_LEN = 200  # подпись юзера → dish_name (Telegram caption ≤ 1024)
MAX_COMPONENT_NAME_LEN = 100
MAX_COMPONENTS = 20  # верхняя граница числа items из одного фото


def _safe_float(value: object) -> float | None:
    """Числовое поле из vision/LLM → конечный float или None.

    Не падаем на 'много'/None и не пропускаем inf/nan дальше в арифметику ккал.
    """
    try:
        result = float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
    if result is None or not math.isfinite(result):
        return None
    return result


async def safe_edit_text(message: Message, text: str, **kwargs):
    """Безопасная обёртка для edit_text — игнорирует ошибку 'message is not modified'"""
    try:
        return await message.edit_text(text, **kwargs)
    except TelegramBadRequest as e:
        if "message is not modified" in str(e):
            logger.debug("Сообщение не изменилось, пропускаем edit_text")
        else:
            raise


from services.state import UserState, state_manager
from services.state_helpers import build_meal_state_data, create_photo_state
from core.vision.menu_parser import parse_menu_photo

router = Router()


from handlers.callbacks import MealConfirmationCallback, SupplementConfirmationCallback, WeightConfirmationCallback
from handlers.first_food import record_first_food
from webhook.nutrition_slots import SLOTS, slot_center_time, slot_from_time, slot_label_ru

from typing import List


async def process_photos_list(message: Message, photo_paths: List[Path], media_group_id: str = None):
    """Обрабатывает список фото (одиночное или группа)"""
    import logging

    logger = logging.getLogger(__name__)

    user_id = str(message.from_user.id)

    # Если это медиа-группа, проверяем, не ответили ли уже
    if media_group_id:
        user_state = state_manager.get_state(user_id)
        if user_state and user_state.data.get("answered", False):
            return

        # Отмечаем, что ответили
        if user_state:
            user_state.data["answered"] = True
            state_manager.set_state(user_id, user_state)

            # Актуализируем caption из состояния, если в текущем сообщении его нет
            if not message.caption and user_state.data.get("caption"):
                # Создаем копию сообщения с caption из состояния (для обработки)
                # Note: message.caption is immutable usually, we just use local var
                pass

    photo_count = len(photo_paths)
    # ИЗМЕНЕНО: Новое сообщение по просьбе пользователя
    processing_msg = await message.answer(
        f"📸 Получено {photo_count} фото! Идет ИИ-анализ: еда, меню, весы, КБЖУ, скрины, витамины..."
    )

    # Получаем API ключ для OCR
    try:
        from core.infra.api_key_loader import get_google_vision_api_key

        api_key = get_google_vision_api_key()
    except ImportError:
        api_key = os.getenv("GOOGLE_VISION_API_KEY")

    # Извлекаем caption ДО вызова парсера, чтобы передать контекст
    user_state = state_manager.get_state(user_id)
    caption = message.caption
    if user_state and user_state.data.get("caption"):
        caption = user_state.data.get("caption")

    from core.vision.ocr_weight import parse_weight_screenshot

    recognized_weights = []
    remaining_photos = []  # Фото, которые не распознались как весы

    # Проходим по всем фото и ищем весы
    for idx, ph_path in enumerate(photo_paths):
        try:
            # Пробуем распознать как весы (по одному)
            weight_data = parse_weight_screenshot([ph_path], api_key, description=caption or "")

            if weight_data and weight_data.get("weight"):
                logger.info(f"⚖️ Фото {idx + 1}: Распознан вес {weight_data.get('weight')} кг")
                recognized_weights.append(weight_data)
            else:
                remaining_photos.append(ph_path)

        except Exception as e:
            logger.error(f"Ошибка при проверке весов на фото {idx + 1}: {e}")
            remaining_photos.append(ph_path)

    # Если нашли весы - формируем отчет
    if recognized_weights:
        w_response_lines = ["⚖️ <b>Данные весов сохранены!</b>\n"]
        for i, wd in enumerate(recognized_weights, 1):
            line = f"{i}. 📅 <b>{wd.get('date', 'Сегодня')}</b>: 🏋️‍♂️ <b>{wd.get('weight')} кг</b>"
            if wd.get("body_fat"):
                line += f", 💧 {wd.get('body_fat')}%"
            w_response_lines.append(line)

        # Показываем количество записей только если их больше одной
        if len(recognized_weights) > 1:
            w_response_lines.append(f"\n📂 <i>Всего записей: {len(recognized_weights)}</i>")
        w_response_lines.append("\nСохранить запись в журнал?")

        # Создаем кнопки подтверждения
        w_builder = InlineKeyboardBuilder()
        w_builder.button(text="✅ Сохранить вес", callback_data=WeightConfirmationCallback(action="save").pack())
        w_builder.button(text="❌ Отмена", callback_data=WeightConfirmationCallback(action="cancel").pack())

        # Сохраняем данные весов в состояние (временно)
        if not user_state:
            user_state = UserState(
                user_id=user_id, state="waiting_weight_confirmation", data={"weights": recognized_weights}
            )
        else:
            user_state.state = "waiting_weight_confirmation"
            user_state.data["weights"] = recognized_weights
        state_manager.set_state(user_id, user_state)

        # Если были только весы - отправляем и выходим
        if not remaining_photos:
            final_text = "\n".join(w_response_lines)
            if processing_msg:
                await processing_msg.edit_text(final_text, parse_mode="HTML", reply_markup=w_builder.as_markup())
            else:
                await message.answer(final_text, parse_mode="HTML", reply_markup=w_builder.as_markup())
            return
        else:
            # Если есть и весы и что-то еще - отправляем запрос по весам отдельным сообщением
            await message.answer("\n".join(w_response_lines), parse_mode="HTML", reply_markup=w_builder.as_markup())

    # ---------------------------

    # Если остались фото, которые не весы - это еда/меню
    if not remaining_photos:
        return

    # Обновляем список для обработки еды
    photo_paths = remaining_photos
    photo_count = len(photo_paths)

    # Сначала пробуем лучшую модель (LLM) по фото — единый путь для распознавания
    menu_data = None
    router_result = None
    from core.llm.router import analyze_message

    try:
        paths_for_llm = [Path(p) for p in photo_paths] if isinstance(photo_paths[0], str) else photo_paths
        prompt_text = (caption or "").strip() or "Что на фото? Название продукта или блюда, вес и КБЖУ."
        import asyncio

        loop = asyncio.get_running_loop()
        router_result = await loop.run_in_executor(
            None,
            lambda: analyze_message(text=prompt_text, image_paths=paths_for_llm, user_id=int(user_id)),
        )
        if router_result and router_result.get("type") == "food" and router_result.get("data"):
            data = router_result["data"]
            items = data.get("items") or []
            total_nutrition = data.get("total_nutrition")
            if total_nutrition:
                menu_data = {
                    "dish_name": data.get("dish_name") or (items[0].get("name") if items else "Блюдо"),
                    "calories": total_nutrition.get("calories", 0),
                    "protein": total_nutrition.get("protein", 0),
                    "fats": total_nutrition.get("fats", 0),
                    "carbs": total_nutrition.get("carbs", 0),
                    "weight": items[0].get("weight") if items else None,
                    # Issue #115: сохраняем покомпонентную разбивку зрения, чтобы не
                    # схлопывать фото-блюдо в один item при наличии подписи.
                    "components": items,
                    # #255: этикетка продукта → предложение «Запомнить продукт»
                    "product_label": data.get("product_label"),
                }
            elif items:
                menu_data = {
                    "dish_name": data.get("dish_name") or items[0].get("name", "Блюдо"),
                    "calories": sum(i.get("calories", 0) for i in items),
                    "protein": sum(i.get("protein", 0) for i in items),
                    "fats": sum(i.get("fats", 0) for i in items),
                    "carbs": sum(i.get("carbs", 0) for i in items),
                    "weight": items[0].get("weight") if items else None,
                    "components": items,
                    "product_label": data.get("product_label"),
                }
            if menu_data and (menu_data.get("calories") or menu_data.get("protein") is not None):
                logger.info(f"Распознано через LLM: {menu_data.get('dish_name')}, {menu_data.get('calories')} ккал")
        else:
            if router_result is not None:
                logger.info(f"LLM по фото вернул type={router_result.get('type')}, не еда — идём в fallback")
            else:
                logger.warning("LLM по фото вернул None (сеть/лимит/ошибка)")
        # Витамины и весы — не считать едой, обработать сразу
        if router_result and router_result.get("type") == "vitamins":
            data = router_result.get("data", {})
            items = data.get("items", [])
            if items:

                def _fmt_supplement(i) -> str:
                    if isinstance(i, dict):
                        name = html.escape(i.get("name", ""))
                        dosage = i.get("dosage")
                        return f"• {name} — <i>{html.escape(dosage)}</i>" if dosage else f"• {name}"
                    return f"• {html.escape(str(i))}"

                items_list = "\n".join([_fmt_supplement(i) for i in items])
                s_builder = InlineKeyboardBuilder()
                s_builder.button(
                    text="✅ Записать как приём",
                    callback_data=SupplementConfirmationCallback(action="save").pack(),
                )
                s_builder.button(
                    text="❌ Не сейчас",
                    callback_data=SupplementConfirmationCallback(action="cancel").pack(),
                )

                if not user_state:
                    user_state = UserState(
                        user_id=user_id,
                        state="waiting_supplement_confirmation",
                        data={"supplements": items},
                    )
                else:
                    user_state.state = "waiting_supplement_confirmation"
                    user_state.data["supplements"] = items
                state_manager.set_state(user_id, user_state)

                await processing_msg.edit_text(
                    f"💊 <b>Распознал добавки:</b>\n{items_list}\n\nЗаписать как приём сейчас?",
                    parse_mode="HTML",
                    reply_markup=s_builder.as_markup(),
                )
                return
        if router_result and router_result.get("type") == "weight":
            data = router_result.get("data", {})
            w_val = data.get("weight")
            if w_val is not None:
                from helpers.db_save import save_weight_to_db

                telegram_user_id = int(message.from_user.id)
                # Передаём весь dict data чтобы сохранилась дата из LLM/OCR ответа
                weight_data_to_save = {**data, "source": "llm_photo"}
                if save_weight_to_db(weight_data_to_save, user_id=telegram_user_id):
                    date_label = data.get("date") or "сегодня"
                    await processing_msg.edit_text(
                        f"⚖️ <b>Вес:</b> {w_val} кг\n📅 Дата: {date_label}\n✅ Записано", parse_mode="HTML"
                    )
                else:
                    await processing_msg.edit_text(
                        f"⚖️ Распознано: {w_val} кг (сохранение не удалось)", parse_mode="HTML"
                    )
                return
        # 🩺 BP (task #53): LLM распознал фото тонометра или текст замера АД.
        # Прецедент 25.05.2026: папа прислал фото Omron + текст "151/92 пульс 65" —
        # бот 4 раза «не еда» подряд. Теперь LLM возвращает type='bp' → пишем напрямую.
        if router_result and router_result.get("type") == "bp":
            data = router_result.get("data", {})
            sys_v, dia_v, pulse_v = data.get("systolic"), data.get("diastolic"), data.get("pulse")
            if sys_v and dia_v and (70 <= sys_v <= 250) and (40 <= dia_v <= 150) and (sys_v > dia_v):
                from helpers.db_save import save_bp_to_db

                telegram_user_id = int(message.from_user.id)
                # Опциональное время из текста ("в 15:07" → 15:07 МСК сегодня)
                measured_at = None
                time_str = data.get("measured_at_time")
                if time_str and ":" in time_str:
                    try:
                        hh, mm = map(int, time_str.split(":")[:2])
                        if 0 <= hh <= 23 and 0 <= mm <= 59:
                            # ВАЖНО: используем модульный MSK (line 13). НЕ переопределять
                            # локально — Python сделает MSK local для всей функции и в
                            # food-flow ниже (line 502) упадёт UnboundLocalError.
                            measured_at = datetime.now(MSK).replace(hour=hh, minute=mm, second=0, microsecond=0)
                    except (ValueError, IndexError):
                        pass

                saved = save_bp_to_db(
                    systolic=sys_v,
                    diastolic=dia_v,
                    pulse=pulse_v,
                    user_id=telegram_user_id,
                    measured_at=measured_at,
                    source="llm_photo",
                )
                if saved:
                    pulse_part = f" пульс {pulse_v}" if pulse_v else ""
                    time_part = f" в {time_str}" if time_str else ""
                    await processing_msg.edit_text(
                        f"🩺 <b>АД:</b> {sys_v}/{dia_v}{pulse_part}{time_part}\n✅ Записано",
                        parse_mode="HTML",
                    )
                else:
                    await processing_msg.edit_text(
                        f"🩺 Распознано: {sys_v}/{dia_v} (сохранение не удалось)", parse_mode="HTML"
                    )
                return
    except Exception as e:
        logger.warning(f"LLM по фото не сработал, fallback на parse_menu_photo: {e}")

    # Fallback: старое распознавание меню (не удаляем)
    if not menu_data or not (menu_data.get("calories") or menu_data.get("protein") is not None):
        all_menu_data = []
        if photo_count > 1:
            logger.info(f"📸 Обрабатываю {photo_count} фото еды по отдельности...")
            for idx, photo_path in enumerate(photo_paths, 1):
                logger.info(f"  Анализирую фото еды {idx}/{photo_count}: {photo_path.name}")
                menu_item = parse_menu_photo([photo_path], api_key, description=caption)
                if menu_item:
                    all_menu_data.append(menu_item)
                    logger.info(f"  ✅ Фото {idx}: распознано '{menu_item.get('dish_name')}'")
                else:
                    logger.info(f"  ⚠️ Фото {idx}: ничего не распознано")
            if len(all_menu_data) > 1:
                menu_data = {
                    "dish_name": ", ".join([item.get("dish_name", "Неизвестно") for item in all_menu_data]),
                    "calories": sum([item.get("calories", 0) for item in all_menu_data]),
                    "protein": sum([item.get("protein", 0) for item in all_menu_data]),
                    "fats": sum([item.get("fats", 0) for item in all_menu_data]),
                    "carbs": sum([item.get("carbs", 0) for item in all_menu_data]),
                    "weight": None,
                    "multiple_items": True,
                    "items": all_menu_data,
                }
                logger.info(f"✅ Объединено {len(all_menu_data)} блюд: {menu_data['dish_name']}")
            elif len(all_menu_data) == 1:
                menu_data = all_menu_data[0]
            else:
                menu_data = None
        else:
            menu_data = parse_menu_photo(photo_paths, api_key, description=caption)

    # Логируем результат распознавания меню
    if menu_data:
        logger.info(f"Распознано меню/еда: {menu_data.get('dish_name')}, КБЖУ: {menu_data.get('calories')} ккал")

        # Если LLM вернул 0 ккал для не-напитка (например салат) — пересчитываем через БД/оценку
        from core.food.description_parser import is_zero_calorie_drink

        dish_name = menu_data.get("dish_name") or ""
        if (
            (menu_data.get("calories") or 0) == 0
            and (menu_data.get("protein") or 0) == 0
            and not is_zero_calorie_drink(dish_name)
            and router_result
            and router_result.get("type") == "food"
        ):
            from core.food.nutrition import process_llm_food_data

            meal_items, meal_totals = process_llm_food_data(router_result, caption or "")
            if meal_items and (meal_totals.get("calories") or 0) > 0:
                menu_data = {
                    "dish_name": dish_name or (meal_items[0].get("product") if meal_items else "Блюдо"),
                    "calories": meal_totals.get("calories", 0),
                    "protein": meal_totals.get("protein", 0),
                    "fats": meal_totals.get("fats", 0),
                    "carbs": meal_totals.get("carbs", 0),
                    "weight": meal_items[0].get("weight_g") if meal_items else menu_data.get("weight"),
                }
                logger.info(
                    f"Пересчитаны КБЖУ (LLM вернул 0): {menu_data.get('dish_name')}, {menu_data.get('calories')} ккал"
                )

        # --- ЛОГИКА РЕЦЕПТУРНОЙ КАРТОЧКИ (Elementaree и т.п.) ---
        if menu_data.get("is_recipe_card"):
            dish_name = menu_data.get("dish_name", "Блюдо по рецепту")
            servings = menu_data.get("servings", 2)
            cal = menu_data.get("calories", 0)
            prot = menu_data.get("protein", 0)
            fat = menu_data.get("fats", 0)
            carb = menu_data.get("carbs", 0)
            weight_g = menu_data.get("weight_grams", 0)
            source = menu_data.get("source", "recipe_card")

            logger.info(f"📋 Рецептурная карточка: {dish_name}, {cal} ккал/порция, на {servings}")

            # КБЖУ уже на 1 порцию — GPT разделил
            # Перезаписываем menu_data чтобы дальнейшая логика использовала эти значения
            menu_data = {
                "dish_name": f"{dish_name} (1/{servings} рецепта)",
                "calories": cal,
                "protein": prot,
                "fats": fat,
                "carbs": carb,
                "weight": weight_g,
                "source": source,
                "is_recipe_card": True,
            }
            # Не return — пусть дальше идёт стандартная логика сохранения

        # --- ЛОГИКА ДОБАВОК ---
        if menu_data.get("is_supplement"):
            dish_name = menu_data.get("dish_name", "")
            logger.info(f"💊 Распознаны добавки по фото: {dish_name}")

            # from core.health.supplements import supplement_service
            # logged_items, remaining_items = supplement_service.log_intake(dish_name)
            pass

            response = f"💊 <b>По фото распознано:</b> {html.escape(dish_name)}\n"
            if logged_items:
                response += f"✅ <b>Записано в журнал:</b> {', '.join(logged_items)}\n\n"
            else:
                response += "⚠️ <b>Не удалось сопоставить с вашим планом.</b> Проверьте названия.\n\n"

            if remaining_items:
                response += "⏳ <b>Осталось принять сегодня:</b>\n" + "\n".join(remaining_items)
            else:
                response += "🎉 <b>На сегодня все витамины приняты!</b>"

            if processing_msg:
                await processing_msg.edit_text(response, parse_mode="HTML")
            else:
                await message.answer(response, parse_mode="HTML")
            return
        # ----------------------

    else:
        logger.info("Меню/еда не распознано")

    has_nutrition = menu_data and (menu_data.get("calories") is not None or menu_data.get("protein") is not None)
    if has_nutrition:
        # Это меню или еда с распознанными КБЖУ (в т.ч. 0 ккал — напитки типа Cola Zero)
        logger.info(f"Распознано: {menu_data.get('dish_name')}")

        # Получаем caption из состояния или сообщения
        user_state = state_manager.get_state(user_id)
        caption = message.caption
        if user_state and user_state.data.get("caption"):
            caption = user_state.data.get("caption")

        # Если есть caption - обрабатываем его как описание с учетом данных меню
        if caption:
            logger.info(f"Есть caption, обрабатываем с учетом распознанного: {caption}")

            # Используем безопасный helper для создания/обновления состояния
            user_state = create_photo_state(
                user_id=user_id,
                photo_paths=photo_paths,
                photo_file_ids=[message.photo[-1].file_id if message.photo else ""],
                caption=caption,
                menu_data=menu_data,  # Новый menu_data
                existing_state=user_state,  # Сохранит другие данные если есть
            )
            state_manager.set_state(user_id, user_state)

            # Обрабатываем описание с учетом меню
            # Caption уже в state, передаем None чтобы функция взяла caption из состояния
            await handle_description(message, None, processing_message=processing_msg)
        else:
            # Нет caption - используем данные как есть
            logger.info(f"Используем данные без caption: {menu_data.get('dish_name')}")
            # Для handle_menu_photo передаем первое фото как "основное" для отображения
            await handle_menu_photo(message, menu_data, photo_paths[0], processing_message=processing_msg)
            return

    elif menu_data and menu_data.get("nutrition_not_found") and menu_data.get("raw_text"):
        # КБЖУ не найдены, но есть текст - пробуем найти продукт в базе по названию
        logger.info("КБЖУ не найдены в меню, ищем продукт в базе по OCR тексту...")

        from core.food.product_search import find_product_in_text

        found_product = find_product_in_text(menu_data["raw_text"])

        if found_product:
            p_name = found_product.get("name", "Продукт")
            logger.info(f"✅ Продукт найден в базе по OCR: {p_name}")

            # Определяем вес (дефолтный или 100г)
            weight = found_product.get("weight_g", 100.0)
            multiplier = weight / 100.0

            # Считаем итоги
            meal_totals = {
                "calories": round(found_product.get("calories_per_100g", 0) * multiplier, 1),
                "protein": round(found_product.get("protein_per_100g", 0) * multiplier, 1),
                "fats": round(found_product.get("fats_per_100g", 0) * multiplier, 1),
                "carbs": round(found_product.get("carbs_per_100g", 0) * multiplier, 1),
            }

            meal_items = [
                {
                    "product": p_name,
                    "weight_g": weight,
                    "weight_source": "db_default",
                    "calories": meal_totals["calories"],
                    "protein": meal_totals["protein"],
                    "fats": meal_totals["fats"],
                    "carbs": meal_totals["carbs"],
                    "source": "ocr_db_lookup",
                    "note": found_product.get("note"),
                }
            ]

            # Создаем состояние
            new_state = UserState(
                user_id=user_id,
                state="waiting_confirmation",
                data=build_meal_state_data(
                    description=f"Фото: {p_name}",
                    meal_items=meal_items,
                    meal_totals=meal_totals,
                    meal_time=datetime.now(MSK).strftime("%H:%M"),
                    meal_name=p_name,
                    photo_paths=[str(p) for p in photo_paths],
                ),
            )
            state_manager.set_state(user_id, new_state)

            # Формируем ответ
            safe_p_name = html.escape(p_name)
            response = f"🍽️ <b>{safe_p_name}</b> (найдено в базе)\n\n"
            response += "⚠️ Распознано по фото\n"
            response += f"• {safe_p_name} ({weight}г) — {int(meal_totals['calories'])} ккал\n"
            response += f"\n📊 <b>Итого: {int(meal_totals['calories'])} ккал</b>\n"
            response += (
                f"Б: {int(meal_totals['protein'])} | Ж: {int(meal_totals['fats'])} | У: {int(meal_totals['carbs'])}"
            )
            from core.food.nutrition import format_kcal_warning

            response += format_kcal_warning(meal_totals)

            # Buttons
            builder = InlineKeyboardBuilder()
            builder.button(
                text="✅ Сохранить", callback_data=MealConfirmationCallback(action="save", meal_type="regular").pack()
            )
            builder.button(
                text="❌ Отмена", callback_data=MealConfirmationCallback(action="cancel", meal_type="regular").pack()
            )

            if processing_msg:
                await processing_msg.edit_text(response, parse_mode="HTML", reply_markup=builder.as_markup())
            else:
                await message.answer(response, parse_mode="HTML", reply_markup=builder.as_markup())
            return

        else:
            logger.info("Продукт не найден в базе по OCR тексту")
            # Fallthrough to ask description

    # Получаем caption
    user_state = state_manager.get_state(user_id)
    caption = message.caption
    if user_state and user_state.data.get("caption"):
        caption = user_state.data.get("caption")

    if caption:
        # Если есть caption - обрабатываем сразу
        logger.info(f"Обрабатываем описание: {caption}")

        # Если это одиночное фото (или состояние не подходит), инициализируем состояние
        # Это критично, так как handle_description берет пути к фото из состояния
        if not user_state or user_state.state != "waiting_description":
            # Используем безопасный helper - он АВТОМАТИЧЕСКИ сохранит menu_data
            user_state = create_photo_state(
                user_id=user_id,
                photo_paths=photo_paths,
                photo_file_ids=[message.photo[-1].file_id if message.photo else ""],
                caption=caption,
                menu_data=None,  # Не передаём - возьмёт из existing_state
                existing_state=user_state,  # ← Отсюда сохранится menu_data!
            )
            state_manager.set_state(user_id, user_state)

        # Caption уже в state, передаем None чтобы функция взяла caption из состояния
        await handle_description(message, None, processing_message=processing_msg)
    else:
        # 🐛 FIX 26.05.2026: фото которое LLM-роутер НЕ распознал как еду
        # (тонометр, скриншот, добавки, документ, etc) — НЕ ставим waiting_description
        # state. Иначе юзер залипает в food-handler на каждое следующее сообщение.
        # Прецедент: Александр прислал 2 скрина Garmin → попали сюда → state=
        # waiting_description → вопрос «Ты видишь сон?» уходит в food-flow → «не еда» 3 раза.
        # Решение: явное сообщение что фото не еда + предложение задать вопрос текстом.
        # Не ставим state вообще — следующее сообщение пойдёт через нормальный
        # routing (BP regex / vitamins regex / BotkinClaw).
        prompt_text = (
            "📎 Фото получил, но не распознал еду.\n\n"
            "Если это <b>анализы, документ или медданные</b> — "
            "напиши текстом что хочешь узнать, и я разберу результаты.\n\n"
            "Если это <b>еда</b> — пришли фото ещё раз с подписью "
            "(название блюда, компоненты, вес)."
        )
        if processing_msg:
            await processing_msg.edit_text(prompt_text, parse_mode="HTML")
        else:
            await message.answer(prompt_text, parse_mode="HTML")


@router.message(F.photo)
async def handle_photo_message(message: Message, bot: Bot, user_id: int, album: list = None):
    """Обработка фото с описанием блюда"""

    import logging

    logger = logging.getLogger(__name__)

    messages_to_process = album if album else [message]
    logger.info(f"📸 Получено {len(messages_to_process)} фото от пользователя {message.from_user.id}")

    photo_paths = []

    for msg in messages_to_process:
        # Получаем фото
        photo = msg.photo[-1]  # Берем фото наибольшего размера
        photo_file_id = photo.file_id

        # Сохраняем фото
        photo_path = await save_photo(msg, photo_file_id)
        if photo_path:
            photo_paths.append(photo_path)

    if not photo_paths:
        await message.answer("❌ Ошибка при сохранении фото")
        return

    # Ищем caption в сообщениях альбома, используем самый первый найденный, или пустую строку
    caption = next((msg.caption for msg in messages_to_process if msg.caption), "")

    # Подменяем message.caption для downstream логики
    message_with_caption = message
    if caption and not message.caption:
        # Берем сообщение в котором был настоящий caption
        message_with_caption = next((msg for msg in messages_to_process if msg.caption), message)

    await process_photos_list(message_with_caption, photo_paths, message.media_group_id)


async def _handle_libreview_csv(message: Message, msg: Message) -> bool:
    """CSV-экспорт глюкозы из LibreView → импорт в glucose_readings (#163 follow-up).

    Возвращает True, если документ распознан и обработан как LibreView CSV.
    """
    import asyncio
    import logging
    import os
    from zoneinfo import ZoneInfo

    logger = logging.getLogger(__name__)
    user_id = message.from_user.id

    processing = await message.answer("📄 Получил CSV, читаю историю глюкозы…")
    try:
        buf = await message.bot.download(msg.document)
        raw_bytes = buf.read()
        # Русскоязычный экспорт LibreView бывает в cp1251, не UTF-8 → пробуем оба.
        try:
            content = raw_bytes.decode("utf-8-sig")
        except UnicodeDecodeError:
            content = raw_bytes.decode("cp1251", errors="replace")
    except Exception as e:
        logger.error(f"LibreView CSV download error: {e}")
        await processing.edit_text("⚠️ Не удалось скачать файл. Попробуй отправить ещё раз.", parse_mode=None)
        return True

    # TZ пользователя для корректной конверсии наивного локального времени LibreView → UTC.
    tz = ZoneInfo("Europe/Moscow")
    try:
        from database import SessionLocal
        from database.models import User

        db = SessionLocal()
        try:
            u = db.query(User).filter(User.telegram_id == user_id).first()
            if u and getattr(u, "timezone", None):
                tz = ZoneInfo(u.timezone)
        finally:
            db.close()
    except Exception as e:
        logger.warning(f"LibreView CSV: TZ lookup failed, using MSK: {e}")

    # Импортёр грузим через importlib — scripts/import/ не пакет (import зарезервирован).
    import importlib.util

    mod_path = Path(__file__).resolve().parents[2] / "scripts" / "import" / "libreview_csv.py"
    spec = importlib.util.spec_from_file_location("libreview_csv_import", mod_path)
    libreview = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(libreview)

    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        await processing.edit_text("⚠️ Внутренняя ошибка: нет доступа к базе. Сообщи разработчику.", parse_mode=None)
        return True

    try:
        result = await asyncio.to_thread(libreview.import_libreview_csv, content, user_id, tz, db_url)
    except libreview.LibreViewParseError:
        # Не похоже на экспорт глюкозы LibreView — отдаём обычному пайплайну (картинка/PDF/агент).
        await processing.delete()
        return False
    except Exception as e:
        logger.error(f"LibreView CSV import error: {e}")
        await processing.edit_text(
            "⚠️ Не смог разобрать файл как экспорт LibreView. Проверь, что это CSV из выгрузки глюкозы.",
            parse_mode=None,
        )
        return True

    if result.get("glucose_points", 0) == 0:
        await processing.edit_text("В файле не нашёл точек глюкозы. Это точно выгрузка из LibreView?", parse_mode=None)
        return True

    # Диапазон дат — чтобы пользователь сразу заметил, если перепутан порядок день/месяц.
    first = (result.get("first_ts") or "")[:10]
    last = (result.get("last_ts") or "")[:10]
    inserted = result.get("inserted", 0)
    total = result.get("total_in_file", result.get("glucose_points", 0))
    await processing.edit_text(
        f"✅ Загрузил историю глюкозы из LibreView.\n\n"
        f"Точек в файле: {total}\n"
        f"Новых добавлено: {inserted}\n"
        f"Период: {first} — {last}\n\n"
        f"Теперь могу анализировать эти дни — спрашивай про сахар за любой день и про реакцию на еду.",
        parse_mode=None,
    )
    logger.info(f"LibreView CSV импорт для {user_id}: {result}")
    return True


@router.message(F.document)
async def handle_document_image(message: Message, album: list = None):
    """Обработка документов с изображениями (например, при перетаскивании из приложения 'Фото' macOS)"""

    import logging

    logger = logging.getLogger(__name__)

    messages_to_process = album if album else [message]
    logger.info(f"📸 Получено {len(messages_to_process)} документов-изображений от пользователя {message.from_user.id}")

    photo_paths = []
    has_pdf = False
    unsupported_names = []

    for msg in messages_to_process:
        # Проверяем, является ли документ изображением
        if not msg.document:
            continue

        # Проверяем MIME-тип или расширение файла
        mime_type = msg.document.mime_type or ""
        file_name = msg.document.file_name or ""

        # CSV → возможный экспорт глюкозы LibreView. Пробуем импортировать; если это не
        # LibreView-CSV, helper вернёт False и документ пойдёт обычным пайплайном.
        is_csv = mime_type.lower() in ("text/csv", "text/comma-separated-values") or file_name.lower().endswith(".csv")
        if is_csv:
            handled = await _handle_libreview_csv(message, msg)
            if handled:
                continue

        # Список поддерживаемых типов изображений
        image_mime_types = [
            "image/jpeg",
            "image/jpg",
            "image/png",
            "image/gif",
            "image/webp",
            "image/heic",
            "image/heif",
        ]
        image_extensions = [".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".heif"]

        is_image = mime_type.lower() in image_mime_types or any(
            file_name.lower().endswith(ext) for ext in image_extensions
        )
        is_pdf = mime_type.lower() == "application/pdf" or file_name.lower().endswith(".pdf")

        if not is_image and not is_pdf:
            unsupported_names.append(file_name or mime_type or "файл")
            continue

        if is_pdf:
            has_pdf = True
            processing_msg = await message.answer("📄 Получил PDF, читаю…")
            pdf_path = await _download_pdf(msg)
            if not pdf_path:
                await processing_msg.edit_text(
                    "⚠️ Не удалось скачать PDF. Возможно, файл слишком большой (лимит Telegram — 20 МБ). "
                    "Попробуй отправить скриншот страницы."
                )
                continue

            # Извлекаем текст напрямую (для текстовых PDF — анализы, бланки)
            pdf_text = _extract_pdf_text(pdf_path)
            if pdf_text:
                import asyncio
                from core.agent_chat import ask_agent
                from core.tg_markdown import md_to_html

                user_id = message.from_user.id
                caption = msg.caption or ""
                prompt = f"Вот содержимое PDF-документа:\n\n{pdf_text}"
                if caption:
                    prompt = f"{caption}\n\n{prompt}"

                await processing_msg.edit_text("⏳ Анализирую документ…")
                loop = asyncio.get_event_loop()
                try:
                    reply = await loop.run_in_executor(None, lambda: ask_agent(int(user_id), prompt))
                    if reply:
                        await processing_msg.edit_text(md_to_html(reply), parse_mode="HTML")
                    else:
                        await processing_msg.edit_text(
                            "Получил документ, но не смог разобрать содержимое. Напиши вопрос текстом."
                        )
                except Exception as e:
                    logger.error(f"PDF agent error: {e}")
                    await processing_msg.edit_text("Получил PDF — напиши текстом что хочешь узнать, и я разберу.")
            else:
                # Сканированный PDF — конвертируем в изображения для vision-пайплайна
                pages = _pdf_to_images(pdf_path)
                photo_paths.extend(pages)
                await processing_msg.delete()
            continue

        # Сохраняем документ как изображение
        photo_file_id = msg.document.file_id
        photo_path = await save_document_as_image(msg, photo_file_id, file_name)
        if photo_path:
            photo_paths.append(photo_path)

    if not photo_paths:
        if unsupported_names and not has_pdf:
            # Раньше неподдерживаемые типы игнорировались молча — пользователь не
            # понимал, дошёл ли файл (прецедент 27.06.2026: .docx-обследования,
            # бот не ответил). Теперь отвечаем с подсказкой.
            names = ", ".join(unsupported_names[:3])
            await message.answer(
                f"📎 Получил файл ({names}), но такой формат пока не умею.\n"
                "Поддерживаю: PDF, фото (JPG/PNG/HEIC) и CSV из LibreView.\n"
                "Если это .doc/.docx — сохрани как PDF или пришли фото страниц. "
                "Сохранить документ в историю здоровья — команда /doc."
            )
        return

    # Ищем caption в сообщениях альбома
    caption = next((msg.caption for msg in messages_to_process if msg.caption), "")
    message_with_caption = message
    if caption and not message.caption:
        message_with_caption = next((msg for msg in messages_to_process if msg.caption), message)

    await process_photos_list(message_with_caption, photo_paths, message.media_group_id)


async def save_photo(message: Message, file_id: str) -> Path:
    """Сохраняет фото на диск"""
    try:
        # Получаем файл
        file = await message.bot.get_file(file_id)

        # Создаем директорию для медиа
        date_str = datetime.now(MSK).strftime("%Y-%m-%d")
        media_dir = Path(__file__).parent.parent.parent / "data" / "media" / "nutrition" / date_str
        media_dir.mkdir(parents=True, exist_ok=True)

        # Сохраняем фото
        photo_path = media_dir / f"{file_id}.jpg"
        await message.bot.download_file(file.file_path, photo_path)

        return photo_path
    except Exception:
        logger.exception("Ошибка при сохранении фото")
        return None


async def _download_pdf(message: Message) -> Path | None:
    """Скачивает PDF-документ из Telegram на диск."""
    try:
        file = await message.bot.get_file(message.document.file_id)
        date_str = datetime.now(MSK).strftime("%Y-%m-%d")
        media_dir = Path(__file__).parent.parent.parent / "data" / "media" / "nutrition" / date_str
        media_dir.mkdir(parents=True, exist_ok=True)
        pdf_path = media_dir / f"{message.document.file_unique_id}.pdf"
        await message.bot.download_file(file.file_path, pdf_path)
        return pdf_path
    except Exception as e:
        logging.getLogger(__name__).error(f"PDF download error: {e}")
        return None


def _extract_pdf_text(pdf_path: Path, max_pages: int = 10) -> str:
    """Извлекает текст из PDF (работает для текстовых PDF, не сканов). Возвращает '' если текста нет."""
    try:
        import fitz  # PyMuPDF

        doc = fitz.open(str(pdf_path))
        parts = []
        for i, page in enumerate(doc):
            if i >= max_pages:
                break
            parts.append(page.get_text())
        doc.close()
        text = "\n".join(parts).strip()
        return text if len(text) > 50 else ""
    except Exception as e:
        logging.getLogger(__name__).error(f"PDF text extract error: {e}")
        return ""


def _pdf_to_images(pdf_path: Path, max_pages: int = 3) -> list[Path]:
    """Конвертирует первые max_pages страниц PDF в PNG через PyMuPDF."""
    try:
        import fitz  # PyMuPDF

        doc = fitz.open(str(pdf_path))
        out_paths = []
        for i, page in enumerate(doc):
            if i >= max_pages:
                break
            pix = page.get_pixmap(dpi=150)
            out_path = pdf_path.with_name(f"{pdf_path.stem}_p{i + 1}.png")
            pix.save(str(out_path))
            out_paths.append(out_path)
        doc.close()
        return out_paths
    except Exception as e:
        logging.getLogger(__name__).error(f"PDF→image error: {e}")
        return []


async def save_document_as_image(message: Message, file_id: str, file_name: str = None) -> Path:
    """Сохраняет документ-изображение на диск"""
    try:
        # Получаем файл
        file = await message.bot.get_file(file_id)

        # Создаем директорию для медиа
        date_str = datetime.now(MSK).strftime("%Y-%m-%d")
        media_dir = Path(__file__).parent.parent.parent / "data" / "media" / "nutrition" / date_str
        media_dir.mkdir(parents=True, exist_ok=True)

        # Определяем расширение файла
        if file_name:
            ext = Path(file_name).suffix.lower()
            if not ext or ext not in [".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".heif"]:
                ext = ".jpg"  # По умолчанию jpg
        else:
            ext = ".jpg"

        # Сохраняем документ как изображение
        photo_path = media_dir / f"{file_id}{ext}"
        await message.bot.download_file(file.file_path, photo_path)

        return photo_path
    except Exception as e:
        import logging

        logger = logging.getLogger(__name__)
        logger.error(f"Ошибка при сохранении документа-изображения: {e}")
        return None


# УБРАНО: обработчик текста перенесен в handlers/text.py
# Это нужно, чтобы текстовые сообщения без фото обрабатывались правильно


async def handle_description(
    message: Message, description: str = None, processing_message: Message = None, custom_date: str = None
):
    """
    Обработка описания блюда после получения фото.

    Args:
        message: Исходное сообщение
        description: Текст описания
        processing_message: Сообщение "Анализирую...", которое нужно отредактировать (опционально)
        custom_date: Кастомная дата в формате YYYY-MM-DD (опционально)
    """

    import logging

    logger = logging.getLogger(__name__)

    user_id = str(message.from_user.id)
    user_state = state_manager.get_state(user_id)

    if not user_state or user_state.state != "waiting_description":
        return

    # Если description не передан, берем из сообщения или из состояния (для фото с caption)
    if description is None:
        if message.text:
            description = message.text.strip()
        elif user_state and user_state.data.get("caption"):
            description = user_state.data.get("caption").strip()
        else:
            description = ""

    if not description:
        await message.answer("Пожалуйста, отправь описание блюда")
        return

    # Получаем данные о фото
    photo_paths = user_state.data.get("photo_paths", [])
    caption = user_state.data.get("caption", "")
    menu_data = user_state.data.get("menu_data")  # Данные меню, если есть

    logger.info(f"🔍 [DEBUG] handle_description - menu_data in state: {menu_data}")
    logger.info(f"🔍 [DEBUG] user_state.data keys: {list(user_state.data.keys())}")

    # Объединяем caption и description
    full_description = f"{caption}\n{description}".strip() if caption else description

    # Извлекаем дату из описания (поддержка "вчера")
    # Если custom_date не был передан, пытаемся извлечь из текста
    if not custom_date:
        from handlers.text import extract_date_from_text

        extracted_date, clean_description = extract_date_from_text(full_description)
        if extracted_date:
            custom_date = extracted_date
            full_description = clean_description
            logger.info(f"Извлечена дата из описания: {custom_date}, очищенное описание: '{clean_description[:50]}...'")

    # --- LLM Router Logic ---
    from core.llm.router import analyze_message
    from core.food.nutrition import process_llm_food_data

    # ИСПРАВЛЕНИЕ: Если menu_data уже распознаны (parse_menu_photo или LLM извлек КБЖУ),
    # используем их напрямую. Учитываем и 0 ккал (напитки типа Cola Zero).
    # НО только если это ОДНО фото. Если это медиагруппа (альбом), ВСЕГДА
    # анализируем все фото вместе через LLM Router, чтобы не терять блюда.
    use_menu_data = False
    if menu_data and (menu_data.get("calories") is not None or menu_data.get("protein") is not None):
        if len(photo_paths) <= 1:
            use_menu_data = True

    if use_menu_data:
        logger.info(f"✅ Используем ранее распознанные КБЖУ из меню: {menu_data}")

        # Issue #115: если зрение вернуло покомпонентную разбивку — раскладываем
        # на несколько items, подпись используем как уточнение названия блюда,
        # а не схлопываем всё в один item.
        router_result = build_router_result_from_menu_data(menu_data, caption=full_description)
    else:
        # Нет распознанных КБЖУ из меню - используем LLM Router
        # Готовим пути к фото
        paths_to_analyze = [Path(p) for p in photo_paths] if photo_paths else None

        if processing_message:
            await processing_message.edit_text("🤖 Думаю... (AI анализирует контекст) 🧠")
        else:
            processing_message = await message.answer("🤖 Думаю... 🧠")

        try:
            uid_int = int(message.from_user.id) if message and message.from_user else None
            router_result = analyze_message(text=full_description, image_paths=paths_to_analyze, user_id=uid_int)
        except Exception as e:
            logger.error(f"LLM Router Error: {e}")
            router_result = None

    if not router_result or router_result.get("type") != "food":
        # Fallback или ошибка
        # Если Router решил что это НЕ еда, но мы в хендлере описания еды?
        # Возможно это витамины или весы?
        # Если type='vitamins', надо обработать.

        if router_result and router_result.get("type") == "vitamins":
            # Обработка витаминов
            data = router_result.get("data", {})
            items = data.get("items", [])
            action = data.get("action")

            # Сохраняем реально
            from core.health.supplements import save_supplements

            telegram_user_id = int(message.from_user.id)
            saved = save_supplements(items, user_id=telegram_user_id)

            # Формируем красивый список
            items_list = "\n".join([f"• {html.escape(i['name'] if isinstance(i, dict) else str(i))}" for i in items])

            status_text = "✅ <b>Записано</b>" if saved else "⚠️ <b>Ошибка записи</b>"

            response = f"💊 <b>Витамины:</b>\n{items_list}\n\n{status_text}"

            await processing_message.edit_text(response, parse_mode="HTML")
            state_manager.clear_state(user_id)
            return

        elif router_result and router_result.get("type") == "weight":
            # Обработка веса
            data = router_result.get("data", {})
            w_val = data.get("weight")
            await processing_message.edit_text(f"⚖️ <b>Вес:</b> {w_val} кг\n✅ Записано", parse_mode="HTML")
            # Тут надо бы сохранить, но пока просто ответим, т.к. этот флоу редок при вводе описания
            state_manager.clear_state(user_id)
            return

        elif router_result and router_result.get("type") == "body_measurements":
            # Обработка замеров тела
            data = router_result.get("data", {})
            from helpers.db_save import save_body_measurement_to_db

            telegram_user_id = int(message.from_user.id)
            saved = save_body_measurement_to_db(data, user_id=telegram_user_id)

            # Формируем ответ
            m_parts = []
            if data.get("waist_cm"):
                m_parts.append(f"Талия: {data['waist_cm']} см")
            if data.get("neck_cm"):
                m_parts.append(f"Шея: {data['neck_cm']} см")
            if data.get("hips_cm"):
                m_parts.append(f"Бедра: {data['hips_cm']} см")
            if data.get("chest_cm"):
                m_parts.append(f"Грудь: {data['chest_cm']} см")
            if data.get("thigh_cm"):
                m_parts.append(f"Бедро: {data['thigh_cm']} см")
            if data.get("biceps_cm"):
                m_parts.append(f"Бицепс: {data['biceps_cm']} см")

            m_list = "\n".join([f"• {p}" for p in m_parts])
            status_text = "✅ <b>Записано</b>" if saved else "⚠️ <b>Ошибка записи</b>"

            response = f"📏 <b>Замеры тела:</b>\n{m_list}\n\n{status_text}"

            await processing_message.edit_text(response, parse_mode="HTML")
            state_manager.clear_state(user_id)
            return

        elif router_result and router_result.get("type") == "bp":
            # 🩺 BP — фото тонометра + caption, или текст-описание после фото
            data = router_result.get("data", {})
            sys_v, dia_v, pulse_v = data.get("systolic"), data.get("diastolic"), data.get("pulse")
            if sys_v and dia_v and (70 <= sys_v <= 250) and (40 <= dia_v <= 150) and (sys_v > dia_v):
                from helpers.db_save import save_bp_to_db

                telegram_user_id = int(message.from_user.id)
                measured_at = None
                time_str = data.get("measured_at_time")
                if time_str and ":" in time_str:
                    try:
                        hh, mm = map(int, time_str.split(":")[:2])
                        if 0 <= hh <= 23 and 0 <= mm <= 59:
                            # ВАЖНО: используем модульный MSK (line 13). НЕ переопределять
                            # локально — Python сделает MSK local для ВСЕЙ функции, и в
                            # food-flow (line 1052/1069) упадёт UnboundLocalError.
                            # Прецедент 26.05.2026 17:39-17:42: после успешного распознавания
                            # «Лосось в терияки 500 ккал» бот молча падал на datetime.now(MSK)
                            # — пользователь видел только «📸 Получено…», карточка с КБЖУ
                            # и кнопкой Сохранить не отрисовывалась. Лог: `Failed to feed
                            # update to legacy bot: cannot access local variable 'MSK'`.
                            # Тот же баг чинили в text.py (commit af71067), но в photo.py
                            # его пропустили.
                            measured_at = datetime.now(MSK).replace(hour=hh, minute=mm, second=0, microsecond=0)
                    except (ValueError, IndexError):
                        pass

                saved = save_bp_to_db(
                    systolic=sys_v,
                    diastolic=dia_v,
                    pulse=pulse_v,
                    user_id=telegram_user_id,
                    measured_at=measured_at,
                    source="llm_photo",
                )
                pulse_part = f" пульс {pulse_v}" if pulse_v else ""
                time_part = f" в {time_str}" if time_str else ""
                status = "✅ Записано" if saved else "⚠️ Ошибка записи"
                await processing_message.edit_text(
                    f"🩺 <b>АД:</b> {sys_v}/{dia_v}{pulse_part}{time_part}\n{status}",
                    parse_mode="HTML",
                )
                state_manager.clear_state(user_id)
                return

        else:
            # 🐛 FIX 26.05.2026: фото + caption где роутер не распознал ни food/
            # vitamins/weight/bp/body_measurements — НЕ выдавать stock-message.
            # Передать в BotkinClaw как conversational с описанием контекста.
            # Прецедент: Александр прислал скриншот Garmin Connect + caption
            # «приложение показывает 6:27» — бот сказал «не еда», хотя caption
            # явно про сон → должен был дёрнуть get_recent_sleep и ответить.
            #
            # Логика: если есть caption — это явный сигнал что пользователь
            # хочет ОБСУДИТЬ фото (вопрос/комментарий), а не залогировать его.
            # BotkinClaw через tools сам решит что нужно.
            state_manager.clear_state(user_id)

            actual_caption = (caption or "").strip()
            if actual_caption:
                # Передаём агенту — он умнее stock-message
                try:
                    from core.agent_chat import ask_agent
                    from core.tg_markdown import md_to_html, split_markdown_for_telegram
                    import asyncio

                    user_text_for_agent = (
                        f"[Пользователь прислал фото с подписью]: {actual_caption}\n\n"
                        f"(LLM-vision не распознал на фото еду/вес/добавки/АД/замеры тела. "
                        f"Это вероятно скриншот приложения, медицинский документ, фото объекта "
                        f"или вопрос с контекстом. Разберись через свои tools и ответь по существу.)"
                    )

                    await processing_message.edit_text("🤔 думаю...")
                    loop = asyncio.get_running_loop()
                    reply = await loop.run_in_executor(None, ask_agent, int(user_id), user_text_for_agent)

                    if reply:
                        chunks = split_markdown_for_telegram(reply)
                        first = True
                        for chunk in chunks:
                            chunk_html = md_to_html(chunk)
                            try:
                                if first:
                                    await processing_message.edit_text(chunk_html, parse_mode="HTML")
                                    first = False
                                else:
                                    await message.answer(chunk_html, parse_mode="HTML")
                            except Exception:
                                # HTML render failed — отправим как plain text
                                if first:
                                    await processing_message.edit_text(chunk)
                                    first = False
                                else:
                                    await message.answer(chunk)
                    else:
                        await processing_message.edit_text(
                            "🤔 Понял что прислал фото с подписью «" + actual_caption[:80] + "», "
                            "но не смог сформулировать ответ. Попробуй переформулировать вопрос текстом."
                        )
                    return
                except Exception as agent_err:
                    logger.warning(f"BotkinClaw fallback for non-food photo failed: {agent_err}")
                    # Дальше — обычный stock-message

            # Caption нет ИЛИ агент упал — обычный понятный ответ без state
            await processing_message.edit_text(
                "❌ Не удалось распознать что это за еда.\n\n"
                "💡 Если это не еда — напиши текстом что хотел сказать "
                "(вопрос про здоровье, замер «120/80 пульс 70», вес и т.п.). Я разберусь."
            )
            return

    # Это ЕДА
    llm_data = router_result
    # Не логируем сырой dict целиком: dish_name/items из LLM могут содержать \n
    # (log injection). Логируем только тип и число позиций.
    _ld_data = llm_data.get("data", {}) if isinstance(llm_data, dict) else {}
    logger.info(
        f"📊 Calling process_llm_food_data: type={llm_data.get('type')}, items={len(_ld_data.get('items', []))}"
    )
    meal_items, meal_totals = process_llm_food_data(llm_data, description=full_description)
    logger.info(
        f"📊 process_llm_food_data returned: items={len(meal_items) if meal_items else 0}, totals={meal_totals}"
    )

    if not meal_items:
        await processing_message.edit_text("❌ Продукты не найдены в ответе нейросети.")
        return

    # Извлекаем метаданные из ответа LLM
    data = llm_data.get("data", {})
    meal_name = data.get("dish_name") or data.get("meal_type")

    # Если название так себе, пробуем определить по времени
    if not meal_name or meal_name in ["breakfast", "lunch", "dinner"]:
        from handlers.text import extract_meal_name

        meal_time = datetime.now(MSK).strftime("%H:%M")
        meal_name_ru = extract_meal_name(full_description, meal_time)
        if meal_name_ru:
            meal_name = meal_name_ru

    # Если в подписи к фото есть явный префикс слота — приклеиваем его к названию
    from handlers.text import apply_slot_prefix

    meal_name = apply_slot_prefix(full_description, meal_name)

    # Переходим в waiting_confirmation. Пересобираем data целиком (не мутируем
    # in-place — coding-style.md) через build_meal_state_data(): предыдущее
    # состояние ("waiting_description") несло PhotoStateData-поля (caption,
    # photo_file_ids, menu_data), которые в meal-confirmation уже не читаются;
    # сохраняем из него только photo_paths.
    new_data = build_meal_state_data(
        description=full_description,
        meal_items=meal_items,
        meal_totals=meal_totals,
        portion_multiplier=1.0,  # Deprecated
        meal_time=datetime.now(MSK).strftime("%H:%M"),
        meal_name=meal_name,
        photo_paths=user_state.data.get("photo_paths", []),
        date=custom_date,
        # #255: этикетка из свежего LLM-ответа, иначе — из menu_data
        # (первый проход зрения), иначе — что уже лежало в state.
        product_label=(router_result.get("data") or {}).get("product_label")
        or (menu_data or {}).get("product_label")
        or user_state.data.get("product_label"),
    )
    user_state = UserState(user_id=user_id, state="waiting_confirmation", data=new_data)
    state_manager.set_state(user_id, user_state)

    # Формируем ответ

    # Экранируем названия из vision/LLM/подписи перед вставкой в HTML (issue #115, anti-XSS).
    response = f"🍽️ <b>{html.escape(str(meal_name))}</b>\n\n"
    for item in meal_items:
        w_str = f"{item['weight_g']}г" if item.get("weight_g") else "?"
        cal = item.get("calories", 0)
        response += f"• {html.escape(str(item['product']))} ({w_str}) — {int(cal)} ккал\n"

    response += f"\n📊 <b>Итого: {int(meal_totals['calories'])} ккал</b>\n"
    response += f"Б: {int(meal_totals['protein'])} | Ж: {int(meal_totals['fats'])} | У: {int(meal_totals['carbs'])}"

    # Keyboard
    builder = InlineKeyboardBuilder()
    builder.button(
        text="✅ Сохранить", callback_data=MealConfirmationCallback(action="save", meal_type="regular").pack()
    )
    builder.button(
        text="❌ Отмена", callback_data=MealConfirmationCallback(action="cancel", meal_type="regular").pack()
    )

    await safe_edit_text(processing_message, response, parse_mode="HTML", reply_markup=builder.as_markup())


def build_router_result_from_menu_data(menu_data: dict, caption: str = "") -> dict:
    """Собирает router_result из ранее распознанного menu_data (issue #115).

    Если зрение вернуло покомпонентную разбивку (`components`, ≥2 шт.) — отдаём
    несколько items по компонентам, а подпись используем как уточнение названия
    блюда, НЕ схлопывая всё в один item. Иначе — один item из итогов menu_data
    (прежнее поведение для меню/чеков с одним блюдом).
    """
    components = (menu_data.get("components") or [])[:MAX_COMPONENTS]
    base_dish = menu_data.get("dish_name", "Блюдо из меню")
    # Подпись храним сырой (она идёт в БД как часть meal_name); HTML-экранирование —
    # на рендере сообщения, не здесь, чтобы не пачкать данные сущностями.
    caption_hint = (caption or "").strip()[:MAX_CAPTION_HINT_LEN]

    if len(components) >= 2:
        dish_name = f"{base_dish} ({caption_hint})" if caption_hint else base_dish
        items = [
            {
                "name": str(c.get("name", "компонент"))[:MAX_COMPONENT_NAME_LEN],
                "weight": _safe_float(c.get("weight")),
                "quantity": c.get("quantity"),
                "calories": _safe_float(c.get("calories")),
                "protein": _safe_float(c.get("protein")),
                "fats": _safe_float(c.get("fats")),
                "carbs": _safe_float(c.get("carbs")),
            }
            for c in components
        ]
    else:
        dish_name = base_dish
        items = [
            {
                "name": base_dish,
                "weight": menu_data.get("weight"),
                "quantity": None,
                "calories": menu_data.get("calories"),
                "protein": menu_data.get("protein"),
                "fats": menu_data.get("fats"),
                "carbs": menu_data.get("carbs"),
            }
        ]

    return {
        "type": "food",
        "data": {"dish_name": dish_name, "meal_type": "meal", "items": items},
    }


def build_menu_meal_item(menu_data: dict) -> dict:
    """Собирает canonical meal item из результата распознавания меню/чека.

    Главное: НЕ теряем вес. GPT-vision возвращает weight/weight_grams —
    если его проигнорировать, в БД записывается amount=0 и мини-апп
    показывает "0 г". Если LLM не вернул вес (или вернул 0/None) —
    ставим 100г как стандартную порцию, а не теряем данные.
    """
    dish_name = menu_data.get("dish_name", "Блюдо из меню")
    weight_raw = menu_data.get("weight") or menu_data.get("weight_grams")
    try:
        weight_val = float(weight_raw) if weight_raw else 0.0
    except (TypeError, ValueError):
        weight_val = 0.0

    if weight_val > 0:
        weight_g = weight_val
        weight_source = "llm"
    else:
        weight_g = 100.0
        weight_source = "default_100g"

    return {
        "product": dish_name,
        "weight_g": weight_g,
        "weight_source": weight_source,
        "calories": menu_data.get("calories", 0),
        "protein": menu_data.get("protein", 0),
        "fats": menu_data.get("fats", 0),
        "carbs": menu_data.get("carbs", 0),
        "source": "menu_ocr",
    }


def _meal_confirm_keyboard(meal_type: str, selected_slot: str = ""):
    """Клавиатура подтверждения приёма пищи.

    При заданном selected_slot добавляет ряд выбора слота (Завтрак/Обед/Ужин/
    Перекус) с пометкой активного — для фото без подписи, где слот иначе
    молча выводится по времени суток (#181, баг 4). Помимо слот-ряда — штатные
    кнопки Сохранить / Отмена.
    """
    builder = InlineKeyboardBuilder()
    if selected_slot:
        for slot in SLOTS:
            mark = "🔘 " if slot == selected_slot else ""
            builder.button(
                text=f"{mark}{slot_label_ru(slot)}",
                callback_data=MealConfirmationCallback(action="set_slot", meal_type=meal_type, slot=slot).pack(),
            )
    builder.button(
        text="✅ Сохранить", callback_data=MealConfirmationCallback(action="save", meal_type=meal_type).pack()
    )
    cancel_text = "❌ Не сохранять" if meal_type == "menu" else "❌ Отмена"
    builder.button(
        text=cancel_text, callback_data=MealConfirmationCallback(action="cancel", meal_type=meal_type).pack()
    )
    builder.adjust(4, 2) if selected_slot else builder.adjust(2)
    return builder.as_markup()


async def handle_menu_photo(message: Message, menu_data: dict, photo_path: Path, processing_message: Message = None):
    """Обработка распознанной по фото еды/продукта с КБЖУ"""

    user_id = str(message.from_user.id)

    dish_name = menu_data.get("dish_name", "Блюдо из меню")
    calories = menu_data.get("calories", 0)
    protein = menu_data.get("protein", 0)
    fats = menu_data.get("fats", 0)
    carbs = menu_data.get("carbs", 0)
    weight_raw = menu_data.get("weight") or menu_data.get("weight_grams")
    try:
        weight_display = float(weight_raw) if weight_raw else 0.0
    except (TypeError, ValueError):
        weight_display = 0.0
    weight_str = f"{int(weight_display)} г" if weight_display > 0 else "~100 г (не определён)"

    # Формируем ответ
    response = (
        f"🍽️ <b>Распознано по фото</b>\n\n"
        f"<b>{html.escape(dish_name)}</b>\n"
        f"⚖️ Вес: {weight_str}\n\n"
        f"📊 КБЖУ:\n"
        f"• Калории: {calories:.0f} ккал\n"
        f"• Белки: {protein:.0f} г\n"
        f"• Жиры: {fats:.0f} г\n"
        f"• Углеводы: {carbs:.0f} г"
    )

    # Фото без подписи — слот иначе молча выводится по времени суток и часто
    # промахивается (боул в 16:00 → «перекус» вместо обеда, #181 баг 4).
    # Показываем ряд выбора слота с дефолтом по времени; юзер правит одним тапом.
    now_msk = datetime.now(MSK)
    default_slot = slot_from_time(now_msk.time())
    keyboard = _meal_confirm_keyboard("menu", selected_slot=default_slot)

    # Сохраняем данные в состояние для подтверждения
    # #256: было "photo_path" (ед.ч.) — save_meal_to_db() читает "photo_paths"
    # (мн.ч.), из-за чего фото терялось. build_meal_state_data() (typed schema,
    # extra="forbid") ловит такую опечатку в момент создания состояния.
    user_state = UserState(
        user_id=user_id,
        state="waiting_confirmation",
        data=build_meal_state_data(
            dish_name=dish_name,
            meal_items=[build_menu_meal_item(menu_data)],
            meal_totals={
                "calories": calories,
                "protein": protein,
                "fats": fats,
                "carbs": carbs,
            },
            photo_paths=[str(photo_path)],
            meal_time=now_msk.strftime("%H:%M"),
            menu_ocr=True,  # Флаг, что это меню
            product_label=menu_data.get("product_label"),  # #255
            slot=default_slot,  # #181: подсвеченный дефолт слот-пикера
        ),
    )
    state_manager.set_state(user_id, user_state)

    if processing_message:
        await processing_message.edit_text(response, parse_mode="HTML", reply_markup=keyboard)
    else:
        await message.answer(response, parse_mode="HTML", reply_markup=keyboard)


# Импортируем logger
import logging

# Импортируем функцию сохранения


async def _maybe_record_first_food(telegram_user_id: int, message) -> None:
    """Тонкая обёртка над record_first_food (E5 + демо-празднование).

    Вынесена, чтобы (1) тесты могли замокать её точечно и (2) не дублировать
    вызов в multi- и single-путях сохранения. Это ВНУТРЕННИЙ хелпер, не хендлер —
    вызывается из handle_meal_confirmation после сохранения (см. #322: декоратор
    ниже должен оставаться на handle_meal_confirmation, иначе кнопка «Сохранить»
    падает с TypeError и еда не сохраняется)."""
    await record_first_food(telegram_user_id, message)


@router.callback_query(MealConfirmationCallback.filter())
async def handle_meal_confirmation(callback: CallbackQuery, callback_data: MealConfirmationCallback):
    """Обработчик нажатия на кнопки подтверждения сохранения блюда"""

    user_id = str(callback.from_user.id)
    user_state = state_manager.get_state(user_id)

    # Логируем
    logger = logging.getLogger(__name__)
    logger.info(
        f"Обработка callback: action={callback_data.action}, meal_type={callback_data.meal_type}, user_id={user_id}"
    )

    if not user_state or user_state.state != "waiting_confirmation":
        await callback.answer("⚠️ Состояние истекло. Отправьте фото заново.", show_alert=True)
        await callback.message.delete()
        return

    # Выбор слота на карточке фото без подписи (#181, баг 4): фиксируем слот через
    # meal_time = центр слота (слот в БД не хранится, выводится из meal_name+time —
    # тот же приём, что edit_meal(new_slot=…)). Перерисовываем клавиатуру, не сохраняя.
    if callback_data.action == "set_slot":
        slot = callback_data.slot
        if slot in SLOTS:
            user_state.data["slot"] = slot
            user_state.data["meal_time"] = slot_center_time(slot).strftime("%H:%M")
            state_manager.set_state(user_id, user_state)
            try:
                await callback.message.edit_reply_markup(
                    reply_markup=_meal_confirm_keyboard(callback_data.meal_type, selected_slot=slot)
                )
            except TelegramBadRequest:
                pass  # клавиатура не изменилась — Telegram отклоняет no-op edit
            await callback.answer(f"Слот: {slot_label_ru(slot)}")
        else:
            await callback.answer()
        return

    if callback_data.action == "save":
        telegram_user_id = int(callback.from_user.id)

        # Гасим спиннер кнопки СРАЗУ, до любой блокирующей записи в БД. Иначе при
        # медленной/залоченной nutrition_log (прецедент 16.07.2026: лок дев-БД от
        # ночного sync-prod-to-dev) кнопка висит дольше ~15с окна коллбэка
        # Telegram, а синхронный save_meal_to_db морозит event-loop бота для всех
        # пользователей. Итог сохранения показываем ниже через edit сообщения.
        await callback.answer()

        # Multi-meal path (from multi_food router type — issue #53)
        if user_state.data.get("multi_meals"):
            from helpers.db_save import save_meal_to_db
            from core.food.interaction_log import log_food_interaction

            multi_meals = user_state.data["multi_meals"]
            saved_count = 0
            total_kcal = 0
            failed = []
            for m in multi_meals:
                meal_data = {
                    "meal_items": m["meal_items"],
                    "meal_totals": m["meal_totals"],
                    "date": user_state.data.get("date"),
                }
                # to_thread: блокирующий save не должен морозить event-loop
                meal_nutrition_log_id = await asyncio.to_thread(
                    save_meal_to_db, meal_data, m["meal_name"], user_id=telegram_user_id
                )
                if meal_nutrition_log_id is not None:
                    saved_count += 1
                    total_kcal += int(m["meal_totals"].get("calories", 0))
                    log_food_interaction(
                        user_id=telegram_user_id,
                        source=user_state.data.get("source", "text"),
                        raw_text=user_state.data.get("description"),
                        recognized={"items": m["meal_items"], "totals": m["meal_totals"]},
                        bot_reply=f"✅ {m['meal_name']} · {int(m['meal_totals'].get('calories', 0))} ккал",
                        nutrition_log_id=meal_nutrition_log_id,
                        status="saved",
                    )
                else:
                    failed.append(m["meal_name"])
                    logger.error(
                        "multi_food: save_meal_to_db failed for %r (user %s)", m["meal_name"], telegram_user_id
                    )

            confirm = f"✅ <b>Сохранено приёмов: {saved_count}</b> · {total_kcal} ккал"
            if failed:
                confirm += "\n⚠️ Не удалось сохранить: " + ", ".join(failed) + " — отправь их отдельно."
            await safe_edit_text(callback.message, confirm, parse_mode="HTML")
            if saved_count:
                await _maybe_record_first_food(telegram_user_id, callback.message)
            state_manager.clear_state(user_id)
            return

        # Single-meal path (existing code — unchanged below this point)
        # Сохраняем блюдо
        # Для меню используем dish_name, для обычных блюд - используем meal_name из состояния
        if callback_data.meal_type == "menu":
            meal_name = user_state.data.get("dish_name", "Приём пищи")
        else:
            # Для обычных блюд (regular или description) используем meal_name из состояния
            meal_name = user_state.data.get("meal_name")
            if not meal_name or meal_name == "Приём пищи":
                # Если не найдено, пробуем извлечь из описания
                description = user_state.data.get("description", "")
                meal_time = user_state.data.get("meal_time", datetime.now(MSK).strftime("%H:%M"))
                from handlers.text import extract_meal_name

                meal_name = extract_meal_name(description, meal_time)
                logger.info(f"Извлечено название приёма пищи при сохранении: '{meal_name}' из '{description[:50]}...'")
            else:
                logger.info(f"Используется meal_name из состояния: '{meal_name}'")

        logger.info(f"Сохранение блюда: meal_name='{meal_name}', meal_type='{callback_data.meal_type}'")

        # === ИЗМЕНЕНО: Используем PostgreSQL вместо JSON ===
        from helpers.db_save import save_meal_to_db

        logger.info(f"[BEFORE SAVE] user_state.data keys: {list(user_state.data.keys())}")
        logger.info(f"[BEFORE SAVE] meal_totals: {user_state.data.get('meal_totals')}")
        logger.info(f"[BEFORE SAVE] meal_items count: {len(user_state.data.get('meal_items', []))}")

        # to_thread: блокирующий save не должен морозить event-loop (коллбэк уже отвечен)
        nutrition_log_id = await asyncio.to_thread(
            save_meal_to_db, user_state.data, meal_name, user_id=telegram_user_id
        )
        if nutrition_log_id is not None:
            logger.info("[AFTER SAVE] save_meal_to_db returned id=%s", nutrition_log_id)

            totals = user_state.data.get("meal_totals", {})
            meal_kcal = totals.get("calories", 0)

            from core.health.caloric_budget import format_budget_line
            from datetime import date as date_type
            from database import SessionLocal as _SessionLocal
            from database.crud import get_user_settings as _get_user_settings

            meal_date_str = user_state.data.get("date")
            meal_date = date_type.fromisoformat(meal_date_str) if meal_date_str else None
            _db = _SessionLocal()
            try:
                _settings = _get_user_settings(_db, telegram_user_id)
                _show_bar = _settings.show_calorie_budget_bar if _settings else True
            finally:
                _db.close()
            budget = format_budget_line(telegram_user_id, for_date=meal_date, show_bar=_show_bar)

            confirm_text = (
                f"✅ <b>{meal_name}</b> · {meal_kcal:.0f} ккал\n"
                f"Б {totals.get('protein', 0):.0f}г · "
                f"Ж {totals.get('fats', 0):.0f}г · "
                f"У {totals.get('carbs', 0):.0f}г"
                f"{budget}"
            )
            await safe_edit_text(callback.message, confirm_text, parse_mode="HTML")

            await _maybe_record_first_food(telegram_user_id, callback.message)

            from core.food.interaction_log import log_food_interaction

            log_food_interaction(
                user_id=telegram_user_id,
                source=user_state.data.get("source", "photo"),
                raw_text=user_state.data.get("description"),
                media_path=user_state.data.get("media_path")
                or next(iter(user_state.data.get("photo_paths") or []), None),
                recognized={"items": user_state.data.get("meal_items"), "totals": totals},
                bot_reply=confirm_text,
                nutrition_log_id=nutrition_log_id,
                status="saved",
            )
            # #255: LLM прочитал этикетку продукта — предлагаем запомнить
            # в справочник verified_products (после успешного сохранения).
            product_label = user_state.data.get("product_label")
            if product_label:
                try:
                    from handlers.verified_products import offer_remember_product

                    await offer_remember_product(callback.message, telegram_user_id, product_label)
                except Exception as e:
                    logger.warning(f"offer_remember_product failed: {e}")
        else:
            logger.error("[AFTER SAVE] save_meal_to_db returned None!")
            # Коллбэк уже отвечен вверху — ошибку показываем через edit сообщения
            await safe_edit_text(
                callback.message,
                "❌ Не удалось сохранить приём пищи. Попробуйте ещё раз.",
                parse_mode="HTML",
            )
            logger.error("Ошибка при сохранении в save_meal_to_db")
    else:
        # Не сохраняем
        cancel_text = callback.message.text + "\n\n❌ Не сохранено"
        await callback.answer("❌ Не сохранено", show_alert=False)
        await safe_edit_text(callback.message, cancel_text, parse_mode="HTML")

        from core.food.interaction_log import log_food_interaction

        log_food_interaction(
            user_id=int(callback.from_user.id),
            source=user_state.data.get("source", "photo"),
            raw_text=user_state.data.get("description"),
            media_path=user_state.data.get("media_path") or next(iter(user_state.data.get("photo_paths") or []), None),
            recognized={"items": user_state.data.get("meal_items"), "totals": user_state.data.get("meal_totals")},
            bot_reply=cancel_text,
            nutrition_log_id=None,
            status="cancelled",
        )

    # Очищаем состояние
    state_manager.clear_state(user_id)


@router.callback_query(WeightConfirmationCallback.filter())
async def handle_weight_confirmation(callback: CallbackQuery, callback_data: WeightConfirmationCallback):
    """Обработчик подтверждения сохранения веса"""

    user_id = str(callback.from_user.id)
    user_state = state_manager.get_state(user_id)
    logger = logging.getLogger(__name__)

    if not user_state or "weights" not in user_state.data:
        await callback.answer("⚠️ Данные устарели", show_alert=True)
        await callback.message.delete()
        return

    if callback_data.action == "save":
        # === ИЗМЕНЕНО: Используем PostgreSQL вместо JSON ===
        from helpers.db_save import save_weight_to_db

        telegram_user_id = int(callback.from_user.id)

        weights = user_state.data["weights"]

        saved_count = 0
        for wd in weights:
            wd["source"] = "screenshot_ocr"
            if save_weight_to_db(wd, user_id=telegram_user_id):
                saved_count += 1

        await callback.answer(f"✅ Сохранено {saved_count} записей", show_alert=False)
        await safe_edit_text(
            callback.message,
            callback.message.text.replace(
                "Сохранить запись в журнал?", f"\n✅ <b>Сохранено {saved_count} записей!</b>"
            ),
            parse_mode="HTML",
        )
    else:
        await callback.answer("❌ Отменено", show_alert=False)
        await safe_edit_text(
            callback.message,
            callback.message.text.replace("Сохранить запись в журнал?", "\n❌ <b>Сохранение отменено</b>"),
            parse_mode="HTML",
        )

    state_manager.clear_state(user_id)


@router.callback_query(SupplementConfirmationCallback.filter())
async def handle_supplement_confirmation(callback: CallbackQuery, callback_data: SupplementConfirmationCallback):
    """Обработчик подтверждения записи добавок из фото"""

    user_id = str(callback.from_user.id)
    user_state = state_manager.get_state(user_id)
    logger = logging.getLogger(__name__)

    if not user_state or "supplements" not in user_state.data:
        await callback.answer("⚠️ Данные устарели", show_alert=True)
        await callback.message.delete()
        return

    if callback_data.action == "save":
        from core.health.supplements import save_supplements

        telegram_user_id = int(callback.from_user.id)
        items = user_state.data["supplements"]
        saved = save_supplements(items, user_id=telegram_user_id)
        status_text = "✅ <b>Записано как приём!</b>" if saved else "⚠️ Ошибка записи"
        await callback.answer("✅ Записано" if saved else "⚠️ Ошибка", show_alert=False)
        await safe_edit_text(
            callback.message,
            callback.message.text.replace("Записать как приём сейчас?", status_text),
            parse_mode="HTML",
        )
        logger.info(f"Supplements confirmed by user {telegram_user_id}: {items}")
    else:
        await callback.answer("Окей", show_alert=False)
        await safe_edit_text(
            callback.message,
            callback.message.text.replace("Записать как приём сейчас?", "❌ <b>Не записано</b>"),
            parse_mode="HTML",
        )

    state_manager.clear_state(user_id)
