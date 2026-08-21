"""add cgm_followers (self-service follower-аккаунты CGM, #381)

Revision ID: cgmfol0self01
Revises: meal0remind01
Create Date: 2026-08-21

Таблица под самообслуживание CGM: пользователь вводит креды своего
follower-аккаунта LibreLinkUp в /connect_cgm, а не передаёт их администратору
для правки прод-.env (см. docs/researches/2026-08-17-cgm-follower-self-service.md).

Пароль пишется только зашифрованным (core.infra.secrets, env SECRETS_KEY) —
колонка называется password_enc, чтобы plaintext туда не заехал незамеченным.

RLS включается так же, как в cgm0glucose01 — только на postgres
(sqlite-тесты гоняют create_all, не alembic-миграции).
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "cgmfol0self01"
down_revision: Union[str, None] = "meal0remind01"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "cgm_followers",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("owner_user_id", sa.BigInteger(), nullable=False),
        sa.Column("region", sa.String(length=8), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("password_enc", sa.Text(), nullable=False),
        sa.Column("label", sa.String(length=100), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=True),
        sa.Column("last_ok_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.telegram_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        # Один и тот же аккаунт в одном регионе не заводим дважды: лишние логины
        # одним email — прямой путь к Cloudflare-бану 476 (см. #135/#139/#141).
        sa.UniqueConstraint("region", "email", name="cgm_followers_region_email_key"),
    )
    op.create_index("idx_cgm_followers_region", "cgm_followers", ["region"], unique=False)
    op.create_index("idx_cgm_followers_owner", "cgm_followers", ["owner_user_id"], unique=False)

    # Гранты + RLS для hv_app (postgres-only). Роль hv_app создаётся в baseline-ревизии.
    # Ночной импортёр (scripts/import/librelinkup.py) ходит под владельцем БД по
    # DATABASE_URL и app.user_id не выставляет — RLS его не касается, как и для
    # cgm_connections; политика защищает доступ со стороны бота/API.
    op.execute(
        """
        GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE cgm_followers TO hv_app;
        GRANT SELECT,USAGE ON SEQUENCE cgm_followers_id_seq TO hv_app;

        ALTER TABLE cgm_followers ENABLE ROW LEVEL SECURITY;

        -- Владелец записи — тот, кто её создал; колонка owner_user_id ссылается
        -- на users.telegram_id (тот же домен, что app.user_id).
        CREATE POLICY user_isolation ON cgm_followers TO hv_app
            USING ((owner_user_id = (NULLIF(current_setting('app.user_id'::text, true), ''::text))::bigint));
        """
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS user_isolation ON cgm_followers;")
    op.drop_index("idx_cgm_followers_owner", table_name="cgm_followers")
    op.drop_index("idx_cgm_followers_region", table_name="cgm_followers")
    op.drop_table("cgm_followers")
