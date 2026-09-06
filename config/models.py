"""Все имена LLM-моделей проекта — в одном месте.

Зачем: смена модели (цена/качество) — правка одной строки или env-переменной,
без поиска по коду. Прецедент: откат агента Opus 4.8 → Sonnet 4.6 01.06.2026
по стоимости потребовал лезть в код.

Каждое значение можно переопределить env-переменной (удобно для A/B на проде
без пересборки образа: docker compose с env_file подхватит).

Цены и алиасы для учёта расходов — отдельно в core/llm_usage.py (там
справочник «модель → $/MT», не выбор модели).
"""

import os

# ── BotkinClaw (AI-врач, Anthropic Messages API) ────────────────────────────
# Sonnet 5 — рабочая модель агента (апгрейд с Sonnet 4.6 06.09.2026: та же
# средняя по цене/качеству линейка, но новее; откат с Opus 4.8 01.06.2026 остаётся
# в силе — Opus давал ~$7.5/активный день ≈ $100/мес, Sonnet в ~5× дешевле).
AGENT_MODEL = os.getenv("BOTKIN_AGENT_MODEL", "claude-sonnet-5")
# Fallback при 529/503/429 — другой compute pool (обычно свободнее), плюс
# страховка если у Sonnet 5 обнаружится регресс: прошлая рабочая модель.
# ⚠️ Sonnet 4.5 НЕ поддерживает output_config.effort (см. agent_chat).
AGENT_FALLBACK_MODEL = os.getenv("BOTKIN_AGENT_FALLBACK_MODEL", "claude-sonnet-4-6")

# ── Парсинг еды из текста (core/llm/router.py) ──────────────────────────────
FOOD_TEXT_MODEL_ANTHROPIC = os.getenv("BOTKIN_FOOD_TEXT_MODEL", "claude-sonnet-4-6")
FOOD_TEXT_MODEL_OPENAI = os.getenv("BOTKIN_FOOD_TEXT_MODEL_OPENAI", "gpt-4o")

# ── Vision: фото еды и меню ─────────────────────────────────────────────────
VISION_MODEL_OPENAI = os.getenv("BOTKIN_VISION_MODEL_OPENAI", "gpt-4o")
VISION_MODEL_GEMINI = os.getenv("BOTKIN_VISION_MODEL_GEMINI", "gemini-2.0-flash")

# ── OCR веса с фото весов (core/vision/ocr_weight.py) ───────────────────────
WEIGHT_OCR_MODEL_GEMINI = os.getenv("BOTKIN_WEIGHT_OCR_MODEL_GEMINI", "gemini-2.0-flash")
WEIGHT_OCR_MODEL_OPENAI = os.getenv("BOTKIN_WEIGHT_OCR_MODEL_OPENAI", "gpt-4o")

# ── Поиск продуктов в базе КБЖУ (core/food/product_search.py) ────────────────
PRODUCT_SEARCH_MODEL = os.getenv("BOTKIN_PRODUCT_SEARCH_MODEL", "gpt-4o-mini")

# ── Голосовые → текст (core/infra/voice_service.py) ─────────────────────────
VOICE_TRANSCRIBE_MODEL = os.getenv("BOTKIN_VOICE_MODEL", "whisper-1")
