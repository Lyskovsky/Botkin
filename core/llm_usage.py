"""
LLM usage tracking — one row per Anthropic Messages API call into llm_usage_log.

Powers admin panel «Расходы на нейронки». Two callers today:
    - core/llm/router.py: food/photo parsing (Claude Sonnet vision)
    - core/agent_chat.py: BotkinClaw conversational agent + tool-use rounds

Pricing as of 2026-09 (Anthropic public list):
    claude-opus-4-8:   $5 / MT input, $25 / MT output, $6.25 cache_write, $0.50 cache_read
    claude-opus-4-7:   $5 / MT input, $25 / MT output, $6.25 cache_write, $0.50 cache_read
    claude-sonnet-5:   $2 / MT input, $10 / MT output, $2.50 cache_write, $0.20 cache_read
    claude-sonnet-4-6: $3 / MT input, $15 / MT output, $3.75 cache_write, $0.30 cache_read
    claude-sonnet-4-5: $3 / MT input, $15 / MT output, $3.75 cache_write, $0.30 cache_read
    claude-haiku-4-5:  $1 / MT input, $5  / MT output, $1.25 cache_write, $0.10 cache_read
    gpt-4o (OpenAI):   $2.50 / MT input, $10 / MT output (cache N/A here)

Cost is computed at insert time and stored as NUMERIC(10,6) USD. If pricing
changes later, historical rows remain correct — we don't re-derive.
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

# Per-1M-token prices in USD.
# Tuple: (input, output, cache_write, cache_read). cache_* default to None.
_PRICING: dict[str, tuple[float, float, float, float]] = {
    # Claude — Anthropic
    # Opus 4.8/4.7 — $5/$25 (Opus подешевел с 4.5; раньше тут стояла старая
    # цена 4.1-эры $15/$75 — завышала Opus-расход в учёте ×3). cache_write=1.25×in,
    # cache_read=0.1×in. Сверено со справкой Claude API (2026-06).
    "claude-opus-4-8": (5.00, 25.00, 6.25, 0.50),
    "claude-opus-4-7": (5.00, 25.00, 6.25, 0.50),
    "claude-sonnet-5": (2.00, 10.00, 2.50, 0.20),
    "claude-sonnet-4-6": (3.00, 15.00, 3.75, 0.30),
    "claude-sonnet-4-5": (3.00, 15.00, 3.75, 0.30),
    "claude-sonnet-4-5-20251001": (3.00, 15.00, 3.75, 0.30),
    "claude-haiku-4-5": (1.00, 5.00, 1.25, 0.10),
    "claude-haiku-4-5-20251001": (1.00, 5.00, 1.25, 0.10),
    # OpenAI — for completeness when router falls back to GPT-4o
    "gpt-4o": (2.50, 10.00, 0.0, 0.0),
    "gpt-4o-mini": (0.15, 0.60, 0.0, 0.0),
}


def compute_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_creation_tokens: int = 0,
    cache_read_tokens: int = 0,
) -> float:
    """Return USD cost for the given token mix. 0 if model unknown."""
    rate = _PRICING.get(model)
    if not rate:
        # Try prefix match (claude-sonnet-4-5-20251001 → claude-sonnet-4-5)
        for known, prices in _PRICING.items():
            if model.startswith(known):
                rate = prices
                break
    if not rate:
        logger.warning("compute_cost: unknown model %r, returning 0", model)
        return 0.0

    inp_price, out_price, cw_price, cr_price = rate
    return round(
        (
            input_tokens * inp_price
            + output_tokens * out_price
            + cache_creation_tokens * cw_price
            + cache_read_tokens * cr_price
        )
        / 1_000_000,
        6,
    )


def log_usage(
    *,
    purpose: str,
    model: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_creation_tokens: int = 0,
    cache_read_tokens: int = 0,
    user_id: Optional[int] = None,
) -> None:
    """Best-effort insert into llm_usage_log. Never raises — failures are
    logged but don't propagate (this is observability, not business logic).
    """
    try:
        from sqlalchemy import text as _text
        from database import SessionLocal

        cost = compute_cost(model, input_tokens, output_tokens, cache_creation_tokens, cache_read_tokens)

        db = SessionLocal()
        try:
            db.execute(
                _text(
                    """
                    INSERT INTO llm_usage_log
                        (user_id, purpose, model, input_tokens, output_tokens,
                         cache_creation_tokens, cache_read_tokens, cost_usd)
                    VALUES
                        (:uid, :purpose, :model, :inp, :out, :cw, :cr, :cost)
                    """
                ),
                {
                    "uid": user_id,
                    "purpose": purpose,
                    "model": model,
                    "inp": input_tokens,
                    "out": output_tokens,
                    "cw": cache_creation_tokens,
                    "cr": cache_read_tokens,
                    "cost": cost,
                },
            )
            db.commit()
        finally:
            db.close()
    except Exception:  # noqa: BLE001 — observability, never propagate
        logger.exception("log_usage failed (purpose=%s model=%s)", purpose, model)


def log_anthropic_response(
    *,
    purpose: str,
    model: str,
    response_json: dict,
    user_id: Optional[int] = None,
) -> None:
    """Convenience: extract usage block from Anthropic Messages API response."""
    usage = response_json.get("usage", {}) or {}
    log_usage(
        purpose=purpose,
        model=response_json.get("model") or model,
        input_tokens=usage.get("input_tokens", 0) or 0,
        output_tokens=usage.get("output_tokens", 0) or 0,
        cache_creation_tokens=usage.get("cache_creation_input_tokens", 0) or 0,
        cache_read_tokens=usage.get("cache_read_input_tokens", 0) or 0,
        user_id=user_id,
    )
