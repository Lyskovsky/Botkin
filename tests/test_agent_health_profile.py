from types import SimpleNamespace
from core.agent_chat import _health_profile_block


def test_block_empty_when_no_profile():
    user = SimpleNamespace(onboarding_data={})
    assert _health_profile_block(user) == ""
    assert _health_profile_block(SimpleNamespace(onboarding_data=None)) == ""


def test_block_lists_allergies_only():
    user = SimpleNamespace(onboarding_data={"allergies": ["пыльца", "кошки"]})
    block = _health_profile_block(user)
    assert "Аллергии:" in block
    assert "пыльца" in block and "кошки" in block
    assert "Хронические диагнозы:" not in block


def test_block_lists_both():
    user = SimpleNamespace(
        onboarding_data={
            "allergies": ["пыльца"],
            "chronic_conditions": ["Бронхиальная астма (J45.0)"],
        }
    )
    block = _health_profile_block(user)
    assert "Аллергии: пыльца" in block
    assert "Бронхиальная астма (J45.0)" in block
    assert "Медпрофиль" in block


# ── Курение в блоке медпрофиля (#340) ────────────────────────────────────────


def test_block_includes_smoking():
    """users.smoking_status доходит до промпта человекочитаемой строкой."""
    user = SimpleNamespace(onboarding_data={"chronic_conditions": ["Гипотиреоз"]}, smoking_status="current")
    block = _health_profile_block(user)
    assert "Курение: курит" in block
    assert "Гипотиреоз" in block


def test_block_smoking_only_is_not_empty():
    """Курение без диагнозов/аллергий — блок всё равно есть (нужен для советов)."""
    block = _health_profile_block(SimpleNamespace(onboarding_data={}, smoking_status="former"))
    assert "Курение: бросил" in block


def test_block_ignores_unknown_smoking_value():
    """Мусорное/пустое значение smoking_status не порождает пустую строку."""
    assert _health_profile_block(SimpleNamespace(onboarding_data={}, smoking_status="")) == ""
    assert _health_profile_block(SimpleNamespace(onboarding_data={}, smoking_status="wat")) == ""
