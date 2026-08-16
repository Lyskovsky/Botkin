"""Тесты admin-контекст-блока системного промпта агента (#337).

Admin-статус (BOTKIN_ADMIN_IDS) — отдельная ось от когорты данных. Без явного
сигнала в промпте LLM гадает по family-персоне и отказывает админу в триаже
фидбека. build_admin_context даёт этот сигнал только админам.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.agent_chat import ADMIN_CONTEXT_PROMPT, build_admin_context


def test_admin_gets_context_block():
    block = build_admin_context(True)
    assert block == ADMIN_CONTEXT_PROMPT
    assert block, "admin-блок не должен быть пустым"
    # ключевые сигналы: admin-тулы фидбека доступны по просьбе
    assert "triage_feedback" in block
    assert "list_feedback" in block
    # явно разводит admin-статус и когорту (чтобы не было отказа «ты family»)
    assert "когорт" in block.lower()


def test_non_admin_gets_empty_block():
    assert build_admin_context(False) == ""
