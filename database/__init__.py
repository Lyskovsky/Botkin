# Database connection and session management

import os
from typing import Generator
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session
from dotenv import load_dotenv

from database.models import Base

load_dotenv()

# Database URL from environment — required, no hardcoded fallback.
# Раньше тут был дефолт с паролем (репозиторий публичный) — удалён.
# Конфигурируется через .env / .env.production / переменную окружения.
DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError(
        "DATABASE_URL не задана. Укажите её через окружение (.env / .env.production). "
        "Дефолт намеренно не предусмотрен, чтобы не коммитить креды в открытый код."
    )

# Postgres session-level timeouts — защита от бесконечного зависания под локом.
# Прецедент 16.07.2026: meal-save на дев-Postgres висел ≥15с на INSERT в
# nutrition_log (INSERT ждал лок без тайм-аута; /day=0 через 11 мин, 3×).
#   • lock_timeout — прерывает ОЖИДАНИЕ лока (не время выполнения запроса),
#     чтобы INSERT падал с ошибкой за секунды, а не висел.
#   • idle_in_transaction_session_timeout — убивает зависшую
#     idle-in-transaction (частый держатель ACCESS EXCLUSIVE / очереди локов).
# НАМЕРЕННО без statement_timeout: он прибил бы длинные легитимные
# агент/дашборд-запросы. Значения — из env (дефолты ниже), чтобы ops мог
# тюнить без деплоя. Alembic поднимает свой движок (env.py::engine_from_config)
# → миграции сохраняют обычное ожидание локов, эти тайм-ауты их не касаются.
DEFAULT_LOCK_TIMEOUT_MS = 5000
DEFAULT_IDLE_TX_TIMEOUT_MS = 15000


def _build_connect_args(db_url: str) -> dict:
    """psycopg2 connect_args с session-таймаутами для postgres; {} для прочих
    (sqlite в тестах не понимает libpq `options`). Значения читаются из env на
    момент вызова — переопределяются `DB_LOCK_TIMEOUT_MS` / `DB_IDLE_TX_TIMEOUT_MS`."""
    if not db_url.startswith("postgresql"):
        return {}
    lock_ms = int(os.getenv("DB_LOCK_TIMEOUT_MS", str(DEFAULT_LOCK_TIMEOUT_MS)))
    idle_ms = int(os.getenv("DB_IDLE_TX_TIMEOUT_MS", str(DEFAULT_IDLE_TX_TIMEOUT_MS)))
    opts = f"-c lock_timeout={lock_ms} -c idle_in_transaction_session_timeout={idle_ms}"
    return {"options": opts}


# Create engine
engine = create_engine(
    DATABASE_URL,
    echo=False,  # Set to True for SQL query logging
    pool_pre_ping=True,  # Verify connections before using
    pool_size=5,
    max_overflow=10,
    connect_args=_build_connect_args(DATABASE_URL),
    # hide_parameters — приватность, НЕ косметика (#347, security-review).
    # DBAPIError.__str__ по умолчанию печатает `[SQL: …] [parameters: {…}]`, а
    # параметром INSERT в agent_conversations уезжает ВЕСЬ turn диалога —
    # диагнозы, лекарства, анализы. Любой logger.exception по такой ошибке
    # писал это в /opt/botkin/logs открытым текстом (проверено на проде
    # 26.07.2026: в трейсбеке лежал ответ агента про диагноз пациента).
    # Ценой отладочного удобства (в ошибке остаётся только текст SQL)
    # закрываем утечку медданных в логи.
    hide_parameters=True,
)

# Session factory
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def init_db():
    """Initialize database - create all tables"""
    Base.metadata.create_all(bind=engine)


def get_db() -> Generator[Session, None, None]:
    """Dependency for getting DB session"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# Export CRUD operations for easy access
from database.crud import (
    # User operations
    get_user_by_telegram_id,
    get_user_by_health_token,
    get_user_by_share_token,
    create_user,
    update_user_last_active,
    generate_health_token,
    generate_share_token,
    reset_share_token,
    # Nutrition operations
    create_nutrition_log,
    get_nutrition_logs_by_date,
    get_nutrition_logs_by_period,
    get_activity_logs_by_period,
    get_last_activity_date,
    get_nutrition_totals_by_date,
    delete_nutrition_log,
    # Weight operations
    create_weight,
    upsert_manual_weight,
    get_latest_weight,
    get_weights_by_period,
    get_weight_stats,
    # Supplement operations
    create_supplement_log,
    get_supplements_by_date,
    get_supplements_by_period,
    # Activity operations
    create_or_update_activity,
    get_activity_by_date,
    get_activities_by_period,
    get_average_activity_stats,
    # Blood test operations
    create_blood_test,
    get_latest_blood_test,
    get_blood_tests_by_period,
    get_all_blood_tests,
    create_body_measurement,
    # Verified products (#255)
    get_verified_products,
    find_verified_product,
    upsert_verified_product,
    increment_verified_product_usage,
)

__all__ = [
    "SessionLocal",
    "init_db",
    "get_db",
    "get_user_by_telegram_id",
    "get_user_by_health_token",
    "get_user_by_share_token",
    "create_user",
    "update_user_last_active",
    "generate_health_token",
    "generate_share_token",
    "reset_share_token",
    "create_nutrition_log",
    "get_nutrition_logs_by_date",
    "get_nutrition_logs_by_period",
    "get_activity_logs_by_period",
    "get_last_activity_date",
    "get_nutrition_totals_by_date",
    "delete_nutrition_log",
    "create_weight",
    "upsert_manual_weight",
    "get_latest_weight",
    "get_weights_by_period",
    "get_weight_stats",
    "create_supplement_log",
    "get_supplements_by_date",
    "get_supplements_by_period",
    "create_or_update_activity",
    "get_activity_by_date",
    "get_activities_by_period",
    "get_average_activity_stats",
    "create_blood_test",
    "get_latest_blood_test",
    "get_blood_tests_by_period",
    "get_all_blood_tests",
    "create_body_measurement",
    "get_verified_products",
    "find_verified_product",
    "upsert_verified_product",
    "increment_verified_product_usage",
]
