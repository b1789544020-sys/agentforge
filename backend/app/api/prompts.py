"""Prompt template management and version-to-version evaluation."""
from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import delete as sql_delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session_factory, get_session
from app.eval import control as eval_control
from app.eval.runner import EvalRunner, RunEvalResult
from app.eval.scorer import EvalScorer
from app.models import Agent, EvalCase, PromptTemplate, PromptVersion

router = APIRouter(prefix="/agents/{agent_id}/prompt-templates", tags=["prompts"])


class TemplateCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    template: str = Field(min_length=1)
    description: str | None = None


class VersionCreate(BaseModel):
    template: str = Field(min_length=1)


class BindTemplate(BaseModel):
    version: int | None = None
    variables: dict[str, Any] = Field(default_factory=dict)


class CompareTemplates(BaseModel):
    version_a: int
    version_b: int
    cases: list[dict[str, Any]] = Field(min_length=1)
    scorer_name: str = "composite"
    pass_threshold: float = 0.7
    max_concurrency: int = 2
    variables: dict[str, Any] | None = None


async def _owned_template(agent_id: str, template_id: str, session: AsyncSession) -> PromptTemplate:
    template = await session.get(PromptTemplate, template_id)
    if template is None or template.agent_id != agent_id:
        raise HTTPException(status_code=404, detail="prompt template not found")
    return template


def _version_payload(row: PromptVersion) -> dict[str, Any]:
    return {"id": row.id, "version": row.version, "template": row.content, "created_at": row.created_at}


async def _template_payload(row: PromptTemplate, session: AsyncSession) -> dict[str, Any]:
    versions = (
        await session.execute(
            select(PromptVersion).where(PromptVersion.template_id == row.id).order_by(PromptVersion.version.desc())
        )
    ).scalars().all()
    return {
        "id": row.id, "agent_id": row.agent_id, "name": row.name,
        "description": row.description, "created_at": row.created_at,
        "latest_version": versions[0].version if versions else 0,
        "versions": [_version_payload(version) for version in versions],
    }


@router.get("")
async def list_templates(agent_id: str, session: AsyncSession = Depends(get_session)) -> list[dict[str, Any]]:
    rows = (
        await session.execute(select(PromptTemplate).where(PromptTemplate.agent_id == agent_id))
    ).scalars().all()
    return [await _template_payload(row, session) for row in rows]


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_template(
    agent_id: str, payload: TemplateCreate, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    if await session.get(Agent, agent_id) is None:
        raise HTTPException(status_code=404, detail="agent not found")
    existing = (
        await session.execute(select(PromptTemplate).where(
            PromptTemplate.agent_id == agent_id, PromptTemplate.name == payload.name
        ))
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status_code=409, detail="prompt template name already exists")
    row = PromptTemplate(
        id=str(uuid.uuid4()), agent_id=agent_id, name=payload.name, description=payload.description
    )
    session.add(row)
    await session.flush()
    session.add(PromptVersion(id=str(uuid.uuid4()), template_id=row.id, version=1, content=payload.template))
    await session.commit()
    return await _template_payload(row, session)


@router.get("/{template_id}")
async def get_template(
    agent_id: str, template_id: str, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    return await _template_payload(await _owned_template(agent_id, template_id, session), session)


@router.post("/{template_id}/versions", status_code=status.HTTP_201_CREATED)
async def create_version(
    agent_id: str, template_id: str, payload: VersionCreate,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    await _owned_template(agent_id, template_id, session)
    latest = await session.scalar(
        select(func.max(PromptVersion.version)).where(PromptVersion.template_id == template_id)
    )
    row = PromptVersion(
        id=str(uuid.uuid4()), template_id=template_id,
        version=int(latest or 0) + 1, content=payload.template,
    )
    session.add(row)
    await session.commit()
    return _version_payload(row)


@router.post("/{template_id}/bind")
async def bind_template(
    agent_id: str, template_id: str, payload: BindTemplate,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    agent = await session.get(Agent, agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="agent not found")
    await _owned_template(agent_id, template_id, session)
    stmt = select(PromptVersion).where(PromptVersion.template_id == template_id)
    if payload.version is None:
        stmt = stmt.order_by(PromptVersion.version.desc()).limit(1)
    else:
        stmt = stmt.where(PromptVersion.version == payload.version)
    version = (await session.execute(stmt)).scalars().first()
    if version is None:
        raise HTTPException(status_code=404, detail="prompt version not found")
    try:
        config = json.loads(agent.config or "{}")
    except (TypeError, ValueError):
        config = {}
    config.update({
        "prompt_template_id": template_id,
        "prompt_version": version.version,
        "prompt_variables": payload.variables,
    })
    agent.config = json.dumps(config, ensure_ascii=False)
    await session.commit()
    return {"template_id": template_id, "version": version.version, "config": config}


@router.delete("/{template_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_template(
    agent_id: str, template_id: str, session: AsyncSession = Depends(get_session)
) -> None:
    template = await _owned_template(agent_id, template_id, session)
    await session.execute(sql_delete(PromptVersion).where(PromptVersion.template_id == template_id))
    await session.delete(template)
    await session.commit()


def _comparison_agent(agent: Agent, template_id: str, version: int, variables: dict[str, Any]) -> Agent:
    try:
        config = json.loads(agent.config or "{}")
    except (TypeError, ValueError):
        config = {}
    config.update({
        "prompt_template_id": template_id,
        "prompt_version": version,
        "prompt_variables": variables,
    })
    return Agent(
        id=agent.id, name=agent.name, description=agent.description,
        system_prompt=agent.system_prompt, model=agent.model, provider=agent.provider,
        temperature=agent.temperature, max_steps=agent.max_steps, tools=agent.tools,
        config=json.dumps(config, ensure_ascii=False),
    )


def _comparison_payload(version: int, result: RunEvalResult) -> dict[str, Any]:
    payload = asdict(result)
    payload["version"] = version
    payload["pass_rate"] = result.passed / result.total if result.total else 0.0
    return payload


@router.post("/{template_id}/compare")
async def compare_template_versions(
    agent_id: str, template_id: str, payload: CompareTemplates,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    from app.api.evals import _resolve_scorer

    agent = await session.get(Agent, agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="agent not found")
    await _owned_template(agent_id, template_id, session)
    versions = (
        await session.execute(select(PromptVersion).where(
            PromptVersion.template_id == template_id,
            PromptVersion.version.in_([payload.version_a, payload.version_b]),
        ))
    ).scalars().all()
    if len(versions) != 2 or payload.version_a == payload.version_b:
        raise HTTPException(status_code=404, detail="two distinct prompt versions are required")
    scorer = _resolve_scorer(payload.scorer_name)
    if not isinstance(scorer, EvalScorer):
        raise HTTPException(status_code=400, detail="comparison requires a structured scorer")

    case_ids: list[str] = []
    for index, raw in enumerate(payload.cases):
        case_id = str(uuid.uuid4())
        case_ids.append(case_id)
        session.add(EvalCase(
            id=case_id, dataset=f"prompt-compare:{template_id}", name=f"case-{index}",
            input=str(raw.get("input", "")),
            expected=None if raw.get("expected") is None else str(raw.get("expected")),
            meta=json.dumps(raw.get("metadata") or {}, ensure_ascii=False), index=index,
        ))
    await session.commit()
    variables = payload.variables
    if variables is None:
        try:
            variables = (json.loads(agent.config or "{}").get("prompt_variables") or {})
        except (TypeError, ValueError):
            variables = {}

    async def evaluate(version: int) -> RunEvalResult:
        eval_id = str(uuid.uuid4())
        event = eval_control.register(eval_id)
        try:
            async with async_session_factory() as inner:
                persistent_agent = await inner.get(Agent, agent_id)
                assert persistent_agent is not None
                runner = EvalRunner(
                    inner, _comparison_agent(persistent_agent, template_id, version, variables),
                    pass_threshold=payload.pass_threshold,
                    max_concurrency=payload.max_concurrency,
                    session_factory=async_session_factory, cancel_event=event,
                )
                return await runner.run_eval(
                    case_ids, scorer=scorer, pass_threshold=payload.pass_threshold,
                    dataset=f"prompt-compare:{template_id}", eval_run_id=eval_id,
                )
        finally:
            eval_control.unregister(eval_id)

    result_a, result_b = await asyncio.gather(
        evaluate(payload.version_a), evaluate(payload.version_b)
    )
    a = _comparison_payload(payload.version_a, result_a)
    b = _comparison_payload(payload.version_b, result_b)
    return {
        "version_a": a, "version_b": b,
        "delta": {
            "average_score": b["average_score"] - a["average_score"],
            "pass_rate": b["pass_rate"] - a["pass_rate"],
        },
    }
