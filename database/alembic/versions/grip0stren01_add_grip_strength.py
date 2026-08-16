"""add grip strength columns to body_measurements

Revision ID: grip0stren01
Revises: usrfb0back01
Create Date: 2026-07-04

Сила хвата (динамометрия) — маркер саркопении/функциональной силы.
Ручной ввод через бот/админку, как и остальная антропометрия в этой таблице.

IF NOT EXISTS: колонки уже накачены на prod вручную 04.07.2026 до релиза
кода (нужны были срочно для записи первого замера) — upgrade() должен быть
безопасен и при повторном прогоне alembic upgrade head на этом стенде.
"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "grip0stren01"
down_revision: Union[str, None] = "usrfb0back01"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE body_measurements ADD COLUMN IF NOT EXISTS grip_right_kg double precision")
    op.execute("ALTER TABLE body_measurements ADD COLUMN IF NOT EXISTS grip_left_kg double precision")


def downgrade() -> None:
    op.execute("ALTER TABLE body_measurements DROP COLUMN IF EXISTS grip_left_kg")
    op.execute("ALTER TABLE body_measurements DROP COLUMN IF EXISTS grip_right_kg")
