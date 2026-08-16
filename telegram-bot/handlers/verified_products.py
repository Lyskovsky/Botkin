"""Кнопка «💾 Запомнить продукт» — автонаполнение справочника verified_products (#255).

После сохранения приёма пищи, в котором LLM прочитал этикетку (product_label),
пользователю предлагается одной кнопкой запомнить продукт. Никакого ручного
CRUD — урок /my_products (0 строк за всё время, удалена 2026-04-21).
"""

import html
import logging

from aiogram import Router
from aiogram.filters.callback_data import CallbackData
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

logger = logging.getLogger(__name__)

router = Router()

# Ожидающие подтверждения этикетки, user_id → product_label dict.
# In-memory: рестарт бота теряет «висящие» предложения — приемлемо,
# продукт запомнится при следующем фото той же этикетки.
_pending_labels: dict = {}


class RememberProductCallback(CallbackData, prefix="rememberprod"):
    action: str  # save | skip


def label_is_complete(label: dict) -> bool:
    """Минимум для записи в справочник: имя + все 4 макро на 100 г."""
    if not label or not (label.get("name") or "").strip():
        return False
    required = ("calories_per_100g", "protein_per_100g", "fats_per_100g", "carbs_per_100g")
    return all(label.get(k) is not None for k in required)


async def offer_remember_product(message: Message, user_id: int, label: dict) -> bool:
    """Показывает предложение запомнить продукт. True — предложение отправлено."""
    if not label_is_complete(label):
        return False

    _pending_labels[user_id] = label

    builder = InlineKeyboardBuilder()
    builder.button(text="💾 Запомнить продукт", callback_data=RememberProductCallback(action="save").pack())
    builder.button(text="✖️ Не надо", callback_data=RememberProductCallback(action="skip").pack())
    builder.adjust(2)

    brand = f" ({html.escape(str(label['brand']))})" if label.get("brand") else ""
    portion = f", порция {label['portion_g']:g} г" if label.get("portion_g") else ""
    fiber = f", клетчатка {label['fiber_per_100g']:g}" if label.get("fiber_per_100g") is not None else ""
    await message.answer(
        f"📋 Прочитал этикетку: <b>{html.escape(str(label['name']))}</b>{brand}\n"
        f"На 100 г: {label['calories_per_100g']:g} ккал, "
        f"Б {label['protein_per_100g']:g} / Ж {label['fats_per_100g']:g} / У {label['carbs_per_100g']:g}"
        f"{fiber}{portion}\n\n"
        f"Запомнить? В следующий раз возьму эти цифры, а не оценку по фото.",
        parse_mode="HTML",
        reply_markup=builder.as_markup(),
    )
    return True


@router.callback_query(RememberProductCallback.filter())
async def handle_remember_product(callback: CallbackQuery, callback_data: RememberProductCallback):
    user_id = int(callback.from_user.id)
    label = _pending_labels.pop(user_id, None)

    if callback_data.action != "save":
        await callback.answer("Ок, не запоминаю")
        await callback.message.delete()
        return

    if label is None:
        await callback.answer("⚠️ Данные устарели (бот перезапускался). Пришли этикетку ещё раз.", show_alert=True)
        await callback.message.delete()
        return

    try:
        from core.food.verified_products import normalize_product_name
        from database import SessionLocal, upsert_verified_product

        db = SessionLocal()
        try:
            upsert_verified_product(
                db,
                user_id=user_id,
                name=label["name"],
                name_norm=normalize_product_name(label["name"]),
                brand=label.get("brand"),
                barcode=label.get("barcode"),
                calories_per_100g=float(label["calories_per_100g"]),
                protein_per_100g=float(label["protein_per_100g"]),
                fats_per_100g=float(label["fats_per_100g"]),
                carbs_per_100g=float(label["carbs_per_100g"]),
                fiber_per_100g=label.get("fiber_per_100g"),
                portion_g=label.get("portion_g"),
                source="label_photo",
            )
        finally:
            db.close()
    except Exception as e:
        logger.error(f"remember product failed for user {user_id}: {e}", exc_info=True)
        await callback.answer("❌ Не получилось сохранить, попробуй позже", show_alert=True)
        return

    await callback.answer("✅ Запомнил!")
    await callback.message.edit_text(
        f"💾 <b>{html.escape(str(label['name']))}</b> — в справочнике. Дальше беру КБЖУ с этикетки автоматически.",
        parse_mode="HTML",
    )
