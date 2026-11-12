"""Freeze self-contained Gate comparison conditions from database records."""
from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.schema import public_legacy_dto
from app.config import get_settings
from app.models import AgentVersion, EvalCase, EvalSuite, KnowledgeBinding, KnowledgeChunk
from app.quality.gate import GateConditions


def canonical_sha256(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def text_sha256(value: Any) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def _json(value: str | None, fallback: Any) -> Any:
    try:
        return json.loads(value or json.dumps(fallback))
    except (TypeError, ValueError):
        return fallback


async def _knowledge_ids(
    session: AsyncSession, version: AgentVersion, snapshot: dict[str, Any],
) -> list[str]:
    config = snapshot.get("config") if isinstance(snapshot.get("config"), dict) else {}
    configured = config.get("knowledge_base_ids")
    if configured:
        return sorted(str(item) for item in configured)
    return sorted((await session.execute(
        select(KnowledgeBinding.kb_id).where(KnowledgeBinding.agent_id == version.agent_id)
    )).scalars().all())


def _corpus_manifest(rows: list[tuple[str, str]]) -> str:
    digest = hashlib.sha256()
    for chunk_id, content in sorted(rows, key=lambda row: row[0]):
        digest.update(chunk_id.encode("utf-8"))
        digest.update(b"\0")
        digest.update(content.encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


async def build_gate_conditions(
    session: AsyncSession, version: AgentVersion, suite: EvalSuite, threshold: float,
) -> dict[str, Any]:
    case_ids = [str(item) for item in _json(suite.case_ids, [])]
    rows = list((await session.execute(
        select(EvalCase).where(EvalCase.id.in_(case_ids))
    )).scalars().all())
    by_id = {row.id: row for row in rows}
    ordered = [by_id[case_id] for case_id in case_ids]
    snapshot = _json(version.snapshot, {})
    public_snapshot = public_legacy_dto(snapshot)
    config = _json(version.config, {})
    kb_ids = await _knowledge_ids(session, version, snapshot)
    required_layers = ["output", "system"]
    metadata = [_json(row.meta, {}) for row in ordered]
    if any(item.get("expected_tool") for item in metadata if isinstance(item, dict)):
        required_layers.append("tool")
    if kb_ids:
        required_layers.append("retrieval")
    for item in metadata:
        if not isinstance(item, dict):
            continue
        declared = item.get("required_layers")
        if isinstance(declared, list):
            required_layers.extend(str(layer) for layer in declared)
    required_layers = list(dict.fromkeys(required_layers))
    scorer_config = {"required_layers": required_layers}
    retrieval = None
    if kb_ids:
        corpus_rows = list((await session.execute(
            select(KnowledgeChunk.id, KnowledgeChunk.content)
            .where(KnowledgeChunk.kb_id.in_(kb_ids))
        )).all())
        manifest = _corpus_manifest(corpus_rows)
        settings = get_settings()
        retrieval = {
            "embedding_provider": settings.embedding_provider,
            "embedding_model": "hashing" if settings.embedding_provider == "hashing" else settings.embedding_model,
            "embedding_dimensions": settings.embedding_dim,
            "index_snapshot_id": f"corpus-{manifest[:16]}",
            "corpus_manifest_sha256": manifest,
            "top_k": 5,
        }
    digest = config.get("model_digest")
    if not isinstance(digest, str) or len(digest) != 64:
        digest = None
    seed = config.get("seed") if isinstance(config.get("seed"), int) else None
    conditions = {
        "suite": {
            "suite_id": suite.id, "case_ids": case_ids,
            "case_snapshots": [{
                "case_id": row.id, "input": row.input, "expected": row.expected,
                "metadata": public_legacy_dto(meta),
                "input_sha256": text_sha256(row.input),
                "expected_sha256": canonical_sha256(row.expected),
                "metadata_sha256": canonical_sha256(public_legacy_dto(meta)),
            } for row, meta in zip(ordered, metadata, strict=True)],
            "scorer": {
                "name": suite.scorer, "version": "v1", "config": scorer_config,
                "config_sha256": canonical_sha256(scorer_config),
            },
            "case_pass_threshold": 0.7,
            "gate_threshold_at_execution": float(threshold),
        },
        "execution": {
            "agent_version_id": version.id,
            "definition_sha256": canonical_sha256(public_snapshot),
            "provider": version.provider, "model": version.model, "model_digest": digest,
            "temperature": float(version.temperature), "seed": seed, "run_count": 1,
            "tool_definitions_sha256": canonical_sha256(public_legacy_dto(_json(version.tools, []))),
        },
        "retrieval": retrieval,
        "comparison": {
            "allowed_changes": ["/execution/agent_version_id", "/execution/definition_sha256"],
            "required_invariants": [
                "/suite/case_snapshots", "/suite/scorer", "/suite/case_pass_threshold",
                "/execution/provider", "/execution/model", "/execution/model_digest",
                "/execution/temperature", "/execution/seed", "/execution/run_count",
                "/execution/tool_definitions_sha256", "/retrieval",
            ],
        },
    }
    return GateConditions.model_validate(conditions).model_dump(mode="json")


__all__ = ["build_gate_conditions", "canonical_sha256"]
