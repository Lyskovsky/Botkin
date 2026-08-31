"""add heart_rate_events (уведомления Apple Watch о пульсе вне нормы)

Revision ID: hrevt01
Revises: ecg01
Create Date: 2026-08-31

Канала для этих событий не было: 31.08.2026 три эпизода тахикардии покоя за одно
утро (11:21 до 110, 11:39 до 105, 12:29 до 107) остались только на экране
телефона. Сама ЭКГ, снятая по их поводу, в базу попала, а события — нет.

Нужны, чтобы сопоставлять эпизоды с гликемией: 20.08 такой подъём пульса
совпал по минутам со снижением глюкозы до 3,10 ммоль/л.

Сырых сэмплов пульса не храним — только метаданные события.
RLS включается как в cgm0glucose01 (postgres-only).
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "hrevt01"
down_revision: Union[str, None] = "ecg01"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "heart_rate_events",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("event_type", sa.String(length=32), nullable=True),
        sa.Column("threshold_bpm", sa.SmallInteger(), nullable=True),
        sa.Column("min_bpm", sa.SmallInteger(), nullable=True),
        sa.Column("max_bpm", sa.SmallInteger(), nullable=True),
        sa.Column("avg_bpm", sa.SmallInteger(), nullable=True),
        sa.Column("duration_min", sa.SmallInteger(), nullable=True),
        sa.Column("device", sa.String(length=64), nullable=True),
        sa.Column("source", sa.String(length=120), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.telegram_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "source", name="heart_rate_events_user_source_key"),
    )
    op.create_index("idx_hr_events_user_started", "heart_rate_events", ["user_id", "started_at"], unique=False)

    op.execute(
        """
        GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE heart_rate_events TO hv_app;
        GRANT SELECT,USAGE ON SEQUENCE heart_rate_events_id_seq TO hv_app;

        ALTER TABLE heart_rate_events ENABLE ROW LEVEL SECURITY;

        CREATE POLICY user_isolation ON heart_rate_events TO hv_app
            USING ((user_id = (NULLIF(current_setting('app.user_id'::text, true), ''::text))::bigint));
        """
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS user_isolation ON heart_rate_events;")
    op.drop_index("idx_hr_events_user_started", table_name="heart_rate_events")
    op.drop_table("heart_rate_events")
