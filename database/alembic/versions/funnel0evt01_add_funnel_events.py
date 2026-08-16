"""add funnel_events table (onboarding/activation analytics, #8)

Revision ID: funnel0evt01
Revises: grip0stren01
Create Date: 2026-07-15

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "funnel0evt01"
down_revision: Union[str, None] = "grip0stren01"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"
    json_type = postgresql.JSONB() if is_postgres else sa.JSON()

    op.create_table(
        "funnel_events",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), primary_key=True, autoincrement=True),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("event", sa.String(40), nullable=False),
        sa.Column("track", sa.String(8), nullable=True),
        sa.Column("source", sa.String(64), nullable=True),
        sa.Column("meta", json_type, nullable=True),
    )
    # ix_funnel_events_user_id — зеркалит index=True на FunnelEvent.user_id (модель).
    op.create_index("ix_funnel_events_user_id", "funnel_events", ["user_id"])
    op.create_index("idx_funnel_event_ts", "funnel_events", ["event", sa.text("ts DESC")])
    op.create_index(
        "idx_funnel_once",
        "funnel_events",
        ["user_id", "event"],
        unique=True,
        postgresql_where=sa.text("event IN ('first_food_logged','first_agent_question')"),
    )

    if is_postgres:
        op.execute("ALTER TABLE funnel_events ENABLE ROW LEVEL SECURITY")
        op.execute("CREATE POLICY funnel_admin_only ON funnel_events FOR ALL TO hv_app USING (FALSE)")


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("DROP POLICY IF EXISTS funnel_admin_only ON funnel_events")
    op.drop_index("idx_funnel_once", table_name="funnel_events")
    op.drop_index("idx_funnel_event_ts", table_name="funnel_events")
    op.drop_index("ix_funnel_events_user_id", table_name="funnel_events")
    op.drop_table("funnel_events")
