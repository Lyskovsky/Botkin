"""add meal reminder fields to user_settings (#229)

Revision ID: meal0remind01
Revises: food0inter01
Create Date: 2026-06-28

Напоминания о логировании еды (opt-in). Фиксированные слоты в локальной TZ
пользователя (режим Романовой: Завтрак 11:00, Обед 14:30, Ужин 22:00).
  meal_reminders_enabled   — тумблер
  meal_reminder_times      — {label: 'HH:MM'} (JSON)
  meal_reminder_last_sent  — {label: 'YYYY-MM-DD'} идемпотентность слот/день (JSON)
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "meal0remind01"
down_revision: Union[str, None] = "funnel0evt01"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"
    json_type = postgresql.JSONB() if is_postgres else sa.JSON()
    json_default = sa.text("'{}'::jsonb") if is_postgres else sa.text("'{}'")

    op.add_column(
        "user_settings",
        sa.Column(
            "meal_reminders_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "user_settings",
        sa.Column("meal_reminder_times", json_type, nullable=False, server_default=json_default),
    )
    op.add_column(
        "user_settings",
        sa.Column("meal_reminder_last_sent", json_type, nullable=False, server_default=json_default),
    )


def downgrade() -> None:
    op.drop_column("user_settings", "meal_reminder_last_sent")
    op.drop_column("user_settings", "meal_reminder_times")
    op.drop_column("user_settings", "meal_reminders_enabled")
