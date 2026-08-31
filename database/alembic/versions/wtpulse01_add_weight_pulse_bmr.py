"""add weights.heart_rate / bmr_kcal / fat_mass_kg / lean_mass_kg

Revision ID: wtpulse01
Revises: hrevt01
Create Date: 2026-08-31

Весы Withings отдают 11 величин, а канал забирал 6. Особенно важен пульс: он
измеряется стоя и натощак, то есть по сути пульс покоя. За 16–28.08 весы
зафиксировали 22 измерения, 13 из них выше 100 уд/мин с максимумом 127 — при
том что по данным часов тахикардия выглядела эпизодической (6 событий за двое
суток). Эти значения лежали в аккаунте Withings и никуда не попадали.

Плюс основной обмен по составу тела (точнее оценки Apple), жировая и
безжировая масса в килограммах — раньше в базе был только процент жира.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "wtpulse01"
down_revision: Union[str, None] = "hrevt01"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("weights", sa.Column("heart_rate", sa.SmallInteger(), nullable=True))
    op.add_column("weights", sa.Column("bmr_kcal", sa.SmallInteger(), nullable=True))
    op.add_column("weights", sa.Column("fat_mass_kg", sa.Float(), nullable=True))
    op.add_column("weights", sa.Column("lean_mass_kg", sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column("weights", "lean_mass_kg")
    op.drop_column("weights", "fat_mass_kg")
    op.drop_column("weights", "bmr_kcal")
    op.drop_column("weights", "heart_rate")
