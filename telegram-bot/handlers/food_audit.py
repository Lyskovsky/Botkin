"""Аудит пищевого pipeline через бота (#224, follow-up #193). /food_audit (admin).

Читалка таблицы ``food_interactions`` прямо в Telegram — self-serve эквивалент
``scripts/review_food_interactions.py`` (тот требует доступа к БД/SSH). Показывает
цепочку «что прислал → что распознал → что ответил → что записалось» для
пользователя. Нужна для E2E-проверки врезки ``log_food_interaction`` без SSH.

Только для админов (``config.users.is_admin``), как ``/feedback_queue``.
"""

import json
import logging

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from config.users import is_admin

logger = logging.getLogger(__name__)
router = Router()

_STATUS_EMOJI = {"saved": "✅", "cancelled": "❌", "edited": "✏️"}
_MAX_ROWS = 10  # сообщение бота ~4096 символов → держим короткую сводку
_LIMIT_CAP = 50  # верхний предел выборки из БД
_FIELD_CAP = 200  # обрезка длинных полей (raw_text/recognized)
_REPLY_CAP = 300


def parse_audit_args(raw: str, caller_id: int) -> tuple[int, int]:
    """Разобрать ``/food_audit [user_id] [limit]``.

    Дефолт: цель = вызвавший админ, limit = ``_MAX_ROWS``. ``user_id`` — первый
    числовой токен, ``limit`` — второй (клампится в 1..``_LIMIT_CAP``). Суффикс
    ``@botname`` и нечисловые токены игнорируются.
    """
    parts = (raw or "").strip().split()
    args = parts[1:]  # первый токен — сама команда (возможно с @botname)
    user_id = caller_id
    limit = _MAX_ROWS
    if args and args[0].lstrip("-").isdigit():
        user_id = int(args[0])
    if len(args) >= 2 and args[1].lstrip("-").isdigit():
        limit = max(1, min(int(args[1]), _LIMIT_CAP))
    return user_id, limit


def _cap(value: str, n: int) -> str:
    return value if len(value) <= n else value[:n] + "…"


def _fmt_row(row) -> str:
    ts = row.created_at.strftime("%Y-%m-%d %H:%M") if getattr(row, "created_at", None) else "—"
    emoji = _STATUS_EMOJI.get(row.status, "·")
    nl = f" → nutrition_log #{row.nutrition_log_id}" if row.nutrition_log_id else ""
    lines = [f"[{ts}] {row.source} · {emoji} {row.status}{nl}", f"  прислал:   {row.raw_text or '—'}"]
    if row.media_path:
        lines.append(f"  медиа:     {row.media_path}")
    if row.recognized:
        try:
            recognized = json.dumps(row.recognized, ensure_ascii=False)
        except (TypeError, ValueError):
            recognized = str(row.recognized)
        lines.append(f"  распознал: {_cap(recognized, _FIELD_CAP)}")
    lines.append(f"  ответ:     {_cap(row.bot_reply or '—', _REPLY_CAP)}")
    return "\n".join(lines)


def format_food_audit(rows: list, user_id: int) -> str:
    """Отрендерить цепочку пищевых взаимодействий для админа (plain text)."""
    if not rows:
        return f"🍽 Нет пищевых взаимодействий для user {user_id}."
    total = len(rows)
    shown = rows[:_MAX_ROWS]
    header = f"🍽 Пищевые взаимодействия user {user_id} (новые сверху):"
    body = "\n\n".join(_fmt_row(r) for r in shown)
    out = f"{header}\n\n{body}"
    if total > _MAX_ROWS:
        out += f"\n\n… показаны первые {_MAX_ROWS} из {total}. Уточни лимит: /food_audit {user_id} {total}"
    return out


@router.message(Command("food_audit"))
async def cmd_food_audit(message: Message) -> None:
    if not is_admin(message.from_user.id):
        await message.answer("Команда доступна только администраторам.")
        return

    target_user, limit = parse_audit_args(message.text or "", caller_id=message.from_user.id)

    from database import SessionLocal
    from core.food.interaction_log import get_food_interactions

    db = SessionLocal()
    try:
        rows = get_food_interactions(db, target_user, limit=limit)
    except Exception as e:
        logger.error(f"food_audit: не смог получить взаимодействия: {e}")
        await message.answer("❌ Не смог получить пищевые взаимодействия.")
        return
    finally:
        db.close()

    # parse_mode=None: цепочка содержит пользовательский текст (raw_text/bot_reply)
    # и JSON распознанного — символ '<' сломал бы глобальный parse_mode=HTML (тот же
    # приём, что в /feedback_queue).
    await message.answer(format_food_audit(rows, target_user), parse_mode=None)
