"""Deployments, versioned templates, and private knowledge bases.

Revision ID: e14bc78a209f
Revises: d03af6b28e47
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "e14bc78a209f"
down_revision: Union[str, None] = "d03af6b28e47"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "deployments",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("agent_id", sa.String(36), nullable=False),
        sa.Column("team_id", sa.String(36)),
        sa.Column("agent_version_id", sa.String(36), sa.ForeignKey("agent_versions.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("token_prefix", sa.String(16), nullable=False),
        sa.Column("token_revoked", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("runtime_spec", sa.Text(), nullable=False),
        sa.Column("version_history", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("published_by", sa.String(36)),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_deployments_agent_id", "deployments", ["agent_id"])
    op.create_index("ix_deployments_team_id", "deployments", ["team_id"])
    op.create_index("ix_deployments_token_hash", "deployments", ["token_hash"], unique=True)
    op.create_table(
        "agent_templates",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("team_id", sa.String(36)),
        sa.Column("owner_id", sa.String(36)),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column("category", sa.String(100), nullable=False, server_default="general"),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_agent_templates_team_id", "agent_templates", ["team_id"])
    op.create_table(
        "agent_template_versions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("template_id", sa.String(36), sa.ForeignKey("agent_templates.id", ondelete="CASCADE"), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("snapshot", sa.Text(), nullable=False),
        sa.Column("created_by", sa.String(36)),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("template_id", "version_number", name="uq_agent_template_version"),
    )
    op.create_index("ix_agent_template_versions_template_id", "agent_template_versions", ["template_id"])
    op.create_table(
        "knowledge_bases",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("team_id", sa.String(36)),
        sa.Column("owner_id", sa.String(36)),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_knowledge_bases_team_id", "knowledge_bases", ["team_id"])
    op.create_table(
        "knowledge_documents",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("kb_id", sa.String(36), sa.ForeignKey("knowledge_bases.id", ondelete="CASCADE"), nullable=False),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("media_type", sa.String(100), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="ready"),
        sa.Column("error", sa.Text()),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_knowledge_documents_kb_id", "knowledge_documents", ["kb_id"])
    op.create_table(
        "knowledge_chunks",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("document_id", sa.String(36), sa.ForeignKey("knowledge_documents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kb_id", sa.String(36), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("section", sa.String(300)),
        sa.Column("embedding", sa.LargeBinary()),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_knowledge_chunks_document_id", "knowledge_chunks", ["document_id"])
    op.create_index("ix_knowledge_chunks_kb_id", "knowledge_chunks", ["kb_id"])
    op.create_table(
        "knowledge_bindings",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("agent_id", sa.String(36), sa.ForeignKey("agents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kb_id", sa.String(36), sa.ForeignKey("knowledge_bases.id", ondelete="CASCADE"), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("agent_id", "kb_id", name="uq_agent_knowledge_binding"),
    )
    op.create_index("ix_knowledge_bindings_agent_id", "knowledge_bindings", ["agent_id"])
    op.create_index("ix_knowledge_bindings_kb_id", "knowledge_bindings", ["kb_id"])


def downgrade() -> None:
    op.drop_table("knowledge_bindings")
    op.drop_table("knowledge_chunks")
    op.drop_table("knowledge_documents")
    op.drop_table("knowledge_bases")
    op.drop_table("agent_template_versions")
    op.drop_table("agent_templates")
    op.drop_table("deployments")
