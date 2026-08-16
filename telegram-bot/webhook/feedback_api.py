"""FastAPI router for the mini-app feedback button (#271).

Третий канал захвата инбокса обратной связи (#188), рядом с командой /feedback
и agent-инструментом flag_for_devs. Пользователь пишет баг/идею в настройках
мини-аппа → POST /api/feedback → та же запись в user_feedback (source='webapp').

Endpoint:
  POST /api/feedback
    Body: {"text": "...", "kind": "bug"|"feature"|"question"|"unspecified"}
    Требует валидный Telegram WebApp initData (get_tg_user).
    Уважает opt-out (is_feedback_opted_out) — как командный и агентский каналы.
"""

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator

from webhook.tg_auth import get_tg_user

router = APIRouter()

FeedbackKind = Literal["bug", "feature", "question", "unspecified"]


class FeedbackPayload(BaseModel):
    text: str = Field(..., max_length=4000)
    kind: FeedbackKind = "unspecified"

    @field_validator("text")
    @classmethod
    def _strip_nonempty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("text must not be empty")
        return v


@router.post("/api/feedback")
async def post_feedback(
    payload: FeedbackPayload,
    tg_user: dict = Depends(get_tg_user),
):
    """Записать фидбек из мини-аппа в общий инбокс (#188)."""
    user_id = tg_user.get("id")
    if not user_id:
        raise HTTPException(status_code=400, detail="No user id in initData")

    # Импорт внутри хендлера — чтобы тесты могли подменить database.SessionLocal.
    from database import SessionLocal
    from database.crud import create_feedback, is_feedback_opted_out

    db = SessionLocal()
    try:
        # Тот же opt-out-гейт, что у командного и агентского каналов захвата.
        if is_feedback_opted_out(db, user_id):
            return {"status": "opted_out"}
        row = create_feedback(
            db,
            user_id=user_id,
            text=payload.text,
            source="webapp",
            kind=payload.kind,
        )
        return {"status": "ok", "id": row.id}
    finally:
        db.close()
