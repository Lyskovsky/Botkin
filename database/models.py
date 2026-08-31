# Database Models for Botkin Multi-user

import secrets
from datetime import date, datetime, time
from typing import Optional, List
from sqlalchemy import (
    String,
    Integer,
    SmallInteger,
    BigInteger,
    Float,
    Numeric,
    Boolean,
    DateTime,
    Date,
    Time,
    JSON,
    Text,
    ARRAY,
    ForeignKey,
    UniqueConstraint,
    CheckConstraint,
    Index,
    TypeDecorator,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.sql import func


class Base(DeclarativeBase):
    """Base class for all models"""

    pass


class SafeArray(TypeDecorator):
    impl = JSON
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            return ARRAY(Text)
        return JSON()


# Тип, совместимый и с прод (PostgreSQL → JSONB), и с тестами (SQLite → JSON).
# На postgres колонка получает JSONB (зеркалит прод-схему), на sqlite — обычный JSON,
# чтобы in-memory тесты не падали (в sqlite типа JSONB нет).
JSONBCompat = JSON().with_variant(JSONB(), "postgresql")


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("cohort IN ('owner','family','early_user','external')", name="users_cohort_check"),
        CheckConstraint(
            "pack_name IN ('generic','cardiac','bariatric','female-cycle','respiratory_allergic')",
            name="users_pack_name_check",
        ),
        # На проде kb_status имеет CHECK-констрейнт ck_kb_status.
        CheckConstraint(
            "kb_status IS NULL OR kb_status IN ('shared','private','none')",
            name="ck_kb_status",
        ),
    )

    telegram_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    username: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    first_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    last_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    email: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    phone: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    # Зеркалим прод: эти колонки имеют server_default, но NULLable (NOT NULL добавим позже отдельной миграцией).
    is_active: Mapped[Optional[bool]] = mapped_column(Boolean, default=True, server_default="true", nullable=True)
    role: Mapped[Optional[str]] = mapped_column(String(50), default="user", server_default="user", nullable=True)
    registered_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=True
    )
    last_active: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    timezone: Mapped[Optional[str]] = mapped_column(
        String(50), default="Europe/Moscow", server_default="Europe/Moscow", nullable=True
    )

    # Apple Health token for API authentication
    health_token: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, unique=True)

    # Share token for public dashboard (security by obscurity)
    share_token: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, unique=True)

    # Garmin credentials (encrypted in production)
    garmin_email: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    garmin_password: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    # Biometric profile — needed for medical calculations (BMI, PhenoAge, LE8, Framingham)
    # Set once during /setup; required for panels to show meaningful scores.
    birth_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    height_cm: Mapped[Optional[int]] = mapped_column(SmallInteger, nullable=True)
    # Зеркалим прод: server_default есть, но NULLable.
    sex: Mapped[Optional[str]] = mapped_column(String(10), default="male", server_default="male", nullable=True)
    # Курительный статус для AHA Life's Essential 8.
    # Значения: "never" / "former_5plus" / "former_1to5" / "former_lt1" / "current".
    # NULL = неизвестно (LE8 покажет «нет данных»).
    # Прод хранит формальный DEFAULT NULL::character varying (это no-op: NULL и так дефолт).
    # В ORM server_default НЕ задаём — он ломает create_all на SQLite ("unrecognized token").
    # Чтобы alembic check не показывал ложный дифф по этому no-op-дефолту, он отфильтрован
    # в database/alembic/env.py через compare_server_default-хук (_compare_server_default).
    smoking_status: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)

    # Manual calorie targets (for users without Garmin)
    bmr: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # Базовый метаболизм, ккал/день
    avg_active_calories: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # Средние активные калории
    target_weight_kg: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # Целевой вес для расчёта макросов

    # Multi-user cohort / container / pack / BYOK fields (Sprint 1a)
    cohort: Mapped[str] = mapped_column(String(20), default="external", server_default="external")
    container_id: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    container_port: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    pack_name: Mapped[str] = mapped_column(String(50), default="generic", server_default="generic")
    # Per-user HS256 secret для подписи агентских JWT (BotkinClaw → tools API).
    # Генерируется автоматически на INSERT, чтобы новые юзеры всегда могли
    # запустить разговорного агента. Бэкфилл существующих: scripts/backfill_jwt_secret.py.
    jwt_secret: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, default=lambda: secrets.token_hex(32))
    # Per-user system prompt для BotkinClaw (in-process AI-агента) — см. ADR-0002.
    # Source of truth для тона/контекста. Меняется SQL'ом в users.agent_system_prompt.
    agent_system_prompt: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Согласие пользователя на доступ команды разработки к его переписке с
    # BotkinClaw (product-review: feature requests, баги, неудобства).
    # Управляется тогглом в мини-аппе. Default TRUE для текущей закрытой стадии.
    agent_review_consent: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true", nullable=False)

    # Onboarding state machine (Sprint 1a Task 9)
    # Steps: name → age → sex → height → has_garmin → done
    # Зеркалим прод: обе колонки server_default, но NULLable.
    onboarding_step: Mapped[Optional[str]] = mapped_column(
        String(30), default="done", server_default="done", nullable=True
    )
    onboarding_data: Mapped[Optional[dict]] = mapped_column(
        JSONBCompat, default=dict, server_default="{}", nullable=True
    )

    # Статус публикации knowledge_base пользователя: shared / private / none.
    # NULL = неизвестно. Прод имеет CHECK ck_kb_status (объявлен в __table_args__).
    kb_status: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)

    # Relationships
    nutrition_logs: Mapped[List["NutritionLog"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    weights: Mapped[List["Weight"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    supplements: Mapped[List["SupplementLog"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    activities: Mapped[List["ActivityLog"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    blood_tests: Mapped[List["BloodTest"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    body_measurements: Mapped[List["BodyMeasurement"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    settings: Mapped[Optional["UserSettings"]] = relationship(
        back_populates="user", uselist=False, cascade="all, delete-orphan"
    )


class NutritionLog(Base):
    __tablename__ = "nutrition_log"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "date", "meal_time", "meal_name", name="nutrition_log_user_id_date_meal_time_meal_name_key"
        ),
        Index("idx_nutrition_user_date", "user_id", "date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # Зеркалим прод: user_id NULLable.
    user_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("users.telegram_id", ondelete="CASCADE"), nullable=True
    )
    date: Mapped[date] = mapped_column(Date, nullable=False)
    meal_time: Mapped[Optional[time]] = mapped_column(Time, nullable=True)
    meal_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    items: Mapped[dict] = mapped_column(JSONBCompat, nullable=False)
    totals: Mapped[dict] = mapped_column(JSONBCompat, nullable=False)
    photo_paths: Mapped[Optional[List[str]]] = mapped_column(SafeArray, nullable=True)
    created_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=True
    )

    # Relationship
    user: Mapped["User"] = relationship(back_populates="nutrition_logs")


# UserProduct + UserProductVariant models removed Apr 2026 (/my_products feature retired).
# DB tables user_products + user_product_variants are dropped in the matching migration.


class Weight(Base):
    __tablename__ = "weights"
    __table_args__ = (
        UniqueConstraint("user_id", "measured_at", name="weights_user_id_measured_at_key"),
        Index("idx_weights_user_date", "user_id", "measured_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # Зеркалим прод: user_id NULLable.
    user_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("users.telegram_id", ondelete="CASCADE"), nullable=True
    )
    measured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    weight: Mapped[float] = mapped_column(Float, nullable=False)
    body_fat: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    muscle_mass: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    water: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    bmi: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    visceral_fat: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    bone_mass: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    source: Mapped[Optional[str]] = mapped_column(
        String(50), nullable=True
    )  # 'apple_health', 'zepp', 'manual', 'screenshot_ocr'

    # Relationship
    user: Mapped["User"] = relationship(back_populates="weights")


class SupplementLog(Base):
    __tablename__ = "supplements_log"
    __table_args__ = (Index("idx_supplements_user_date", "user_id", "date"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # Зеркалим прод: user_id NULLable.
    user_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("users.telegram_id", ondelete="CASCADE"), nullable=True
    )
    date: Mapped[date] = mapped_column(Date, nullable=False)
    time: Mapped[Optional[time]] = mapped_column(Time, nullable=True)
    supplement_name: Mapped[str] = mapped_column(String(255), nullable=False)
    dosage: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    created_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=True
    )

    # Relationship
    user: Mapped["User"] = relationship(back_populates="supplements")


class ActivityLog(Base):
    __tablename__ = "activity_log"
    __table_args__ = (
        UniqueConstraint("user_id", "date", name="activity_log_user_id_date_key"),
        Index("idx_activity_user_date", "user_id", "date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # Зеркалим прод: user_id NULLable.
    user_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("users.telegram_id", ondelete="CASCADE"), nullable=True
    )
    date: Mapped[date] = mapped_column(Date, nullable=False)
    steps: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    active_calories: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    total_calories: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    bmr_calories: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    distance_km: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    sleep_hours: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    heart_rate_avg: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    hrv: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    stress_level: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # Зеркалим прод: server_default есть, но NULLable.
    source: Mapped[Optional[str]] = mapped_column(
        String(50), default="apple_health", server_default="apple_health", nullable=True
    )
    raw_data: Mapped[Optional[dict]] = mapped_column(
        JSONBCompat, nullable=True
    )  # For storing full Garmin/Apple Health payload
    synced_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=True
    )

    # Relationship
    user: Mapped["User"] = relationship(back_populates="activities")


class BloodTest(Base):
    __tablename__ = "blood_tests"
    __table_args__ = (
        Index("idx_blood_tests_user_date", "user_id", "test_date"),
        # Уникальный индекс с прода (анализ за дату+тип у пользователя уникален).
        Index("blood_tests_user_date_type_unique", "user_id", "test_date", "test_type", unique=True),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # Зеркалим прод: user_id NULLable.
    user_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("users.telegram_id", ondelete="CASCADE"), nullable=True
    )
    test_date: Mapped[date] = mapped_column(Date, nullable=False)
    test_type: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)  # "Биохимия", "Гормоны"
    values: Mapped[dict] = mapped_column(JSONBCompat, nullable=False)  # {"cholesterol": 5.66, "LDL": 3.2, ...}
    file_path: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # Path to PDF file
    # Зеркалим прод: server_default есть, но NULLable.
    status: Mapped[Optional[str]] = mapped_column(
        String(50), default="current", server_default="current", nullable=True
    )  # current, historical
    created_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=True
    )

    # Relationship
    user: Mapped["User"] = relationship(back_populates="blood_tests")


class BodyMeasurement(Base):
    __tablename__ = "body_measurements"
    # На проде индекс idx_measurements_user_date построен по (user_id, measured_at).
    __table_args__ = (Index("idx_measurements_user_date", "user_id", "measured_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # Зеркалим прод: user_id NULLable.
    user_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("users.telegram_id", ondelete="CASCADE"), nullable=True
    )
    # Прод: measured_at timestamptz DEFAULT now(), NULLable.
    measured_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=True
    )
    date: Mapped[date] = mapped_column(Date, nullable=False)
    waist_cm: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    neck_cm: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    hips_cm: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    chest_cm: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    thigh_cm: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    biceps_cm: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    grip_right_kg: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    grip_left_kg: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=True
    )

    # Relationship
    user: Mapped["User"] = relationship(back_populates="body_measurements")


class UserSettings(Base):
    __tablename__ = "user_settings"

    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.telegram_id", ondelete="CASCADE"), primary_key=True
    )
    show_calorie_budget_bar: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    # BMR resolution mode:
    #   'auto'   — use Garmin → Apple Health → default (live data from wearables)
    #   'manual' — use bmr_override + activity_avg_override (user-entered, Mifflin-St Jeor)
    bmr_source: Mapped[str] = mapped_column(String(10), default="auto", server_default="auto")
    bmr_override: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    activity_avg_override: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # Activity level for Mifflin-St Jeor PAL multiplier (sedentary/light/moderate/high)
    activity_level: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    target_weight_kg: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    target_weight_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    # Calorie goal: signed % relative to maintenance.
    # -15 = 15% deficit (lose weight), 0 = maintenance, +10 = 10% surplus (gain).
    calorie_goal_pct: Mapped[int] = mapped_column(Integer, default=-15, server_default="-15")
    supplement_reminders_enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    feedback_opt_out: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    supplement_reminder_time: Mapped[time] = mapped_column(Time, server_default="08:00:00")
    supplements: Mapped[list] = mapped_column(JSONBCompat, default=list, server_default="[]")
    # Meal-logging reminders (opt-in). Fixed daily slots in the user's local timezone.
    #   meal_reminder_times:    {"Завтрак": "11:00", "Обед": "14:30", "Ужин": "22:00"}  (label -> "HH:MM")
    #   meal_reminder_last_sent:{"Завтрак": "2026-06-28", ...}  (label -> ISO date, idempotency per slot/day)
    meal_reminders_enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    meal_reminder_times: Mapped[dict] = mapped_column(JSONBCompat, default=dict, server_default="{}")
    meal_reminder_last_sent: Mapped[dict] = mapped_column(JSONBCompat, default=dict, server_default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    user: Mapped["User"] = relationship(back_populates="settings")


class AgentConversation(Base):
    """История диалога BotkinClaw (in-process AI-агент). Зеркалит прод-таблицу."""

    __tablename__ = "agent_conversations"
    __table_args__ = (
        CheckConstraint(
            "role IN ('user','assistant','tool_use','tool_result')",
            name="agent_conversations_role_check",
        ),
        # На проде created_at DESC.
        Index("idx_agent_conv_user_created", "user_id", text("created_at DESC")),
        # Частичный индекс с прода: только строки, где source IS NOT NULL.
        Index("idx_agent_conv_source", "source", postgresql_where=text("source IS NOT NULL")),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    role: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[dict] = mapped_column(JSONBCompat, nullable=False)
    tool_use_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    source: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class FoodInteraction(Base):
    """Наблюдаемость пищевого pipeline (#193).

    Пишется В ДОПОЛНЕНИЕ к nutrition_log (не вместо): сохраняет сырое сообщение
    пользователя, что бот распознал (состав/БЖУ/ккал до подтверждения), ответ бота
    и связь с итоговой записью nutrition_log + статус. Позволяет ретро-аудит
    «что прислал → что распознал → что ответил → что записалось» (см.
    core.food.interaction_log.log_food_interaction).

    nutrition_log_id — БЕЗ FK намеренно: аудит-след должен переживать удаление/
    правку самой записи еды (status='cancelled'/'edited').
    """

    __tablename__ = "food_interactions"
    __table_args__ = (
        CheckConstraint(
            "source IN ('text','photo','voice')",
            name="food_interactions_source_check",
        ),
        CheckConstraint(
            "status IN ('saved','cancelled','edited')",
            name="food_interactions_status_check",
        ),
        Index("idx_food_inter_user_created", "user_id", text("created_at DESC")),
    )

    # BIGINT на проде (высоконаполняемая таблица), но на SQLite автоинкремент
    # работает только у INTEGER PRIMARY KEY (rowid) — отсюда with_variant.
    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    raw_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    media_path: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    recognized: Mapped[Optional[dict]] = mapped_column(JSONBCompat, nullable=True)
    bot_reply: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    nutrition_log_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'saved'"))


class UserFeedback(Base):
    """Инбокс обратной связи (#188, Фаза 1 — захват).

    Один инбокс для всех каналов: /feedback (command), агент (flag_for_devs),
    и позже кнопка мини-аппа (webapp). Nullable-поля priority/github_issue/
    dedup_of/resolved_at/notified_at заведены под Фазы 2-3, Фаза 1 их не пишет.
    """

    __tablename__ = "user_feedback"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('bug','feature','question','unspecified')",
            name="user_feedback_kind_check",
        ),
        CheckConstraint(
            "source IN ('command','agent','webapp')",
            name="user_feedback_source_check",
        ),
        CheckConstraint(
            "status IN ('new','triaged','in_progress','done','wontfix','duplicate')",
            name="user_feedback_status_check",
        ),
        Index("idx_user_feedback_status_created", "status", text("created_at DESC")),
    )

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    # user_id — без hard-FK на users.telegram_id намеренно: аудит-след переживает удаление юзера.
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False, server_default="unspecified")
    text: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    agent_context: Mapped[Optional[dict]] = mapped_column(JSONBCompat, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="new")
    priority: Mapped[Optional[str]] = mapped_column(String(4), nullable=True)
    github_issue: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    dedup_of: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    notified_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class VerifiedProduct(Base):
    """Справочник проверенных продуктов (#255).

    Точные КБЖУ с этикетки для упакованных продуктов — чтобы LLM-vision не
    оценивал один и тот же батончик заново (и с ошибками) при каждом фото.
    user_id NULL = общая запись, видимая всем пользователям; иначе — личная.

    Наполняется автоматически (кнопка «Запомнить продукт» после исправления
    КБЖУ или фото этикетки) и сид-скриптом. Ручного CRUD нет намеренно:
    предыдущая инкарнация /my_products умерла с 0 строк за всё время жизни
    (удалена 2026-04-21, см. AI_CHANGELOG).

    name_norm — нормализованное имя (core.food.verified_products.
    normalize_product_name), поддерживается приложением. Уникальность — два
    частичных индекса: (user_id, name_norm) для личных записей и (name_norm)
    для общих; обычный UNIQUE не работает из-за NULL в user_id.
    """

    __tablename__ = "verified_products"
    __table_args__ = (
        CheckConstraint(
            "source IN ('user_correction','label_photo','manual','import')",
            name="verified_products_source_check",
        ),
        Index(
            "uq_verified_products_user_name",
            "user_id",
            "name_norm",
            unique=True,
            postgresql_where=text("user_id IS NOT NULL"),
            sqlite_where=text("user_id IS NOT NULL"),
        ),
        Index(
            "uq_verified_products_global_name",
            "name_norm",
            unique=True,
            postgresql_where=text("user_id IS NULL"),
            sqlite_where=text("user_id IS NULL"),
        ),
        Index("idx_verified_products_barcode", "barcode"),
    )

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    user_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("users.telegram_id", ondelete="CASCADE"), nullable=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    name_norm: Mapped[str] = mapped_column(String(255), nullable=False)
    brand: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    aliases: Mapped[Optional[list]] = mapped_column(JSONBCompat, nullable=True)
    barcode: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    calories_per_100g: Mapped[float] = mapped_column(Float, nullable=False)
    protein_per_100g: Mapped[float] = mapped_column(Float, nullable=False)
    fats_per_100g: Mapped[float] = mapped_column(Float, nullable=False)
    carbs_per_100g: Mapped[float] = mapped_column(Float, nullable=False)
    fiber_per_100g: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    portion_g: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    times_used: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class AuditLog(Base):
    """Аудит доступа к данным (наполняется DB-триггером audit_admin_access). Зеркалит прод."""

    __tablename__ = "audit_log"
    __table_args__ = (
        Index("idx_audit_ts", text("ts DESC")),
        Index("idx_audit_user_table", "db_user", "table_name"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    db_user: Mapped[str] = mapped_column(Text, nullable=False)
    query_type: Mapped[str] = mapped_column(Text, nullable=False)
    table_name: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    query_excerpt: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class BloodPressureLog(Base):
    """Замеры артериального давления (Omron → Apple Health). Зеркалит прод-таблицу."""

    __tablename__ = "blood_pressure_logs"
    __table_args__ = (
        UniqueConstraint("user_id", "measured_at", name="blood_pressure_logs_user_id_measured_at_key"),
        Index("idx_bp_user_date", "user_id", text("measured_at DESC")),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.telegram_id", ondelete="CASCADE"), nullable=False
    )
    measured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    systolic: Mapped[int] = mapped_column(Integer, nullable=False)
    diastolic: Mapped[int] = mapped_column(Integer, nullable=False)
    heart_rate: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    source: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    created_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=True
    )


class DailySummary(Base):
    """Дневные агрегаты (orphan-таблица: пуста на проде, не управляется бизнес-логикой). Зеркалит прод."""

    __tablename__ = "daily_summaries"
    __table_args__ = (
        UniqueConstraint("user_id", "date", name="daily_summaries_user_id_date_key"),
        Index("idx_summaries_user_date", "user_id", text("date DESC")),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.telegram_id", ondelete="CASCADE"), nullable=False
    )
    date: Mapped[date] = mapped_column(Date, nullable=False)
    total_calories: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    total_protein: Mapped[Optional[float]] = mapped_column(Numeric(6, 2), nullable=True)
    total_fats: Mapped[Optional[float]] = mapped_column(Numeric(6, 2), nullable=True)
    total_carbs: Mapped[Optional[float]] = mapped_column(Numeric(6, 2), nullable=True)
    had_workout: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    sleep_hours: Mapped[Optional[float]] = mapped_column(Numeric(4, 2), nullable=True)
    weight: Mapped[Optional[float]] = mapped_column(Numeric(5, 2), nullable=True)
    bp_systolic: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    bp_diastolic: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    created_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=True
    )


class LLMUsageLog(Base):
    """Учёт расхода токенов/стоимости LLM-вызовов. Зеркалит прод-таблицу."""

    __tablename__ = "llm_usage_log"
    __table_args__ = (
        Index("idx_llm_usage_created", text("created_at DESC")),
        Index("idx_llm_usage_purpose", "purpose", text("created_at DESC")),
        Index("idx_llm_usage_user", "user_id", text("created_at DESC")),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    purpose: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str] = mapped_column(Text, nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    output_tokens: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    cache_creation_tokens: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    cache_read_tokens: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    cost_usd: Mapped[float] = mapped_column(Numeric(10, 6), server_default="0", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class SleepRecord(Base):
    """Записи сна (orphan-таблица: пуста на проде, не управляется бизнес-логикой). Зеркалит прод."""

    __tablename__ = "sleep_records"
    __table_args__ = (Index("idx_sleep_user_date", "user_id", text("date DESC")),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.telegram_id", ondelete="CASCADE"), nullable=False
    )
    date: Mapped[date] = mapped_column(Date, nullable=False)
    sleep_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    sleep_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    duration_hours: Mapped[Optional[float]] = mapped_column(Numeric(4, 2), nullable=True)
    quality_score: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    deep_sleep_minutes: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    rem_sleep_minutes: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    light_sleep_minutes: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    awake_minutes: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    source: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    created_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=True
    )


class Workout(Base):
    """Тренировки (пишутся raw-SQL путями apple_health/agent_tools_api). Зеркалит прод-таблицу."""

    __tablename__ = "workouts"
    __table_args__ = (Index("idx_workouts_user_date", "user_id", text("date DESC")),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.telegram_id", ondelete="CASCADE"), nullable=False
    )
    date: Mapped[date] = mapped_column(Date, nullable=False)
    workout_type: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    duration_minutes: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    start_time: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    end_time: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    calories_burned: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    source: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    created_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=True
    )
    distance_km: Mapped[Optional[float]] = mapped_column(Numeric(8, 3), nullable=True)


class CgmConnection(Base):
    """Маппинг follower LibreLinkUp patient_id → пользователь Botkin (CGM-глюкоза, #96).

    Один сервисный follower-аккаунт (dr@botkin.health) видит всех, кто его пригласил;
    эта таблица связывает каждого patient_id из get_patients() с telegram_id юзера.
    """

    __tablename__ = "cgm_connections"

    # BigInteger PK; на sqlite (тесты) рендерим как Integer, иначе sqlite не автоинкрементит.
    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer(), "sqlite"), primary_key=True, autoincrement=True
    )
    patient_id: Mapped[str] = mapped_column(String(36), nullable=False, unique=True)
    telegram_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.telegram_id", ondelete="CASCADE"), nullable=False
    )
    connected_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=True
    )


class CgmFollower(Base):
    """Креды follower-аккаунта LibreLinkUp, введённые самим пользователем (#381).

    Зачем отдельная таблица, а не колонки в cgm_connections: приглашение follower'а
    в LibreLinkUp работает только ВНУТРИ региона (ограничение Abbott), поэтому на
    каждый регион нужен свой аккаунт, а один такой аккаунт может наблюдать сразу
    несколько пациентов — связь «один follower → много patient_id».

    Пароль хранится ТОЛЬКО зашифрованным (core.infra.secrets.encrypt_secret);
    имя колонки password_enc об этом и говорит. Ключ живёт в env SECRETS_KEY,
    то есть на другом носителе, чем БД.
    """

    __tablename__ = "cgm_followers"

    # BigInteger PK; на sqlite (тесты) рендерим как Integer — иначе не автоинкрементит.
    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer(), "sqlite"), primary_key=True, autoincrement=True
    )
    owner_user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.telegram_id", ondelete="CASCADE"), nullable=False
    )
    region: Mapped[str] = mapped_column(String(8), nullable=False)
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    password_enc: Mapped[str] = mapped_column(Text, nullable=False)
    label: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    created_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=True
    )
    # Диагностика: когда последний раз логин прошёл и чем упал последний неудачный.
    last_ok_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Отзыв без удаления строки — симметрично PersonalAccessToken.revoked_at.
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("region", "email", name="cgm_followers_region_email_key"),
        Index("idx_cgm_followers_region", "region"),
        Index("idx_cgm_followers_owner", "owner_user_id"),
    )


class GlucoseReading(Base):
    """Точки глюкозы CGM (Abbott FreeStyle Libre 3 → LibreLinkUp API, #96)."""

    __tablename__ = "glucose_readings"
    __table_args__ = (
        UniqueConstraint("user_id", "ts", name="glucose_readings_user_id_ts_key"),
        Index("idx_glucose_user_ts", "user_id", text("ts DESC")),
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer(), "sqlite"), primary_key=True, autoincrement=True
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.telegram_id", ondelete="CASCADE"), nullable=False
    )
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    value: Mapped[float] = mapped_column(Numeric(5, 2), nullable=False)  # mmol/L
    trend: Mapped[Optional[int]] = mapped_column(SmallInteger, nullable=True)  # 0-5, LibreLinkUp trend arrow
    source: Mapped[Optional[str]] = mapped_column(
        String(50), default="librelinkup", server_default="librelinkup", nullable=True
    )
    raw: Mapped[Optional[dict]] = mapped_column(JSONBCompat, nullable=True)
    created_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=True
    )


class HealthReport(Base):
    """HTML-отчёт пользователя, доступный по публичному токену GET /r/{token}."""

    __tablename__ = "health_reports"
    __table_args__ = (
        Index("ix_health_reports_user_id", "user_id"),
        Index("ix_health_reports_token", "token", unique=True),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.telegram_id", ondelete="CASCADE"), nullable=False
    )
    token: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    html: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class PersonalAccessToken(Base):
    """Долгоживущий PAT для MCP-коннектора Claude Desktop (#228).

    Пользователь сам выпускает токен в боте/мини-аппе (self-service, без ручной
    выдачи Александром). Коннектор обменивает его на короткоживущий JWT через
    POST /api/agent/exchange_pat_for_jwt, дальше дёргает существующие /api/agent/*.

    Формат токена: ``pat_<telegram_id>_<hex32>`` (зеркалит ``hvt_`` Apple Health).
    Scope:
      • ``rw`` — личный токен владельца (чтение + запись);
      • ``ro`` — read-only, эту строку владелец отдаёт врачу/близкому, чтобы
        поделиться своими данными без права что-либо менять.
    Отзыв — выставлением ``revoked_at`` (старый токен сразу перестаёт работать).
    """

    __tablename__ = "personal_access_tokens"
    __table_args__ = (
        CheckConstraint("scope IN ('ro', 'rw')", name="personal_access_tokens_scope_check"),
        Index("ix_personal_access_tokens_token", "token", unique=True),
        Index("ix_personal_access_tokens_user_id", "user_id"),
    )

    # BigInteger PK; на sqlite (тесты) рендерим как Integer, иначе нет автоинкремента.
    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer(), "sqlite"), primary_key=True, autoincrement=True
    )
    # Чьи данные открывает токен. FK на users.telegram_id (PK таблицы users), НЕ на users.id.
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.telegram_id", ondelete="CASCADE"), nullable=False
    )
    # Уникальность обеспечивает явный индекс ix_personal_access_tokens_token (см. __table_args__),
    # который зеркалит миграцию pat0token01. Column-level unique=True здесь НЕ ставим — иначе
    # SQLAlchemy добавит вдобавок безымянный UniqueConstraint, которого нет в миграции, и alembic
    # check ловит расхождение ORM↔схема ("New upgrade operations detected: add UniqueConstraint(token)").
    token: Mapped[str] = mapped_column(String(128), nullable=False)
    # Человекочитаемая метка («Мой ноут», «Психолог Ника») — задаёт пользователь.
    name: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    scope: Mapped[str] = mapped_column(String(2), default="rw", server_default="rw", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    last_used_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    # Аудит: кто выпустил токен (telegram_id). В self-service-потоке == user_id.
    created_by_user: Mapped[int] = mapped_column(BigInteger, nullable=False)

    @property
    def is_active(self) -> bool:
        """Токен действителен, пока не отозван."""
        return self.revoked_at is None


class FunnelEvent(Base):
    """Событие продуктовой воронки онбординга/активации.

    Отдельно от audit_log (тот про админ-DML). user_id без FK намеренно
    (событие может опережать полную запись юзера). RLS (агент не читает) —
    в миграции, не в ORM.
    """

    __tablename__ = "funnel_events"

    # BigInteger PK; на sqlite (тесты) рендерим как Integer, иначе нет автоинкремента
    # (см. тот же приём у PersonalAccessToken.id выше).
    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer(), "sqlite"), primary_key=True, autoincrement=True
    )
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    event: Mapped[str] = mapped_column(String(40), nullable=False)
    track: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)
    source: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    meta: Mapped[Optional[dict]] = mapped_column(JSONBCompat, default=dict)

    __table_args__ = (
        Index("idx_funnel_event_ts", "event", text("ts DESC")),
        Index(
            "idx_funnel_once",
            "user_id",
            "event",
            unique=True,
            sqlite_where=text("event IN ('first_food_logged','first_agent_question')"),
            postgresql_where=text("event IN ('first_food_logged','first_agent_question')"),
        ),
    )


_ONCE_EVENTS = {"first_food_logged", "first_agent_question"}


def log_event(db, user_id, event, track=None, source=None, meta=None, once=False):
    """Записать событие воронки. once=True (или «первое» событие) → идемпотентно
    (полагается на partial-unique-индекс idx_funnel_once; дубликат гасится).
    Коммит — на вызывающей стороне."""
    from sqlalchemy.exc import IntegrityError

    ev = FunnelEvent(user_id=user_id, event=event, track=track, source=source, meta=meta or {})
    if once or event in _ONCE_EVENTS:
        # db.add() must happen INSIDE the SAVEPOINT: adding beforehand and
        # only wrapping flush() leaves the pending object attached to the
        # outer transaction, so a failed flush marks the *whole* session
        # transaction deactive (not just the savepoint) — verified empirically,
        # matches the pattern in SQLAlchemy's own begin_nested() docs.
        try:
            with db.begin_nested():
                db.add(ev)
                db.flush()
        except IntegrityError:
            pass
    else:
        db.add(ev)
    return ev
