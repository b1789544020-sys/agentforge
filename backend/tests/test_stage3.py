"""Stage 3 durable execution and cross-worker infrastructure tests."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest
import yaml

from app import config as config_module
from app.agent.loop import AgentLoop, LoopConfig, RunState
from app.agent.tools import ToolExecutor, ToolRegistry, ToolSpec
from app.api.ws_broadcaster import RunBroadcaster
from app.database import async_session_factory
from app.llm.client import ChatResult, MockLLMClient, ToolCall
from app.models import Agent, EvalRun, Run


ROOT = Path(__file__).resolve().parents[2]


def _registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(ToolSpec(
        name="echo", description="echo", handler=lambda text: {"text": text},
        schema={"type": "object", "properties": {"text": {"type": "string"}}},
    ))
    return registry


@pytest.mark.asyncio
async def test_resume_restores_state_and_cumulative_counters(db_session) -> None:
    agent = Agent(name="recover", provider="mock", model="mock", max_steps=5, tools='["echo"]')
    db_session.add(agent)
    await db_session.commit()
    registry = _registry()
    first_llm = MockLLMClient(responses=[ChatResult(
        content="", tool_calls=[ToolCall(id="one", name="echo", arguments={"text": "x"})],
        prompt_tokens=3, completion_tokens=2,
    )])
    holder: dict[str, AgentLoop] = {}

    def pause_on_tool_result(step) -> None:
        if step.kind == "tool_result":
            holder["loop"].pause()

    first = AgentLoop(
        agent, db_session, llm=first_llm, tools=registry,
        tool_executor=ToolExecutor(registry), config=LoopConfig(on_step=pause_on_tool_result),
    )
    holder["loop"] = first
    paused = await first.run("task")
    assert paused.status == "paused"
    state = RunState.from_json((await db_session.get(Run, paused.run_id)).state)
    assert state.step_count == paused.step_count
    assert state.duration_ms == paused.duration_ms
    assert state.step_index == 2

    second = AgentLoop(
        agent, db_session,
        llm=MockLLMClient(responses=[ChatResult(content="done", prompt_tokens=4, completion_tokens=3)]),
        tools=registry, tool_executor=ToolExecutor(registry),
    )
    resumed = await second.resume(paused.run_id)
    assert resumed.status == "success"
    assert resumed.output == "done"
    assert resumed.step_count > len(resumed.steps)
    assert resumed.step_count == paused.step_count + len(resumed.steps)
    assert resumed.duration_ms >= paused.duration_ms
    assert resumed.total_tokens == paused.total_tokens + 7


@pytest.mark.asyncio
async def test_recovery_requeues_pending_and_checkpointed_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.tasks import recover_interrupted_runs

    agent_id = "agent-recovery"
    async with async_session_factory() as session:
        session.add(Agent(id=agent_id, name="recovery", provider="mock", model="mock"))
        session.add(Run(id="pending-run", agent_id=agent_id, input="a", status="pending"))
        session.add(Run(
            id="checkpoint-run", agent_id=agent_id, input="b", status="running",
            state=RunState(run_id="checkpoint-run", task="b", step_index=2).to_json(),
        ))
        await session.commit()
    monkeypatch.setenv("AGENTFORGE_TASK_BACKEND", "celery")
    monkeypatch.setenv("AGENTFORGE_CONTROL_BACKEND", "memory")
    config_module.get_settings.cache_clear()
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr("app.dispatch.submit_run", lambda run_id, _agent_id, *_args: calls.append(("run", run_id)))
    monkeypatch.setattr("app.dispatch.submit_resume", lambda run_id, _agent_id: calls.append(("resume", run_id)))
    try:
        assert await recover_interrupted_runs() == 2
        assert set(calls) == {("run", "pending-run"), ("resume", "checkpoint-run")}
    finally:
        config_module.get_settings.cache_clear()


class FakeRedis:
    def __init__(self) -> None:
        self.data: dict[str, str] = {}
        self.subscribers: list[FakePubSub] = []

    async def set(self, key: str, value: str, **_kwargs: Any) -> bool:
        self.data[key] = value
        return True

    async def get(self, key: str) -> str | None:
        return self.data.get(key)

    async def delete(self, key: str) -> None:
        self.data.pop(key, None)

    def pubsub(self) -> "FakePubSub":
        channel = FakePubSub()
        self.subscribers.append(channel)
        return channel

    async def publish(self, channel: str, data: str) -> None:
        for subscriber in self.subscribers:
            await subscriber.messages.put({"channel": channel, "data": data})


class FakePubSub:
    def __init__(self) -> None:
        self.messages: asyncio.Queue[dict[str, str]] = asyncio.Queue()

    async def psubscribe(self, _pattern: str) -> None:
        return None

    async def get_message(self, **_kwargs: Any) -> dict[str, str] | None:
        try:
            return await asyncio.wait_for(self.messages.get(), timeout=0.05)
        except asyncio.TimeoutError:
            return None

    async def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_redis_pause_and_pubsub_cross_workers(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.agent import control
    from app.infra import redis as redis_module

    fake = FakeRedis()
    redis_module._client = fake
    monkeypatch.setenv("AGENTFORGE_CONTROL_BACKEND", "redis")
    config_module.get_settings.cache_clear()
    receiver = RunBroadcaster()
    try:
        assert await control.request_pause_async("run-redis")
        assert await control.pause_requested("run-redis")
        await control.clear_pause_async("run-redis")
        assert not await control.pause_requested("run-redis")

        queue = await receiver.subscribe("run-redis")
        await receiver.start()
        RunBroadcaster().publish_step("run-redis", {"id": "remote-step"})
        message = await asyncio.wait_for(queue.get(), timeout=1)
        assert message["event"] == "run.step" and message["id"] == "remote-step"
    finally:
        await receiver.stop()
        redis_module._client = None
        config_module.get_settings.cache_clear()


def test_celery_dispatch_and_worker_safety_config(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.dispatch import submit_run
    from app.tasks import celery_app, execute_run_task

    monkeypatch.setenv("AGENTFORGE_TASK_BACKEND", "celery")
    config_module.get_settings.cache_clear()
    delay = Mock()
    monkeypatch.setattr(execute_run_task, "delay", delay)
    try:
        submit_run("run-id", "agent-id", "input", None)
        delay.assert_called_once_with("run-id")
    finally:
        config_module.get_settings.cache_clear()
    assert celery_app.conf.task_acks_late is True
    assert celery_app.conf.task_reject_on_worker_lost is True
    assert celery_app.conf.worker_prefetch_multiplier == 1
    assert "stuck-run-watchdog" in celery_app.conf.beat_schedule


@pytest.mark.asyncio
async def test_terminal_eval_retry_is_idempotent() -> None:
    from app.eval.service import execute_eval

    async with async_session_factory() as session:
        session.add(Agent(id="eval-agent", name="eval", provider="mock", model="mock"))
        session.add(EvalRun(
            id="eval-terminal", agent_id="eval-agent", status="completed",
            request="not valid json and irrelevant for a terminal retry",
        ))
        await session.commit()
    await execute_eval("eval-terminal")


def test_compose_has_durable_workers_migrations_and_production_frontend() -> None:
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    services = compose["services"]
    assert {"postgres", "qdrant", "redis", "migrate", "celery-worker", "celery-beat"}.issubset(services)
    assert services["agentforge-api"]["depends_on"]["migrate"]["condition"] == "service_completed_successfully"
    assert services["agentforge-api"]["environment"]["AGENTFORGE_RUN_MIGRATIONS"] == "false"
    assert services["celery-worker"]["depends_on"]["agentforge-api"]["condition"] == "service_healthy"
    frontend = (ROOT / "docker" / "frontend.Dockerfile").read_text(encoding="utf-8")
    assert "FROM nginx:" in frontend and "npm run dev" not in frontend
    nginx = (ROOT / "frontend" / "nginx.conf").read_text(encoding="utf-8")
    assert "proxy_set_header Upgrade" in nginx and "agentforge-api:8000" in nginx
