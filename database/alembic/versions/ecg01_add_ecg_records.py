"""add ecg_records (ЭКГ с Apple Watch, метаданные)

Revision ID: ecg01
Revises: cgmfol0self01
Create Date: 2026-08-31

Канала для ЭКГ в проекте не было вовсе: Health Auto Export умеет отдавать тип
«ЭКГ», но принимать её было нечем — ни таблицы, ни парсера.

Храним только метаданные: время, длительность, средний пульс, классификацию
ритма и число точек вольтажа. Сам сигнал (~15 000 точек на 30 секунд) в базу
не пишем — без визуализации он бесполезен, а врачу его выгружают из «Здоровья»
отдельным PDF.

RLS включается так же, как в cgm0glucose01 — только на postgres (sqlite-тесты
гоняют create_all, не alembic).
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "ecg01"
down_revision: Union[str, None] = "cgmfol0self01"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "ecg_records",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("classification", sa.String(length=64), nullable=True),
        sa.Column("average_heart_rate", sa.SmallInteger(), nullable=True),
        sa.Column("duration_sec", sa.SmallInteger(), nullable=True),
        sa.Column("sampling_hz", sa.Numeric(precision=6, scale=1), nullable=True),
        sa.Column("voltage_samples", sa.Integer(), nullable=True),
        sa.Column("symptoms", sa.String(length=255), nullable=True),
        sa.Column("device", sa.String(length=64), nullable=True),
        sa.Column("source", sa.String(length=120), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.telegram_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "source", name="ecg_records_user_source_key"),
    )
    op.create_index("idx_ecg_user_recorded", "ecg_records", ["user_id", "recorded_at"], unique=False)

    # Гранты + RLS для hv_app (postgres-only). Роль hv_app создаётся в baseline.
    op.execute(
        """
        GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE ecg_records TO hv_app;
        GRANT SELECT,USAGE ON SEQUENCE ecg_records_id_seq TO hv_app;

        ALTER TABLE ecg_records ENABLE ROW LEVEL SECURITY;

        CREATE POLICY user_isolation ON ecg_records TO hv_app
            USING ((user_id = (NULLIF(current_setting('app.user_id'::text, true), ''::text))::bigint));
        """
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS user_isolation ON ecg_records;")
    op.drop_index("idx_ecg_user_recorded", table_name="ecg_records")
    op.drop_table("ecg_records")
