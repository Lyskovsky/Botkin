"""Тесты промпт-инъекции справочника проверенных продуктов (#255)."""

from unittest.mock import MagicMock, patch

from core.food.verified_products import (
    PROMPT_BLOCK_MAX_CHARS,
    build_known_products_block,
    normalize_product_name,
)
from database.crud import upsert_verified_product
from database.models import User

USER_ID = 111


def _make_user(db, telegram_id=USER_ID):
    db.add(User(telegram_id=telegram_id, first_name=f"user{telegram_id}"))
    db.commit()


def _add_product(db, name, **overrides):
    kwargs = dict(
        name=name,
        name_norm=normalize_product_name(name),
        calories_per_100g=288.0,
        protein_per_100g=33.4,
        fats_per_100g=13.2,
        carbs_per_100g=9.0,
        fiber_per_100g=22.8,
        portion_g=50.0,
        brand="Solvie",
        source="manual",
    )
    kwargs.update(overrides)
    return upsert_verified_product(db, user_id=USER_ID, **kwargs)


# ── build_known_products_block ───────────────────────────────────────────────


def test_empty_catalog_returns_empty_string(test_db):
    _make_user(test_db)
    assert build_known_products_block(USER_ID, db=test_db) == ""


def test_block_contains_product_data(test_db):
    _make_user(test_db)
    _add_product(test_db, "Solvie Protein Barre")
    block = build_known_products_block(USER_ID, db=test_db)

    assert "Solvie Protein Barre" in block
    assert "288 kcal" in block
    assert "P 33.4" in block
    assert "fiber 22.8" in block
    assert "portion 50 g" in block


def test_block_respects_max_chars(test_db):
    _make_user(test_db)
    for i in range(20):
        _add_product(test_db, f"Очень длинное название продукта номер {i} с брендом и деталями")
    block = build_known_products_block(USER_ID, db=test_db)
    assert len(block) <= PROMPT_BLOCK_MAX_CHARS


def test_block_respects_limit(test_db):
    _make_user(test_db)
    for i in range(5):
        _add_product(test_db, f"Продукт {i}")
    block = build_known_products_block(USER_ID, db=test_db, limit=2)
    assert len([line for line in block.splitlines() if line.startswith("- ")]) == 2


# ── инъекция в LLM-пути роутера ──────────────────────────────────────────────


def _fake_response(payload_capture):
    """Мок requests.post: запоминает payload, отвечает минимальным валидным JSON."""

    def _post(url, headers=None, json=None, timeout=None):
        payload_capture.append(json)
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {
            "content": [{"text": '{"type": "other", "data": {}}'}],
            "usage": {},
            "choices": [{"message": {"content": '{"type": "other", "data": {}}'}, "finish_reason": "stop"}],
            "candidates": [{"content": {"parts": [{"text": '{"type": "other", "data": {}}'}]}}],
        }
        return resp

    return _post


def test_claude_payload_gets_products_block_without_cache_control():
    captured = []
    with (
        patch("core.llm.router._known_products_block", return_value="VERIFIED PRODUCTS CATALOG\n- Тест"),
        patch("core.llm.router.requests.post", side_effect=_fake_response(captured)),
    ):
        from core.llm.router import analyze_message_claude

        analyze_message_claude(text="батончик", user_id=USER_ID)

    system = captured[0]["system"]
    assert len(system) == 2
    assert "cache_control" in system[0]  # базовый промпт кешируется
    assert system[1]["text"].startswith("VERIFIED PRODUCTS CATALOG")
    assert "cache_control" not in system[1]  # per-user блок кеш не ломает


def test_claude_payload_unchanged_when_block_empty():
    captured = []
    with (
        patch("core.llm.router._known_products_block", return_value=""),
        patch("core.llm.router.requests.post", side_effect=_fake_response(captured)),
    ):
        from core.llm.router import analyze_message_claude

        analyze_message_claude(text="батончик", user_id=USER_ID)

    assert len(captured[0]["system"]) == 1


def test_gemini_payload_gets_products_block():
    captured = []
    with (
        patch("core.llm.router._known_products_block", return_value="VERIFIED PRODUCTS CATALOG\n- Тест"),
        patch("core.llm.router.requests.post", side_effect=_fake_response(captured)),
    ):
        from core.llm.router import analyze_message_gemini

        analyze_message_gemini(text="батончик", user_id=USER_ID)

    parts = captured[0]["contents"][0]["parts"]
    assert any("VERIFIED PRODUCTS CATALOG" in p.get("text", "") for p in parts)


def test_known_products_block_swallows_db_errors():
    from core.llm.router import _known_products_block

    with patch(
        "core.food.verified_products.build_known_products_block",
        side_effect=RuntimeError("db down"),
    ):
        assert _known_products_block(USER_ID) == ""


def test_known_products_block_empty_without_user_id():
    from core.llm.router import _known_products_block

    assert _known_products_block(None) == ""
