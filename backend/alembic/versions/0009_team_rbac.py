"""Organizations, team RBAC, resource scopes, audit, and approvals.

Revision ID: c92e7a41f0b8
Revises: b81f0c42d9a6
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "c92e7a41f0b8"
down_revision: Union[str, None] = "b81f0c42d9a6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "organizations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_organizations_name", "organizations", ["name"], unique=True)
    op.create_table(
        "teams",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("org_id", sa.String(36), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("created_by", sa.String(36)),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("org_id", "name", name="uq_team_org_name"),
    )
    op.create_index("ix_teams_org_id", "teams", ["org_id"])
    op.create_table(
        "memberships",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("team_id", sa.String(36), sa.ForeignKey("teams.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("role", sa.String(24), nullable=False, server_default="developer"),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("team_id", "user_id", name="uq_membership_team_user"),
    )
    op.create_index("ix_memberships_team_id", "memberships", ["team_id"])
    op.create_index("ix_memberships_user_id", "memberships", ["user_id"])
    op.create_table(
        "audit_logs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("actor_id", sa.String(36)),
        sa.Column("team_id", sa.String(36)),
        sa.Column("action", sa.String(100), nullable=False),
        sa.Column("target_type", sa.String(50), nullable=False),
        sa.Column("target_id", sa.String(64), nullable=False),
        sa.Column("detail", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("ip", sa.String(64)),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )
    for column in ("actor_id", "team_id", "action", "created_at"):
        op.create_index(f"ix_audit_logs_{column}", "audit_logs", [column])
    op.create_table(
        "approval_requests",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("team_id", sa.String(36), sa.ForeignKey("teams.id", ondelete="CASCADE"), nullable=False),
        sa.Column("requester_id", sa.String(36), nullable=False),
        sa.Column("action", sa.String(100), nullable=False),
        sa.Column("target_type", sa.String(50), nullable=False),
        sa.Column("target_id", sa.String(64), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("resolver_id", sa.String(36)),
        sa.Column("resolution_reason", sa.Text()),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("resolved_at", sa.DateTime()),
    )
    for column in ("team_id", "requester_id", "status"):
        op.create_index(f"ix_approval_requests_{column}", "approval_requests", [column])

    scoped = {
        "agents": ("team_id",), "agent_versions": ("team_id",), "agent_runs": ("team_id",),
        "memories": ("team_id",), "eval_runs": ("team_id",),
        "eval_cases": ("agent_id", "owner_id", "team_id"), "tool_metadata": ("team_id",),
    }
    for table, columns in scoped.items():
        with op.batch_alter_table(table) as batch:
            for column in columns:
                batch.add_column(sa.Column(column, sa.String(36), nullable=True))
                batch.create_index(f"ix_{table}_{column}", [column])
    with op.batch_alter_table("agents") as batch:
        batch.create_foreign_key("fk_agents_team_id_teams", "teams", ["team_id"], ["id"], ondelete="SET NULL")


def downgrade() -> None:
    with op.batch_alter_table("agents") as batch:
        batch.drop_constraint("fk_agents_team_id_teams", type_="foreignkey")
    scoped = {
        "agents": ("team_id",), "agent_versions": ("team_id",), "agent_runs": ("team_id",),
        "memories": ("team_id",), "eval_runs": ("team_id",),
        "eval_cases": ("agent_id", "owner_id", "team_id"), "tool_metadata": ("team_id",),
    }
    for table, columns in scoped.items():
        with op.batch_alter_table(table) as batch:
            for column in columns:
                batch.drop_index(f"ix_{table}_{column}")
                batch.drop_column(column)
    op.drop_table("approval_requests")
    op.drop_table("audit_logs")
    op.drop_table("memberships")
    op.drop_table("teams")
    op.drop_table("organizations")

