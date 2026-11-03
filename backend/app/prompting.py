"""Strict, sandboxed Jinja prompt rendering and version resolution."""
from __future__ import annotations

import json
from typing import Any

from jinja2.sandbox import SandboxedEnvironment
from jinja2 import StrictUndefined
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Agent, PromptTemplate, PromptVersion


_environment = SandboxedEnvironment(undefined=StrictUndefined, autoescape=False)


def render_prompt(content: str, variables: dict[str, Any]) -> str:
    return _environment.from_string(content).render(**variables)


async def resolve_system_prompt(
    session: AsyncSession, agent: Agent, user_input: str
) -> str:
    raw = agent.system_prompt or ""
    try:
        config = json.loads(agent.config or "{}") if isinstance(agent.config, str) else dict(agent.config or {})
    except (TypeError, ValueError):
        config = {}
    template_id = config.get("prompt_template_id")
    if template_id:
        template = await session.get(PromptTemplate, template_id)
        if template is None or template.agent_id != agent.id:
            raise ValueError("bound prompt template does not belong to this agent")
        stmt = select(PromptVersion).where(PromptVersion.template_id == template_id)
        version = config.get("prompt_version")
        if version is not None:
            stmt = stmt.where(PromptVersion.version == int(version))
        else:
            stmt = stmt.order_by(PromptVersion.version.desc()).limit(1)
        row = (await session.execute(stmt)).scalars().first()
        if row is None:
            raise ValueError(f"prompt version {version!r} was not found")
        raw = row.content
    variables = config.get("prompt_variables") or {}
    if not isinstance(variables, dict):
        raise ValueError("config.prompt_variables must be an object")
    variables = {**variables, "input": user_input}
    return render_prompt(raw, variables) if raw else ""


__all__ = ["render_prompt", "resolve_system_prompt"]
