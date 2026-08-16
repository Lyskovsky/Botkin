#!/usr/bin/env python3
"""Botkin MCP-коннектор для Claude Desktop (#228).

stdio-сервер, который Claude Desktop пользователя запускает из .mcpb-бандла.
Достаёт durable PAT (env BOTKIN_PAT, из keychain через user_config) и хост API
(env BOTKIN_API_BASE), обменивает PAT→JWT и проксирует вызовы в /api/agent/*.

Инструменты — тонкие обёртки над серверными эндпоинтами (вся логика, RLS и
enforcement прав ro/rw — на сервере). ro-токен на write-инструменте получит 403.

Гибридная приватность: ЛОКАЛЬНЫЕ файлы пользователя (КПТ-дневник, сканы анализов)
этот коннектор НЕ читает — это работа встроенного в Claude Desktop коннектора
файловой системы. Botkin отдаёт только серверные данные.
"""

from __future__ import annotations

import os
import sys
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP

# .mcpb запускает скрипт по абсолютному пути — кладём его директорию в sys.path,
# чтобы найти соседний botkin_client.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from botkin_client import BotkinClient  # noqa: E402

BASE_URL = os.getenv("BOTKIN_API_BASE", "https://health.orangegate.cc")
PAT = os.getenv("BOTKIN_PAT", "")

mcp = FastMCP("Botkin")
_client = BotkinClient(BASE_URL, PAT) if PAT else None
if _client is None:
    print(
        "Botkin MCP: BOTKIN_PAT не задан. Выпусти токен в боте: /connect_mcp",
        file=sys.stderr,
    )


def _api() -> BotkinClient:
    if _client is None:
        raise RuntimeError("Не задан BOTKIN_PAT. Выпусти токен в боте Botkin: /connect_mcp")
    return _client


# ── Чтение (доступно и ro-, и rw-токену) ──────────────────────────────────────


@mcp.tool()
def get_day_summary(date: str) -> Any:
    """Сводка за день: питание, активность, замеры. date — YYYY-MM-DD."""
    return _api().get("/api/agent/day_summary", date=date)


@mcp.tool()
def get_recent_meals(days: int = 7, compact: bool = False) -> Any:
    """Приёмы пищи за последние `days` дней (калории, БЖУ). compact=True — сжатый вид."""
    return _api().get("/api/agent/recent_meals", days=days, compact=compact)


@mcp.tool()
def get_recent_biomarkers(limit: int = 20) -> Any:
    """Последние биомаркеры из анализов крови (до `limit` записей)."""
    return _api().get("/api/agent/recent_biomarkers", limit=limit)


@mcp.tool()
def get_weight_history(days: Optional[int] = None, series: bool = False) -> Any:
    """История веса. days=None — вся; series=True — временной ряд для графика."""
    return _api().get("/api/agent/weight_history", days=days, series=series)


@mcp.tool()
def get_recent_workouts(days: int = 30) -> Any:
    """Тренировки за последние `days` дней."""
    return _api().get("/api/agent/recent_workouts", days=days)


@mcp.tool()
def get_recent_bp(days: int = 14) -> Any:
    """Измерения артериального давления за последние `days` дней."""
    return _api().get("/api/agent/recent_bp", days=days)


@mcp.tool()
def get_user_profile() -> Any:
    """Профиль пользователя: пол, рост, возраст, цели."""
    return _api().get("/api/agent/user_profile")


@mcp.tool()
def botkin_api(method: str, path: str, params: Optional[dict] = None) -> Any:
    """Низкоуровневый вызов любого эндпоинта Botkin /api/agent/*.

    Escape-hatch для эндпоинтов без отдельной обёртки. method — GET/POST,
    path — например '/api/agent/phenoage', params — query (GET) или тело (POST).
    Сервер сам проверит права: write по ro-токену вернёт ошибку.
    """
    method = method.upper()
    if method == "GET":
        return _api().request("GET", path, params=params or None)
    return _api().request(method, path, json_body=params or {})


# ── Запись (требует rw-токен; ro вернёт понятную ошибку 403) ───────────────────


@mcp.tool()
def log_meal_text(text: str, date: Optional[str] = None, slot: Optional[str] = None) -> Any:
    """Записать приём пищи свободным текстом. date — YYYY-MM-DD (по умолч. сегодня),
    slot — breakfast|lunch|dinner|snack (по умолч. авто). Требует rw-токен."""
    payload: dict = {"text": text}
    if date:
        payload["date"] = date
    if slot:
        payload["slot"] = slot
    return _api().post("/api/agent/log_meal_text", payload)


@mcp.tool()
def log_bp(systolic: int, diastolic: int, pulse: Optional[int] = None, measured_at: Optional[str] = None) -> Any:
    """Записать артериальное давление (мм рт.ст.) и пульс. measured_at — ISO-дата
    (по умолч. сейчас). Требует rw-токен."""
    payload: dict = {"systolic": systolic, "diastolic": diastolic}
    if pulse is not None:
        payload["pulse"] = pulse
    if measured_at:
        payload["measured_at"] = measured_at
    return _api().post("/api/agent/log_bp", payload)


if __name__ == "__main__":
    mcp.run()
