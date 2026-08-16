"""Тесты автонаполнения справочника verified_products (#255): product_label + кнопка."""

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ── project root on sys.path (telegram-bot не пакет) ─────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent
BOT_ROOT = PROJECT_ROOT / "telegram-bot"
for p in [str(PROJECT_ROOT), str(BOT_ROOT)]:
    if p not in sys.path:
        sys.path.insert(0, p)

from core.llm.models import parse_llm_response  # noqa: E402
from database.models import User  # noqa: E402

USER_ID = 111

SOLVIE_LABEL = {
    "name": "Solvie Protein Barre",
    "brand": "Solvie",
    "barcode": "4673728135932",
    "calories_per_100g": 288.0,
    "protein_per_100g": 33.4,
    "fats_per_100g": 13.2,
    "carbs_per_100g": 9.0,
    "fiber_per_100g": 22.8,
    "portion_g": 50.0,
}


# ── pydantic: product_label проходит валидацию ───────────────────────────────


def test_parse_llm_response_keeps_product_label():
    raw = {
        "type": "food",
        "data": {
            "dish_name": "Батончик",
            "items": [{"name": "Solvie Protein Barre", "weight": 50, "calories": 144}],
            "product_label": SOLVIE_LABEL,
        },
    }
    parsed = parse_llm_response(raw)
    label = parsed["data"]["product_label"]
    assert label["name"] == "Solvie Protein Barre"
    assert label["calories_per_100g"] == 288.0
    assert label["barcode"] == "4673728135932"


def test_parse_llm_response_product_label_default_none():
    raw = {"type": "food", "data": {"dish_name": "Суп", "items": []}}
    parsed = parse_llm_response(raw)
    assert parsed["data"]["product_label"] is None


def test_parse_llm_response_coerces_label_strings_and_negatives():
    raw = {
        "type": "food",
        "data": {
            "dish_name": "Батончик",
            "items": [],
            "product_label": {**SOLVIE_LABEL, "calories_per_100g": "288", "portion_g": -5},
        },
    }
    label = parse_llm_response(raw)["data"]["product_label"]
    assert label["calories_per_100g"] == 288.0
    assert label["portion_g"] is None


# ── label_is_complete ────────────────────────────────────────────────────────


def test_label_is_complete_true():
    from handlers.verified_products import label_is_complete

    assert label_is_complete(SOLVIE_LABEL) is True


@pytest.mark.parametrize(
    "broken",
    [
        None,
        {},
        {**SOLVIE_LABEL, "name": ""},
        {**SOLVIE_LABEL, "protein_per_100g": None},
    ],
)
def test_label_is_complete_false(broken):
    from handlers.verified_products import label_is_complete

    assert label_is_complete(broken) is False


# ── offer + callback save ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_offer_remember_product_sends_button():
    from handlers import verified_products as vp

    message = AsyncMock()
    sent = await vp.offer_remember_product(message, USER_ID, SOLVIE_LABEL)

    assert sent is True
    assert vp._pending_labels[USER_ID] == SOLVIE_LABEL
    text = message.answer.call_args.args[0]
    assert "Solvie Protein Barre" in text
    assert "288" in text
    vp._pending_labels.pop(USER_ID, None)


@pytest.mark.asyncio
async def test_offer_skipped_for_incomplete_label():
    from handlers import verified_products as vp

    message = AsyncMock()
    assert await vp.offer_remember_product(message, USER_ID, {"name": "Х"}) is False
    message.answer.assert_not_called()


def _make_callback():
    callback = AsyncMock()
    callback.from_user = MagicMock()
    callback.from_user.id = USER_ID
    callback.message = AsyncMock()
    return callback


@pytest.mark.asyncio
async def test_remember_save_upserts_to_catalog(test_db):
    from handlers import verified_products as vp

    test_db.add(User(telegram_id=USER_ID, first_name="test"))
    test_db.commit()

    vp._pending_labels[USER_ID] = dict(SOLVIE_LABEL)
    callback = _make_callback()

    with (
        patch("database.SessionLocal", return_value=test_db),
        patch.object(test_db, "close"),
    ):
        await vp.handle_remember_product(callback, vp.RememberProductCallback(action="save"))

    from database.crud import find_verified_product

    found = find_verified_product(test_db, USER_ID, "solvie protein barre")
    assert found is not None
    assert found.source == "label_photo"
    assert found.calories_per_100g == 288.0
    assert found.portion_g == 50.0
    assert USER_ID not in vp._pending_labels


@pytest.mark.asyncio
async def test_remember_skip_does_not_save(test_db):
    from handlers import verified_products as vp

    vp._pending_labels[USER_ID] = dict(SOLVIE_LABEL)
    callback = _make_callback()

    await vp.handle_remember_product(callback, vp.RememberProductCallback(action="skip"))

    assert USER_ID not in vp._pending_labels
    callback.message.delete.assert_called_once()


@pytest.mark.asyncio
async def test_remember_save_without_pending_shows_alert():
    from handlers import verified_products as vp

    vp._pending_labels.pop(USER_ID, None)
    callback = _make_callback()

    await vp.handle_remember_product(callback, vp.RememberProductCallback(action="save"))

    assert "устарели" in callback.answer.call_args.args[0]
