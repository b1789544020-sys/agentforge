"""Persist server-side Run v1 observations from executed Steps and retrieval."""
from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.schema import public_legacy_dto
from app.models import Run, Step
from app.quality.run import RunObservationsV1
from app.secrets import redact_text

_CITATION = re.compile(r"\[(\d{1,3})\]")
_TERMINAL = {"success", "completed", "ok", "failed", "error", "cancelled", "terminated"}


def _json(value: str | None, fallback: Any) -> Any:
    try:
        return json.loads(value or json.dumps(fallback))
    except (TypeError, ValueError):
        return fallback


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _error(message: str | None, *, category: str = "internal", step_id: str | None = None) -> dict[str, Any] | None:
    if not message:
        return None
    text = redact_text(str(message))[:4000]
    lowered = text.lower()
    if "timeout" in lowered or "timed out" in lowered:
        category = "timeout"
    elif "cancel" in lowered:
        category = "cancelled"
    elif "permission" in lowered or "forbidden" in lowered or "unauthorized" in lowered:
        category = "permission"
    elif "max_steps" in lowered or "budget" in lowered or "terminated" in lowered:
        category = "budget"
    elif "provider" in lowered or "connection" in lowered or "http" in lowered:
        category = "provider"
    return {"category": category, "message": text, "retryable": category in {"provider", "timeout"}, "step_id": step_id}


def _answer(run: Run, retrieval: dict[str, Any]) -> dict[str, Any]:
    successful = run.status in {"success", "completed", "ok"} and bool(run.output)
    answer = run.output if successful else None
    status = "answered" if successful else "not_produced"
    if answer:
        lowered = answer.lower()
        if any(marker in lowered for marker in ("资料不足", "证据不足", "无法根据所给", "insufficient evidence")):
            status = "insufficient_evidence"
        elif any(marker in lowered for marker in ("拒绝回答", "不能协助", "cannot assist")):
            status = "refused"
    injected = [candidate for query in retrieval.get("queries", []) for candidate in query.get("candidates", []) if candidate.get("injected")]
    sources = {item.get("source_id"): item for item in retrieval.get("sources", [])}
    evidence: list[dict[str, Any]] = []
    if answer:
        lines = answer.splitlines() or [answer]
        for match in _CITATION.finditer(answer):
            index = int(match.group(1))
            if not 1 <= index <= len(injected):
                continue  # Raw text preserves invalid citations; no source is invented.
            source = sources.get(injected[index - 1].get("source_id"))
            if source is None:
                continue
            line = next((item for item in lines if match.group(0) in item), match.group(0))
            evidence.append({
                "evidence_id": f"citation-{len(evidence) + 1}", "source_id": source["source_id"],
                "citation_text": match.group(0), "answer_excerpt": line[:2000],
                "source_excerpt": source["source_excerpt"], "support_status": "not_checked",
            })
    return {"answer": answer, "answer_status": status, "evidence": evidence}


def _tools(steps: list[Step]) -> dict[str, Any]:
    calls: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []
    call_steps = [step for step in steps if step.kind == "tool_call"]
    occurrences: dict[str, int] = {}
    unique_by_step: dict[str, str] = {}
    for step in call_steps:
        payload = _json(step.payload, {})
        original_id = str(payload.get("call_id") or step.id)
        occurrences[original_id] = occurrences.get(original_id, 0) + 1
        call_id = original_id if occurrences[original_id] == 1 else f"{original_id}#{occurrences[original_id]}"
        unique_by_step[step.id] = call_id
        calls.append({
            "call_id": call_id, "call_step_id": step.id,
            "name": step.name or "unknown", "arguments": payload.get("arguments"),
            "risk": payload.get("risk") or "safe", "requested_at": _utc(step.created_at),
            "approval_id": payload.get("approval_id"), "definition_version": payload.get("definition_version"),
        })
    by_id: dict[str, list[Step]] = {}
    for call_step in call_steps:
        original_id = str(_json(call_step.payload, {}).get("call_id") or call_step.id)
        by_id.setdefault(original_id, []).append(call_step)
    for step in steps:
        if step.kind != "tool_result":
            continue
        payload = _json(step.payload, {})
        original_id = str(payload.get("call_id") or "")
        candidates = by_id.get(original_id)
        call = candidates.pop(0) if candidates else None
        if call is None:
            continue
        failed = step.status == "error" or bool(step.error)
        results.append({
            "call_id": unique_by_step[call.id], "result_step_id": step.id,
            "status": "error" if failed else "success", "output": payload.get("result", step.content),
            "error": _error(step.error, category="tool", step_id=step.id) if failed else None,
            "duration_ms": max(0, int(call.duration_ms or 0)),
            "finished_at": _utc(step.created_at),
        })
    return {"calls": calls, "results": results}


def build_run_observations(
    run: Run, steps: list[Step], retrieval: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build observations only from execution artifacts, never model claims."""
    retrieval_output = retrieval or _json(run.retrieval_output, {"queries": [], "sources": []})
    tool_output = _tools(steps)
    error_step = next((step for step in reversed(steps) if step.status == "error"), None)
    complete = run.status in _TERMINAL and run.finished_at is not None
    payload = {
        "quality_record_version": 1,
        "answer_output": _answer(run, retrieval_output),
        "tool_output": tool_output,
        "retrieval_output": retrieval_output,
        "execution_output": {
            "events": [{
                "event_id": f"event-{sequence}", "step_id": step.id, "sequence": sequence,
                "kind": step.kind, "status": step.status, "parent_event_id": None,
            } for sequence, step in enumerate(steps, 1)],
            "metrics": {
                "duration_ms": max(0, run.duration_ms or 0),
                "prompt_tokens": max(0, run.prompt_tokens or 0),
                "completion_tokens": max(0, run.completion_tokens or 0),
                "total_tokens": max(0, run.total_tokens or 0),
                "step_count": max(0, run.step_count or 0),
                "tool_call_count": len(tool_output["calls"]),
                "retrieval_query_count": len(retrieval_output.get("queries", [])),
            },
            "error": _error(run.error, step_id=error_step.id if error_step else None),
            "result": {
                "status": run.status, "started_at": _utc(run.started_at),
                "finished_at": _utc(run.finished_at), "termination_reason": run.error,
                "record_complete": complete,
            },
        },
    }
    safe = public_legacy_dto(payload)
    validated = RunObservationsV1.model_validate(safe)
    return validated.model_dump(mode="json")


async def write_run_quality(
    session: AsyncSession, run: Run, retrieval: dict[str, Any] | None = None,
) -> None:
    """Write all observations in the same transaction as the Run terminal state."""
    steps = list((await session.execute(
        select(Step).where(Step.run_id == run.id).order_by(Step.index.asc())
    )).scalars().all())
    payload = build_run_observations(run, steps, retrieval)
    run.quality_record_version = 1
    for field in ("answer_output", "tool_output", "retrieval_output", "execution_output"):
        setattr(run, field, payload[field])


__all__ = ["build_run_observations", "write_run_quality"]
