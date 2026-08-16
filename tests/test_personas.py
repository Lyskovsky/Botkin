from core.personas import PERSONAS, DEFAULT_PERSONA, get_persona, Persona


def test_registry_has_four_personas_with_unique_keys():
    assert set(PERSONAS) == {"caring_doctor", "strict_coach", "meticulous_professor", "calm_mentor"}
    assert len(PERSONAS) == 4


def test_each_persona_has_nonempty_fields():
    for key, p in PERSONAS.items():
        assert isinstance(p, Persona)
        assert p.key == key
        assert p.display.strip()
        assert p.tagline.strip()
        assert p.tone_prompt.strip()


def test_default_persona_is_caring_doctor():
    assert DEFAULT_PERSONA == "caring_doctor"
    assert DEFAULT_PERSONA in PERSONAS


def test_get_persona_falls_back_to_default_on_unknown_or_none():
    assert get_persona(None).key == DEFAULT_PERSONA
    assert get_persona("bogus").key == DEFAULT_PERSONA
    assert get_persona("strict_coach").key == "strict_coach"


from types import SimpleNamespace
from core.agent_chat import build_default_agent_prompt


def _user(persona=None, **data):
    d = {"name": "Игорь", "age": 35, "sex": "male", "goal": "Похудеть"}
    d.update(data)
    if persona is not None:
        d["persona"] = persona
    return SimpleNamespace(onboarding_data=d, first_name="Игорь", agent_system_prompt=None)


def test_prompt_includes_persona_tone_block_when_set():
    prompt = build_default_agent_prompt(_user(persona="strict_coach"))
    assert "Стиль общения" in prompt
    assert "Строгий тренер" in prompt
    assert "стиль, а не содержание" in prompt.lower()


def test_prompt_uses_default_tone_when_persona_absent():
    prompt = build_default_agent_prompt(_user())  # без persona
    assert "Стиль общения" in prompt
    assert "Заботливый врач" in prompt  # дефолт
