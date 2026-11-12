"""Canonical Agent Definition resolution and shared persistence helpers."""
from __future__ import annotations

import copy
import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.schema import (
    AgentDefinition,
    ResourceMap,
    merge_secret_inputs,
    missing_secret_refs,
    public_definition,
)
from app.agent.tools import ToolRegistry
from app.models import (
    Agent,
    AgentVersion,
    KnowledgeBase,
    KnowledgeBinding,
    PromptTemplate,
    PromptVersion,
    ToolMetadata,
)


@dataclass
class ResolvedDefinition:
    definition: AgentDefinition
    config: dict[str, Any]
    knowledge_base_ids: list[str] = field(default_factory=list)
    knowledge_refs: dict[str, str] = field(default_factory=dict)
    prompt_template_id: str | None = None
    prompt_source_id: str | None = None
    prompt_version: int | None = None
    resolved: list[dict[str, Any]] = field(default_factory=list)
    unresolved: list[dict[str, Any]] = field(default_factory=list)
    unsupported_tools: list[dict[str, Any]] = field(default_factory=list)
    missing_secrets: list[dict[str, Any]] = field(default_factory=list)
    inherited_secrets: list[dict[str, Any]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.unresolved and not self.unsupported_tools


def snapshot_dict(value: AgentVersion | None) -> dict[str, Any]:
    if value is None:
        return {}
    try:
        parsed = json.loads(value.snapshot or "{}")
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _same_tenant(resource: Any, agent: Agent) -> bool:
    if resource.team_id != agent.team_id:
        return False
    if resource.team_id is not None:
        return True
    resource_owner = getattr(resource, "owner_id", None)
    return agent.owner_id is None or resource_owner is None or resource_owner == agent.owner_id


def _unresolved(agent: Agent, kind: str, ref: str, reason: str) -> dict[str, Any]:
    return {"agent": agent.name, "kind": kind, "ref": ref, "reason": reason}


async def _resolve_knowledge(
    session: AsyncSession,
    agent: Agent,
    definition: AgentDefinition,
    resource_map: ResourceMap,
    previous_snapshot: Mapping[str, Any],
    result: ResolvedDefinition,
) -> None:
    previous_rows = (previous_snapshot.get("resources") or {}).get("knowledge_bases") or []
    previous_ids = {
        str(row.get("ref")): str(row.get("kb_id") or row.get("id"))
        for row in previous_rows
        if isinstance(row, Mapping) and row.get("ref") and (row.get("kb_id") or row.get("id"))
    }
    seen_ids: set[str] = set()
    for resource in definition.resources.knowledge_bases:
        target_id = resource_map.knowledge_bases.get(resource.ref) or previous_ids.get(resource.ref)
        if not target_id:
            result.unresolved.append(_unresolved(agent, "knowledge_base", resource.ref, "mapping_required"))
            continue
        if target_id in seen_ids:
            result.unresolved.append(_unresolved(agent, "knowledge_base", resource.ref, "duplicate_target"))
            continue
        seen_ids.add(target_id)
        row = await session.get(KnowledgeBase, target_id)
        if row is None or not _same_tenant(row, agent):
            result.unresolved.append(_unresolved(agent, "knowledge_base", resource.ref, "resource_unavailable"))
            continue
        result.knowledge_base_ids.append(row.id)
        result.knowledge_refs[row.id] = resource.ref
        result.resolved.append({
            "agent": agent.name, "kind": "knowledge_base", "ref": resource.ref,
            "target_id": row.id,
        })
    result.knowledge_base_ids.sort()


async def _resolve_prompt(
    session: AsyncSession,
    agent: Agent,
    definition: AgentDefinition,
    resource_map: ResourceMap,
    result: ResolvedDefinition,
    *,
    existing_agent: bool,
) -> None:
    resource = definition.resources.prompt
    if resource is None:
        return
    qualified_name = f"{agent.name}:{resource.name}"
    target_id = resource_map.prompts.get(qualified_name) or resource_map.prompts.get(resource.name)
    template = await session.get(PromptTemplate, target_id) if target_id else None
    if template is None and not target_id and existing_agent:
        template = (await session.execute(
            select(PromptTemplate).where(
                PromptTemplate.agent_id == agent.id,
                PromptTemplate.name == resource.name,
            )
        )).scalars().first()
    if template is None:
        result.unresolved.append(_unresolved(agent, "prompt", resource.name, "resource_unavailable"))
        return
    owner = await session.get(Agent, template.agent_id)
    if owner is None or not _same_tenant(owner, agent):
        result.unresolved.append(_unresolved(agent, "prompt", resource.name, "resource_unavailable"))
        return
    clone = template.agent_id != agent.id
    if clone and existing_agent:
        result.unresolved.append(_unresolved(agent, "prompt", resource.name, "template_not_owned"))
        return
    stmt = select(PromptVersion).where(PromptVersion.template_id == template.id)
    if resource.version is None:
        stmt = stmt.order_by(PromptVersion.version.desc()).limit(1)
    else:
        stmt = stmt.where(PromptVersion.version == resource.version)
    version = (await session.execute(stmt)).scalars().first()
    if version is None:
        result.unresolved.append(_unresolved(agent, "prompt", resource.name, "version_unavailable"))
        return
    if clone:
        result.prompt_source_id = template.id
    else:
        result.prompt_template_id = template.id
    result.prompt_version = version.version
    result.resolved.append({
        "agent": agent.name, "kind": "prompt", "ref": resource.name,
        "target_id": template.id, "version": version.version,
        "action": "copy" if clone else "bind",
    })


async def resolve_definition(
    session: AsyncSession,
    agent: Agent,
    definition: AgentDefinition,
    *,
    resource_map: ResourceMap | None = None,
    secret_inputs: Mapping[str, str | None] | None = None,
    previous_config: Mapping[str, Any] | None = None,
    previous_snapshot: Mapping[str, Any] | None = None,
    existing_agent: bool = True,
) -> ResolvedDefinition:
    """Preflight all resources and secrets without mutating the session."""
    resource_map = resource_map or ResourceMap()
    result = ResolvedDefinition(definition=definition, config=copy.deepcopy(definition.config))
    for local_key in ("prompt_template_id", "prompt_version", "knowledge_base_ids"):
        result.config.pop(local_key, None)
    metadata = await session.execute(select(ToolMetadata))
    disabled = {row.name for row in metadata.scalars() if not row.enabled}
    registry_names = {row.name for row in ToolRegistry.list_all()}
    for name in definition.tools:
        if name not in registry_names or name in disabled:
            result.unsupported_tools.append({"agent": agent.name, "tool": name})
    await _resolve_knowledge(
        session, agent, definition, resource_map, previous_snapshot or {}, result,
    )
    await _resolve_prompt(
        session, agent, definition, resource_map, result, existing_agent=existing_agent,
    )
    result.config["knowledge_base_ids"] = list(result.knowledge_base_ids)
    protected, report = merge_secret_inputs(definition, previous_config, secret_inputs)
    protected["knowledge_base_ids"] = list(result.knowledge_base_ids)
    result.config = protected
    result.missing_secrets = report["missing_secrets"]
    result.inherited_secrets = report["inherited_secrets"]
    return result


async def _copy_prompt(
    session: AsyncSession, agent: Agent, source_id: str,
) -> PromptTemplate:
    source = await session.get(PromptTemplate, source_id)
    if source is None:
        raise ValueError("mapped prompt template is no longer available")
    copied = PromptTemplate(
        id=str(uuid.uuid4()), agent_id=agent.id, name=source.name,
        description=source.description,
    )
    session.add(copied)
    await session.flush()
    versions = (await session.execute(
        select(PromptVersion).where(PromptVersion.template_id == source.id)
        .order_by(PromptVersion.version.asc())
    )).scalars().all()
    if not versions:
        raise ValueError("mapped prompt template has no versions")
    for row in versions:
        session.add(PromptVersion(
            id=str(uuid.uuid4()), template_id=copied.id,
            version=row.version, content=row.content,
        ))
    await session.flush()
    return copied


async def apply_definition(
    session: AsyncSession, agent: Agent, resolved: ResolvedDefinition,
) -> None:
    """Apply a fully preflighted Definition; the caller owns the transaction."""
    if not resolved.ok:
        raise ValueError("cannot apply a Definition with unresolved resources")
    definition = resolved.definition
    prompt_template_id = resolved.prompt_template_id
    if resolved.prompt_source_id is not None:
        prompt_template_id = (await _copy_prompt(session, agent, resolved.prompt_source_id)).id
    agent.name = definition.name
    agent.description = definition.description
    agent.system_prompt = definition.system_prompt
    agent.provider = definition.provider
    agent.model = definition.model
    agent.temperature = definition.temperature
    agent.max_steps = definition.max_steps
    agent.tools = json.dumps(definition.tools, ensure_ascii=False)
    config = copy.deepcopy(resolved.config)
    config.pop("prompt_template_id", None)
    config.pop("prompt_version", None)
    if prompt_template_id is not None:
        config["prompt_template_id"] = prompt_template_id
        config["prompt_version"] = resolved.prompt_version
    config["knowledge_base_ids"] = list(resolved.knowledge_base_ids)
    agent.config = json.dumps(config, ensure_ascii=False)
    agent._schema_secret_refs = [row.model_dump() for row in definition.secret_refs]
    agent._schema_extensions = copy.deepcopy(definition.extensions)
    agent._schema_knowledge_refs = dict(resolved.knowledge_refs)

    bindings = (await session.execute(
        select(KnowledgeBinding).where(KnowledgeBinding.agent_id == agent.id)
    )).scalars().all()
    existing = {row.kb_id: row for row in bindings}
    requested = set(resolved.knowledge_base_ids)
    for kb_id, binding in existing.items():
        if kb_id not in requested:
            await session.delete(binding)
    for kb_id in sorted(requested - set(existing)):
        session.add(KnowledgeBinding(agent_id=agent.id, kb_id=kb_id))
    await session.flush()


def definition_response(version: AgentVersion) -> dict[str, Any]:
    snapshot = snapshot_dict(version)
    missing = missing_secret_refs(snapshot, required_only=False)
    warnings: list[dict[str, Any]] = []
    if snapshot.get("schema_version") != 1:
        config = snapshot.get("config") or {}
        if config.get("prompt_template_id"):
            warnings.append({"code": "unpinned_prompt"})
        if config.get("knowledge_base_ids"):
            warnings.append({"code": "unpinned_knowledge_bindings"})
    if any(row["required"] for row in missing):
        warnings.append({"code": "missing_required_secrets"})
    if snapshot.get("extensions"):
        warnings.append({"code": "extensions_preserved_not_executed"})
    return {
        "definition": public_definition(snapshot),
        "base_version_id": version.id,
        "latest_version_id": version.id,
        "version_number": version.version_number,
        "warnings": warnings,
        "missing_secrets": missing,
    }


__all__ = [
    "ResolvedDefinition", "apply_definition", "definition_response",
    "resolve_definition", "snapshot_dict",
]
