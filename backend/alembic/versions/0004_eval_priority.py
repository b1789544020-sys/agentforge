"""Add queue priority to evaluation runs.

Revision ID: 94c20e6ab713
Revises: 2f8c4a91d6b0
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "94c20e6ab713"
down_revision: Union[str, None] = "2f8c4a91d6b0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("eval_runs", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("priority", sa.Integer(), nullable=False, server_default="0")
        )


def downgrade() -> None:
    with op.batch_alter_table("eval_runs", schema=None) as batch_op:
        batch_op.drop_column("priority")
