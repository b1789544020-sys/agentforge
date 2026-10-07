"""tool_metadata plugin table

Revision ID: 7d2b9f1c3a4e
Revises: c7f91c261ae3
Create Date: 2026-09-04 10:00:00.000000

Adds the ``tool_metadata`` table for the 0.1.3 plugin layer: rows
here are sparse runtime overrides of the source-of-truth tool
registry in ``app.agent.tools``.  The agent loop and the
``/api/tools`` endpoint read from this table first and fall back to
the registry when no row exists, so metadata (description, risk,
scope, category, parameters schema, enabled flag) can be edited
through the API at runtime without a process restart.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "7d2b9f1c3a4e"
down_revision: Union[str, None] = "c7f91c261ae3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "tool_metadata",
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("risk", sa.String(length=20), nullable=True),
        sa.Column("scope", sa.String(length=50), nullable=True),
        sa.Column("category", sa.String(length=50), nullable=True),
        sa.Column("parameters_schema", sa.Text(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("name"),
    )
    with op.batch_alter_table("tool_metadata", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_tool_metadata_category"), ["category"], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table("tool_metadata", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_tool_metadata_category"))
    op.drop_table("tool_metadata")
