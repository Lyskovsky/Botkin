"""
User configuration for Botkin bot.

Open registration: any Telegram user can sign up.
Authorization is handled via users.is_active in the database (AuthMiddleware).

Admin ID и маппинг telegram_id → ФИО (PII) НЕ хранятся в публичном репо:
- ADMIN_USER_IDS — из env BOTKIN_ADMIN_IDS (список через запятую) + legacy BOTKIN_ADMIN_ID
- KB_USERS — из приватного config/users_private.py (в .gitignore), fallback {}.
"""

import os


def _parse_admin_ids() -> set[int]:
    """Множество админ-Telegram-ID из env.

    Источник — BOTKIN_ADMIN_IDS (несколько ID через запятую/точку с запятой).
    Обратная совместимость: старая одиночная BOTKIN_ADMIN_ID тоже учитывается.
    Нечисловые/пустые токены и 0 игнорируются. Команды /block, /unblock, /users,
    /sync, /feedback_queue.
    """
    ids: set[int] = set()
    raw = os.getenv("BOTKIN_ADMIN_IDS", "") or ""
    for token in raw.replace(";", ",").split(","):
        token = token.strip()
        if token.isdigit() and int(token) != 0:
            ids.add(int(token))
    legacy = (os.getenv("BOTKIN_ADMIN_ID", "") or "").strip()
    if legacy.isdigit() and int(legacy) != 0:
        ids.add(int(legacy))
    return ids


# Множество админ-ID (источник истины). Задаётся оператором в .env стенда.
ADMIN_USER_IDS: set[int] = _parse_admin_ids()
# Основной админ (наименьший ID) — для сообщений/справки; 0 если админ не задан.
ADMIN_USER_ID: int = min(ADMIN_USER_IDS) if ADMIN_USER_IDS else 0


def is_admin(telegram_id: int) -> bool:
    """True, если telegram_id входит в список админов (BOTKIN_ADMIN_IDS + legacy)."""
    return telegram_id in ADMIN_USER_IDS


# ---------------------------------------------------------------------------
# KB user mapping — единый маппинг telegram_id → имя папки FamilyHealth.
# Реальные ФИО — PII, вынесены в приватный config/users_private.py (.gitignore).
# В публичном репо / чистом клоне маппинг пустой.
# ---------------------------------------------------------------------------

try:
    from config.users_private import KB_USERS
except ImportError:
    KB_USERS: dict[int, str] = {}
