"""B2B-ветка онбординга (коуч/нутрициолог) — СТАБ.

Врезается через deep-link /start coach[_<token>]. Реальный флоу (панель
коуча, привязка клиентов) — future (роадмап п.4/5). Сейчас — заглушка,
чтобы код-шов существовал и B2C-путь его не касался.
"""

from handlers.onboarding import send_message


async def start_coach_onboarding(payload: dict) -> None:
    chat_id = (payload.get("message") or {}).get("chat", {}).get("id")
    await send_message(
        chat_id,
        "👋 Это вход для коучей и нутрициологов. B2B-онбординг скоро — "
        "мы свяжемся. А пока можешь пользоваться ботом как обычный пользователь: /start",
    )
