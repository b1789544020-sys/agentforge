"""Additive four-layer quality evidence and human feedback resolutions.

Revision ID: b72f41a9c012
Revises: e14bc78a209f
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b72f41a9c012"
down_revision: Union[str, None] = "e14bc78a209f"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("agent_runs") as batch:
        batch.add_column(sa.Column("quality_record_version", sa.Integer(), nullable=True))
        for name in ("answer_output", "tool_output", "retrieval_output", "execution_output"):
            batch.add_column(sa.Column(name, sa.Text(), nullable=True))
    with op.batch_alter_table("gate_runs") as batch:
        batch.add_column(sa.Column("quality_record_version", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("conditions", sa.Text(), nullable=True))
        batch.add_column(sa.Column("evidence", sa.Text(), nullable=True))
    with op.batch_alter_table("feedback") as batch:
        batch.add_column(sa.Column("agent_version_id", sa.String(36), nullable=True))
        batch.create_foreign_key(
            "fk_feedback_agent_version", "agent_versions", ["agent_version_id"], ["id"],
            ondelete="SET NULL",
        )
        batch.create_index("ix_feedback_agent_version_id", ["agent_version_id"])
    op.create_table(
        "feedback_resolutions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "feedback_id", sa.String(36),
            sa.ForeignKey("feedback.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column(
            "gate_run_id", sa.String(36),
            sa.ForeignKey("gate_runs.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("case_id", sa.String(36), nullable=False),
        sa.Column("verdict", sa.String(32), nullable=False),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column("created_by", sa.String(36), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )
    op.create_index(
        "ix_feedback_resolutions_feedback_created", "feedback_resolutions",
        ["feedback_id", "created_at"],
    )
    op.create_index(
        "ix_feedback_resolutions_gate_case", "feedback_resolutions",
        ["gate_run_id", "case_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_feedback_resolutions_gate_case", table_name="feedback_resolutions")
    op.drop_index("ix_feedback_resolutions_feedback_created", table_name="feedback_resolutions")
    op.drop_table("feedback_resolutions")
    with op.batch_alter_table("feedback") as batch:
        batch.drop_index("ix_feedback_agent_version_id")
        batch.drop_constraint("fk_feedback_agent_version", type_="foreignkey")
        batch.drop_column("agent_version_id")
    with op.batch_alter_table("gate_runs") as batch:
        for name in ("evidence", "conditions", "quality_record_version"):
            batch.drop_column(name)
    with op.batch_alter_table("agent_runs") as batch:
        for name in (
            "execution_output", "retrieval_output", "tool_output", "answer_output",
            "quality_record_version",
        ):
            batch.drop_column(name)
