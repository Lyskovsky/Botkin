from types import SimpleNamespace
from core.agent_chat import _health_profile_ask_block, _health_profile_block


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


# ── Мягкий сбор медпрофиля: инструкция спросить один раз (#340) ───────────────


def test_ask_block_present_when_profile_empty():
    """Профиль пуст, вопрос не задавали → агент получает инструкцию спросить."""
    block = _health_profile_ask_block(SimpleNamespace(onboarding_data={"name": "Тест"}))
    assert "save_health_profile" in block
    assert "ОДИН раз" in block


def test_ask_block_absent_when_profile_filled():
    """Диагнозы уже есть → спрашивать нечего."""
    user = SimpleNamespace(onboarding_data={"chronic_conditions": ["Гипотиреоз"]})
    assert _health_profile_ask_block(user) == ""


def test_ask_block_absent_when_already_asked():
    """Вопрос уже задавали (nothing_to_report) → не повторяем."""
    user = SimpleNamespace(onboarding_data={"health_profile_asked": True})
    assert _health_profile_ask_block(user) == ""


def test_ask_block_survives_missing_onboarding_data():
    """None вместо dict не роняет сборку промпта."""
    assert "save_health_profile" in _health_profile_ask_block(SimpleNamespace(onboarding_data=None))


def test_ask_block_absent_for_users_with_custom_prompt():
    """Семейный юзер с богатым промптом из PROFILE.md/KB — не переспрашиваем."""
    user = SimpleNamespace(onboarding_data={}, agent_system_prompt="## Пациент\nПодробная медистория…")
    assert _health_profile_ask_block(user) == ""
