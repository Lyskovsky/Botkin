# core/health/kb_writer.py
"""Атомарная запись документа в kb_<user_id>.json (секция documents[])."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any


def append_document_to_kb(kb_path: Path, entry: dict[str, Any]) -> None:
    """Добавляет запись в documents[] в kb файле.

    Создаёт файл если не существует. Атомарная замена через tmpfile.

    Args:
        kb_path: абсолютный путь к kb_<user_id>.json
        entry: dict с ключами added_at, file, extracted, user_confirmed
    """
    if kb_path.exists():
        try:
            kb = json.loads(kb_path.read_text(encoding="utf-8"))
        except Exception:
            kb = {}
    else:
        kb = {}
        kb_path.parent.mkdir(parents=True, exist_ok=True)

    if not isinstance(kb.get("documents"), list):
        kb["documents"] = []

    kb["documents"].append(entry)

    tmp = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=kb_path.parent,
        suffix=".tmp",
        delete=False,
    )
    try:
        json.dump(kb, tmp, ensure_ascii=False, indent=2)
        tmp.flush()
        tmp.close()
        Path(tmp.name).replace(kb_path)
    except Exception:
        Path(tmp.name).unlink(missing_ok=True)
        raise
