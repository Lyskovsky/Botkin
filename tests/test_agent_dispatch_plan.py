"""Тесты диспатча инструментов плана→факта (#407): adjust_meal_items, only_open_plans.

Юнит-уровень: напрямую вызываем core.agent_chat._call_tool с замоканным
core.agent_chat.requests — без полного цикла ask_agent (тот уже покрыт в
test_ask_agent.py). Плюс проверяем, что TOOLS содержит новый тул и
get_recent_meals схема несёт only_open_plans.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import core.agent_chat as agent_chat


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200
        self.ok = True

    def json(self):
        return self._payload


class _FakeRequests:
    """Записывает вызовы .post/.get, чтобы тест мог проверить url/json/params."""

    def __init__(self):
        self.calls = []

    def post(self, url, headers=None, json=None, timeout=None):
        self.calls.append({"method": "post", "url": url, "headers": headers, "json": json})
        return _FakeResp({"status": "ok"})

    def get(self, url, headers=None, params=None, timeout=None):
        self.calls.append({"method": "get", "url": url, "headers": headers, "params": params})
        return _FakeResp({"status": "ok", "meals": []})


def test_tools_list_contains_adjust_meal_items():
    names = [t["name"] for t in agent_chat.TOOLS]
    assert "adjust_meal_items" in names


def test_get_recent_meals_schema_has_only_open_plans():
    tool = next(t for t in agent_chat.TOOLS if t["name"] == "get_recent_meals")
    assert "only_open_plans" in tool["input_schema"]["properties"]


def test_dispatch_adjust_meal_items_posts_json(monkeypatch):
    fake = _FakeRequests()
    monkeypatch.setattr(agent_chat, "requests", fake)

    args = {"meal_id": 1, "changes": [], "close_plan": True, "dry_run": False}
    agent_chat._call_tool("adjust_meal_items", args, "tok")

    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["method"] == "post"
    assert call["url"].endswith("/adjust_meal_items")
    assert call["json"] == args
    assert call["headers"]["Authorization"] == "Bearer tok"


def test_dispatch_get_recent_meals_passes_only_open_plans(monkeypatch):
    fake = _FakeRequests()
    monkeypatch.setattr(agent_chat, "requests", fake)

    agent_chat._call_tool("get_recent_meals", {"days": 1, "only_open_plans": True}, "tok")

    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["method"] == "get"
    assert call["url"].endswith("/recent_meals")
    assert call["params"]["only_open_plans"] == "true"
    assert call["params"]["days"] == 1
