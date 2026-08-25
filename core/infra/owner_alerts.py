"""Проактивные алерты владельцу проекта через Telegram Bot API.

Прецедент 24-25.08.2026: у Anthropic закончился баланс, BotkinClaw молчал/
падал у ВСЕХ пользователей почти сутки, узнали только когда пожаловался
Павел Храпкин. Нужен прямой сигнал владельцу в момент отказа API, а не
пост-фактум разбор логов.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

OWNER_TELEGRAM_ID = 895655
_COOLDOWN_SECONDS = 6 * 3600  # не чаще раза в 6 часов на один alert_key
_STATE_PATH = Path(__file__).resolve().parents[2] / "data" / "cache" / "owner_alerts.json"


def _load_state() -> dict:
    try:
        return json.loads(_STATE_PATH.read_text(encoding="utf-8")) if _STATE_PATH.exists() else {}
    except Exception:
        return {}


def _should_send(alert_key: str) -> bool:
    last = _load_state().get(alert_key, 0)
    return (time.time() - last) > _COOLDOWN_SECONDS


def _mark_sent(alert_key: str) -> None:
    state = _load_state()
    state[alert_key] = time.time()
    _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _STATE_PATH.write_text(json.dumps(state), encoding="utf-8")


def notify_owner(alert_key: str, text: str) -> None:
    """Шлёт владельцу сообщение в Telegram, не чаще раза в 6 часов на alert_key.

    Best-effort: любая ошибка (нет токена, сеть, диск) молча логируется —
    алерт не должен ронять основной пайплайн (LLM-вызов уже и так упал).
    """
    if not _should_send(alert_key):
        return
    try:
        from config.settings import get_settings

        token = get_settings().telegram_bot_token
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": OWNER_TELEGRAM_ID, "text": text},
            timeout=10,
        )
        _mark_sent(alert_key)
    except Exception:
        logger.exception("owner_alerts: не удалось отправить алерт %s", alert_key)


def notify_owner_low_anthropic_balance() -> None:
    notify_owner(
        "anthropic_low_balance",
        "⚠️ Botkin: у Anthropic закончился баланс API — BotkinClaw не отвечает пользователям.\n\n"
        "Пополни на platform.claude.com/dashboard → Add funds.",
    )
