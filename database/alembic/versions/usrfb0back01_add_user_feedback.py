"""add user_feedback table + user_settings.feedback_opt_out (#188)

Revision ID: usrfb0back01
Revises: verprod01
Create Date: 2026-07-03

Инбокс обратной связи (Фаза 1 — захват). Один инбокс для каналов
command/agent/webapp. Nullable-поля под Фазы 2-3. user_id без FK намеренно.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "usrfb0back01"
down_revision: Union[str, None] = "verprod01"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"
    json_type = postgresql.JSONB() if is_postgres else sa.JSON()

    op.create_table(
        "user_feedback",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False, server_default=sa.text("'unspecified'")),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("agent_context", json_type, nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default=sa.text("'new'")),
        sa.Column("priority", sa.String(4), nullable=True),
        sa.Column("github_issue", sa.String(64), nullable=True),
        sa.Column("dedup_of", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notified_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("kind IN ('bug','feature','question','unspecified')", name="user_feedback_kind_check"),
        sa.CheckConstraint("source IN ('command','agent','webapp')", name="user_feedback_source_check"),
        sa.CheckConstraint(
            "status IN ('new','triaged','in_progress','done','wontfix','duplicate')",
            name="user_feedback_status_check",
        ),
    )
    op.create_index(
        "idx_user_feedback_status_created",
        "user_feedback",
        ["status", sa.text("created_at DESC")],
    )
    op.add_column(
        "user_settings",
        sa.Column("feedback_opt_out", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )


def downgrade() -> None:
    op.drop_column("user_settings", "feedback_opt_out")
    op.drop_index("idx_user_feedback_status_created", table_name="user_feedback")
    op.drop_table("user_feedback")
