"""Store immutable model settings on each run.

Revision ID: 2f8c4a91d6b0
Revises: 7d2b9f1c3a4e
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "2f8c4a91d6b0"
down_revision: Union[str, None] = "7d2b9f1c3a4e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("agent_runs", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("config", sa.Text(), nullable=False, server_default="{}")
        )


def downgrade() -> None:
    with op.batch_alter_table("agent_runs", schema=None) as batch_op:
        batch_op.drop_column("config")
