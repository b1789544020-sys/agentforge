"""Run the Direction-1 two-regression gate scenario without external services.

Usage: python scripts/tests/quality_gate_e2e.py
The script uses the deterministic mock provider and an isolated in-memory DB.
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_DB_FD, _DB_NAME = tempfile.mkstemp(prefix="agentforge-gate-e2e-", suffix=".db")
os.close(_DB_FD)
_DB_FILE = Path(_DB_NAME)
_DB_FILE.unlink()
sys.path.insert(0, str(ROOT / "backend"))
os.environ.setdefault("AGENTFORGE_DATABASE_URL", f"sqlite+aiosqlite:///{_DB_FILE.as_posix()}")
os.environ.setdefault("AGENTFORGE_LLM_PROVIDER", "mock")

from httpx import ASGITransport, AsyncClient  # noqa: E402
from app.database import async_session_factory, init_db  # noqa: E402
from app.main import create_app  # noqa: E402
from app.models import EvalCase  # noqa: E402


def replies(bad: bool = False) -> dict[str, str]:
    return {f"case-{n}": "wrong" if bad and n <= 2 else f"answer-{n}" for n in range(1, 5)}


async def main() -> None:
    await init_db()
    case_ids: list[str] = []
    async with async_session_factory() as session:
        for n in range(1, 5):
            case_id = str(uuid.uuid4())
            case_ids.append(case_id)
            session.add(EvalCase(
                id=case_id, dataset="gate-e2e", name=f"case-{n}", input=f"case-{n}",
                expected=f"answer-{n}", meta="{}", index=n - 1, tool_calls="[]", score_details="{}",
            ))
        await session.commit()

    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/api/agents", json={
            "name": "quality-gate-e2e", "provider": "mock", "model": "mock-v1",
            "system_prompt": "good", "max_steps": 2, "config": {"mock_replies": replies()},
        })
        response.raise_for_status()
        agent_id = response.json()["id"]
        response = await client.post("/api/suites", json={
            "name": "four-case-regression", "agent_id": agent_id, "case_ids": case_ids,
            "pass_threshold": 0.75, "auto_on_save": False,
        })
        response.raise_for_status()
        suite_id = response.json()["id"]

        async def trigger() -> dict:
            version = (await client.get(f"/api/agents/{agent_id}/versions")).json()[0]
            response = await client.post(f"/api/agents/{agent_id}/gates", json={
                "agent_version_id": version["id"], "suite_id": suite_id,
            })
            response.raise_for_status()
            gate_id = response.json()["id"]
            response = await client.get(f"/api/agents/{agent_id}/gates/{gate_id}")
            response.raise_for_status()
            return response.json()

        good = await trigger()
        assert good["status"] == "passed" and good["pass_rate"] == 1.0, good
        response = await client.patch(f"/api/agents/{agent_id}", json={
            "system_prompt": "bad", "config": {"mock_replies": replies(bad=True)},
        })
        response.raise_for_status()
        bad = await trigger()
        assert bad["status"] == "failed" and bad["pass_rate"] == 0.5, bad
        assert set(bad["failed_case_ids"]) == set(case_ids[:2]), bad
        regressions = [row for row in bad["diff"] if row["change"] == "new_failure"]
        assert len(regressions) == 2, bad
        print(f"PASS: v1={good['status']} ({good['pass_rate']:.0%}); "
              f"v2={bad['status']} ({bad['pass_rate']:.0%}); new_failures={bad['failed_case_ids']}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    finally:
        from app.database import async_engine
        asyncio.run(async_engine.dispose())
        _DB_FILE.unlink(missing_ok=True)
