#!/usr/bin/env python3
"""Команды /connect_mcp и /my_connections — self-service выпуск PAT для MCP-коннектора (#228).

Сценарий:
  • /connect_mcp [имя] → выбор уровня доступа (полный rw / только чтение ro) кнопкой →
    бот создаёт Personal Access Token и показывает его (один раз). Пользователь вставляет
    токен в коннектор Botkin для Claude Desktop. ro-токеном можно поделиться с врачом.
  • /my_connections → список активных токенов с кнопкой «Отозвать» у каждого.

Без ручной выдачи Александром: пользователь сам управляет своими подключениями.
Слой хранения/обмена — Фазы 1–2 (database.crud.create_pat, /api/agent/exchange_pat_for_jwt).
"""

import logging
import os
import re
from datetime import datetime
from typing import Optional

_MONTHS = ["янв", "фев", "мар", "апр", "мая", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"]


def _auto_name() -> str:
    """Автоимя по текущей дате: «29 июн, 10:25»."""
    now = datetime.now()
    return f"{now.day} {_MONTHS[now.month - 1]}, {now.strftime('%H:%M')}"


from aiogram import Router
from aiogram.filters import Command
from aiogram.filters.callback_data import CallbackData
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

logger = logging.getLogger(__name__)
router = Router()

# Хост API, который коннектор Claude Desktop дёргает для обмена PAT→JWT. Это НЕ
# botkin.health (там статический лендинг) — /api/agent/* живёт на health-домене.
# Overridable для дев-стенда.
CONNECTOR_API_BASE = os.getenv("BOTKIN_API_BASE", "https://health.orangegate.cc")

MAX_NAME_LEN = 100

# Имя из /connect_mcp хранится между командой и нажатием кнопки scope.
# Бот однопроцессный (aiogram polling) — in-memory dict достаточно; запись эфемерная.
# Ограничен _MAX_PENDING: заброшенные потоки (нажали /connect_mcp, кнопку не нажали)
# не растут вечно. При переполнении выбрасываем половину самых старых записей.
_MAX_PENDING = 500
_pending_names: dict[int, Optional[str]] = {}


class PatNewCallback(CallbackData, prefix="patnew"):
    scope: str  # "ro" | "rw"


class PatRevokeCallback(CallbackData, prefix="patrev"):
    token_id: int


# ── Чистая логика (тестируется без Telegram) ──────────────────────────────────


def parse_connect_name(text: Optional[str]) -> Optional[str]:
    """Достать необязательное имя из текста команды «/connect_mcp мой ноут».

    Схлопывает пробелы, режет до MAX_NAME_LEN. Пустой аргумент → None.
    """
    if not text:
        return None
    parts = text.split(maxsplit=1)
    if len(parts) < 2:
        return None
    name = re.sub(r"\s+", " ", parts[1]).strip()
    if not name:
        return None
    return name[:MAX_NAME_LEN]


def scope_label(scope: str) -> str:
    """Человекочитаемая подпись режима доступа."""
    return "полный доступ (чтение + запись)" if scope == "rw" else "только чтение"


def format_connections(pats: list[dict]) -> str:
    """Текст списка активных подключений (plain-text, без Markdown — имена могут
    содержать спецсимволы, экранировать накладно)."""
    lines = ["🔌 Твои MCP-подключения:\n"]
    for p in pats:
        name = p.get("name") or "(без имени)"
        kind = "чтение+запись" if p.get("scope") == "rw" else "только чтение"
        used = p["last_used_at"].strftime("%d.%m.%Y") if p.get("last_used_at") else "ещё не использовался"
        lines.append(f"• {name} — {kind}; последнее использование: {used}")
    return "\n".join(lines)


# ── DB-обёртки (через SessionLocal, как в connect_cgm) ────────────────────────


def _create_pat(telegram_id: int, name: Optional[str], scope: str) -> Optional[str]:
    from database import SessionLocal
    from database.crud import create_pat

    db = SessionLocal()
    try:
        pat = create_pat(db, telegram_id, name=name, scope=scope)
        return pat.token
    except Exception as e:
        logger.error(f"connect_mcp: не смог создать PAT для {telegram_id}: {e}")
        return None
    finally:
        db.close()


def _list_pats(telegram_id: int) -> list[dict]:
    from database import SessionLocal
    from database.crud import list_pats

    db = SessionLocal()
    try:
        # Читаем поля внутри сессии — наружу отдаём простые dict (без DetachedInstanceError).
        return [
            {"id": p.id, "name": p.name, "scope": p.scope, "last_used_at": p.last_used_at}
            for p in list_pats(db, telegram_id)
        ]
    finally:
        db.close()


def _revoke_pat(telegram_id: int, token_id: int) -> bool:
    from database import SessionLocal
    from database.crud import revoke_pat

    db = SessionLocal()
    try:
        return revoke_pat(db, telegram_id, token_id)
    except Exception as e:
        logger.error(f"connect_mcp: не смог отозвать PAT {token_id} у {telegram_id}: {e}")
        return False
    finally:
        db.close()


def _scope_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🖊 Полный доступ (для себя)", callback_data=PatNewCallback(scope="rw").pack())],
            [
                InlineKeyboardButton(
                    text="👁 Только чтение (поделиться)", callback_data=PatNewCallback(scope="ro").pack()
                )
            ],
        ]
    )


# ── Хендлеры ──────────────────────────────────────────────────────────────────


@router.message(Command("connect_mcp"))
async def cmd_connect_mcp(message: Message) -> None:
    """`/connect_mcp [имя]` — выпустить токен для MCP-коннектора."""
    if len(_pending_names) >= _MAX_PENDING:
        evict = list(_pending_names.keys())[: _MAX_PENDING // 2]
        for k in evict:
            del _pending_names[k]
    _pending_names[message.from_user.id] = parse_connect_name(message.text)
    await message.answer(
        "🔌 <b>Подключение MCP-коннектора</b>\n\n"
        "Выбери уровень доступа для токена:\n"
        "• <b>Полный доступ</b> — твой AI-ассистент сможет читать и записывать данные.\n"
        "• <b>Только чтение</b> — этой строкой можно поделиться с врачом или близким: "
        "он увидит данные, но ничего не изменит.\n\n"
        "💡 Хочешь дать имя — напиши <code>/connect_mcp мой ноут</code>",
        parse_mode="HTML",
        reply_markup=_scope_keyboard(),
    )


@router.callback_query(PatNewCallback.filter())
async def on_pat_scope_chosen(callback: CallbackQuery, callback_data: PatNewCallback) -> None:
    scope = callback_data.scope
    if scope not in ("ro", "rw"):
        await callback.answer("Неизвестный режим", show_alert=True)
        return

    name = _pending_names.pop(callback.from_user.id, None) or _auto_name()
    token = _create_pat(callback.from_user.id, name, scope)
    if token is None:
        await callback.message.edit_text("⚠️ Не удалось создать токен. Попробуй ещё раз: /connect_mcp")
        await callback.answer()
        return

    await callback.message.edit_text(
        f"✅ Токен создан — <b>{scope_label(scope)}</b>\n\n"
        "Вставь его в настройки MCP-коннектора Botkin:\n\n"
        f"<code>{token}</code>\n\n"
        f"Сервер API: <code>{CONNECTOR_API_BASE}</code>\n\n"
        "⚠️ Токен показан один раз и работает как пароль — храни его в надёжном месте.\n"
        "Список и отзыв подключений — /my_connections.",
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(Command("my_connections"))
async def cmd_my_connections(message: Message) -> None:
    """`/my_connections` — список активных токенов + кнопки отзыва."""
    pats = _list_pats(message.from_user.id)
    if not pats:
        await message.answer("У тебя пока нет активных MCP-подключений.\nСоздать — /connect_mcp")
        return

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"❌ Отозвать: {p.get('name') or '(без имени)'}",
                    callback_data=PatRevokeCallback(token_id=p["id"]).pack(),
                )
            ]
            for p in pats
        ]
    )
    await message.answer(format_connections(pats), reply_markup=keyboard)


@router.callback_query(PatRevokeCallback.filter())
async def on_pat_revoke(callback: CallbackQuery, callback_data: PatRevokeCallback) -> None:
    if _revoke_pat(callback.from_user.id, callback_data.token_id):
        await callback.answer("Токен отозван")
        await callback.message.edit_text(
            "✅ Подключение отозвано — токен больше не работает.\n\nСписок — /my_connections"
        )
    else:
        await callback.answer("Токен уже отозван или не найден", show_alert=True)
