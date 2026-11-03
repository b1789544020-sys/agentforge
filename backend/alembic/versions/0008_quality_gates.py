"""Immutable Agent versions, evaluation suites, and quality gates.

Revision ID: b81f0c42d9a6
Revises: a74d20c19e35
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "b81f0c42d9a6"
down_revision: Union[str, None] = "a74d20c19e35"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "agent_versions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("agent_id", sa.String(36), sa.ForeignKey("agents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("snapshot", sa.Text(), nullable=False),
        sa.Column("system_prompt", sa.Text()),
        sa.Column("model", sa.String(100), nullable=False),
        sa.Column("provider", sa.String(50), nullable=False),
        sa.Column("temperature", sa.Float(), nullable=False),
        sa.Column("max_steps", sa.Integer(), nullable=False),
        sa.Column("tools", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("config", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("gate_status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("created_by", sa.String(36), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("agent_id", "version_number", name="uq_agent_version_number"),
    )
    op.create_index("ix_agent_versions_agent_id", "agent_versions", ["agent_id"])
    op.create_index("ix_agent_versions_gate_status", "agent_versions", ["gate_status"])
    op.create_table(
        "eval_suites",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column("owner_id", sa.String(36), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("team_id", sa.String(36)),
        sa.Column("agent_id", sa.String(36), sa.ForeignKey("agents.id", ondelete="CASCADE")),
        sa.Column("case_ids", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("pass_threshold", sa.Float(), nullable=False, server_default="0.95"),
        sa.Column("scorer", sa.String(100), nullable=False, server_default="composite"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("auto_on_save", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("schedule_interval_seconds", sa.Integer()),
        sa.Column("next_run_at", sa.DateTime()),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )
    for column in ("name", "owner_id", "team_id", "agent_id"):
        op.create_index(f"ix_eval_suites_{column}", "eval_suites", [column])
    op.create_table(
        "gate_policies",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("default_threshold", sa.Float(), nullable=False, server_default="0.95"),
        sa.Column("enforce", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("allow_bypass", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("notify_to", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )
    op.create_table(
        "gate_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("agent_version_id", sa.String(36), sa.ForeignKey("agent_versions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("suite_id", sa.String(36), sa.ForeignKey("eval_suites.id", ondelete="CASCADE"), nullable=False),
        sa.Column("eval_run_id", sa.String(36), sa.ForeignKey("eval_runs.id", ondelete="SET NULL")),
        sa.Column("baseline_gate_id", sa.String(36), sa.ForeignKey("gate_runs.id", ondelete="SET NULL")),
        sa.Column("triggered_by", sa.String(36)),
        sa.Column("trigger_source", sa.String(16), nullable=False, server_default="manual"),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("pass_rate", sa.Float(), nullable=False, server_default="0"),
        sa.Column("threshold", sa.Float(), nullable=False, server_default="0.95"),
        sa.Column("failed_case_ids", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("details", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("diff", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("error", sa.Text()),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("finished_at", sa.DateTime()),
    )
    for column in ("agent_version_id", "suite_id", "status", "created_at"):
        op.create_index(f"ix_gate_runs_{column}", "gate_runs", [column])
    op.create_table(
        "gate_bypasses",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("agent_version_id", sa.String(36), sa.ForeignKey("agent_versions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("gate_run_id", sa.String(36), sa.ForeignKey("gate_runs.id", ondelete="SET NULL")),
        sa.Column("created_by", sa.String(36)),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_gate_bypasses_agent_version_id", "gate_bypasses", ["agent_version_id"])


def downgrade() -> None:
    op.drop_table("gate_bypasses")
    op.drop_table("gate_runs")
    op.drop_table("gate_policies")
    op.drop_table("eval_suites")
    op.drop_table("agent_versions")
