# telegram-bot/handlers/doc_upload.py
"""Handler for /doc command — user uploads medical documents to their KB."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import tempfile
from datetime import date
from pathlib import Path
from typing import Any, Optional

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from core.health.onboarding_lists import ALLERGY_KEYS, CONDITION_KEYS, onboarding_list
from database import SessionLocal
from database.crud import merge_onboarding_lists

logger = logging.getLogger(__name__)

router = Router()

# Корень проекта — два уровня выше telegram-bot/handlers/
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_UPLOADS_DIR = _PROJECT_ROOT / "data" / "uploads"

_MAX_FILE_MB = 20
_IMAGE_MIME = {"image/jpeg", "image/jpg", "image/png", "image/webp", "image/heic", "image/heif"}


class DocUpload(StatesGroup):
    waiting = State()


def _uploads_dir(user_id: int) -> Path:
    d = _UPLOADS_DIR / str(user_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _stored_name(content: bytes, ext: str) -> str:
    """Имя файла: ГГГГ-ММ-ДД_<8 hex от содержимого>.<ext>. Детерминировано, без PII."""
    h = hashlib.sha256(content).hexdigest()[:8]
    return f"{date.today().isoformat()}_{h}{ext}"


def _has_content(extracted: dict) -> bool:
    """Есть ли что сохранять: числа ИЛИ аллергии ИЛИ диагнозы."""
    if not extracted:
        return False
    return bool(extracted.get("values") or extracted.get("allergies") or extracted.get("conditions"))


def _read_existing_profile(user_id: int) -> dict[str, list[str]]:
    """Текущие аллергии/диагнозы юзера из onboarding_data (для превью-пометок)."""
    from database.crud import get_user_by_telegram_id

    db = SessionLocal()
    try:
        user = get_user_by_telegram_id(db, user_id)
        onboarding = (user.onboarding_data or {}) if user else {}
        return {
            "allergies": onboarding_list(onboarding, ALLERGY_KEYS),
            "chronic_conditions": onboarding_list(onboarding, CONDITION_KEYS),
        }
    finally:
        db.close()


def _preview_text(extracted: dict[str, Any], existing: Optional[dict] = None) -> str:
    """Форматирует превью найденных данных. existing — текущий onboarding_data юзера
    (для пометки «новое» vs «уже в профиле»)."""
    existing = existing or {}
    existing_allergies = {s.lower() for s in onboarding_list(existing, ALLERGY_KEYS)}
    existing_conditions = {s.lower() for s in onboarding_list(existing, CONDITION_KEYS)}

    if not _has_content(extracted):
        return (
            "⚠️ Не нашёл данных для сохранения в документе.\n\n"
            "Это всё равно можно сохранить как архив — "
            "запомню что такой документ есть, и смогу перечитать его при разговоре."
        )

    lines: list[str] = ["📋 <b>Нашёл в документе:</b>"]

    doc_date = extracted.get("date")
    if doc_date:
        lines.append(f"• <b>Дата:</b> {doc_date}")
    lab = extracted.get("laboratory")
    if lab:
        lines.append(f"• <b>Лаборатория:</b> {lab}")

    doc_type = extracted.get("doc_type")
    if doc_type:
        lines.append(f"• <b>Тип:</b> {doc_type}")

    values = extracted.get("values") or {}
    for key, val in list(values.items())[:15]:
        lines.append(f"• {key}: {str(val)[:50]}")
    if len(values) > 15:
        lines.append(f"  <i>...и ещё {len(values) - 15} показателей</i>")

    def _mark(item: str, existing_set: set) -> str:
        return f"• {item} — ✓ уже в профиле" if item.lower() in existing_set else f"• {item} — 🆕"

    allergies = extracted.get("allergies") or []
    if allergies:
        lines.append("\n🤧 <b>Аллергии:</b>")
        lines.extend(_mark(a, existing_allergies) for a in allergies)

    conditions = extracted.get("conditions") or []
    if conditions:
        lines.append("\n🩺 <b>Диагнозы:</b>")
        lines.extend(_mark(c, existing_conditions) for c in conditions)

    lines.append("\nСохранить эти данные в твою базу здоровья?")
    return "\n".join(lines)


def _preview_keyboard(has_values: bool) -> InlineKeyboardMarkup:
    """Inline-клавиатура подтверждения."""
    if has_values:
        buttons = [
            [
                InlineKeyboardButton(text="Сохранить ✅", callback_data="docup_save"),
                InlineKeyboardButton(text="Отмена ❌", callback_data="docup_cancel"),
            ]
        ]
    else:
        buttons = [
            [
                InlineKeyboardButton(text="Сохранить как архив 📁", callback_data="docup_save"),
                InlineKeyboardButton(text="Отмена ❌", callback_data="docup_cancel"),
            ]
        ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def append_document_to_kb(user_id: int, entry: dict[str, Any]) -> None:
    """Атомарная запись записи в documents[] в kb_<user_id>.json."""
    kb_path = _PROJECT_ROOT / "data" / "kb" / f"kb_{user_id}.json"
    kb_path.parent.mkdir(parents=True, exist_ok=True)

    if kb_path.exists():
        try:
            kb = json.loads(kb_path.read_text(encoding="utf-8"))
        except Exception:
            kb = {}
    else:
        kb = {}

    if not isinstance(kb.get("documents"), list):
        kb["documents"] = []
    kb["documents"].append(entry)

    tmp = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=kb_path.parent,
        suffix=".tmp",
        delete=False,
    )
    try:
        json.dump(kb, tmp, ensure_ascii=False, indent=2)
        tmp.flush()
        tmp.close()
        Path(tmp.name).replace(kb_path)
    except Exception:
        Path(tmp.name).unlink(missing_ok=True)
        raise


# Что сказать пользователю, когда лабораторной строки из документа не вышло.
# Документ при этом всё равно сохранён в documents[] — молчать нельзя, но и
# «сохранено» без оговорки вводило бы в заблуждение (данных в динамике нет).
_NO_ROW_NOTES = {
    "no_values": "\n📊 Числовых показателей не нашёл — документ лежит в карте здоровья.",
    "not_lab": "\n📊 Лабораторных показателей не нашёл — документ лежит в карте здоровья.",
    "no_date": "\n📊 Дату в документе не распознал — показатели не попали в динамику.",
}


def _save_to_blood_tests(user_id: int, extracted: dict[str, Any], stored_name: str) -> str:
    """Пишет лабораторные показатели документа в Postgres blood_tests.

    Без этого загруженный через /doc анализ не виден ни дашборду, ни /phenoage,
    ни агенту (все читают blood_tests, а не documents[]) — issue #281.

    Возвращает строку-приписку к ответу пользователю. Исключения не выпускает:
    документ к этому моменту уже сохранён в KB, и падение БД не должно выглядеть
    как несохранённый документ.
    """
    from core.health.doc_to_blood_test import build_blood_test_row
    from database.crud import upsert_blood_test

    result = build_blood_test_row(extracted, stored_name=stored_name, user_id=user_id)
    for warning in result.warnings:
        logger.info("doc_upload: user %s — %s", user_id, warning)

    if result.row is None:
        return _NO_ROW_NOTES.get(result.reason, "")

    db = SessionLocal()
    try:
        created = upsert_blood_test(db, result.row)
    except Exception:
        logger.exception("doc_upload: запись в blood_tests не удалась (user %s)", user_id)
        return "\n⚠️ Показатели не попали в динамику — ошибка записи в базу."
    finally:
        db.close()

    verb = "Добавил" if created else "Обновил"
    return f"\n📊 {verb} в динамику показателей: {result.marker_count} (дата анализа: {result.row['test_date']})."


@router.message(Command("doc"))
async def cmd_doc(message: Message, state: FSMContext) -> None:
    """/doc — начать загрузку медицинского документа."""
    await state.set_state(DocUpload.waiting)
    await message.answer(
        "📄 Пришли PDF, фото или скан анализа / заключения врача.\n\n"
        "Поддерживаются: PDF, JPG, PNG, HEIC.\n"
        "Выйти из режима загрузки — /cancel.",
        parse_mode="HTML",
    )


@router.message(Command("cancel"), DocUpload.waiting)
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    """Выход из режима загрузки по /cancel."""
    await state.clear()
    await message.answer("Вышел из режима загрузки документов.")


@router.message(DocUpload.waiting, F.document | F.photo)
async def doc_received(message: Message, state: FSMContext, album: list = None) -> None:
    """Обрабатывает входящий файл в режиме /doc."""
    from core.health.doc_extractor import extract_medical_data
    from handlers.photo import _extract_pdf_text, _pdf_to_images

    # Альбом (несколько файлов одним сообщением) — пока не поддерживаем батч-обработку
    # в /doc (FSM-state pending хранит один файл). Просим прислать по одному, вместо
    # того чтобы молча обработать только первый файл и потерять остальные.
    if album and len(album) > 1:
        await message.answer(
            "📎 Пришли, пожалуйста, документы по одному — так надёжнее, я смогу их правильно распознать."
        )
        return

    user_id = message.from_user.id

    # Определяем тип и скачиваем
    if message.document:
        doc = message.document
        if (doc.file_size or 0) > _MAX_FILE_MB * 1024 * 1024:
            await message.answer(
                f"⚠️ Файл больше {_MAX_FILE_MB} МБ (лимит Telegram). Пришли PDF полегче или скриншоты страниц."
            )
            return
        mime = (doc.mime_type or "").lower()
        fname = (doc.file_name or "").lower()
        is_pdf = mime == "application/pdf" or fname.endswith(".pdf")
        is_image = mime in _IMAGE_MIME or fname.endswith((".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif"))
        if not is_pdf and not is_image:
            await message.answer(
                "📎 Такой формат пока не умею. Поддерживаю PDF, JPG/PNG/HEIC.\n"
                "Если это .doc/.docx — сохрани как PDF или пришли фото страниц."
            )
            return
        file_id = doc.file_id
        ext = ".pdf" if is_pdf else (Path(fname).suffix or ".jpg")
    else:
        is_pdf = False
        file_id = message.photo[-1].file_id
        ext = ".jpg"

    processing = await message.answer("⏳ Читаю…")

    try:
        tg_file = await message.bot.get_file(file_id)
        buf = io.BytesIO()
        await message.bot.download_file(tg_file.file_path, buf)
        content = buf.getvalue()
    except Exception:
        logger.exception("doc_upload: не удалось скачать файл от %s", user_id)
        await processing.edit_text("⚠️ Не удалось скачать файл. Попробуй ещё раз.")
        return

    # Сохраняем как .pending до подтверждения
    stored_name = _stored_name(content, ext)
    tmp_path = _uploads_dir(user_id) / f".pending_{stored_name}"
    tmp_path.write_bytes(content)

    # Извлекаем показатели
    loop = asyncio.get_event_loop()
    try:
        if is_pdf:
            pdf_text = await loop.run_in_executor(None, lambda: _extract_pdf_text(tmp_path))
            if pdf_text:
                extracted = await extract_medical_data(pdf_text.encode(), "text/plain")
            else:
                # Сканированный PDF — берём первую страницу как изображение
                pages = await loop.run_in_executor(None, lambda: _pdf_to_images(tmp_path, max_pages=1))
                if pages:
                    extracted = await extract_medical_data(pages[0].read_bytes(), "image/jpeg")
                else:
                    extracted = {}
        else:
            media_type = "image/png" if ext == ".png" else "image/jpeg"
            extracted = await extract_medical_data(content, media_type)
    except Exception:
        logger.exception("doc_upload: экстракция не удалась (user %s)", user_id)
        extracted = {}

    await state.update_data(pending={"tmp_path": str(tmp_path), "stored_name": stored_name, "extracted": extracted})
    existing = _read_existing_profile(user_id)
    await processing.edit_text(
        _preview_text(extracted, existing),
        reply_markup=_preview_keyboard(_has_content(extracted)),
        parse_mode="HTML",
    )


@router.callback_query(DocUpload.waiting, F.data.in_({"docup_save", "docup_cancel"}))
async def doc_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    """Пользователь подтвердил или отменил сохранение."""
    data = await state.get_data()
    pending = data.get("pending")
    if not pending:
        await callback.answer("Нет документа в обработке", show_alert=True)
        return

    tmp_path = Path(pending["tmp_path"])
    user_id = callback.from_user.id

    if callback.data == "docup_cancel":
        tmp_path.unlink(missing_ok=True)
        await state.update_data(pending=None)
        await callback.message.edit_text("❌ Не сохранил. Пришли другой документ или /cancel.")
        await callback.answer()
        return

    # Сохранение
    final_path = _uploads_dir(user_id) / pending["stored_name"]
    try:
        if tmp_path.exists():
            tmp_path.replace(final_path)
        entry = {
            "added_at": date.today().isoformat(),
            "file": pending["stored_name"],
            "extracted": pending.get("extracted") or {},
            "user_confirmed": True,
        }
        append_document_to_kb(user_id, entry)
    except Exception:
        logger.exception("doc_upload: сохранение не удалось (user %s)", user_id)
        await callback.message.edit_text("⚠️ Не получилось сохранить, попробуй ещё раз.")
        await callback.answer()
        return

    extracted = pending.get("extracted") or {}
    biomarkers_note = _save_to_blood_tests(user_id, extracted, pending["stored_name"])
    profile_note = ""
    if extracted.get("allergies") or extracted.get("conditions"):
        db = SessionLocal()
        try:
            counts = merge_onboarding_lists(
                db,
                user_id,
                {
                    "allergies": extracted.get("allergies") or [],
                    "chronic_conditions": extracted.get("conditions") or [],
                },
            )
        except Exception:
            logger.exception("doc_upload: merge onboarding не удался (user %s)", user_id)
            profile_note = "\n⚠️ Документ сохранён, но профиль обновить не удалось — попробуй ещё раз."
            counts = None
        finally:
            db.close()
        if counts is not None:
            n_a, n_c = counts.get("allergies", 0), counts.get("chronic_conditions", 0)
            if n_a or n_c:
                profile_note = f"\nВ профиль добавлено: аллергии +{n_a}, диагнозы +{n_c}."
            else:
                profile_note = "\nНового в профиль не добавил (всё уже было)."

    await state.update_data(pending=None)
    await callback.message.edit_text(
        "✅ Сохранено в твою базу здоровья."
        + biomarkers_note
        + profile_note
        + "\n\nМожешь прислать ещё документ или /cancel.",
        parse_mode="HTML",
    )
    await callback.answer("Сохранено")


@router.message(DocUpload.waiting)
async def doc_wrong_content(message: Message) -> None:
    """Напоминание если прислали не файл в режиме /doc."""
    await message.answer("Жду PDF, фото или скан документа. Выйти — /cancel.")
