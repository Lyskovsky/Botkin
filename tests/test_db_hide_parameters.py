"""#347 (security-review): содержимое запросов не должно утекать в логи.

`DBAPIError.__str__` по умолчанию печатает `[SQL: …] [parameters: {…}]`.
В параметрах INSERT в `agent_conversations` лежит весь turn диалога — диагнозы,
лекарства, анализы. Любой `logger.exception` по такой ошибке писал это в
`/opt/botkin/logs` открытым текстом (проверено на проде 26.07.2026).

`hide_parameters=True` на движке закрывает это для ВСЕХ путей проекта сразу,
включая пре-существующий `debug_logger.error(..., exc_info=True)` в
`telegram-bot/handlers/text.py`.
"""

import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Синтетическая заглушка вместо реальных медданных: в тексте ошибки её быть не должно.
SECRET_CONTENT = "диагноз-пациента-НЕ-ДЛЯ-ЛОГОВ-42"


def test_engine_hides_query_parameters():
    """Движок проекта сконфигурирован с hide_parameters=True."""
    from database import engine

    assert engine.hide_parameters is True, (
        "hide_parameters выключен — параметры запросов (медданные) поедут в логи через traceback"
    )


def _failing_insert(engine_under_test) -> str:
    """Спровоцировать DBAPIError на INSERT, где в параметрах лежит медтекст."""
    with engine_under_test.connect() as conn:
        conn.execute(text("CREATE TABLE t (id INTEGER PRIMARY KEY, content TEXT NOT NULL)"))
        conn.execute(text("INSERT INTO t (id, content) VALUES (1, :content)"), {"content": SECRET_CONTENT})
        # Тот же id → нарушение PK → DBAPIError, в тексте которого по умолчанию есть параметры
        with pytest.raises(DBAPIError) as exc_info:
            conn.execute(text("INSERT INTO t (id, content) VALUES (1, :content)"), {"content": SECRET_CONTENT})
    return str(exc_info.value)


def test_medical_content_absent_from_error_text_when_hidden():
    """С hide_parameters=True медтекст в сообщение об ошибке не попадает."""
    engine = create_engine("sqlite:///:memory:", hide_parameters=True)

    message = _failing_insert(engine)

    assert SECRET_CONTENT not in message


def test_medical_content_leaks_without_hide_parameters():
    """Контроль: без флага тот же медтекст в сообщении об ошибке присутствует.

    Гарантирует, что тест выше проверяет реальный механизм, а не тавтологию.
    """
    engine = create_engine("sqlite:///:memory:", hide_parameters=False)

    message = _failing_insert(engine)

    assert SECRET_CONTENT in message
