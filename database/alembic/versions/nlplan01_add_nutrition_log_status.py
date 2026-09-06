"""nutrition_log.status — план vs факт (#407)

Revision ID: nlplan01
Revises: wtpulse01
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "nlplan01"
down_revision: Union[str, None] = "wtpulse01"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("nutrition_log", sa.Column("status", sa.String(length=16), nullable=False, server_default="eaten"))
    op.create_index("idx_nutrition_user_date_status", "nutrition_log", ["user_id", "date", "status"])


def downgrade() -> None:
    op.drop_index("idx_nutrition_user_date_status", table_name="nutrition_log")
    op.drop_column("nutrition_log", "status")
