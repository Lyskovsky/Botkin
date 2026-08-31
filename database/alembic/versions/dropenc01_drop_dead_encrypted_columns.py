"""drop dead users.encrypted_openai_key / encrypted_anthropic_key

Revision ID: dropenc01
Revises: cgmfol0self01
Create Date: 2026-08-21

Колонки заведены в baseline (711fd5e3f1e8) под хранение пользовательских
LLM-ключей, но за всё время не были ни прочитаны, ни записаны ни одной строкой
кода: единственные упоминания — сама модель и baseline-ревизия. В проде значения
NULL у всех пользователей.

Держать их дальше вредно именно из-за имени: «encrypted_» создаёт впечатление,
что в проекте есть шифрование пользовательских секретов и что этим колонкам
можно доверять. Настоящее шифрование появилось только в cgmfol0self01
(core/infra/secrets.py, #381), и работает оно с другой таблицей.

Данные не переносим — их нет. Downgrade возвращает колонки пустыми.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "dropenc01"
down_revision: Union[str, None] = "cgmfol0self01"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_column("users", "encrypted_openai_key")
    op.drop_column("users", "encrypted_anthropic_key")


def downgrade() -> None:
    # Возвращаем в том же виде, что в baseline: nullable Text, без данных.
    op.add_column("users", sa.Column("encrypted_anthropic_key", sa.Text(), nullable=True))
    op.add_column("users", sa.Column("encrypted_openai_key", sa.Text(), nullable=True))
