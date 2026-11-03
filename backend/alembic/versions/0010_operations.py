"""Operations metrics, versioned prices, feedback, and alerts.

Revision ID: d03af6b28e47
Revises: c92e7a41f0b8
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "d03af6b28e47"
down_revision: Union[str, None] = "c92e7a41f0b8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("agent_runs") as batch:
        batch.add_column(sa.Column("agent_version_id", sa.String(36), nullable=True))
        batch.create_foreign_key("fk_runs_agent_version", "agent_versions", ["agent_version_id"], ["id"], ondelete="SET NULL")
        batch.create_index("ix_agent_runs_agent_version_id", ["agent_version_id"])
    op.create_table(
        "model_prices",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("provider", sa.String(50), nullable=False),
        sa.Column("model", sa.String(100), nullable=False),
        sa.Column("input_per_million", sa.Float(), nullable=False),
        sa.Column("output_per_million", sa.Float(), nullable=False),
        sa.Column("currency", sa.String(8), nullable=False, server_default="USD"),
        sa.Column("effective_from", sa.DateTime(), nullable=False),
        sa.Column("created_by", sa.String(36)),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("provider", "model", "effective_from", name="uq_model_price_effective"),
    )
    op.create_index("ix_model_prices_effective_from", "model_prices", ["effective_from"])
    op.create_table(
        "feedback",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False, unique=True),
        sa.Column("agent_id", sa.String(36), nullable=False),
        sa.Column("team_id", sa.String(36)),
        sa.Column("rating", sa.String(16), nullable=False),
        sa.Column("expected_answer", sa.Text()),
        sa.Column("context_snapshot", sa.Text(), nullable=False),
        sa.Column("eval_case_id", sa.String(36)),
        sa.Column("suite_id", sa.String(36)),
        sa.Column("created_by", sa.String(36)),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_feedback_run_id", "feedback", ["run_id"], unique=True)
    op.create_index("ix_feedback_agent_id", "feedback", ["agent_id"])
    op.create_index("ix_feedback_team_id", "feedback", ["team_id"])
    op.create_table(
        "alert_rules",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("team_id", sa.String(36)),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("threshold", sa.Float(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_by", sa.String(36)),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_alert_rules_team_id", "alert_rules", ["team_id"])
    op.create_table(
        "alert_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("rule_id", sa.String(36), sa.ForeignKey("alert_rules.id", ondelete="CASCADE"), nullable=False),
        sa.Column("team_id", sa.String(36)),
        sa.Column("status", sa.String(16), nullable=False, server_default="open"),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("detail", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("acknowledged_by", sa.String(36)),
        sa.Column("acknowledged_at", sa.DateTime()),
    )
    for column in ("rule_id", "team_id", "status", "created_at"):
        op.create_index(f"ix_alert_events_{column}", "alert_events", [column])


def downgrade() -> None:
    op.drop_table("alert_events")
    op.drop_table("alert_rules")
    op.drop_table("feedback")
    op.drop_table("model_prices")
    with op.batch_alter_table("agent_runs") as batch:
        batch.drop_index("ix_agent_runs_agent_version_id")
        batch.drop_constraint("fk_runs_agent_version", type_="foreignkey")
        batch.drop_column("agent_version_id")
