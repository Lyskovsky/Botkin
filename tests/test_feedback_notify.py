"""Уведомление автора фидбека — Фаза 3 (#188).

Чистый билдер текста (core/feedback_notify.py) + CRUD-штамп notified_at.
Транзиция→уведомление, идемпотентность, opt-out, кастомный notify_text —
на уровне эндпоинта в tests/test_feedback_triage.py.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "telegram-bot"))

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database.models import Base
from database import crud
from core.feedback_notify import build_notification_text


# ── Чистый билдер текста ─────────────────────────────────────────────────────


@pytest.mark.parametrize("status", ["new", "triaged", "in_progress", "duplicate"])
def test_open_status_without_custom_yields_none(status):
    # Открытые статусы без явного текста не порождают уведомления.
    assert build_notification_text(kind="bug", status=status, text="вес неверный") is None


def test_done_mentions_resolution_and_quotes_original():
    msg = build_notification_text(kind="bug", status="done", text="вес на дашборде неверный")
    assert msg is not None
    assert "вес на дашборде неверный" in msg  # цитата исходного обращения
    assert "разобрал" in msg.lower()  # «разобрались»


def test_done_does_not_leak_internal_issue_number():
    # Внутренний номер GitHub-issue не показываем пользователю — репозиторий приватный.
    msg = build_notification_text(kind="bug", status="done", text="баг")
    assert "#" not in msg


def test_wontfix_has_distinct_text():
    done = build_notification_text(kind="feature", status="done", text="хочу тёмную тему")
    wontfix = build_notification_text(kind="feature", status="wontfix", text="хочу тёмную тему")
    assert wontfix is not None
    assert wontfix != done


def test_custom_text_overrides_and_works_for_open_status():
    # question можно закрыть человеческим ответом даже без смены статуса.
    msg = build_notification_text(
        kind="question", status="new", text="как посчитать клетчатку?", custom="Клетчатка считается по таблице USDA."
    )
    assert msg == "Клетчатка считается по таблице USDA."


def test_blank_custom_falls_through_to_status_text():
    # Пробельный custom игнорируется, не подменяет generic.
    msg = build_notification_text(kind="bug", status="done", text="баг", custom="   ")
    assert msg is not None
    assert "разобрал" in msg.lower()


def test_long_original_text_truncated():
    long_text = "очень длинная жалоба " * 40  # ~800 символов
    msg = build_notification_text(kind="bug", status="done", text=long_text)
    assert "…" in msg  # многоточие-обрезка
    assert len(msg) < len(long_text)


# ── CRUD: штамп notified_at ──────────────────────────────────────────────────


@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(autocommit=False, autoflush=False, bind=engine)()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)


def _mk(db, text="вес неверный"):
    return crud.create_feedback(db, user_id=895655, text=text, source="command")


def test_mark_notified_stamps_when_null(db_session):
    row = _mk(db_session)
    assert row.notified_at is None
    updated = crud.mark_feedback_notified(db_session, row.id)
    assert updated.notified_at is not None


def test_mark_notified_is_idempotent(db_session):
    row = _mk(db_session)
    first = crud.mark_feedback_notified(db_session, row.id)
    stamp = first.notified_at
    second = crud.mark_feedback_notified(db_session, row.id)
    assert second.notified_at == stamp  # не перезаписывает исходный штамп


def test_mark_notified_not_found_returns_none(db_session):
    assert crud.mark_feedback_notified(db_session, 9999) is None
