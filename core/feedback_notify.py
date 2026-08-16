"""Тексты уведомлений автору фидбека при разборе обращения (Фаза 3, #188).

Чистый модуль без БД и сети: строит текст личного сообщения, которое бот шлёт
пользователю, когда его баг/идея/вопрос переходит в разобранный статус. Отправка
и штамп `notified_at` — в webhook/agent_tools_api.py (там есть токен и сеть).

Правила:
- `custom` (явный текст от админа) имеет приоритет над авто-текстом и работает при
  ЛЮБОМ статусе — так `question` закрывается человеческим ответом без смены статуса.
- Иначе авто-текст только для `done`/`wontfix`; для остальных статусов — None
  (уведомлять нечего).
"""

from typing import Optional

# Статусы, при которых бот сам порождает generic-уведомление.
NOTIFY_STATUSES = ("done", "wontfix")

_SNIPPET_LIMIT = 160


def _snippet(text: str, limit: int = _SNIPPET_LIMIT) -> str:
    """Короткая цитата исходного обращения (чтобы автор вспомнил контекст)."""
    clean = " ".join((text or "").split())
    if len(clean) <= limit:
        return clean
    return clean[: limit - 1].rstrip() + "…"


def build_notification_text(
    *,
    kind: str,
    status: str,
    text: str,
    custom: Optional[str] = None,
) -> Optional[str]:
    """Текст уведомления автору или None, если уведомлять не нужно.

    kind: 'bug'|'feature'|'question'|'unspecified' (сейчас на текст не влияет,
    но передаём для будущей тональности). status — новый статус записи.
    custom — явный текст от админа, перекрывает всё.

    Внутренний номер GitHub-issue в тексте намеренно НЕ упоминается: репозиторий
    приватный, для пользователя это лишь непонятная метка.
    """
    if custom and custom.strip():
        return custom.strip()

    quote = _snippet(text)
    if status == "done":
        return (
            f"Ты писал(а): «{quote}»\n\n"
            f"Мы разобрались 🙏 Спасибо, что помогаешь делать бота лучше! "
            f"Если что-то ещё — пиши /feedback."
        )
    if status == "wontfix":
        return (
            f"Ты писал(а): «{quote}»\n\n"
            f"Посмотрели — пока оставим как есть, но твой сигнал записали. Спасибо, что не прошёл(а) мимо!"
        )
    return None
