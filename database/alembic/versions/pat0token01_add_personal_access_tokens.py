"""add personal_access_tokens (PAT для MCP-коннектора Claude Desktop, #228)

Revision ID: pat0token01
Revises: food0inter01
Create Date: 2026-06-28

Долгоживущие PAT, которые пользователь сам выпускает в боте/мини-аппе. Коннектор
обменивает их на короткоживущий JWT через POST /api/agent/exchange_pat_for_jwt
(Фаза 2), дальше дёргает существующие /api/agent/*.

⚠️ RLS на этой таблице НЕ включаем — сознательно. Публичный exchange-эндпоинт ищет
строку `WHERE token = :token` ДО того как известен пользователь (app.user_id ещё не
выставлен). Включённый RLS `USING (user_id = app.user_id)` вернул бы 0 строк и сломал
обмен. Это тот же trust-model, что у `users.health_token`: таблица `users` в baseline
тоже без RLS, lookup `hvt_`-токена работает так же. Сам токен — bearer-capability;
изоляция данных обеспечивается RLS на nutrition_log/weights/… (app.user_id из JWT,
выданного уже ПОСЛЕ валидации PAT). hv_app получает только SELECT/INSERT/UPDATE
(revoke = UPDATE revoked_at, DELETE не нужен — soft-delete).

sqlite-тесты гоняют Base.metadata.create_all, не эту миграцию (как и в cgm-ревизии).
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "pat0token01"
down_revision: Union[str, None] = "food0inter01"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "personal_access_tokens",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("token", sa.String(length=128), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=True),
        sa.Column("scope", sa.String(length=2), server_default="rw", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by_user", sa.BigInteger(), nullable=False),
        sa.CheckConstraint("scope IN ('ro', 'rw')", name="personal_access_tokens_scope_check"),
        sa.ForeignKeyConstraint(["user_id"], ["users.telegram_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_personal_access_tokens_token", "personal_access_tokens", ["token"], unique=True)
    op.create_index("ix_personal_access_tokens_user_id", "personal_access_tokens", ["user_id"], unique=False)

    # Гранты для hv_app — БЕЗ RLS (см. шапку ревизии). Роль hv_app создаётся в baseline.
    op.execute(
        """
        GRANT SELECT,INSERT,UPDATE ON TABLE personal_access_tokens TO hv_app;
        GRANT SELECT,USAGE ON SEQUENCE personal_access_tokens_id_seq TO hv_app;
        """
    )


def downgrade() -> None:
    op.drop_index("ix_personal_access_tokens_user_id", table_name="personal_access_tokens")
    op.drop_index("ix_personal_access_tokens_token", table_name="personal_access_tokens")
    op.drop_table("personal_access_tokens")
