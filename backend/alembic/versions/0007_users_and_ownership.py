"""Users, JWT roles, and Agent ownership.

Revision ID: a74d20c19e35
Revises: e31b9a48c206
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a74d20c19e35"
down_revision: Union[str, None] = "e31b9a48c206"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False, server_default="user"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.UniqueConstraint("email"),
    )
    op.create_index("ix_users_email", "users", ["email"], unique=True)
    with op.batch_alter_table("agents", schema=None) as batch_op:
        batch_op.add_column(sa.Column("owner_id", sa.String(length=36), nullable=True))
        batch_op.create_foreign_key(
            "fk_agents_owner_id_users", "users", ["owner_id"], ["id"], ondelete="SET NULL"
        )
        batch_op.create_index("ix_agents_owner_id", ["owner_id"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("agents", schema=None) as batch_op:
        batch_op.drop_index("ix_agents_owner_id")
        batch_op.drop_column("owner_id")
    op.drop_index("ix_users_email", table_name="users")
    op.drop_table("users")
