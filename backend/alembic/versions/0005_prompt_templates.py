"""Prompt templates and immutable versions.

Revision ID: b63f1a72c904
Revises: 94c20e6ab713
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b63f1a72c904"
down_revision: Union[str, None] = "94c20e6ab713"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "prompt_templates",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("agent_id", sa.String(length=36), sa.ForeignKey("agents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.UniqueConstraint("agent_id", "name", name="uq_prompt_template_name"),
    )
    op.create_index("ix_prompt_templates_agent_id", "prompt_templates", ["agent_id"])
    op.create_table(
        "prompt_versions",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("template_id", sa.String(length=36), sa.ForeignKey("prompt_templates.id", ondelete="CASCADE"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.UniqueConstraint("template_id", "version", name="uq_prompt_version"),
    )
    op.create_index("ix_prompt_versions_template_id", "prompt_versions", ["template_id"])


def downgrade() -> None:
    op.drop_index("ix_prompt_versions_template_id", table_name="prompt_versions")
    op.drop_table("prompt_versions")
    op.drop_index("ix_prompt_templates_agent_id", table_name="prompt_templates")
    op.drop_table("prompt_templates")
