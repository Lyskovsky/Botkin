"""FastAPI router для кнопки «Экспорт для врача» в мини-аппе (#290).

POST /api/doctor_report — генерит PDF-отчёт (секции в клиническом порядке IPS)
и отправляет его пользователю Telegram-документом. Требует валидный WebApp
initData (get_tg_user). Общий путь доставки — send_doctor_report_to_chat
(тот же helper переиспользует агент-тул в follow-up).
"""

from fastapi import APIRouter, Body, Depends, HTTPException

from webhook.tg_auth import get_tg_user

router = APIRouter()


@router.post("/api/doctor_report")
async def post_doctor_report(tg_user: dict = Depends(get_tg_user), body: dict = Body(default={})):
    """Сгенерировать PDF-отчёт для врача и отправить пользователю в чат."""
    user_id = tg_user.get("id")
    if not user_id:
        raise HTTPException(status_code=400, detail="No user id in initData")

    # Импорт внутри хендлера — чтобы тесты могли подменить database.SessionLocal.
    from database import SessionLocal
    from services.doctor_report import send_doctor_report_to_chat
    from services.report_i18n import resolve_report_language

    lang = resolve_report_language((body or {}).get("language"), tg_user.get("language_code"))

    db = SessionLocal()
    try:
        result = send_doctor_report_to_chat(db, user_id, lang=lang)
    finally:
        db.close()

    if not result.get("sent"):
        # Ошибка рендера/доставки — фронт покажет пользователю «не удалось».
        raise HTTPException(status_code=502, detail=result.get("error", "send-failed"))
    return {"status": "sent"}
