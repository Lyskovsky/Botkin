"""Agent Tools API — 8 endpoints for NanoClaw containers.

All endpoints require JWT auth via Depends(get_agent_user).
Prefix: /api/agent

Tasks 5-7 of HealthVault Sprint 1a:
  - Task 5: Write endpoints (log_meal_text, log_supplement, log_bp, regenerate_health_token)
  - Task 6: Write endpoints continued
  - Task 7: Read endpoints (recent_meals, kb_value, dashboard_summary, user_profile)
"""

import re
import sys
import asyncio
import secrets
import logging
from datetime import datetime, date, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from fastapi import APIRouter, Body, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

# Ensure project root on path for database imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from sqlalchemy import func  # noqa: E402

from database.models import ActivityLog, GlucoseReading, NutritionLog, Weight  # noqa: E402
from webhook.jwt_auth import get_agent_user, get_db, require_agent_scope  # noqa: E402
from webhook.rate_limit import SlidingWindowRateLimiter  # noqa: E402
from core.health.glucose_stats import compute_glucose_stats, glucose_staleness  # noqa: E402
from core.health.glucose_runtime import refresh_glucose_for_telegram as _refresh_glucose, LoginOnCooldownError  # noqa: E402
from bot_token import resolve_bot_token  # noqa: E402
from config.settings import public_base_url  # noqa: E402

logger = logging.getLogger(__name__)


router = APIRouter(prefix="/api/agent", tags=["agent-tools"])


# ── PAT → JWT exchange (публичный bootstrap для MCP-коннектора, #228) ──────────

# Лимит на публичный exchange: не больше 10 попыток в минуту с одного IP,
# чтобы перебор PAT по сети упирался в стену. Состояние в памяти процесса.
_EXCHANGE_RATE_LIMIT = 10
_EXCHANGE_RATE_WINDOW_S = 60.0
_exchange_limiter = SlidingWindowRateLimiter(_EXCHANGE_RATE_LIMIT, _EXCHANGE_RATE_WINDOW_S)


class ExchangePATRequest(BaseModel):
    pat: str = Field(..., min_length=10, max_length=128)


@router.post("/exchange_pat_for_jwt")
async def exchange_pat_for_jwt(
    body: ExchangePATRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    """Обменять долгоживущий PAT на короткоживущий агентский JWT.

    Единственный публичный (без JWT) эндпоинт. Коннектор Claude Desktop хранит
    durable PAT в keychain и дёргает этот метод, чтобы получить JWT для /api/agent/*.
    JWT наследует scope токена: 'ro'-PAT → 'ro'-JWT (write-эндпоинты вернут 403).
    """
    from database.crud import get_active_pat_by_token
    from database.models import User
    from core.agent_chat import agent_id_for
    from webhook.jwt_auth import generate_agent_jwt, JWT_TTL_HOURS

    client_ip = request.headers.get("x-real-ip") or (request.client.host if request.client else "unknown")
    if not _exchange_limiter.allow(client_ip):
        raise HTTPException(status_code=429, detail="Слишком много запросов. Попробуйте через минуту.")

    pat = get_active_pat_by_token(db, body.pat)
    if not pat:
        raise HTTPException(status_code=401, detail="Недействительный или отозванный токен")

    user = db.query(User).filter_by(telegram_id=pat.user_id).first()
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="Пользователь не найден или неактивен")
    if not user.jwt_secret:
        raise HTTPException(status_code=401, detail="Пользователь не инициализирован (нет jwt_secret)")

    token = generate_agent_jwt(user.telegram_id, agent_id_for(user), user.jwt_secret, scope=pat.scope)
    return {
        "access_token": token,
        "token_type": "Bearer",
        "expires_in": JWT_TTL_HOURS * 3600,
        "scope": pat.scope,
    }


# ── Timezone helpers ──────────────────────────────────────────────────────────

_DEFAULT_TZ = "Europe/Moscow"


def _get_user_tz(user) -> ZoneInfo:
    """Return ZoneInfo timezone for user. Falls back to Europe/Moscow."""
    tz_name = getattr(user, "timezone", None) or _DEFAULT_TZ
    try:
        return ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, KeyError):
        logger.warning(
            "Unknown timezone %r for user %s, falling back to %s",
            tz_name,
            getattr(user, "telegram_id", "?"),
            _DEFAULT_TZ,
        )
        return ZoneInfo(_DEFAULT_TZ)


def _dt_to_user_tz(dt: datetime, user) -> datetime:
    """Convert a UTC (or timezone-aware) datetime to the user's local timezone."""
    if dt is None:
        return dt
    tz = _get_user_tz(user)
    # Ensure dt is timezone-aware (treat naive as UTC)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(tz)


def _dt_isoformat_local(dt: datetime, user) -> Optional[str]:
    """Convert UTC datetime to user's timezone and return ISO string (no microseconds)."""
    if dt is None:
        return None
    local_dt = _dt_to_user_tz(dt, user)
    # Format without microseconds for readability: 2026-05-27T21:18:00+03:00
    return local_dt.strftime("%Y-%m-%dT%H:%M:%S%z")


def _today_in_user_tz(user) -> date:
    """Return today's date in the user's local timezone."""
    tz = _get_user_tz(user)
    return datetime.now(tz).date()


# ── Request / Response schemas ────────────────────────────────────────────────


class LogMealTextRequest(BaseModel):
    text: str
    date: Optional[str] = None  # YYYY-MM-DD; defaults to today
    slot: Optional[str] = None  # breakfast | lunch | dinner | snack; auto-detected if None


class LogSupplementRequest(BaseModel):
    supplement_name: str
    dosage: Optional[str] = None
    date: Optional[str] = None  # YYYY-MM-DD; defaults to today
    time: Optional[str] = None  # HH:MM
    force: bool = False  # True — записать даже если та же добавка уже логировалась в этот день


class FlagForDevsRequest(BaseModel):
    category: str  # bug | feature | question
    user_msg: str
    agent_note: Optional[str] = None


class ListFeedbackRequest(BaseModel):
    # #269 триаж (admin-only): status=None/'all' → все статусы
    status: Optional[str] = "new"
    limit: int = 20


class TriageFeedbackRequest(BaseModel):
    # #269 триаж (admin-only): частичное обновление — передавай только меняемые поля
    feedback_id: int
    status: Optional[str] = None  # new/triaged/in_progress/done/wontfix/duplicate
    priority: Optional[str] = None  # P0-P3
    github_issue: Optional[str] = None
    # Фаза 3 (#188): явный текст ответа автору. Перекрывает авто-текст и уходит
    # пользователю при любом статусе — так `question` закрывается человеческим ответом.
    notify_text: Optional[str] = Field(None, max_length=3000)


class LogBPRequest(BaseModel):
    systolic: int = Field(..., ge=50, le=300, description="Systolic pressure mmHg")
    diastolic: int = Field(..., ge=30, le=200, description="Diastolic pressure mmHg")
    pulse: Optional[int] = Field(None, ge=30, le=250, description="Pulse bpm")
    measured_at: Optional[str] = None  # ISO datetime; defaults to now


class EditMealRequest(BaseModel):
    meal_id: int
    new_date: Optional[str] = None  # YYYY-MM-DD — перенести на другой день
    new_slot: Optional[str] = None  # breakfast | lunch | dinner | snack — сменить слот/время
    new_name: Optional[str] = None  # переименовать


class DeleteMealRequest(BaseModel):
    meal_id: int


# ── Helpers ───────────────────────────────────────────────────────────────────


def _parse_date(date_str: Optional[str], user=None) -> date:
    """Parse YYYY-MM-DD string or return today in user's local timezone."""
    if not date_str:
        return _today_in_user_tz(user) if user is not None else date.today()
    try:
        return date.fromisoformat(date_str)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid date format: {date_str!r}. Use YYYY-MM-DD.")


def _parse_time(time_str: Optional[str]):
    """Parse HH:MM string or return None."""
    if not time_str:
        return None
    from datetime import time as time_cls

    try:
        h, m = time_str.split(":")
        return time_cls(int(h), int(m))
    except Exception:
        raise HTTPException(status_code=400, detail=f"Invalid time format: {time_str!r}. Use HH:MM.")


def _slot_to_meal_time(slot: Optional[str]):
    """Map slot name to a default meal time."""
    from datetime import time as time_cls

    mapping = {
        "breakfast": time_cls(8, 0),
        "lunch": time_cls(13, 0),
        "dinner": time_cls(19, 0),
        "snack": time_cls(16, 0),
    }
    if slot is None:
        return time_cls(12, 0), "Приём пищи"
    slot = slot.lower()
    if slot not in mapping:
        raise HTTPException(status_code=400, detail=f"Invalid slot {slot!r}. Use: breakfast, lunch, dinner, snack.")
    name_map = {
        "breakfast": "Завтрак",
        "lunch": "Обед",
        "dinner": "Ужин",
        "snack": "Перекус",
    }
    return mapping[slot], name_map[slot]


def _as_dict(values):
    """blood_tests.values: dict в Postgres (JSONB), но str если пришло как JSON-текст."""
    if isinstance(values, str):
        import json as _json

        try:
            return _json.loads(values)
        except Exception:
            return {}
    return values or {}


def _resolve_user_kb_path(user) -> tuple[Optional[Path], str]:
    """Resolve per-user KB file path with fallback to legacy locations.

    Search order (first hit wins):
      1. ``data/kb/kb_<telegram_id>.json`` — current layout (since 2026-05-24
         refactor). Auto-mounted into the bot container via the existing
         ``./data:/app/data`` bind-mount in docker-compose.prod.yml.
      2. ``kb_<telegram_id>.json`` at repo root — legacy layout, kept for
         backward-compat during the rolling migration. Required per-file
         bind-mounts in docker-compose.prod.yml (now removed).
      3. ``knowledge_base.json`` at repo root — owner-cohort fallback (Alex).

    Returns ``(path, source_label)``. Path is None when no KB available;
    caller should return ``"kb-not-available"`` sentinel to the agent.
    """
    project_root = Path(__file__).resolve().parents[2]
    new_path = project_root / "data" / "kb" / f"kb_{user.telegram_id}.json"
    legacy_path = project_root / f"kb_{user.telegram_id}.json"

    if new_path.exists():
        return new_path, f"data/kb/kb_{user.telegram_id}.json"
    if legacy_path.exists():
        return legacy_path, f"kb_{user.telegram_id}.json"
    if user.cohort == "owner":
        owner_kb = project_root / "knowledge_base.json"
        if owner_kb.exists():
            return owner_kb, "knowledge_base.json"
    return None, "kb-not-available"


# ── Task 5: Write endpoints ───────────────────────────────────────────────────


@router.post("/log_meal_text")
async def log_meal_text(
    req: LogMealTextRequest,
    user=Depends(require_agent_scope("rw")),
    db: Session = Depends(get_db),
):
    """Parse free-text meal description and save to nutrition_log.

    Attempts to use the existing food parsing pipeline. Falls back to a stub
    that stores the raw text when the parser is not available / tightly coupled.
    """
    from database.crud import create_nutrition_log
    from core.llm.router import analyze_message
    from core.food.nutrition import process_llm_food_data

    record_date = _parse_date(req.date, user)
    meal_time, meal_name = _slot_to_meal_time(req.slot)

    # Use the real food parser (Claude vision/text via core.llm.router).
    # Returns dict like {"type": "food", "data": {...}} which the photo/text
    # handlers feed into process_llm_food_data() to get (items, totals).
    items: list = []
    totals: dict = {}
    parse_error: Optional[str] = None

    try:
        llm_result = analyze_message(text=req.text, user_id=user.telegram_id)
        if not llm_result or llm_result.get("type") != "food":
            parse_error = (
                "LLM не распознал это как еду "
                f"(type={llm_result.get('type') if llm_result else 'None'}). "
                "Опиши конкретнее: что, сколько, как приготовлено."
            )
        else:
            items, totals = process_llm_food_data(llm_result, req.text)
    except Exception as e:
        logger.exception("log_meal_text: parser failed")
        parse_error = f"парсер упал: {e}"

    # Refuse to write a row if we got no KБЖУ — empty rows break the
    # Mini App dashboard (None aggregations) and are useless to the user.
    if not items or not (totals.get("calories") or 0):
        return {
            "status": "rejected",
            "reason": parse_error or "не удалось распарсить КБЖУ для этого описания",
            "hint": "опиши подробнее: продукт + примерный вес/количество, например 'куриная грудка 200г и рис 150г'",
        }

    # Normalize fiber default so JSONB stores 0, not null
    if totals.get("fiber") is None:
        totals["fiber"] = 0

    log = create_nutrition_log(
        db=db,
        user_id=user.telegram_id,
        date=record_date,
        meal_time=meal_time,
        meal_name=meal_name,
        items=items,
        totals=totals,
    )

    return {
        "status": "ok",
        "meal_id": log.id,
        "date": record_date.isoformat(),
        "slot": req.slot or "auto",
        "meal_name": meal_name,
        "items_count": len(items),
        "totals": totals,
    }


@router.post("/edit_meal")
async def edit_meal(
    req: EditMealRequest,
    user=Depends(require_agent_scope("rw")),
    db: Session = Depends(get_db),
):
    """Изменить уже залогированный приём пищи: перенести на дату (new_date),
    сменить слот/время (new_slot), переименовать (new_name). meal_id — из recent_meals."""
    from database.crud import update_nutrition_meal_fields

    meal_name = req.new_name
    meal_time = None
    if req.new_slot:
        meal_time, _slot_default_name = _slot_to_meal_time(req.new_slot)
    new_date = _parse_date(req.new_date, user) if req.new_date else None

    if meal_name is None and meal_time is None and new_date is None:
        raise HTTPException(status_code=400, detail="Nothing to change: pass new_date, new_slot or new_name.")

    try:
        row = update_nutrition_meal_fields(
            db,
            meal_id=req.meal_id,
            user_id=user.telegram_id,
            meal_name=meal_name,
            meal_time=meal_time,
            new_date=new_date,
        )
    except LookupError:
        raise HTTPException(status_code=404, detail=f"meal {req.meal_id} not found")

    return {
        "status": "ok",
        "meal_id": row.id,
        "date": row.date.isoformat(),
        "meal_time": row.meal_time.strftime("%H:%M") if row.meal_time else None,
        "meal_name": row.meal_name,
    }


@router.post("/delete_meal")
async def delete_meal(
    req: DeleteMealRequest,
    user=Depends(require_agent_scope("rw")),
    db: Session = Depends(get_db),
):
    """Удалить залогированный приём пищи по meal_id (из recent_meals)."""
    from database.crud import delete_nutrition_log

    ok = delete_nutrition_log(db, log_id=req.meal_id, user_id=user.telegram_id)
    if not ok:
        raise HTTPException(status_code=404, detail=f"meal {req.meal_id} not found")
    return {"status": "ok", "deleted_meal_id": req.meal_id}


@router.post("/log_supplement")
async def log_supplement(
    req: LogSupplementRequest,
    user=Depends(require_agent_scope("rw")),
    db: Session = Depends(get_db),
):
    """Save a supplement entry to supplements_log."""
    from sqlalchemy import text as _text

    from database.crud import create_supplement_log

    record_date = _parse_date(req.date, user)
    sup_time = _parse_time(req.time)

    # Дедуп-guard (F-002, 02.07.2026): та же добавка уже логировалась в этот день →
    # не пишем молча дубль, а возвращаем warning; агент уточнит у пользователя
    # и при подтверждении повторит вызов с force=true.
    if not req.force:
        existing = db.execute(
            _text(
                "SELECT id, time FROM supplements_log "
                "WHERE user_id = :uid AND date = :d "
                "AND LOWER(REPLACE(supplement_name, '-', ' ')) = LOWER(REPLACE(:name, '-', ' ')) "
                "LIMIT 1"
            ),
            {"uid": user.telegram_id, "d": record_date, "name": req.supplement_name},
        ).first()
        if existing:
            return {
                "status": "duplicate_warning",
                "existing_id": existing.id,
                "existing_time": str(existing.time) if existing.time else None,
                "date": record_date.isoformat(),
                "supplement_name": req.supplement_name,
                "hint": (
                    "Эта добавка уже залогирована в этот день. Запись НЕ создана. "
                    "Спроси пользователя, действительно ли это повторный приём; "
                    "если да — повтори вызов с force=true."
                ),
            }

    log = create_supplement_log(
        db=db,
        user_id=user.telegram_id,
        date=record_date,
        time=sup_time,
        supplement_name=req.supplement_name,
        dosage=req.dosage,
    )

    return {
        "status": "ok",
        "supplement_id": log.id,
        "date": record_date.isoformat(),
        "supplement_name": req.supplement_name,
        "dosage": req.dosage,
    }


@router.post("/flag_for_devs")
async def flag_for_devs(
    req: FlagForDevsRequest,
    user=Depends(require_agent_scope("rw")),
    db: Session = Depends(get_db),
):
    """Агент флагает пожелание/багрепорт в инбокс user_feedback (#188)."""
    from database.crud import create_feedback, is_feedback_opted_out

    kind = req.category if req.category in ("bug", "feature", "question") else "unspecified"

    if is_feedback_opted_out(db, user.telegram_id):
        return {"status": "skipped_opt_out"}

    row = create_feedback(
        db,
        user_id=user.telegram_id,
        text=req.user_msg,
        source="agent",
        kind=kind,
        agent_context={"agent_note": req.agent_note} if req.agent_note else None,
    )
    return {"status": "ok", "feedback_id": row.id, "kind": kind}


def _feedback_to_dict(row) -> dict:
    """Плоский структурный вид записи фидбека для агента (#269)."""
    ctx = row.agent_context if isinstance(row.agent_context, dict) else {}
    return {
        "id": row.id,
        "kind": row.kind,
        "status": row.status,
        "priority": row.priority,
        "source": row.source,
        "user_id": row.user_id,
        "text": row.text,
        "agent_note": ctx.get("agent_note"),
        "github_issue": row.github_issue,
        "dedup_of": row.dedup_of,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "resolved_at": row.resolved_at.isoformat() if row.resolved_at else None,
        "notified_at": row.notified_at.isoformat() if row.notified_at else None,
    }


async def _send_feedback_notification(chat_id: int, text: str) -> bool:
    """Личное сообщение автору фидбека о разборе обращения (Фаза 3, #188). True при успехе.

    parse_mode НЕ задаём — текст плоский: в нём цитата пользователя, HTML-разметку
    включать нельзя (символ '<' в обращении иначе сломал бы разметку/инъектнул теги)."""
    import httpx

    bot_token = resolve_bot_token()
    if not bot_token:
        logger.warning("feedback-notify: bot token missing, cannot notify %s", chat_id)
        return False
    try:
        async with httpx.AsyncClient() as http:
            resp = await http.post(
                f"https://api.telegram.org/bot{bot_token}/sendMessage",
                json={"chat_id": chat_id, "text": text},
                timeout=10.0,
            )
        data = resp.json()
        if not data.get("ok"):
            logger.warning("feedback-notify sendMessage failed for %s: %s", chat_id, data.get("description"))
            return False
        return True
    except Exception as e:
        logger.error("feedback-notify exception for %s: %s", chat_id, e, exc_info=True)
        return False


@router.post("/list_feedback")
async def list_feedback(
    req: ListFeedbackRequest,
    user=Depends(require_agent_scope("ro")),
    db: Session = Depends(get_db),
):
    """Список записей инбокса фидбека для триажа (#269). Только для админов."""
    from config.users import is_admin
    from database.crud import list_recent_feedback

    if not is_admin(user.telegram_id):
        raise HTTPException(status_code=403, detail="триаж доступен только администраторам")
    status = req.status if req.status not in (None, "", "all") else None
    limit = max(1, min(req.limit, 100))
    rows = list_recent_feedback(db, status=status, limit=limit)
    return {"status": "ok", "count": len(rows), "feedback": [_feedback_to_dict(r) for r in rows]}


@router.post("/triage_feedback")
async def triage_feedback(
    req: TriageFeedbackRequest,
    user=Depends(require_agent_scope("rw")),
    db: Session = Depends(get_db),
):
    """Триаж записи фидбека: статус/приоритет/GitHub-issue (#269). Только для админов.

    Частичное обновление — применяются только переданные (не-None) поля.
    Возвращает обновлённую запись, чтобы агент подтвердил результат.
    """
    from config.users import is_admin
    from database.crud import (
        FEEDBACK_PRIORITIES,
        FEEDBACK_STATUSES,
        get_feedback,
        set_feedback_github,
        set_feedback_priority,
        update_feedback_status,
    )

    if not is_admin(user.telegram_id):
        raise HTTPException(status_code=403, detail="триаж доступен только администраторам")
    row = get_feedback(db, req.feedback_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"фидбек #{req.feedback_id} не найден")
    # #269: провалидировать ВСЕ поля ДО первой мутации — каждый CRUD-хелпер
    # коммитит отдельно, поэтому иначе невалидный priority после валидного status
    # оставил бы частичное изменение при ответе 400 (неатомарность).
    if req.status is not None and req.status not in FEEDBACK_STATUSES:
        raise HTTPException(status_code=400, detail=f"невалидный статус {req.status!r}")
    if req.priority is not None and req.priority not in FEEDBACK_PRIORITIES:
        raise HTTPException(status_code=400, detail=f"невалидный приоритет {req.priority!r}")
    if req.github_issue and len(req.github_issue) > 64:
        raise HTTPException(status_code=400, detail="github_issue слишком длинный (макс 64; передавай номер)")
    try:
        if req.status is not None:
            row = update_feedback_status(db, req.feedback_id, req.status)
        if req.priority is not None:
            row = set_feedback_priority(db, req.feedback_id, req.priority)
        if req.github_issue is not None:
            row = set_feedback_github(db, req.feedback_id, req.github_issue)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # Фаза 3 (#188): уведомить автора, когда обращение разобрано (done/wontfix) или
    # когда админ явно ответил (notify_text). Идемпотентность — guard по notified_at:
    # штампуем ТОЛЬКО после успешной отправки, поэтому сбой сети → повтор на след. триаже,
    # а второй done по уже уведомлённой записи молча пропускается.
    from core.feedback_notify import build_notification_text
    from database.crud import is_feedback_opted_out, mark_feedback_notified

    notified = False
    notify_skipped: Optional[str] = None
    msg = build_notification_text(
        kind=row.kind,
        status=row.status,
        text=row.text,
        custom=req.notify_text,
    )
    if msg:
        if row.notified_at is not None:
            notify_skipped = "already_notified"
        elif is_feedback_opted_out(db, row.user_id):
            # opt-out = никаких исходящих контактов по фидбеку; не шлём и не штампуем.
            notify_skipped = "opt_out"
        elif await _send_feedback_notification(row.user_id, msg):
            mark_feedback_notified(db, row.id)
            db.refresh(row)
            notified = True
        else:
            notify_skipped = "send_failed"

    return {
        "status": "ok",
        "feedback": _feedback_to_dict(row),
        "notified": notified,
        "notify_skipped": notify_skipped,
    }


@router.post("/log_bp")
async def log_bp(
    req: LogBPRequest,
    user=Depends(require_agent_scope("rw")),
    db: Session = Depends(get_db),
):
    """Save a blood pressure reading to blood_pressure_logs."""
    from sqlalchemy import text as _text

    # Parse measured_at
    if req.measured_at:
        try:
            measured_at = datetime.fromisoformat(req.measured_at)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid measured_at: {req.measured_at!r}. Use ISO datetime.")
    else:
        measured_at = datetime.now(timezone.utc)

    db.execute(
        _text(
            """INSERT INTO blood_pressure_logs
               (user_id, measured_at, systolic, diastolic, heart_rate, source)
               VALUES (:uid, :ts, :sys, :dia, :hr, 'agent_api')
               ON CONFLICT (user_id, measured_at) DO UPDATE
                 SET systolic = EXCLUDED.systolic,
                     diastolic = EXCLUDED.diastolic,
                     heart_rate = COALESCE(EXCLUDED.heart_rate, blood_pressure_logs.heart_rate)"""
        ),
        {
            "uid": user.telegram_id,
            "ts": measured_at,
            "sys": req.systolic,
            "dia": req.diastolic,
            "hr": req.pulse,
        },
    )
    db.commit()

    return {
        "status": "ok",
        "measured_at": _dt_isoformat_local(measured_at, user),
        "systolic": req.systolic,
        "diastolic": req.diastolic,
        "pulse": req.pulse,
    }


@router.post("/regenerate_health_token")
async def regenerate_health_token(
    user=Depends(require_agent_scope("rw")),
    db: Session = Depends(get_db),
):
    """Generate a new health_token for the user and save it to users table."""
    new_token = f"hvt_{user.telegram_id}_{secrets.token_hex(16)}"
    user.health_token = new_token
    db.commit()

    return {
        "status": "ok",
        "health_token": new_token,
    }


# ── Task 7: Read endpoints ────────────────────────────────────────────────────


@router.get("/recent_meals")
async def recent_meals(
    days: int = 7,
    compact: bool = False,
    user=Depends(get_agent_user),
    db: Session = Depends(get_db),
):
    """Return nutrition_log rows for the last N days.

    compact=True — лёгкий формат для поиска по длинному периоду («ел ли я X за
    3 месяца»): `items` становится списком имён продуктов (строки), `totals`
    сводится к калориям. Режет payload в ~5-10 раз (один такой запрос на 90 дней
    раньше стоил ~120k токенов / $2). Авто-включается при days > 14, чтобы один
    вызов не раздувал контекст.
    """
    from database.crud import get_nutrition_logs_by_period

    if days < 1 or days > 90:
        raise HTTPException(status_code=400, detail="days must be between 1 and 90")

    # Длинные окна по умолчанию компактны (защита от token-blowup).
    if days > 14:
        compact = True

    end_date = _today_in_user_tz(user)
    start_date = end_date - timedelta(days=days - 1)

    logs = get_nutrition_logs_by_period(db, user.telegram_id, start_date, end_date)

    result = []
    for log in logs:
        if compact:
            names = [
                (it.get("food") or it.get("product") or it.get("name") or "").strip()
                for it in (log.items or [])
                if (it.get("food") or it.get("product") or it.get("name"))
            ]
            items_out = names
            totals_out = {"calories": (log.totals or {}).get("calories")}
        else:
            items_out = log.items
            totals_out = log.totals
        result.append(
            {
                "id": log.id,
                "date": log.date.isoformat(),
                "meal_time": log.meal_time.strftime("%H:%M") if log.meal_time else None,
                "meal_name": log.meal_name,
                "items": items_out,
                "totals": totals_out,
            }
        )

    return {
        "status": "ok",
        "days": days,
        "compact": compact,
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "meals": result,
    }


@router.get("/recent_glucose")
async def recent_glucose(
    hours: int = 24,
    date: Optional[str] = None,
    user=Depends(get_agent_user),
    db: Session = Depends(get_db),
):
    """Точки глюкозы CGM + сводка (TIR, avg, min/max).

    Окно: либо `hours` (последние N часов, по умолчанию 24), либо `date`
    (конкретный календарный день YYYY-MM-DD в локальной TZ юзера; приоритетнее `hours`).

    Статистика — по ВСЕМ точкам окна. Точки для отображения прорежены РАВНОМЕРНО по
    всему окну (децимация), а не обрезаны до последних N — иначе при широком окне агент
    видел бы только последнюю ночь и не мог сопоставить еду с дневной кривой (#163).
    """
    # ── Определяем окно: конкретный день (date) или последние N часов (hours) ──
    if date is not None:
        try:
            day = datetime.strptime(date, "%Y-%m-%d").date()
        except ValueError:
            raise HTTPException(status_code=400, detail="date must be YYYY-MM-DD")
        tz = _get_user_tz(user)
        start_local = datetime(day.year, day.month, day.day, tzinfo=tz)
        since = start_local.astimezone(timezone.utc)
        until = (start_local + timedelta(days=1)).astimezone(timezone.utc)
        window_desc = {"date": date}
    else:
        if hours < 1 or hours > 168:
            raise HTTPException(status_code=400, detail="hours must be between 1 and 168")
        since = datetime.now(timezone.utc) - timedelta(hours=hours)
        until = None
        window_desc = {"hours": hours}

    # On-demand: подтянуть свежую глюкозу для этого юзера прямо сейчас (#129).
    # Сетевой sync-вызов — в threadpool с таймаутом (зависший LLU не должен держать воркер);
    # любая ошибка/таймаут не валит ответ — fallback на данные из БД.
    refresh_skipped = False
    try:
        await asyncio.wait_for(
            asyncio.to_thread(_refresh_glucose, user.telegram_id),
            timeout=10.0,
        )
    except LoginOnCooldownError as e:
        refresh_skipped = True
        logger.info(
            "recent_glucose: логин на cooldown для %s, %.0fс до следующей попытки", user.telegram_id, e.retry_in
        )
    except Exception as e:
        logger.warning("recent_glucose: on-demand refresh не удался для %s: %s", user.telegram_id, e)

    max_points = 96

    # Все точки окна в хронологическом порядке (одним запросом — и для статистики, и для прореживания).
    q = db.query(GlucoseReading.ts, GlucoseReading.value, GlucoseReading.trend).filter(
        GlucoseReading.user_id == user.telegram_id, GlucoseReading.ts >= since
    )
    if until is not None:
        q = q.filter(GlucoseReading.ts < until)
    rows = q.order_by(GlucoseReading.ts.asc()).all()

    if not rows:
        return {
            "status": "ok",
            **window_desc,
            "total_count": 0,
            "stats": {"count": 0},
            "downsampled": False,
            "points": [],
            "refresh_skipped": refresh_skipped,
            **glucose_staleness(None, datetime.now(timezone.utc), refresh_skipped),
        }

    values = [float(val) for _, val, _ in rows]

    # Прореживание: равномерно выбрать max_points точек по всему окну (сохраняет форму
    # кривой). Глобальные min/max в stats считаются по ВСЕМ точкам, не по выборке.
    total = len(rows)
    if total > max_points:
        step = total / max_points
        idxs = sorted({min(int(i * step), total - 1) for i in range(max_points)})
        sampled = [rows[i] for i in idxs]
        downsampled = True
    else:
        sampled = rows
        downsampled = False

    points = [{"ts": _dt_isoformat_local(ts, user), "value": float(val), "trend": tr} for ts, val, tr in sampled]

    last_ts = rows[-1][0]  # хронологический порядок → последняя точка окна
    return {
        "status": "ok",
        **window_desc,
        "total_count": total,
        "returned_count": len(points),
        "stats": compute_glucose_stats(values),
        "downsampled": downsampled,
        "points": points,
        "refresh_skipped": refresh_skipped,
        "last_point_local": _dt_isoformat_local(last_ts, user),
        **glucose_staleness(last_ts, datetime.now(timezone.utc), refresh_skipped),
    }


@router.get("/glucose_stats")
async def glucose_stats(
    days: int = 7,
    user=Depends(get_agent_user),
    db: Session = Depends(get_db),
):
    """Сводная статистика глюкозы за N дней: TIR%, среднее, разброс + границы периода."""
    if days < 1 or days > 90:
        raise HTTPException(status_code=400, detail="days must be between 1 and 90")

    now = datetime.now(timezone.utc)
    since = now - timedelta(days=days)
    values = [
        float(v)
        for (v,) in db.query(GlucoseReading.value)
        .filter(GlucoseReading.user_id == user.telegram_id, GlucoseReading.ts >= since)
        .all()
    ]
    return {
        "status": "ok",
        "days": days,
        "since": since.date().isoformat(),
        "until": now.date().isoformat(),
        "stats": compute_glucose_stats(values),
    }


@router.get("/kb_value")
async def kb_value(
    key: str,
    user=Depends(get_agent_user),
):
    """Look up a value in knowledge_base.json by key path.

    Resolution order:
      1. Per-user KB at `kb_<telegram_id>.json` at repo root (any cohort) —
         synced from FamilyHealth/<user>/knowledge_base.json on demand.
      2. Owner-cohort fallback: legacy `knowledge_base.json` (Alex-only).
      3. Otherwise: returns null with source='kb-not-available'.
    """
    kb_path, source = _resolve_user_kb_path(user)
    if kb_path is None:
        return {"key": key, "value": None, "source": source}  # "kb-not-available"

    import json

    try:
        with open(kb_path, encoding="utf-8") as f:
            kb = json.load(f)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to read {source}: {e}")

    # Support dot-notation path traversal: e.g. "blood_tests.0.values.cholesterol"
    value = kb
    for part in key.split("."):
        if isinstance(value, dict):
            value = value.get(part)
        elif isinstance(value, list):
            try:
                value = value[int(part)]
            except (ValueError, IndexError):
                value = None
        else:
            value = None
        if value is None:
            break

    return {"key": key, "value": value, "source": source}


@router.get("/list_kb_keys")
async def list_kb_keys(user=Depends(get_agent_user)):
    """Top-level keys present in this user's knowledge_base.

    Возвращает реальный список ключей именно этого юзера — у разных людей
    схемы расходятся (у Андрея есть `echocardiogram`/`current_medications`,
    у Павла — `mrt`/`tumor_markers`, у Александра — `cardio`/`endoscopy`).
    Агент должен звать это перед `get_kb_value`, чтобы не гадать.

    Для каждого ключа отдаём type (list/dict/scalar) и count (длина для
    list/dict), чтобы агент понимал где искать. Служебные ключи `_*`
    и крупные дампы (`apple_health`, `cgm_data`) фильтруются.

    Секция `documents` — файлы, загруженные пользователем через /doc
    (`handlers/doc_upload.append_document_to_kb`). Числовые лабораторные
    показатели из них дублируются в Postgres `blood_tests` (#281), поэтому
    за анализами агент идёт в /recent_biomarkers, а сюда — за самим документом.
    """
    kb_path, source = _resolve_user_kb_path(user)
    if kb_path is None:
        return {"keys": [], "source": source}  # "kb-not-available"

    if not kb_path.exists():
        return {"keys": [], "source": "kb-not-found"}

    import json

    try:
        with open(kb_path, encoding="utf-8") as f:
            kb = json.load(f)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to read {source}: {e}")

    SKIP = {"apple_health", "cgm_data", "pdf_files", "_changelog"}
    keys = []
    for k, v in kb.items():
        if k.startswith("_") or k in SKIP:
            continue
        if isinstance(v, list):
            t, n = "list", len(v)
        elif isinstance(v, dict):
            t, n = "dict", len(v)
        else:
            t, n = "scalar", None
        # пустые секции тоже показываем — пусть агент видит что нет данных
        keys.append({"key": k, "type": t, "count": n})

    return {"keys": keys, "source": source, "total": len(keys)}


_CORRECTION_KEY_RE = re.compile(r"^[a-zA-Z0-9_-]{1,100}$")
_CORRECTION_MAX_VALUE_LEN = 2000


class AgentCorrectionRequest(BaseModel):
    key: str = Field(..., description="Уникальный ключ факта (snake_case, ≤100 символов)")
    value: str = Field(..., description="Значение (≤2000 символов)")
    reason: str = Field("", description="Откуда факт — слова пользователя")


@router.post("/add_agent_correction")
async def add_agent_correction(
    req: AgentCorrectionRequest,
    user=Depends(require_agent_scope("rw")),
):
    """Сохранить поправку или новый факт в секцию agent_corrections KB пользователя.

    Агент должен вызывать этот endpoint СРАЗУ при получении корректирующей
    информации от пользователя (дата операции, диагноз, новый препарат и т.п.).
    Данные записываются в KB-файл — при следующем разговоре агент увидит их.

    Ключ — только [a-zA-Z0-9_-], длина ≤100. Значение — строка ≤2000 символов.
    При повторном вызове с тем же ключом значение обновляется.
    """
    import json
    import tempfile

    if not _CORRECTION_KEY_RE.match(req.key):
        raise HTTPException(
            status_code=422,
            detail=f"Недопустимый ключ '{req.key}': только буквы, цифры, _ и -, длина ≤100",
        )
    if len(req.value) > _CORRECTION_MAX_VALUE_LEN:
        raise HTTPException(
            status_code=422,
            detail=f"Значение слишком длинное: {len(req.value)} > {_CORRECTION_MAX_VALUE_LEN}",
        )

    kb_path, source = _resolve_user_kb_path(user)
    if kb_path is None or not kb_path.exists():
        raise HTTPException(status_code=404, detail=f"KB не найден для пользователя {user.telegram_id}")

    try:
        kb = json.loads(kb_path.read_text(encoding="utf-8"))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Ошибка чтения {source}: {e}")

    corrections = kb.setdefault("agent_corrections", {})
    corrections[req.key] = {
        "value": req.value,
        "reason": req.reason,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }

    # Атомарная запись через временный файл
    try:
        tmp = tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=kb_path.parent,
            suffix=".tmp",
            delete=False,
        )
        json.dump(kb, tmp, ensure_ascii=False, indent=2)
        tmp.flush()
        tmp.close()
        Path(tmp.name).replace(kb_path)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Ошибка записи KB: {e}")

    return {"status": "ok", "key": req.key, "source": source}


@router.get("/open_questions")
async def open_questions(user=Depends(get_agent_user)):
    """Открытые клинические вопросы и красные флаги из KB пользователя.

    Каждый человек ведёт «висящие» вопросы которые ждут решения врача,
    повторного анализа или клинического follow-up — например «K+/Mg+
    ни разу не сдавались при QTc 0.60», «микрогематурия 04.2025 без
    дообследования», «HbA1c на Метформине ни разу не измерен».

    Бот ДОЛЖЕН проактивно поднимать их при ЛЮБОМ медицинском вопросе,
    даже если пользователь спрашивает о другом. Прецедент 25.05.2026 —
    папа спрашивал «какие диагнозы» и «разбор анализов», бот корректно
    отвечал по факту, но НЕ упомянул что K/Mg/ТТГ должны быть в
    следующем заборе (хотя в его KB это давно как красный флаг).

    Источники в KB (бот пробует по очереди):
      1. `open_questions` (список строк) — у папы
      2. `open_issues` (список строк/dict) — альтернативное имя
      3. `urgent_problems` (dict с приоритетами) — если завели
      4. `red_flags` (список) — ещё один синоним

    Возвращает: {questions: [...], source: '<key>', count: N}.
    Если ничего не найдено — questions=[], source='not-tracked'.
    """
    kb_path, source = _resolve_user_kb_path(user)
    if kb_path is None or not kb_path.exists():
        return {"questions": [], "source": "kb-not-available", "count": 0}

    import json

    try:
        with open(kb_path, encoding="utf-8") as f:
            kb = json.load(f)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to read {source}: {e}")

    # Try multiple known key names — у людей разные схемы
    candidates = ["open_questions", "open_issues", "urgent_problems", "red_flags"]
    for k in candidates:
        v = kb.get(k)
        if v is None:
            continue
        if isinstance(v, list) and len(v) > 0:
            return {"questions": v, "source": k, "count": len(v)}
        if isinstance(v, dict) and len(v) > 0:
            # Преобразуем dict в список «{приоритет}: {описание}» строк
            flattened = [f"{prio}: {desc}" for prio, desc in v.items()]
            return {"questions": flattened, "source": k, "count": len(flattened)}

    return {"questions": [], "source": "not-tracked", "count": 0}


class RenderReportRequest(BaseModel):
    report_type: str = Field(
        "biomarker_dynamics",
        description=(
            "Тип отчёта: 'biomarker_dynamics' (общая панель 2×3) или "
            "'single_biomarker' (один маркер крупным планом, требует поле marker)."
        ),
    )
    marker: Optional[str] = Field(
        None,
        description="Имя биомаркера для single_biomarker. Принимает алиасы и русские названия.",
    )


@router.post("/render_report")
async def render_report(
    req: RenderReportRequest,
    user=Depends(require_agent_scope("rw")),
):
    """Сгенерировать PNG-инфографику и отправить юзеру в Telegram.

    Side-effect: вызывает sendPhoto через Bot API на user.telegram_id.
    Tool возвращает агенту короткий статус — агент дальше отвечает текстом
    («вот разбор, главное что вижу — ...»).

    Зачем не возвращать base64 / image-url агенту: Anthropic Messages API
    умеет принимать image на вход модели, но передать картинку дальше в
    Telegram всё равно надо отдельным вызовом. Проще, чтобы тул сам слал.
    """
    import requests as _requests
    from core.reports.biomarker_dynamics import (
        render_biomarker_dynamics_png,
        render_single_marker_png,
    )

    if req.report_type not in ("biomarker_dynamics", "single_biomarker"):
        return {
            "status": "error",
            "error": (f"unknown report_type '{req.report_type}'; supported: biomarker_dynamics, single_biomarker"),
        }

    kb_path, _source = _resolve_user_kb_path(user)
    if kb_path is None:
        return {"status": "error", "error": "kb-not-available", "sent": False}

    user_name = (user.first_name or "").strip()
    caption_suffix = ""
    try:
        if req.report_type == "single_biomarker":
            if not req.marker:
                return {
                    "status": "error",
                    "error": "missing-marker: для single_biomarker нужен параметр marker",
                    "sent": False,
                }
            result = render_single_marker_png(kb_path, req.marker, user_name=user_name)
            # render_single_marker_png может вернуть dict с ошибкой
            if isinstance(result, dict):
                return {"status": "error", "sent": False, **result}
            png_bytes = result
            caption_suffix = f" · {req.marker}"
        else:
            png_bytes = render_biomarker_dynamics_png(kb_path, user_name=user_name)
    except Exception as e:
        logger.error("render_report failed for %s: %s", user.telegram_id, e, exc_info=True)
        return {"status": "error", "error": f"render-failed: {e}", "sent": False}

    if not png_bytes:
        return {
            "status": "error",
            "error": "not-enough-data: нет биомаркеров с ≥2 наблюдениями",
            "sent": False,
        }

    bot_token = resolve_bot_token()
    if not bot_token:
        return {"status": "error", "error": "bot-token-missing", "sent": False}

    try:
        resp = _requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendPhoto",
            data={
                "chat_id": user.telegram_id,
                "caption": (f"📊 Динамика биомаркеров{' · ' + user_name if user_name else ''}{caption_suffix}"),
            },
            files={"photo": ("biomarkers.png", png_bytes, "image/png")},
            timeout=20,
        )
        result = resp.json()
        if not result.get("ok"):
            logger.warning("sendPhoto failed: %s", result)
            return {"status": "error", "error": f"telegram: {result.get('description')}", "sent": False}
    except Exception as e:
        logger.error("sendPhoto exception for %s: %s", user.telegram_id, e, exc_info=True)
        return {"status": "error", "error": f"telegram-exception: {e}", "sent": False}

    return {
        "status": "ok",
        "sent": True,
        "report_type": req.report_type,
        "kb_source": kb_path.name,
        "telegram_message_id": result["result"].get("message_id"),
    }


@router.post("/doctor_report")
async def doctor_report(
    body: dict = Body(default={}),
    user=Depends(require_agent_scope("rw")),
    db=Depends(get_db),
):
    """Сгенерировать PDF-отчёт для врача (секции IPS) и отправить юзеру в чат.

    Side-effect: шлёт PDF Telegram-документом на user.telegram_id через общий
    helper send_doctor_report_to_chat (тот же путь, что у кнопки мини-аппа, #290/#291).
    body.language ('ru'|'en') задаёт язык отчёта (#300); иначе — ru.
    Агенту возвращаем короткий статус {status, sent} — дальше он отвечает текстом.
    """
    from services.doctor_report import send_doctor_report_to_chat
    from services.report_i18n import resolve_report_language

    lang = resolve_report_language((body or {}).get("language"), None)
    return send_doctor_report_to_chat(db, user.telegram_id, lang=lang)


# ── Универсальный render через QuickChart.io ─────────────────────────────────


class RenderChartRequest(BaseModel):
    """Универсальный рендер графика через публичный QuickChart.io.

    chart: Chart.js v4 JSON конфиг (как объект, не строка).
    caption: подпись к фото в Telegram (опционально).
    width / height: размеры в пикселях, по умолчанию mobile-friendly.
    """

    chart: dict = Field(..., description="Chart.js v4 config object")
    caption: Optional[str] = Field(None, description="Подпись к фото в Telegram")
    width: int = Field(600, ge=200, le=1600)
    height: int = Field(400, ge=200, le=1200)


_BOTKIN_PALETTE = [
    "#34c759",  # Botkin green (норма)
    "#ff9500",  # Botkin orange (внимание/выше нормы)
    "#007aff",  # Apple blue (нейтральный)
    "#5856d6",  # фиолет
    "#ff2d55",  # розово-красный
    "#5ac8fa",  # светло-голубой
]


def _enrich_chart_spec(chart: dict) -> dict:
    """Добавить визуальные defaults в минимальный spec.

    Агент шлёт `{type, data: {labels, datasets: [{label, data, yAxisID?}]}, options?}`,
    мы добиваем стиль (цвета, толщина, точки, заголовок, легенду, шрифт).
    Это режет output tokens агента примерно вдвое и заодно даёт единый
    Botkin-стиль на всех графиках.
    """
    if not isinstance(chart, dict):
        return chart
    chart = dict(chart)  # shallow copy чтобы не мутировать вход

    # Дефолты для каждого dataset
    data = chart.get("data") or {}
    datasets = data.get("datasets") or []
    chart_type = chart.get("type", "line")
    new_datasets = []
    for i, ds in enumerate(datasets):
        ds = dict(ds)
        color = _BOTKIN_PALETTE[i % len(_BOTKIN_PALETTE)]
        ds.setdefault("borderColor", color)
        if chart_type in ("line", "scatter"):
            ds.setdefault("backgroundColor", color + "26")  # ~15% alpha hex
            ds.setdefault("borderWidth", 2)
            ds.setdefault("pointRadius", 4)
            ds.setdefault("pointHoverRadius", 6)
            ds.setdefault("tension", 0.3)
            ds.setdefault("fill", False)
        elif chart_type in ("bar",):
            ds.setdefault("backgroundColor", color)
            ds.setdefault("borderWidth", 0)
        elif chart_type in ("doughnut", "pie", "polarArea"):
            # Для круговых каждый сегмент свой цвет — расширяем массив
            n = len(ds.get("data") or [])
            ds.setdefault(
                "backgroundColor",
                [_BOTKIN_PALETTE[k % len(_BOTKIN_PALETTE)] for k in range(n)],
            )
        new_datasets.append(ds)
    data["datasets"] = new_datasets
    chart["data"] = data

    # Дефолты options
    options = chart.get("options") or {}
    options.setdefault("responsive", False)
    options.setdefault("maintainAspectRatio", False)

    plugins = options.get("plugins") or {}
    plugins.setdefault(
        "legend",
        {"display": len(new_datasets) > 1, "position": "top", "labels": {"font": {"size": 13}}},
    )
    title_cfg = plugins.get("title") or {}
    if title_cfg.get("text"):
        title_cfg.setdefault("display", True)
        title_cfg.setdefault("font", {"size": 15, "weight": "bold"})
        title_cfg.setdefault("padding", {"bottom": 12})
        plugins["title"] = title_cfg
    options["plugins"] = plugins

    # Сетка побледнее, шрифт читаемее
    scales = options.get("scales") or {}
    for ax_id, ax in list(scales.items()):
        ax = dict(ax) if isinstance(ax, dict) else {}
        ax.setdefault("grid", {"color": "#e5e5ea", "lineWidth": 0.5})
        ax.setdefault("ticks", {"font": {"size": 11}})
        scales[ax_id] = ax
    if scales:
        options["scales"] = scales

    chart["options"] = options
    return chart


@router.post("/render_chart")
async def render_chart(
    req: RenderChartRequest,
    user=Depends(require_agent_scope("rw")),
):
    """Рендерит произвольный график через QuickChart.io и отправляет в Telegram.

    Side-effect: данные графика (числа, подписи, имя) уходят на сервер
    QuickChart Inc. для рендера. См. tradeoffs в обсуждении выбора. Если
    приватность критична — заменить URL на self-hosted instance.
    """
    import os
    import requests as _requests

    bot_token = resolve_bot_token()
    if not bot_token:
        return {"status": "error", "error": "bot-token-missing", "sent": False}

    # Добиваем минимальный spec нашими дефолтами (цвета, шрифты, легенда)
    enriched_chart = _enrich_chart_spec(req.chart)

    # 1. Рендер через QuickChart
    qc_url = os.getenv("QUICKCHART_URL", "https://quickchart.io/chart")
    try:
        qc_resp = _requests.post(
            qc_url,
            json={
                "chart": enriched_chart,
                "width": req.width,
                "height": req.height,
                "backgroundColor": "white",
                "format": "png",
                "version": "4",
            },
            timeout=15,
        )
    except Exception as e:
        logger.error("quickchart request failed for %s: %s", user.telegram_id, e)
        return {"status": "error", "error": f"quickchart-network: {e}", "sent": False}

    if not qc_resp.ok:
        # QuickChart возвращает текст ошибки в body — отдадим агенту чтобы он мог исправить spec
        err_body = qc_resp.text[:400]
        logger.warning("quickchart %s for user %s: %s", qc_resp.status_code, user.telegram_id, err_body)
        return {
            "status": "error",
            "error": f"quickchart-{qc_resp.status_code}",
            "details": err_body,
            "sent": False,
        }

    png_bytes = qc_resp.content
    if not png_bytes or len(png_bytes) < 200:
        return {"status": "error", "error": "quickchart-empty-response", "sent": False}

    # 2. sendPhoto в Telegram
    try:
        tg_resp = _requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendPhoto",
            data={
                "chat_id": user.telegram_id,
                "caption": req.caption or "",
            },
            files={"photo": ("chart.png", png_bytes, "image/png")},
            timeout=20,
        )
        result = tg_resp.json()
        if not result.get("ok"):
            return {"status": "error", "error": f"telegram: {result.get('description')}", "sent": False}
    except Exception as e:
        logger.error("sendPhoto exception in render_chart for %s: %s", user.telegram_id, e, exc_info=True)
        return {"status": "error", "error": f"telegram-exception: {e}", "sent": False}

    return {
        "status": "ok",
        "sent": True,
        "telegram_message_id": result["result"].get("message_id"),
        "chart_size_bytes": len(png_bytes),
    }


@router.get("/meal_context")
async def meal_context(
    user=Depends(get_agent_user),
    db: Session = Depends(get_db),
):
    """P-002: всё нужное для «что мне съесть сейчас» ОДНИМ вызовом —
    остаток КБЖУ на сегодня + ограничения (диагнозы) + любимые продукты.

    Зачем тул, а не три вызова: гарантирует, что ограничения-диагнозы (подагра,
    демпинг-синдром и т.п.) ВСЕГДА в контексте, и экономит токены/реплики.
    """
    import json

    from core.health.caloric_budget import get_daily_budget
    from database.crud import get_nutrition_totals_by_date, get_recent_product_names

    today = _today_in_user_tz(user)

    # Бюджет (consumed/target/remaining) — self-contained хелпер со своей сессией.
    try:
        budget = get_daily_budget(user.telegram_id, for_date=today)
    except Exception:
        logger.exception("meal_context: get_daily_budget failed")
        budget = {}

    totals = get_nutrition_totals_by_date(db, user.telegram_id, today) or {}

    try:
        products = get_recent_product_names(db, user.telegram_id, limit=15, lookback_days=60)
    except Exception:
        products = []

    # Ограничения из KB (диагнозы) — чтобы советы были безопасны под состояние.
    constraints = []
    kb_path, _src = _resolve_user_kb_path(user)
    if kb_path:
        try:
            kb = _as_dict(json.loads(kb_path.read_text(encoding="utf-8")))
            for key in ("chronic_diagnoses", "diagnoses", "conditions"):
                v = kb.get(key)
                if v:
                    constraints = v if isinstance(v, list) else [v]
                    break
        except Exception:
            logger.exception("meal_context: KB read failed")

    return {
        "status": "ok",
        "date": today.isoformat(),
        "budget": {
            "target_kcal": budget.get("target"),
            "consumed_kcal": budget.get("consumed"),
            "remaining_kcal": budget.get("remaining"),
        },
        "eaten_today": {
            "calories": totals.get("calories"),
            "protein": totals.get("protein"),
            "fats": totals.get("fats"),
            "carbs": totals.get("carbs"),
            "fiber": totals.get("fiber"),
        },
        "constraints": constraints,
        "frequent_products": products[:15],
    }


@router.get("/dashboard_summary")
async def dashboard_summary(
    user=Depends(get_agent_user),
    db: Session = Depends(get_db),
):
    """Aggregated health metrics for the last 7 days.

    Returns averages for steps, HR, calories consumed, and latest weight.
    Handles missing data gracefully (None values).
    """
    from database.crud import (
        get_activity_logs_by_period,
        get_nutrition_logs_by_period,
        get_latest_weight,
    )

    end_date = _today_in_user_tz(user)
    start_date = end_date - timedelta(days=6)

    activity_rows = get_activity_logs_by_period(db, user.telegram_id, start_date, end_date)
    nutrition_rows = get_nutrition_logs_by_period(db, user.telegram_id, start_date, end_date)
    latest_weight = get_latest_weight(db, user.telegram_id)

    # Activity aggregations
    steps_vals = [r.steps for r in activity_rows if r.steps is not None]
    hr_vals = [r.heart_rate_avg for r in activity_rows if r.heart_rate_avg is not None]
    kcal_burned_vals = [r.total_calories for r in activity_rows if r.total_calories is not None]

    # Nutrition aggregations — sum per day, then average
    from collections import defaultdict

    kcal_by_day: dict = defaultdict(float)
    for row in nutrition_rows:
        totals = row.totals or {}
        kcal = totals.get("calories") or 0
        kcal_by_day[row.date.isoformat()] += kcal
    kcal_consumed_vals = list(kcal_by_day.values())

    def _avg(vals):
        return round(sum(vals) / len(vals), 1) if vals else None

    return {
        "status": "ok",
        "period": {"start": start_date.isoformat(), "end": end_date.isoformat(), "days": 7},
        "activity": {
            "avg_steps": int(_avg(steps_vals)) if _avg(steps_vals) is not None else None,
            "avg_hr": int(_avg(hr_vals)) if _avg(hr_vals) is not None else None,
            "avg_kcal_burned": _avg(kcal_burned_vals),
            "days_with_data": len(activity_rows),
        },
        "nutrition": {
            "avg_kcal_consumed": _avg(kcal_consumed_vals),
            "days_with_logs": len(kcal_by_day),
        },
        "weight": {
            "latest_kg": latest_weight.weight if latest_weight else None,
            "latest_date": latest_weight.measured_at.date().isoformat() if latest_weight else None,
            "body_fat_pct": latest_weight.body_fat if latest_weight else None,
        },
        "dashboard_url": f"{public_base_url()}/mc/{user.share_token}" if user.share_token else None,
    }


@router.get("/recent_bp")
async def recent_bp(
    days: int = 14,
    user=Depends(get_agent_user),
    db: Session = Depends(get_db),
):
    """Recent blood-pressure measurements (last `days`).

    Returns each row with measured_at, systolic, diastolic, pulse, source.
    Plus simple aggregates: mean/min/max systolic+diastolic, latest pulse,
    pct of measurements above 140/90 (Stage 1 hypertension threshold).
    """
    from sqlalchemy import text as sql_text

    days = max(1, min(days, 90))
    sql = sql_text(
        """
        SELECT measured_at, systolic, diastolic, heart_rate, source
        FROM blood_pressure_logs
        WHERE user_id = :uid
          AND measured_at >= NOW() - (:days || ' days')::interval
        ORDER BY measured_at DESC
        LIMIT 200
        """
    )
    rows = db.execute(sql, {"uid": user.telegram_id, "days": days}).fetchall()
    items = [
        {
            "measured_at": _dt_isoformat_local(r.measured_at, user),
            "systolic": r.systolic,
            "diastolic": r.diastolic,
            "pulse": r.heart_rate,
            "source": r.source,
        }
        for r in rows
    ]

    if not items:
        return {"status": "ok", "period_days": days, "count": 0, "items": []}

    sys_vals = [i["systolic"] for i in items]
    dia_vals = [i["diastolic"] for i in items]
    above_threshold = sum(1 for i in items if i["systolic"] >= 140 or i["diastolic"] >= 90)

    return {
        "status": "ok",
        "period_days": days,
        "count": len(items),
        "stats": {
            "systolic": {"avg": round(sum(sys_vals) / len(sys_vals), 1), "min": min(sys_vals), "max": max(sys_vals)},
            "diastolic": {"avg": round(sum(dia_vals) / len(dia_vals), 1), "min": min(dia_vals), "max": max(dia_vals)},
            "stage1_pct": round(100 * above_threshold / len(items), 1),
        },
        "items": items[:30],  # cap for token budget
    }


@router.get("/menstrual_data")
async def menstrual_data(
    months: int = 6,
    user=Depends(get_agent_user),
    db: Session = Depends(get_db),
):
    """Menstrual cycle data from Apple Health (menstrual_log table).

    Returns individual flow records and computed cycle lengths.
    Use for questions about cycle regularity, length, flow intensity.
    """
    from sqlalchemy import text as sql_text

    months = max(1, min(months, 24))
    sql = sql_text(
        """
        SELECT date, flow, source
        FROM menstrual_log
        WHERE user_id = :uid
          AND date >= CURRENT_DATE - (:months * 30)
        ORDER BY date ASC
        """
    )
    rows = db.execute(sql, {"uid": user.telegram_id, "months": months}).fetchall()

    if not rows:
        all_rows = db.execute(
            sql_text("SELECT date, flow, source FROM menstrual_log WHERE user_id=:uid ORDER BY date ASC"),
            {"uid": user.telegram_id},
        ).fetchall()
        if not all_rows:
            return {"status": "ok", "count": 0, "items": [], "cycles": [], "stats": {}}
        rows = all_rows

    items = [{"date": str(r.date), "flow": r.flow} for r in rows]

    # Вычислить начала циклов: первый день после перерыва >2 дней
    from datetime import date as date_type

    period_starts = []
    prev_date = None
    for item in items:
        if item["flow"] == "none":
            prev_date = None
            continue
        d = date_type.fromisoformat(item["date"])
        if prev_date is None or (d - prev_date).days > 2:
            period_starts.append(item["date"])
        prev_date = d

    cycles = []
    for i in range(1, len(period_starts)):
        d1 = date_type.fromisoformat(period_starts[i - 1])
        d2 = date_type.fromisoformat(period_starts[i])
        length = (d2 - d1).days
        if 15 < length < 60:
            cycles.append({"start": period_starts[i - 1], "length_days": length})

    cycle_lengths = [c["length_days"] for c in cycles]
    avg_cycle = round(sum(cycle_lengths) / len(cycle_lengths), 1) if cycle_lengths else None
    variation = round(max(cycle_lengths) - min(cycle_lengths), 0) if len(cycle_lengths) > 1 else 0

    return {
        "status": "ok",
        "count": len(items),
        "period_starts": period_starts,
        "cycles": cycles[-12:],
        "stats": {
            "avg_cycle_days": avg_cycle,
            "variation_days": variation,
            "total_periods": len(period_starts),
        },
        "items": items,
    }


@router.get("/recent_sleep")
async def recent_sleep(
    days: int = 14,
    user=Depends(get_agent_user),
    db: Session = Depends(get_db),
):
    """Recent sleep — из колонки activity_log.sleep_hours (надёжный источник).

    ВАЖНО (фикс 06.06.2026): раньше читали `raw_data->>'sleepingSeconds'`, которое
    заполняется лишь иногда (из daily-summary) → агент НЕ видел сон, который реально
    есть в БД (последние ночи имели пустой sleepingSeconds, но заполненный
    sleep_hours). Теперь читаем колонку `sleep_hours`, которую надёжно пишет
    `scripts/util/server_backfill_postgres.py::sync_sleep` из файлов Garmin sleep/.
    sleep_score/deep_h/rem_h остаются в raw_data (пишет тот же sync_sleep).

    Date semantics: `date` — календарный день; сон относится к ночи, ЗАКАНЧИВАЮЩЕЙСЯ
    в этот день (конвенция Garmin).
    """
    from sqlalchemy import text as sql_text

    days = max(1, min(days, 90))
    sql = sql_text(
        """
        SELECT date,
               sleep_hours                          AS duration_hours,
               (raw_data->>'sleep_score')::int      AS quality_score,
               (raw_data->>'deep_h')::numeric * 60  AS deep_min,
               (raw_data->>'rem_h')::numeric * 60   AS rem_min,
               source
        FROM activity_log
        WHERE user_id = :uid
          AND sleep_hours IS NOT NULL
          AND sleep_hours > 0
          AND date >= CURRENT_DATE - (:days || ' days')::interval
        ORDER BY date DESC
        """
    )
    rows = db.execute(sql, {"uid": user.telegram_id, "days": days}).fetchall()
    items = [
        {
            "date": r.date.isoformat(),
            "duration_hours": round(float(r.duration_hours), 2) if r.duration_hours is not None else None,
            "quality_score": r.quality_score,
            "deep_min": int(r.deep_min) if r.deep_min is not None else None,
            "rem_min": int(r.rem_min) if r.rem_min is not None else None,
            "source": r.source,
        }
        for r in rows
    ]

    # Свежесть: последняя НОЧЬ с данными, независимо от окна `days`. Чтобы агент
    # при пустом окне честно сказал «последний сон за DATE», а не «данных нет»
    # (данные Garmin приходят с задержкой; авто-синк идёт периодически).
    latest = db.execute(
        sql_text(
            "SELECT MAX(date) FROM activity_log WHERE user_id = :uid AND sleep_hours IS NOT NULL AND sleep_hours > 0"
        ),
        {"uid": user.telegram_id},
    ).scalar()
    latest_iso = latest.isoformat() if latest else None

    if not items:
        return {
            "status": "ok",
            "period_days": days,
            "count": 0,
            "items": [],
            "latest_available_date": latest_iso,
        }

    dur = [i["duration_hours"] for i in items if i["duration_hours"]]
    qual = [i["quality_score"] for i in items if i["quality_score"]]
    # Sleep quality flags by duration vs 7h adequate / 6h marginal.
    below_6h = sum(1 for d in dur if d < 6)
    return {
        "status": "ok",
        "period_days": days,
        "count": len(items),
        "stats": {
            "avg_duration_h": round(sum(dur) / len(dur), 2) if dur else None,
            "min_duration_h": round(min(dur), 2) if dur else None,
            "max_duration_h": round(max(dur), 2) if dur else None,
            "avg_quality": round(sum(qual) / len(qual), 1) if qual else None,
            "nights_below_6h": below_6h,
            "nights_below_6h_pct": round(100 * below_6h / len(dur), 1) if dur else None,
        },
        "items": items[:14],
        "latest_available_date": latest_iso,
    }


@router.get("/recent_supplements")
async def recent_supplements(
    days: int = 30,
    user=Depends(get_agent_user),
    db: Session = Depends(get_db),
):
    """Recent supplement intake log with per-supplement aggregation.

    Reads from `supplements_log` (filled by aiogram bot when user logs
    "выпил магний" etc). Returns:
      - per-supplement: days_taken in period, total_intakes (multi-dose/day OK),
        last_taken_date, last_dosage seen
      - period stats: total log lines

    Default 30 days — typical regimen feedback window.
    """
    from sqlalchemy import text as sql_text

    days = max(1, min(days, 180))
    sql = sql_text(
        """
        SELECT supplement_name,
               COUNT(*)                          AS total_intakes,
               COUNT(DISTINCT date)              AS days_taken,
               MAX(date)                         AS last_date,
               (ARRAY_AGG(dosage ORDER BY date DESC, time DESC NULLS LAST))[1] AS last_dosage
        FROM supplements_log
        WHERE user_id = :uid
          AND date >= CURRENT_DATE - (:days || ' days')::interval
        GROUP BY supplement_name
        ORDER BY days_taken DESC, supplement_name
        """
    )
    rows = db.execute(sql, {"uid": user.telegram_id, "days": days}).fetchall()
    items = [
        {
            "supplement": r.supplement_name,
            "days_taken": r.days_taken,
            "total_intakes": r.total_intakes,
            "intakes_per_day_avg": round(r.total_intakes / r.days_taken, 2) if r.days_taken else 0,
            "adherence_pct": round(100 * r.days_taken / days, 1),
            "last_date": r.last_date.isoformat() if r.last_date else None,
            "last_dosage": r.last_dosage,
        }
        for r in rows
    ]
    return {
        "status": "ok",
        "period_days": days,
        "unique_supplements": len(items),
        "total_log_entries": sum(i["total_intakes"] for i in items),
        "items": items,
    }


@router.get("/supplement_daily_log")
async def supplement_daily_log(
    days: int = 30,
    supplement: Optional[str] = None,
    user=Depends(get_agent_user),
    db: Session = Depends(get_db),
):
    """Per-day supplement intake log for correlation analysis.

    В отличие от /recent_supplements (только агрегаты), возвращает для каждой
    добавки СПИСОК ДАТ приёма за окно + границы окна — чтобы сопоставлять факт
    приёма по дням с данными сна/HRV/активности (напр. магний ↔ качество сна).

    supplements_log хранит строку на каждый приём; «не принимал» = отсутствие
    строки. Отдаём разреженно: taken_dates + [start_date, end_date] полностью
    определяют taken/not-taken для любого дня без плотной сетки N×дни.

    Параметры:
      days: окно, 1..180 (дефолт 30).
      supplement: опц. подстрока имени (регистронезависимо) — один препарат.
    """
    from datetime import time as _dtime

    from database.models import SupplementLog

    days = max(1, min(days, 180))
    # «Сегодня» в tz пользователя — иначе поздние вечерние записи уедут за окно
    # (та же логика, что в других эндпоинтах агента).
    tz_name = getattr(user, "timezone", None) or "Europe/Moscow"
    try:
        user_tz = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, KeyError):
        user_tz = ZoneInfo("Europe/Moscow")
    today = datetime.now(user_tz).date()
    start_date = today - timedelta(days=days - 1)

    rows = (
        db.query(
            SupplementLog.supplement_name,
            SupplementLog.date,
            SupplementLog.time,
            SupplementLog.dosage,
        )
        .filter(
            SupplementLog.user_id == user.telegram_id,
            SupplementLog.date >= start_date,
        )
        .order_by(SupplementLog.supplement_name, SupplementLog.date)
        .all()
    )

    name_filter = supplement.strip().lower() if supplement else None

    # Группируем по имени добавки, копим множество дат приёма + последнюю дозу.
    groups: dict = {}
    for r in rows:
        name = r.supplement_name or ""
        if name_filter and name_filter not in name.lower():
            continue
        g = groups.setdefault(name, {"dates": set(), "last_key": None, "last_dosage": None})
        g["dates"].add(r.date)
        key = (r.date, r.time or _dtime.min)
        if g["last_key"] is None or key > g["last_key"]:
            g["last_key"] = key
            g["last_dosage"] = r.dosage

    supplements = []
    for name, g in groups.items():
        dates_sorted = sorted(g["dates"])
        supplements.append(
            {
                "supplement": name,
                "days_taken": len(dates_sorted),
                "adherence_pct": round(100 * len(dates_sorted) / days, 1),
                "taken_dates": [d.isoformat() for d in dates_sorted],
                "last_dosage": g["last_dosage"],
            }
        )
    # По убыванию частоты, затем по имени — как в /recent_supplements.
    supplements.sort(key=lambda s: (-s["days_taken"], s["supplement"]))

    return {
        "status": "ok",
        "period_days": days,
        "start_date": start_date.isoformat(),
        "end_date": today.isoformat(),
        "unique_supplements": len(supplements),
        "supplements": supplements,
    }


@router.get("/recent_biomarkers")
async def recent_biomarkers(
    limit: int = 20,
    user=Depends(get_agent_user),
    db: Session = Depends(get_db),
):
    """Most recent blood tests (latest `limit`).

    Each row has test_date + test_type + values (jsonb dict of marker → value).
    Default raised to 20 so questions like "как менялся холестерин" cover
    ~1 year of history without follow-up calls.
    """
    from sqlalchemy import text as sql_text

    limit = max(1, min(limit, 100))
    sql = sql_text(
        """
        SELECT test_date, test_type, "values"
        FROM blood_tests
        WHERE user_id = :uid
        ORDER BY test_date DESC
        LIMIT :lim
        """
    )
    rows = db.execute(sql, {"uid": user.telegram_id, "lim": limit}).fetchall()
    from core.health.kb_schema import to_canonical

    tests = []
    for r in rows:
        canon, _w = to_canonical(_as_dict(r.values), passthrough_unmapped=True)
        # test_date — date на Postgres, str через SQLite (raw text query); поддержим оба.
        d = r.test_date.isoformat() if hasattr(r.test_date, "isoformat") else str(r.test_date)
        tests.append({"date": d, "type": r.test_type, "values": canon})
    return {"status": "ok", "count": len(tests), "tests": tests}


@router.get("/latest_biomarkers")
async def latest_biomarkers(
    user=Depends(get_agent_user),
    db: Session = Depends(get_db),
):
    """Latest value per canonical biomarker with staleness info.

    Unlike /recent_biomarkers (raw test rows, useful for trends),
    this returns ONE entry per canonical key — the most recent value
    and its staleness status.

    Use this when the user asks about a specific marker or their overall
    biomarker state. Use /recent_biomarkers for historical trends.
    """
    from datetime import date as _date

    from sqlalchemy import text as sql_text

    from core.health.biomarkers import aggregate_biomarkers
    from core.health.kb_schema import CANONICAL
    from core.health.staleness import stale_label

    rows = db.execute(
        sql_text('SELECT test_date, "values" FROM blood_tests WHERE user_id = :uid ORDER BY test_date DESC'),
        {"uid": user.telegram_id},
    ).fetchall()

    # test_date is a date on Postgres but a str via SQLite (raw text query) — handle both.
    tests = [
        {
            "date": r.test_date.isoformat() if hasattr(r.test_date, "isoformat") else str(r.test_date),
            "values": _as_dict(r.values),
        }
        for r in rows
    ]
    bio = aggregate_biomarkers(tests)

    result: dict[str, dict] = {}
    stale_keys: list[str] = []

    for key, entry in bio.items():
        if key.startswith("_"):
            continue
        marker = CANONICAL.get(key)
        unit = marker.unit if marker is not None else ""
        sl = stale_label(entry.get("days_ago"), entry.get("staleness_threshold_days"))
        if entry.get("is_stale"):
            stale_keys.append(key)
        result[key] = {
            "value": entry["value"],
            "unit": unit,
            "date": entry["date"],
            "days_ago": entry.get("days_ago"),
            "threshold_days": entry.get("staleness_threshold_days"),
            "is_stale": entry.get("is_stale", False),
            "stale_label": sl,
        }

    return {
        "status": "ok",
        "as_of": _date.today().isoformat(),
        "count": len(result),
        "biomarkers": result,
        "stale_count": len(stale_keys),
        "stale_keys": stale_keys,
    }


@router.get("/phenoage")
async def phenoage(
    user=Depends(get_agent_user),
    db: Session = Depends(get_db),
):
    """Biological age via Levine 2018 (Aging Cell) PhenoAge formula.

    Requires 9 markers from blood_tests.values (latest available value per
    marker, scanning all of user's history). Plus chronological age from
    users.birth_date.

    Returns: bio_age, chronological_age, delta, markers with direction
    ('younger'/'older' vs NHANES median for ~48yo male) and freshness.
    """
    from sqlalchemy import text as sql_text

    # Required markers — keys in blood_tests.values JSONB.
    markers = ["albumin_g_l", "creatinine", "glucose", "hs_CRP", "lymphocytes", "MCV", "RDW_CV", "ALP", "WBC"]

    # Забираем все строки юзера и канонизируем в Python — формат-агностично
    # (работает для CamelCase Александра и snake_case_with_units Димы).
    from core.health.kb_schema import to_canonical

    rows = db.execute(
        sql_text('SELECT test_date, "values" FROM blood_tests WHERE user_id = :uid ORDER BY test_date DESC'),
        {"uid": user.telegram_id},
    ).fetchall()

    latest: dict[str, dict] = {}
    for r in rows:
        canon, _w = to_canonical(_as_dict(r.values))
        # test_date — date на Postgres, str через SQLite (raw text query); поддержим оба.
        d = r.test_date.isoformat() if hasattr(r.test_date, "isoformat") else str(r.test_date)
        for key in markers:
            if key in canon and key not in latest:
                latest[key] = {"value": float(canon[key]), "date": d}

    # Chronological age
    chrono_age = None
    if user.birth_date:
        today = date.today()
        chrono_age = (
            today.year
            - user.birth_date.year
            - ((today.month, today.day) < (user.birth_date.month, user.birth_date.day))
        )

    # NHANES median for ~48yo male, plus direction (higher_is_younger)
    nhanes = {
        "albumin_g_l": (42.0, True),  # g/L (4.2 g/dL)
        "creatinine": (92.8, False),  # µmol/L (1.05 mg/dL)
        "glucose": (5.3, False),  # mmol/L (95 mg/dL)
        "hs_CRP": (1.0, False),  # mg/L (ln(0.1) → 0)
        "lymphocytes": (28.0, True),  # %
        "MCV": (90.0, False),  # fL
        "RDW_CV": (13.8, False),  # %
        "ALP": (68.0, False),  # U/L
        "WBC": (6.7, False),  # ×10³/µL
    }

    today_date = date.today()
    marker_list: list[dict] = []
    younger_count = 0
    stale_markers: list[str] = []
    for key in markers:
        info = latest.get(key)
        if not info:
            marker_list.append({"name": key, "value": None, "direction": "unknown", "date": None})
            continue
        med, higher_younger = nhanes[key]
        v = info["value"]
        is_younger = (v > med) if higher_younger else (v < med)
        if is_younger:
            younger_count += 1
        days_ago = (today_date - date.fromisoformat(info["date"])).days
        stale = days_ago > 365
        if stale:
            stale_markers.append(f"{key} ({info['date']})")
        marker_list.append(
            {
                "name": key,
                "value": round(v, 3),
                "direction": "younger" if is_younger else "older",
                "date": info["date"],
                "days_ago": days_ago,
                "stale_over_year": stale,
            }
        )

    bio_age: Optional[float] = None
    error: Optional[str] = None
    if chrono_age is None:
        error = "users.birth_date not set"
    elif None in [latest.get(k, {}).get("value") for k in markers]:
        missing = [k for k in markers if k not in latest]
        error = f"missing markers: {missing}"
    elif latest["hs_CRP"]["value"] <= 0:
        error = "hs_CRP must be > 0 for ln()"
    else:
        # Levine 2018 formula — чистая функция (core.health.phenoage),
        # биомаркеры уже в канонических единицах. Импорт вне try, чтобы
        # ImportError не маскировался под "calculation error".
        from core.health.phenoage import phenoage_from_markers

        try:
            bio_age = round(
                phenoage_from_markers(
                    chrono_age,
                    {k: latest[k]["value"] for k in markers},
                ),
                1,
            )
        except (ValueError, OverflowError, KeyError) as e:
            error = f"calculation error: {e}"

    return {
        "status": "ok" if bio_age is not None else "incomplete",
        "bio_age": bio_age,
        "chronological_age": chrono_age,
        "delta_years": round(bio_age - chrono_age, 1) if bio_age and chrono_age else None,
        "interpretation": (
            "моложе паспорта"
            if bio_age and chrono_age and bio_age < chrono_age
            else "старше паспорта"
            if bio_age and chrono_age and bio_age > chrono_age
            else None
        ),
        "younger_markers_count": f"{younger_count}/9",
        "stale_markers": stale_markers,
        "error": error,
        "formula": "Levine 2018 (Aging Cell) — 9 biomarkers + chronological age",
        "markers": marker_list,
    }


@router.get("/recent_workouts")
async def recent_workouts(
    days: int = 30,
    user=Depends(get_agent_user),
    db: Session = Depends(get_db),
):
    """Workout summary by training-load canons (Seiler/Attia/Maffetone).

    Reads workouts_log_<user_id>.json from /app/telegram-bot/ (Garmin activity
    parser writes there). Returns Z2 min/week, HIIT min/week, A:C load ratio,
    polarized distribution, mistagged HIIT flag.

    Источник данных (приоритет):
    1. File `workouts_log_<user_id>.json` — rich data (Z2 zones, training load, MAF).
       Сейчас есть только у owner (Alex, push_workouts_to_container.py).
    2. Fallback: таблица `workouts` в БД — для остальных пользователей.
       Меньше полей (только type, duration, distance, calories), без zones/load,
       но достаточно для базовых вопросов «сколько раз бегал», «когда тренировался».
    """
    import json as _json
    from pathlib import Path as _Path
    from sqlalchemy import text as sql_text

    days = max(1, min(days, 180))
    today_date = _today_in_user_tz(user)
    cutoff = today_date - timedelta(days=days)

    wk_path = _Path(f"/app/telegram-bot/workouts_log_{user.telegram_id}.json")

    # ── Fallback: DB-based мульти-юзер (когда file отсутствует) ──────────────
    if not wk_path.exists():
        db_rows = db.execute(
            sql_text(
                """
                SELECT date, workout_type, duration_minutes, distance_km,
                       calories_burned, source, start_time
                FROM workouts
                WHERE user_id = :uid AND date >= :cutoff
                ORDER BY date DESC, start_time DESC NULLS LAST
                """
            ),
            {"uid": user.telegram_id, "cutoff": cutoff},
        ).fetchall()
        if not db_rows:
            return {"status": "no_data", "available": False, "reason": "no workouts in DB or file"}

        from collections import Counter as _Counter

        type_labels_ru = {
            "running": "бег",
            "walking": "ходьба",
            "strength_training": "силовая",
            "yoga": "йога",
            "cycling": "велосипед",
            "swimming": "плавание",
            "elliptical": "эллипс",
            "cardio": "кардио",
            "hiit": "HIIT",
            "fitness_equipment": "тренажёр",
            "other": "другое",
        }
        type_counts = _Counter(r.workout_type or "unknown" for r in db_rows)
        by_type = {type_labels_ru.get(t, t): {"count": c, "garmin_type": t} for t, c in type_counts.most_common()}

        # Extremes per type (по duration и distance)
        extremes_by_type: dict[str, Any] = {}
        for t in type_counts:
            of_type = [r for r in db_rows if (r.workout_type or "unknown") == t]
            with_dur = [r for r in of_type if r.duration_minutes]
            with_dist = [r for r in of_type if r.distance_km]
            longest_dur = max(with_dur, key=lambda r: r.duration_minutes) if with_dur else None
            longest_dist = max(with_dist, key=lambda r: r.distance_km) if with_dist else None
            extremes_by_type[type_labels_ru.get(t, t)] = {
                "count": len(of_type),
                "longest_by_duration": {
                    "date": longest_dur.date.isoformat(),
                    "duration_min": longest_dur.duration_minutes,
                    "distance_km": float(longest_dur.distance_km) if longest_dur.distance_km else None,
                }
                if longest_dur
                else None,
                "longest_by_distance": {
                    "date": longest_dist.date.isoformat(),
                    "distance_km": float(longest_dist.distance_km),
                    "duration_min": longest_dist.duration_minutes,
                }
                if longest_dist
                else None,
            }

        weeks = days / 7
        return {
            "status": "ok",
            "source": "db",
            "period_days": days,
            "count": len(db_rows),
            "by_type": by_type,
            "extremes_by_type": extremes_by_type,
            "stats": {
                "per_week": round(len(db_rows) / weeks, 1) if weeks else 0,
                "note": "DB-fallback: нет training_load/Z2/zones, только базовые поля",
            },
            "items": [
                {
                    "date": r.date.isoformat(),
                    "type": r.workout_type,
                    "type_ru": type_labels_ru.get(r.workout_type, r.workout_type),
                    "duration_min": r.duration_minutes,
                    "distance_km": float(r.distance_km) if r.distance_km else None,
                    "calories_burned": r.calories_burned,
                    "source": r.source,
                }
                for r in db_rows[:15]
            ],
        }

    try:
        wd = _json.loads(wk_path.read_text())
    except Exception as e:
        return {"status": "error", "error": f"parse failed: {e}"}

    workouts = wd.get("workouts", [])
    if not workouts:
        return {"status": "no_data", "available": False, "reason": "empty workouts array"}

    # today_date was already computed above using user's timezone
    cutoff = today_date - timedelta(days=days)

    def _to_date(s: str):
        try:
            y, m, d = s.split("-")
            return date(int(y), int(m), int(d))
        except Exception:
            return None

    in_window = []
    for w in workouts:
        wd_date = _to_date(w.get("date", ""))
        if wd_date and cutoff <= wd_date <= today_date:
            in_window.append(w)

    if not in_window:
        return {
            "status": "ok",
            "period_days": days,
            "count": 0,
            "items": [],
            "stats": {"per_week": 0, "z2_min_per_week": 0, "hiit_min_per_week": 0},
        }

    # Aggregate zones (prefer MAF — longevity school — over Garmin hr_zones)
    def _zone_min(w, zone_key):
        zones = w.get("maf_zones") or w.get("hr_zones") or {}
        # Ключи в workouts_log: z1_min..z5_min (а не z1..z5) — как пишет
        # build_workouts_log.py и читает dashboard_generator. Раньше читали
        # голый "z2" → всегда 0 → агент сообщал «0 мин Z2». См. F-001 (08.06.2026).
        return zones.get(zone_key, 0) or zones.get(f"{zone_key}_min", 0) or 0

    weeks = days / 7
    z1_total = sum(_zone_min(w, "z1") for w in in_window)
    z2_total = sum(_zone_min(w, "z2") for w in in_window)
    z3_total = sum(_zone_min(w, "z3") for w in in_window)
    z4_total = sum(_zone_min(w, "z4") for w in in_window)
    z5_total = sum(_zone_min(w, "z5") for w in in_window)
    total_zone_min = z1_total + z2_total + z3_total + z4_total + z5_total

    # «Z2 база» в смысле longevity-школы (Attia/Maffetone, HR-коридор 114-131 для
    # 49 лет) — это aerobic_base_min, посчитанный из посекундных HR-сэмплов
    # (scripts/util/compute_aerobic_base.py). НЕ путать с Garmin-зоной z2 (139+ bpm):
    # лёгкий бег на 128 bpm у Garmin = z1, но это и есть aerobic base. Дашборд берёт
    # именно aerobic_base (dashboard_generator._base_min_for) — агент теперь тоже,
    # иначе при цели Attia 150 мин/нед показывал ~0. См. F-001 (08.06.2026).
    def _aerobic_base_min(w):
        v = w.get("aerobic_base_min")
        if v is not None:
            return float(v)
        maf = w.get("maf_zones") or {}
        if maf.get("z2_min") is not None:
            return float(maf["z2_min"])
        return 0.0

    aerobic_base_total = sum(_aerobic_base_min(w) for w in in_window)

    # Acute vs Chronic load
    seven_ago = today_date - timedelta(days=7)
    acute = [w for w in in_window if _to_date(w["date"]) and _to_date(w["date"]) >= seven_ago]
    acute_load = sum(w.get("training_load") or 0 for w in acute)
    chronic_load_avg = sum(w.get("training_load") or 0 for w in in_window) / weeks if weeks > 0 else 0
    ac_ratio = round(acute_load / chronic_load_avg, 2) if chronic_load_avg > 0 else None

    # Type aggregation — count workouts by Garmin type.
    # IMPORTANT: type is the Garmin classification ('running', 'strength_training',
    # 'walking', 'yoga', ...). activity_name is the user-set route/session label
    # ('Москва - База', 'Гимнастика #3') and is NOT a reliable indicator of
    # exercise type — a session named 'Москва - База' may be running OR walking.
    # ALWAYS read `type` field for classification, not `activity_name`.
    from collections import Counter as _Counter

    type_counts = _Counter(w.get("type") or "unknown" for w in in_window)
    # Russian-friendly labels for the common types so the agent uses them
    type_labels_ru = {
        "running": "бег",
        "walking": "ходьба",
        "strength_training": "силовая",
        "yoga": "йога",
        "cycling": "велосипед",
        "swimming": "плавание",
        "elliptical": "эллипс",
        "cardio": "кардио",
        "hiit": "HIIT",
        "fitness_equipment": "тренажёр",
        "other": "другое",
    }
    by_type = {
        type_labels_ru.get(t, t): {
            "count": c,
            "garmin_type": t,
        }
        for t, c in type_counts.most_common()
    }

    # Extremes per type — рекорды по длительности и дистанции в окне.
    # Нужно потому что items[:15] обрезает выборку до самых свежих, и редкие
    # длинные сессии (марафонские пробежки раз в квартал) туда не попадают.
    # Без этого блока вопрос "самая длинная пробежка года" агенту неотвечаем.
    def _max_by(items, key):
        items = [w for w in items if w.get(key) is not None]
        return max(items, key=lambda w: w[key]) if items else None

    def _extreme_record(w):
        return {
            "date": w.get("date"),
            "name": w.get("activity_name"),
            "duration_min": w.get("duration_min"),
            "distance_km": w.get("distance_km"),
            "avg_hr": w.get("avg_hr"),
        }

    extremes_by_type = {}
    for t in type_counts:
        of_type = [w for w in in_window if (w.get("type") or "unknown") == t]
        longest_dur = _max_by(of_type, "duration_min")
        longest_dist = _max_by(of_type, "distance_km")
        extremes_by_type[type_labels_ru.get(t, t)] = {
            "count": len(of_type),
            "longest_by_duration": _extreme_record(longest_dur) if longest_dur else None,
            "longest_by_distance": _extreme_record(longest_dist) if longest_dist else None,
        }

    return {
        "status": "ok",
        "period_days": days,
        "count": len(in_window),
        "by_type": by_type,
        "extremes_by_type": extremes_by_type,
        "stats": {
            "per_week": round(len(in_window) / weeks, 1),
            # z2_min_per_week = aerobic base (longevity-Z2, HR 114-131), как KPI дашборда
            "z2_min_per_week": round(aerobic_base_total / weeks),
            "z2_metric_note": "z2_min_per_week — это aerobic base (HR 114-131, метрика Attia/Maffetone), НЕ Garmin-зона Z2 (139+)",
            "hiit_min_per_week": round((z4_total + z5_total) / weeks),
            "z2_target_attia": 150,  # mins/week
            "hiit_target_norwegian": 16,  # mins/week (4x4)
            "ac_ratio": ac_ratio,
            "ac_sweet_spot": "0.8-1.3",
        },
        "zones_total_min": {
            "z1": round(z1_total),
            "z2": round(z2_total),
            "z3": round(z3_total),
            "z4": round(z4_total),
            "z5": round(z5_total),
        },
        "polarized_pct": {
            "low (z1+z2)": round(100 * (z1_total + z2_total) / total_zone_min, 1) if total_zone_min else 0,
            "mid (z3)": round(100 * z3_total / total_zone_min, 1) if total_zone_min else 0,
            "high (z4+z5)": round(100 * (z4_total + z5_total) / total_zone_min, 1) if total_zone_min else 0,
            "ideal_seiler": "80/5/15",
        },
        "items": [
            {
                "date": w.get("date"),
                "type": w.get("type"),  # GARMIN classification — primary
                "type_ru": type_labels_ru.get(w.get("type"), w.get("type")),
                "name": w.get("activity_name"),  # user-set route name (e.g. "Москва - База")
                "duration_min": w.get("duration_min"),
                "distance_km": w.get("distance_km"),
                "avg_hr": w.get("avg_hr"),
                "training_load": w.get("training_load"),
                # Потренировочная Z2-база (longevity, HR 114-131) и полная MAF-разбивка
                # зон ИМЕННО этой тренировки. Без этого агент на вопрос «сколько Z2
                # в пробежке 7-го» не имел данных и выдумывал числа. См. F (09.06).
                "aerobic_base_min": w.get("aerobic_base_min"),
                "maf_zones": w.get("maf_zones"),
            }
            for w in sorted(in_window, key=lambda w: w.get("date", ""), reverse=True)[:15]
        ],
    }


@router.get("/weight_history")
async def weight_history(
    days: Optional[int] = None,
    series: bool = False,
    user=Depends(get_agent_user),
    db: Session = Depends(get_db),
):
    """История веса и состава тела (жир/мышцы/висцеральный жир).

    Источник: `weights` table — пишется HAE (Apple Health → Mi-весы), Zepp Life,
    Apple Health XML импортом. История с 2015 у долгих пользователей.

    Параметры:
    - `days`: окно в днях (7-365). Без него — только all-time агрегат.
    - `series`: True — добавить в ответ поле `points` со ВСЕМИ замерами в окне
      (по одному на дату, среднее если в день несколько источников). Нужно
      когда собираешься рисовать график через render_chart. Дефолт False —
      для текстовых вопросов хватает агрегатов, чтобы не раздувать ответ.

    Поля extremes: запись со значением + дата. body_fat фильтруется > 5
    (нулевые значения = весы не смогли измерить, мусор).
    """
    from sqlalchemy import text as sql_text

    in_window = max(7, min(days, 365)) if days else None

    def _to_date_str(ts) -> Optional[str]:
        """Накласть .date().isoformat() на datetime, или вернуть str иначе.

        SQLAlchemy в SQLite (тесты) возвращает datetime, в Postgres — тоже.
        Старое SQL `measured_at::date AS date` ломалось на SQLite, поэтому теперь
        конвертация в Python.
        """
        if ts is None:
            return None
        if hasattr(ts, "date"):
            return ts.date().isoformat()
        return str(ts)[:10]

    # Latest weighing — current state, most useful single fact
    latest_row = db.execute(
        sql_text(
            """
            SELECT measured_at, weight, body_fat, muscle_mass,
                   visceral_fat, bmi, source
            FROM weights
            WHERE user_id = :uid
            ORDER BY measured_at DESC
            LIMIT 1
            """
        ),
        {"uid": user.telegram_id},
    ).fetchone()

    if not latest_row:
        return {"status": "no_data", "count": 0}

    latest = {
        "date": _to_date_str(latest_row.measured_at),
        "weight_kg": round(latest_row.weight, 1),
        "body_fat_pct": round(latest_row.body_fat, 1) if latest_row.body_fat else None,
        "muscle_mass_kg": round(latest_row.muscle_mass, 1) if latest_row.muscle_mass else None,
        "visceral_fat": latest_row.visceral_fat,
        "bmi": round(latest_row.bmi, 1) if latest_row.bmi else None,
        "source": latest_row.source,
    }

    def _extremes(where_clause: str, params: dict) -> dict:
        # Min/max weight (ignores body_fat NULL)
        w_min = db.execute(
            sql_text(
                f"SELECT measured_at, weight FROM weights "
                f"WHERE user_id = :uid {where_clause} ORDER BY weight ASC LIMIT 1"
            ),
            params,
        ).fetchone()
        w_max = db.execute(
            sql_text(
                f"SELECT measured_at, weight FROM weights "
                f"WHERE user_id = :uid {where_clause} ORDER BY weight DESC LIMIT 1"
            ),
            params,
        ).fetchone()
        # Min/max body_fat (filter > 5 — нулевые значения = весы не измерили)
        bf_min = db.execute(
            sql_text(
                f"SELECT measured_at, body_fat, weight FROM weights "
                f"WHERE user_id = :uid AND body_fat > 5 {where_clause} "
                f"ORDER BY body_fat ASC LIMIT 1"
            ),
            params,
        ).fetchone()
        bf_max = db.execute(
            sql_text(
                f"SELECT measured_at, body_fat, weight FROM weights "
                f"WHERE user_id = :uid AND body_fat > 5 {where_clause} "
                f"ORDER BY body_fat DESC LIMIT 1"
            ),
            params,
        ).fetchone()
        # Counts + date range
        meta = db.execute(
            sql_text(
                f"SELECT COUNT(*) AS n, MIN(measured_at) AS first, "
                f"MAX(measured_at) AS last FROM weights "
                f"WHERE user_id = :uid {where_clause}"
            ),
            params,
        ).fetchone()

        return {
            "count": meta.n,
            "first_date": _to_date_str(meta.first),
            "last_date": _to_date_str(meta.last),
            "min_weight": {"date": _to_date_str(w_min.measured_at), "weight_kg": round(w_min.weight, 1)}
            if w_min
            else None,
            "max_weight": {"date": _to_date_str(w_max.measured_at), "weight_kg": round(w_max.weight, 1)}
            if w_max
            else None,
            "min_body_fat": {
                "date": _to_date_str(bf_min.measured_at),
                "body_fat_pct": round(bf_min.body_fat, 1),
                "weight_kg": round(bf_min.weight, 1),
            }
            if bf_min
            else None,
            "max_body_fat": {
                "date": _to_date_str(bf_max.measured_at),
                "body_fat_pct": round(bf_max.body_fat, 1),
                "weight_kg": round(bf_max.weight, 1),
            }
            if bf_max
            else None,
        }

    result: dict[str, Any] = {
        "status": "ok",
        "latest": latest,
        "all_time": _extremes("", {"uid": user.telegram_id}),
    }

    if in_window:
        # Python-computed cutoff — works одинаково на Postgres и SQLite (тесты)
        cutoff = datetime.now(timezone.utc) - timedelta(days=in_window)
        result["window_days"] = in_window
        result["in_window"] = _extremes(
            "AND measured_at >= :cutoff",
            {"uid": user.telegram_id, "cutoff": cutoff},
        )

    # Полный ряд точек — для рисования графика. Дедуп по дате: среднее когда
    # за день несколько источников (apple_health_v2 + zepp_life дают одно и
    # то же значение, или почти).
    if series:
        if in_window:
            cutoff = datetime.now(timezone.utc) - timedelta(days=in_window)
            rows = db.execute(
                sql_text(
                    """
                    SELECT measured_at, weight, body_fat
                    FROM weights
                    WHERE user_id = :uid AND measured_at >= :cutoff
                    ORDER BY measured_at ASC
                    """
                ),
                {"uid": user.telegram_id, "cutoff": cutoff},
            ).fetchall()
        else:
            rows = db.execute(
                sql_text(
                    """
                    SELECT measured_at, weight, body_fat
                    FROM weights
                    WHERE user_id = :uid
                    ORDER BY measured_at ASC
                    """
                ),
                {"uid": user.telegram_id},
            ).fetchall()

        # Группируем по дате, усредняем (несколько источников → одна точка)
        from collections import defaultdict

        per_day: dict[str, dict] = defaultdict(lambda: {"weights": [], "body_fats": []})
        for r in rows:
            day = _to_date_str(r.measured_at)
            per_day[day]["weights"].append(r.weight)
            if r.body_fat and r.body_fat > 5:  # см. фильтр для extremes
                per_day[day]["body_fats"].append(r.body_fat)

        points = []
        for day in sorted(per_day.keys()):
            ws = per_day[day]["weights"]
            bfs = per_day[day]["body_fats"]
            points.append(
                {
                    "date": day,
                    "weight_kg": round(sum(ws) / len(ws), 2),
                    "body_fat_pct": round(sum(bfs) / len(bfs), 1) if bfs else None,
                }
            )
        result["points"] = points
        result["points_count"] = len(points)

    return result


@router.get("/body_measurements")
async def body_measurements(
    user=Depends(get_agent_user),
    db: Session = Depends(get_db),
):
    """Антропометрия: талия, шея, бёдра, грудь, бедро, бицепс (см), сила хвата (кг).

    Источник: `body_measurements` table — ручной ввод пользователя через бот/админку.
    Талия — важная метрика метаболического здоровья (waist circumference > BMI
    по предсказанию ССЗ-риска, особенно для visceral fat). Сила хвата (grip_right_kg/
    grip_left_kg, ручной динамометр) — маркер саркопении/функциональной силы.

    Возвращает latest замер, all-time min/max каждой метрики с датами, и
    тренд waist (last 6 measurements) — самая клинически релевантная.
    """
    from sqlalchemy import text as sql_text

    rows = db.execute(
        sql_text(
            """
            SELECT date, waist_cm, neck_cm, hips_cm, chest_cm, thigh_cm, biceps_cm,
                   grip_right_kg, grip_left_kg, notes
            FROM body_measurements
            WHERE user_id = :uid
            ORDER BY date DESC
            """
        ),
        {"uid": user.telegram_id},
    ).fetchall()

    if not rows:
        return {"status": "no_data", "count": 0, "reason": "no body_measurements entries"}

    latest = rows[0]
    metrics = ["waist_cm", "neck_cm", "hips_cm", "chest_cm", "thigh_cm", "biceps_cm", "grip_right_kg", "grip_left_kg"]

    def _extremes(metric: str) -> dict | None:
        vals = [(r.date, getattr(r, metric)) for r in rows if getattr(r, metric) is not None]
        if not vals:
            return None
        min_v = min(vals, key=lambda x: x[1])
        max_v = max(vals, key=lambda x: x[1])
        return {
            "min": {"date": min_v[0].isoformat(), "value": round(min_v[1], 1)},
            "max": {"date": max_v[0].isoformat(), "value": round(max_v[1], 1)},
            "current": round(getattr(latest, metric), 1) if getattr(latest, metric) is not None else None,
            "count": len(vals),
        }

    # Waist trend — last 6 measurements (for ratio/direction)
    waist_trend = [
        {"date": r.date.isoformat(), "waist_cm": round(r.waist_cm, 1)} for r in rows[:6] if r.waist_cm is not None
    ]

    return {
        "status": "ok",
        "count": len(rows),
        "latest": {
            "date": latest.date.isoformat(),
            **{m: round(getattr(latest, m), 1) if getattr(latest, m) is not None else None for m in metrics},
            "notes": latest.notes,
        },
        "extremes": {m: _extremes(m) for m in metrics},
        "waist_trend_last_6": waist_trend,
    }


@router.get("/day_summary")
async def day_summary(
    date: str,
    user=Depends(get_agent_user),
    db: Session = Depends(get_db),
):
    """Сводка за конкретный день: ккал, БЖУ, шаги, сон, вес, АД, был ли воркаут.

    Live-агрегация из nutrition_log + activity_log + weights + blood_pressure_logs
    (таблица daily_summaries никогда не заполнялась — аудит 11.06.2026).

    Используй для вопросов «что у меня было 14 марта», «как был день N»,
    «сравни такой-то день с другим».
    """
    from sqlalchemy import text as sql_text
    from datetime import date as date_cls

    try:
        target_date = date_cls.fromisoformat(date)
    except ValueError:
        return {"status": "error", "error": f"invalid date format: {date!r} (expected YYYY-MM-DD)"}

    uid = user.telegram_id

    # Питание: суммируем totals по всем приёмам за день
    nutrition = None
    meals = db.query(NutritionLog).filter(NutritionLog.user_id == uid, NutritionLog.date == target_date).all()
    if meals:

        def _tot(key: str) -> float:
            return round(sum(float((m.totals or {}).get(key) or 0) for m in meals), 1)

        nutrition = {
            "calories": _tot("calories"),
            "protein_g": _tot("protein"),
            "fats_g": _tot("fats"),
            "carbs_g": _tot("carbs"),
            "fiber_g": _tot("fiber"),
            "meals_count": len(meals),
        }

    # Активность: одна строка на день (HAE/Garmin upsert)
    act = db.query(ActivityLog).filter(ActivityLog.user_id == uid, ActivityLog.date == target_date).first()
    activity = None
    sleep_hours = None
    if act:
        activity = {
            "steps": act.steps,
            "active_calories": act.active_calories,
            "distance_km": act.distance_km,
            "heart_rate_avg": act.heart_rate_avg,
            "hrv": act.hrv,
        }
        sleep_hours = float(act.sleep_hours) if act.sleep_hours is not None else None

    # Вес: последний замер за день
    w = (
        db.query(Weight)
        .filter(Weight.user_id == uid, func.date(Weight.measured_at) == target_date)
        .order_by(Weight.measured_at.desc())
        .first()
    )

    # АД и воркауты — таблицы вне ORM (нет на SQLite в тестах) → guarded raw SQL
    blood_pressure = None
    had_workout = None
    try:
        bp = db.execute(
            sql_text(
                """SELECT systolic, diastolic, heart_rate FROM blood_pressure_logs
                   WHERE user_id = :uid AND measured_at::date = :d
                   ORDER BY measured_at DESC LIMIT 1"""
            ),
            {"uid": uid, "d": target_date},
        ).fetchone()
        if bp:
            blood_pressure = {"systolic": bp.systolic, "diastolic": bp.diastolic, "pulse": bp.heart_rate}
        wk = db.execute(
            sql_text("SELECT COUNT(*) FROM workouts WHERE user_id = :uid AND start_time::date = :d"),
            {"uid": uid, "d": target_date},
        ).scalar()
        had_workout = bool(wk)
    except Exception as e:
        logger.warning(f"day_summary raw-SQL part failed (ok on SQLite tests): {e}")
        db.rollback()

    if not meals and not act and not w and not blood_pressure:
        return {"status": "no_data", "date": target_date.isoformat(), "reason": "no records for this date"}

    return {
        "status": "ok",
        "date": target_date.isoformat(),
        "nutrition": nutrition,
        "activity": activity,
        "had_workout": had_workout,
        "sleep_hours": sleep_hours,
        "weight_kg": float(w.weight) if w else None,
        "blood_pressure": blood_pressure,
    }


class UpdateUserSettingsRequest(BaseModel):
    """Все поля опциональны — обновляем только те что переданы."""

    target_weight_kg: Optional[float] = Field(None, ge=30, le=300)
    target_weight_date: Optional[str] = None  # YYYY-MM-DD
    calorie_goal_pct: Optional[int] = Field(None, ge=-50, le=50, description="Дефицит/профицит %, -15 = -15%")
    bmr_override: Optional[int] = Field(None, ge=500, le=5000)
    bmr_source: Optional[str] = Field(None, pattern="^(auto|override|fixed)$")
    activity_level: Optional[str] = None
    show_calorie_budget_bar: Optional[bool] = None
    supplement_reminders_enabled: Optional[bool] = None
    supplement_reminder_time: Optional[str] = None  # HH:MM
    supplements: Optional[list] = Field(
        None,
        description="Полный список добавок (заменит существующий). Каждая: {name, dose?, slot?}. Slots: morning_before, morning_with, evening.",
    )


class UpdateProfileRequest(BaseModel):
    """Анкетные поля из users — все опциональны."""

    sex: Optional[str] = Field(None, pattern="^(male|female|other)$")
    height_cm: Optional[int] = Field(None, ge=100, le=250)
    birth_date: Optional[str] = None  # YYYY-MM-DD
    timezone: Optional[str] = None
    smoking_status: Optional[str] = Field(None, pattern="^(never|former|current|occasional)$")
    kb_status: Optional[str] = Field(None, pattern="^(shared|private)$")
    pack_name: Optional[str] = None
    agent_system_prompt: Optional[str] = Field(
        None,
        description="Полный пере-write системного промпта агента. Длинный (5-10к символов). Используй осторожно — лучше предложить пользователю diff перед применением.",
    )


@router.get("/profile_questionnaire")
async def profile_questionnaire(
    user=Depends(get_agent_user),
    db: Session = Depends(get_db),
):
    """Анкета пользователя — поля из users table, заполняемые в onboarding wizard.

    Возвращает: sex, height_cm, birth_date (+ возраст), timezone, smoking_status
    (never/former/current/occasional), kb_status (приватность knowledge_base —
    shared = доступно AI / private = только пользователю), garmin_connected,
    pack_name, agent_system_prompt (превью первых 500 симв).

    Используй когда юзер спрашивает «что я указывал в анкете», «какие у меня
    настройки приватности», «подключён ли Garmin». Также используй ПЕРЕД
    вызовом `update_profile_questionnaire` чтобы показать что изменится.
    """
    from datetime import date as date_cls

    age = None
    if user.birth_date:
        today = date_cls.today()
        age = (
            today.year
            - user.birth_date.year
            - ((today.month, today.day) < (user.birth_date.month, user.birth_date.day))
        )

    prompt = user.agent_system_prompt or ""
    return {
        "status": "ok",
        "profile": {
            "first_name": user.first_name,
            "sex": user.sex,
            "height_cm": user.height_cm,
            "birth_date": user.birth_date.isoformat() if user.birth_date else None,
            "age": age,
            "timezone": user.timezone,
            "cohort": user.cohort,
            "smoking_status": getattr(user, "smoking_status", None),
            "kb_status": getattr(user, "kb_status", None),
            "pack_name": getattr(user, "pack_name", None),
            "garmin_connected": bool(user.garmin_email),
            "garmin_email": user.garmin_email,
        },
        "agent_system_prompt": {
            "length": len(prompt),
            "preview_500": prompt[:500] if prompt else None,
            "note": "Полный текст не возвращается из соображений размера. Используй update_profile_questionnaire чтобы заменить.",
        },
    }


@router.post("/update_profile_questionnaire")
async def update_profile_questionnaire(
    req: UpdateProfileRequest,
    user=Depends(require_agent_scope("rw")),
    db: Session = Depends(get_db),
):
    """Обновить анкетные поля. Только те что переданы — остальные не трогаются.

    SECURITY: agent_system_prompt — длинная строка, потенциально опасное поле
    (определяет поведение агента). Перед массовым переписыванием показать diff
    пользователю.
    """
    from sqlalchemy import text as sql_text
    from datetime import date as date_cls

    updates: dict[str, Any] = {}
    if req.sex is not None:
        updates["sex"] = req.sex
    if req.height_cm is not None:
        updates["height_cm"] = req.height_cm
    if req.birth_date is not None:
        try:
            date_cls.fromisoformat(req.birth_date)
            updates["birth_date"] = req.birth_date
        except ValueError:
            return {"status": "error", "error": f"invalid birth_date: {req.birth_date!r}"}
    if req.timezone is not None:
        updates["timezone"] = req.timezone
    if req.smoking_status is not None:
        updates["smoking_status"] = req.smoking_status
    if req.kb_status is not None:
        updates["kb_status"] = req.kb_status
    if req.pack_name is not None:
        updates["pack_name"] = req.pack_name
    if req.agent_system_prompt is not None:
        updates["agent_system_prompt"] = req.agent_system_prompt

    if not updates:
        return {"status": "noop", "reason": "no fields provided"}

    # Build dynamic UPDATE
    set_clause = ", ".join([f"{k} = :{k}" for k in updates])
    sql = f"UPDATE users SET {set_clause} WHERE telegram_id = :uid"
    params = {**updates, "uid": user.telegram_id}
    db.execute(sql_text(sql), params)
    db.commit()

    return {
        "status": "ok",
        "updated_fields": list(updates.keys()),
        "telegram_id": user.telegram_id,
    }


@router.post("/update_user_settings")
async def update_user_settings_endpoint(
    req: UpdateUserSettingsRequest,
    user=Depends(require_agent_scope("rw")),
    db: Session = Depends(get_db),
):
    """Обновить настройки в user_settings table. Только переданные поля.

    Особенность: `supplements` — JSONB список, передача ПОЛНОСТЬЮ заменяет
    существующий режим. Чтобы добавить одну добавку — сначала get_user_settings,
    модифицировать список, затем update.
    """
    from sqlalchemy import text as sql_text
    from datetime import date as date_cls, time as time_cls
    import json as _json

    # Validate inputs
    if req.target_weight_date is not None:
        try:
            date_cls.fromisoformat(req.target_weight_date)
        except ValueError:
            return {"status": "error", "error": f"invalid target_weight_date: {req.target_weight_date!r}"}
    if req.supplement_reminder_time is not None:
        try:
            h, m = req.supplement_reminder_time.split(":")
            time_cls(int(h), int(m))
        except Exception:
            return {"status": "error", "error": f"invalid supplement_reminder_time: {req.supplement_reminder_time!r}"}

    updates: dict[str, Any] = {}
    for field in (
        "target_weight_kg",
        "target_weight_date",
        "calorie_goal_pct",
        "bmr_override",
        "bmr_source",
        "activity_level",
        "show_calorie_budget_bar",
        "supplement_reminders_enabled",
        "supplement_reminder_time",
    ):
        v = getattr(req, field)
        if v is not None:
            updates[field] = v

    # supplements нужен спецобработка как JSONB
    if req.supplements is not None:
        updates["supplements"] = _json.dumps(req.supplements, ensure_ascii=False)

    if not updates:
        return {"status": "noop", "reason": "no fields provided"}

    # UPSERT: если settings row ещё нет — создать
    cols_to_check = ", ".join([f"'{k}'" for k in updates])  # noqa: F841 (debug only)

    # Check if row exists
    exists = db.execute(
        sql_text("SELECT 1 FROM user_settings WHERE user_id = :uid"),
        {"uid": user.telegram_id},
    ).fetchone()

    params: dict[str, Any] = {**updates, "uid": user.telegram_id}
    if exists:
        set_clause = ", ".join([f"{k} = :{k}" for k in updates])
        # supplements — explicit jsonb cast
        if "supplements" in updates:
            set_clause = set_clause.replace("supplements = :supplements", "supplements = (:supplements)::jsonb")
        set_clause += ", updated_at = NOW()"
        db.execute(sql_text(f"UPDATE user_settings SET {set_clause} WHERE user_id = :uid"), params)
    else:
        cols = list(updates.keys()) + ["user_id"]
        vals = [f"(:{c})::jsonb" if c == "supplements" else f":{c}" for c in updates] + [":uid"]
        db.execute(
            sql_text(f"INSERT INTO user_settings ({', '.join(cols)}) VALUES ({', '.join(vals)})"),
            params,
        )
    db.commit()

    return {
        "status": "ok",
        "updated_fields": list(updates.keys()),
        "telegram_id": user.telegram_id,
        "row_created": not bool(exists),
    }


@router.get("/user_settings")
async def user_settings(
    user=Depends(get_agent_user),
    db: Session = Depends(get_db),
):
    """Настройки пользователя: целевой вес, ежедневные добавки, BMR, цель калорий.

    Источник: `user_settings` table (per-user JSONB с регулярным режимом
    добавок) + поля из `users` (sex, height, birth_date, timezone, smoking_status).
    Используй для 'какие у меня цели', 'какие добавки я регулярно принимаю',
    'какой у меня дефицит калорий', 'когда у меня запланированы напоминания'.
    """
    from sqlalchemy import text as sql_text

    row = db.execute(
        sql_text(
            """
            SELECT show_calorie_budget_bar, bmr_override, target_weight_kg,
                   target_weight_date, supplement_reminders_enabled,
                   supplement_reminder_time, supplements, calorie_goal_pct,
                   bmr_source, activity_level, activity_avg_override
            FROM user_settings WHERE user_id = :uid
            """
        ),
        {"uid": user.telegram_id},
    ).fetchone()

    profile = {
        "first_name": user.first_name,
        "sex": user.sex,
        "height_cm": user.height_cm,
        "birth_date": user.birth_date.isoformat() if user.birth_date else None,
        "timezone": user.timezone,
        "cohort": user.cohort,
        "smoking_status": getattr(user, "smoking_status", None),
        "garmin_connected": bool(user.garmin_email),
    }

    if not row:
        return {"status": "no_settings", "profile": profile, "reason": "user_settings row not created yet"}

    return {
        "status": "ok",
        "profile": profile,
        "goals": {
            "target_weight_kg": row.target_weight_kg,
            "target_weight_date": row.target_weight_date.isoformat() if row.target_weight_date else None,
            "calorie_goal_pct": row.calorie_goal_pct,
        },
        "bmr": {
            "source": row.bmr_source,
            "override": row.bmr_override,
            "activity_level": row.activity_level,
            "activity_avg_override": row.activity_avg_override,
        },
        "supplements_regimen": row.supplements or [],
        "reminders": {
            "supplement_enabled": row.supplement_reminders_enabled,
            "supplement_time": row.supplement_reminder_time.isoformat() if row.supplement_reminder_time else None,
        },
    }


@router.get("/indoor_air")
async def indoor_air(
    days: int = 7,
    user=Depends(get_agent_user),
    db: Session = Depends(get_db),
):
    """Воздух в доме: CO2, температура, влажность, шум (Netatmo Healthy Home Coach).

    Источник: файлы `data/environment/netatmo_log.json` (текущий замер) +
    `netatmo_history.json` (история, дневные агрегаты). Owner-only — Netatmo
    есть только у Alex, у других пользователей не настроен.

    Используй для 'какой CO2 в спальне', 'духота сегодня', 'температура дома',
    'был ли проветрен'. CO2 >1000 ppm — плохо для сна и концентрации, >1400 — критично.
    """
    import json as _json
    import time as _time
    from pathlib import Path as _Path

    # Owner-only — у других пользователей датчиков нет
    if user.cohort != "owner":
        return {"status": "no_data", "reason": "Netatmo датчик только у owner"}

    log_path = _Path("/app/data/environment/netatmo_log.json")
    hist_path = _Path("/app/data/environment/netatmo_history.json")

    result: dict[str, Any] = {"status": "ok"}

    # Текущий замер
    if log_path.exists():
        try:
            log = _json.loads(log_path.read_text())
            if log and isinstance(log, list):
                latest = log[0]
                result["latest"] = {
                    "device_name": latest.get("device_name"),
                    "temperature_c": latest.get("temperature_c"),
                    "co2_ppm": latest.get("co2_ppm"),
                    "humidity_percent": latest.get("humidity_percent"),
                    "noise_db": latest.get("noise_db"),
                    "measured_at": _dt_isoformat_local(
                        datetime.fromtimestamp(latest["timestamp"], tz=timezone.utc), user
                    )
                    if latest.get("timestamp")
                    else None,
                }
        except Exception as e:
            logger.warning(f"indoor_air: failed to read log: {e}")

    # История за N дней (агрегаты)
    days = max(1, min(days, 60))
    if hist_path.exists():
        try:
            history = _json.loads(hist_path.read_text())
            cutoff_ts = int(_time.time()) - days * 24 * 3600
            rooms: dict[str, Any] = {}
            for room_name, room_data in history.items():
                if not isinstance(room_data, dict):
                    continue
                # room_data: {unix_ts: [temp, co2, humidity, noise]}
                points = []
                for ts_str, values in room_data.items():
                    try:
                        ts = int(ts_str)
                        if ts < cutoff_ts:
                            continue
                        if not isinstance(values, list) or len(values) < 4:
                            continue
                        points.append((ts, values))
                    except (ValueError, TypeError):
                        continue
                if not points:
                    continue
                temps = [p[1][0] for p in points if p[1][0] is not None]
                co2s = [p[1][1] for p in points if p[1][1] is not None]
                hums = [p[1][2] for p in points if p[1][2] is not None]
                noises = [p[1][3] for p in points if p[1][3] is not None]
                rooms[room_name] = {
                    "days_with_data": len(points),
                    "co2_avg_ppm": round(sum(co2s) / len(co2s)) if co2s else None,
                    "co2_max_ppm": round(max(co2s)) if co2s else None,
                    "temp_avg_c": round(sum(temps) / len(temps), 1) if temps else None,
                    "humidity_avg_pct": round(sum(hums) / len(hums), 1) if hums else None,
                    "noise_avg_db": round(sum(noises) / len(noises), 1) if noises else None,
                    "noise_max_db": round(max(noises)) if noises else None,
                }
            if rooms:
                result["history"] = {"period_days": days, "by_room": rooms}
        except Exception as e:
            logger.warning(f"indoor_air: failed to read history: {e}")

    if "latest" not in result and "history" not in result:
        return {"status": "no_data", "reason": "no Netatmo files on server"}

    return result


@router.get("/outdoor_weather")
async def outdoor_weather(
    date: Optional[str] = None,
    user=Depends(get_agent_user),
    db: Session = Depends(get_db),
):
    """Погода снаружи: температура, давление, влажность, UV, осадки (Open-Meteo, Москва).

    Источник: файл `data/weather/weather_history.json` (Open-Meteo daily aggregates).

    Без параметра date — последний доступный день. С date='YYYY-MM-DD' — конкретный день.
    Используй для 'какая погода', 'какое давление сегодня', 'был ли дождь вчера'.
    """
    import json as _json
    from pathlib import Path as _Path

    weather_path = _Path("/app/data/weather/weather_history.json")
    if not weather_path.exists():
        return {"status": "no_data", "reason": "weather_history.json не найден"}

    try:
        data = _json.loads(weather_path.read_text())
    except Exception as e:
        return {"status": "error", "error": f"parse failed: {e}"}

    entries = data.get("entries", [])
    if not entries:
        return {"status": "no_data", "reason": "empty entries"}

    if date:
        # Конкретный день
        matched = [e for e in entries if e.get("date") == date]
        if not matched:
            return {"status": "no_data", "date": date, "reason": f"нет записи на {date}"}
        e = matched[0]
    else:
        # Последний день
        e = max(entries, key=lambda x: x.get("date", ""))

    return {
        "status": "ok",
        "date": e.get("date"),
        "city": e.get("city"),
        "temp_max_c": e.get("temp_max"),
        "temp_min_c": e.get("temp_min"),
        "temp_mean_c": e.get("temp_mean"),
        "pressure_mmhg": e.get("pressure_mmhg"),
        "humidity_pct": e.get("humidity_pct"),
        "uv_index_max": e.get("uv_index_max"),
        "precipitation_mm": e.get("precipitation_mm"),
        "sunshine_hours": e.get("sunshine_hours"),
        "weather": e.get("weather"),
    }


@router.get("/recent_trends")
async def recent_trends(
    days: int = 14,
    full_series: bool = False,
    user=Depends(get_agent_user),
    db: Session = Depends(get_db),
):
    """Per-day trends from activity_log.raw_data: HRV, Body Battery, Stress, Steps, Alcohol.

    Complementary to get_dashboard_summary (which gives 7-day AVG only).
    Use this for trend questions: 'падает ли мой HRV?', 'сколько у меня
    Body Battery утром', 'когда самый высокий стресс'.

    `alcohol` (bool) per day — был ли в этот день приём пищи с алкоголем
    (флаг из nutrition_log.totals.has_alcohol). Полезно для корреляций
    'алкоголь → HRV/стресс следующего дня'.

    Окно до 180 дней. По умолчанию возвращается до 30 последних точек в
    `items`. Для корреляций/графиков на длинном окне передай
    `full_series=true` — тогда вернутся ВСЕ точки окна (тяжелее, но нужно
    чтобы посчитать связь на 90-180 днях).
    """
    from sqlalchemy import text as sql_text

    days = max(1, min(days, 180))
    sql = sql_text(
        """
        SELECT al.date,
               al.steps,
               al.heart_rate_avg AS rhr,
               al.hrv,
               al.stress_level,
               al.sleep_hours,
               (al.raw_data->>'bodyBatteryHighestValue')::int  AS body_battery_max,
               (al.raw_data->>'bodyBatteryAtWakeTime')::int    AS body_battery_wake,
               (al.raw_data->>'bodyBatteryLowestValue')::int   AS body_battery_min,
               (al.raw_data->>'averageStressLevel')::int       AS stress_avg,
               COALESCE(nu.alcohol, false)                     AS alcohol
        FROM activity_log al
        LEFT JOIN (
            SELECT date, bool_or((totals->>'has_alcohol') = 'true') AS alcohol
            FROM nutrition_log
            WHERE user_id = :uid
            GROUP BY date
        ) nu ON nu.date = al.date
        WHERE al.user_id = :uid
          AND al.date >= CURRENT_DATE - (:days || ' days')::interval
        ORDER BY al.date DESC
        """
    )
    rows = db.execute(sql, {"uid": user.telegram_id, "days": days}).fetchall()

    items = [
        {
            "date": r.date.isoformat(),
            "steps": r.steps,
            "rhr": r.rhr,
            "hrv": r.hrv,
            "stress_level": r.stress_level or r.stress_avg,
            "sleep_h": float(r.sleep_hours) if r.sleep_hours else None,
            "body_battery_morning": r.body_battery_wake,
            "body_battery_max": r.body_battery_max,
            "body_battery_min": r.body_battery_min,
            "alcohol": bool(r.alcohol),
        }
        for r in rows
    ]

    def _avg_or_none(vals: list):
        clean = [v for v in vals if v is not None]
        return round(sum(clean) / len(clean), 1) if clean else None

    return {
        "status": "ok",
        "period_days": days,
        "count": len(items),
        "stats": {
            "hrv_avg": _avg_or_none([i["hrv"] for i in items]),
            "hrv_min": min((i["hrv"] for i in items if i["hrv"]), default=None),
            "hrv_max": max((i["hrv"] for i in items if i["hrv"]), default=None),
            "rhr_avg": _avg_or_none([i["rhr"] for i in items]),
            "stress_avg": _avg_or_none([i["stress_level"] for i in items]),
            "body_battery_morning_avg": _avg_or_none([i["body_battery_morning"] for i in items]),
            "steps_avg": _avg_or_none([i["steps"] for i in items]),
            "alcohol_days": sum(1 for i in items if i["alcohol"]),
        },
        "items": items if full_series else items[:30],
    }


@router.get("/user_profile")
async def user_profile(
    user=Depends(get_agent_user),
):
    """Return non-sensitive user profile info."""
    return {
        "status": "ok",
        "telegram_id": user.telegram_id,
        "first_name": user.first_name,
        "username": getattr(user, "username", None),
        "cohort": user.cohort,
        "container_id": user.container_id,
        "pack_name": user.pack_name,
        "garmin_email": user.garmin_email,  # intentionally included: agent needs to label data sources
        # NOTE: garmin_password is intentionally excluded
        "health_token": user.health_token,
        "timezone": getattr(user, "timezone", "Europe/Moscow"),
        "sex": getattr(user, "sex", None),
        "height_cm": getattr(user, "height_cm", None),
        "birth_date": user.birth_date.isoformat() if getattr(user, "birth_date", None) else None,
    }
