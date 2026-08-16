#!/usr/bin/env python3
"""
Botkin MCP Server — инструменты для Claude Desktop.

Инструменты:
  read_knowledge_base(person)  — медицинская база знаний члена семьи
  query_nutrition(days)        — питание и добавки из PostgreSQL (SSH)
  run_scout_weekly()           — запустить еженедельный скан новинок
  read_scout_digest(week)      — прочитать готовый дайджест
  get_health_profile()         — профиль здоровья Александра (HEALTH.md)
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

from mcp.server.fastmcp import FastMCP

ROOT = Path(__file__).resolve().parents[2]
FAMILY_HEALTH = Path.home() / "Library/CloudStorage/GoogleDrive-lyskovsky@gmail.com/Мой диск/FamilyHealth"

SSH_SERVER = "root@116.203.213.137"
SSH_OPTS = ["-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=10"]

# PERSON_MAP — алиасы для имён → название папки в ~/FamilyHealth/<имя>.
# Это приватный конфиг (имена членов семьи), не должен лежать в git.
# Загружается из ~/.botkin-personal.yaml (шаблон в docs/operations/personal-data.md).
# Если файла нет — MCP отвечает только на алиас "self" из env BOTKIN_SELF_DIR.
import yaml

_personal_yaml = Path.home() / ".botkin-personal.yaml"
if _personal_yaml.exists():
    PERSON_MAP = yaml.safe_load(_personal_yaml.read_text()).get("person_map", {})
else:
    PERSON_MAP = {"self": os.getenv("BOTKIN_SELF_DIR", "")}

mcp = FastMCP("Botkin")


@mcp.tool()
def read_knowledge_base(person: str = "Александр") -> str:
    """
    Читает медицинскую базу знаний (анализы крови, диагнозы, добавки) для члена семьи.
    person: имя на русском или английском — Александр/Саша, Валерия/Мама, Олег, Игорь, Катя.
    Возвращает JSON со всеми биомаркерами и историей анализов.
    """
    key = person.strip().lower()
    folder_name = PERSON_MAP.get(key)
    if folder_name is None:
        return json.dumps({"error": f"Человек '{person}' не найден. Доступны: Александр, family members"})

    kb_path = FAMILY_HEALTH / folder_name / "knowledge_base.json"
    if not kb_path.exists():
        # Fallback: корневой knowledge_base.json Александра
        if "александр" in key or "саша" in key or "alexander" in key:
            kb_path = ROOT / "knowledge_base.json"
        if not kb_path.exists():
            return json.dumps({"error": f"knowledge_base.json не найден для {person}: {kb_path}"})

    try:
        data = json.loads(kb_path.read_text())
        return json.dumps(data, ensure_ascii=False, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def query_nutrition(days: int = 7, user: str = "Александр") -> str:
    """
    Получает данные о питании и добавках из PostgreSQL (SSH на сервер).
    days: за сколько последних дней (по умолчанию 7).
    user: Александр (895655) или второй пользователь (env HV_SECOND_USER_ID).
    Возвращает JSON с записями питания и добавками.
    """
    user_id = (
        895655 if "александр" in user.lower() or "саша" in user.lower() else int(os.getenv("HV_SECOND_USER_ID", "0"))
    )
    since = (date.today() - timedelta(days=days)).isoformat()

    nutrition_sql = (
        f"SELECT json_agg(t ORDER BY t.date DESC) FROM "
        f"(SELECT date::text, meal_time::text, items, totals "
        f"FROM nutrition_log WHERE date >= '{since}' AND user_id = {user_id}) t"
    )
    supplements_sql = (
        f"SELECT json_agg(t ORDER BY t.date DESC) FROM "
        f"(SELECT date::text, supplement_name, dose_mg, timing "
        f"FROM supplements_log WHERE date >= '{since}' AND user_id = {user_id}) t"
    )

    def ssh_query(sql: str) -> list:
        cmd = [
            "ssh",
            *SSH_OPTS,
            SSH_SERVER,
            f'docker exec healthvault_postgres psql -U healthvault -d healthvault -t -c "COPY ({sql}) TO STDOUT"',
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        raw = result.stdout.strip()
        if not raw or raw == "\\N":
            return []
        try:
            return json.loads(raw) or []
        except Exception:
            return []

    nutrition = ssh_query(nutrition_sql)
    supplements = ssh_query(supplements_sql)

    return json.dumps(
        {
            "period": f"последние {days} дней (с {since})",
            "user": user,
            "nutrition_records": len(nutrition),
            "nutrition": nutrition[:50],  # максимум 50 записей
            "supplements_records": len(supplements),
            "supplements": supplements[:100],
        },
        ensure_ascii=False,
        indent=2,
    )


@mcp.tool()
def run_scout_weekly(force: bool = False) -> str:
    """
    Запускает еженедельный скан новинок по биохакингу/здоровью (GitHub, HN, PubMed, arXiv).
    Возвращает сводку: сколько найдено по каждому источнику.
    После запуска — прочитай дайджест через read_scout_digest() и синтезируй топ-10 находок.
    force: если True — игнорировать дедупликацию (повторно показать уже виденные ссылки).
    """
    script = ROOT / "scripts" / "scout" / "fetch_candidates.py"
    python = ROOT / "venv_mcp" / "bin" / "python"

    args = [str(python), str(script)]
    if force:
        args.append("--no-dedup")

    result = subprocess.run(args, capture_output=True, text=True, timeout=300, cwd=str(ROOT))

    # Найти путь к сгенерированному файлу
    candidates_file = None
    for line in result.stderr.splitlines():
        if "candidates_" in line and ".json" in line:
            # извлечь путь
            for word in line.split():
                if "candidates_" in word and ".json" in word:
                    candidates_file = ROOT / word

    summary = {"status": "ok" if result.returncode == 0 else "error"}
    if result.returncode != 0:
        summary["error"] = result.stderr[-500:]
        return json.dumps(summary, ensure_ascii=False)

    # Прочитать summary из файла
    if candidates_file and Path(candidates_file).exists():
        data = json.loads(Path(candidates_file).read_text())
        summary.update(
            {
                "week": data["week"],
                "total_candidates": data["total"],
                "by_source": data["by_source"],
                "file": str(candidates_file),
                "next_step": "Теперь синтезируй дайджест: прочитай файл кандидатов, отбери топ-10 по profile.yaml и сохрани в docs/scout/{week}.md",
            }
        )
    else:
        summary["stderr"] = result.stderr[-1000:]

    return json.dumps(summary, ensure_ascii=False, indent=2)


@mcp.tool()
def read_scout_digest(week: str = "") -> str:
    """
    Читает сохранённый дайджест Scout Weekly.
    week: формат '2026-W16'. Если пусто — читает последний доступный.
    Возвращает markdown текст дайджеста.
    """
    scout_dir = ROOT / "docs" / "scout"
    if not scout_dir.exists():
        return "Дайджесты ещё не созданы. Запусти run_scout_weekly() сначала."

    digests = sorted(scout_dir.glob("*.md"), reverse=True)
    if not digests:
        return "Нет сохранённых дайджестов."

    if week:
        target = scout_dir / f"{week}.md"
        if not target.exists():
            available = [f.stem for f in digests]
            return f"Дайджест {week} не найден. Доступны: {available}"
        path = target
    else:
        path = digests[0]

    return path.read_text()


@mcp.tool()
def get_health_profile() -> str:
    """
    Читает профиль здоровья Александра: вес, давление, биомаркеры, добавки, цели.
    Источник: HEALTH.md в корне проекта.
    """
    health_md = ROOT / "HEALTH.md"
    if not health_md.exists():
        return "HEALTH.md не найден."
    return health_md.read_text()


@mcp.tool()
def list_scout_candidates(week: str = "") -> str:
    """
    Читает сырые кандидаты из последнего скана (data/scout/candidates_YYYY-Www.json).
    Используй это если хочешь самостоятельно синтезировать дайджест или переранжировать находки.
    week: '2026-W16' или пусто для последнего.
    """
    scout_data = ROOT / "data" / "scout"
    if not scout_data.exists():
        return "Данные скаута не найдены. Запусти run_scout_weekly()."

    files = sorted(scout_data.glob("candidates_*.json"), reverse=True)
    if not files:
        return "Нет файлов кандидатов."

    if week:
        target = scout_data / f"candidates_{week}.json"
        path = target if target.exists() else files[0]
    else:
        path = files[0]

    data = json.loads(path.read_text())
    # Возвращаем сводку + все items для ранжирования
    return json.dumps(
        {
            "week": data["week"],
            "total": data["total"],
            "by_source": data["by_source"],
            "profile_keywords": data["profile_keywords"],
            "items": data["items"],
        },
        ensure_ascii=False,
        indent=2,
    )


if __name__ == "__main__":
    mcp.run()
