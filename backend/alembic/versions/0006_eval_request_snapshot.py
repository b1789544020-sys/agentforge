"""Persist evaluation request snapshots for durable workers.

Revision ID: e31b9a48c206
Revises: b63f1a72c904
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e31b9a48c206"
down_revision: Union[str, None] = "b63f1a72c904"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("eval_runs", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("request", sa.Text(), nullable=False, server_default="{}")
        )


def downgrade() -> None:
    with op.batch_alter_table("eval_runs", schema=None) as batch_op:
        batch_op.drop_column("request")
