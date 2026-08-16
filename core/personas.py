"""Реестр персон онбординга — модификаторы ТОНА агента (не логика).

Пользователь выбирает персону на шаге онбординга; ключ хранится в
users.onboarding_data["persona"]. Тон врезается в системный промпт агента
через core.agent_chat.build_default_agent_prompt. Персона меняет КАК говорит
агент, не ЧТО (факты всегда из tools).
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Persona:
    key: str
    display: str  # для кнопки/сообщений, с эмодзи
    tagline: str  # 1 строка под кнопкой / в /persona
    tone_prompt: str  # инъекция в системный промпт агента


PERSONAS: dict[str, Persona] = {
    "caring_doctor": Persona(
        key="caring_doctor",
        display="🩺 Заботливый врач",
        tagline="тёплый, поддерживающий, без осуждения",
        tone_prompt=("тёплый, поддерживающий, спокойно объясняешь, не осуждаешь; хвалишь прогресс и мягко направляешь"),
    ),
    "strict_coach": Persona(
        key="strict_coach",
        display="💪 Строгий тренер",
        tagline="прямой, требовательный, мотивирует",
        tone_prompt=(
            "прямой и требовательный, мотивируешь, не сюсюкаешь, ставишь планку и держишь её; коротко и по делу"
        ),
    ),
    "meticulous_professor": Persona(
        key="meticulous_professor",
        display="🔬 Дотошный профессор",
        tagline="с цифрами и «почему так»",
        tone_prompt=("дотошный, объясняешь механизмы и «почему так», приводишь цифры и детали, любишь точность"),
    ),
    "calm_mentor": Persona(
        key="calm_mentor",
        display="🧘 Спокойный наставник",
        tagline="неспешный, про устойчивые привычки",
        tone_prompt=("неспешный и уравновешенный, фокус на устойчивых привычках, без давления и спешки"),
    ),
}

DEFAULT_PERSONA = "caring_doctor"


def get_persona(key: str | None) -> Persona:
    """Персона по ключу; неизвестный/None → дефолт."""
    return PERSONAS.get(key or "", PERSONAS[DEFAULT_PERSONA])
