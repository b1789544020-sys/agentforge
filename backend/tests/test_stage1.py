"""Stage 1: embeddings, persistent vectors, providers, and eval control."""
from __future__ import annotations

import asyncio
import sys
import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from app import config as config_module
from app.agent.memory import MemoryStore
from app.database import async_session_factory
from app.llm.client import LLMClient, make_client
from app.main import create_app
from app.memory.embeddings import HashingEmbedder, _reset_default_embedder, get_default_embedder
from app.memory.vector_store import _reset_default_store
from app.models import Agent, EvalCase, EvalRun


@pytest.mark.parametrize("provider,url,model", [
    ("kimi", "https://api.moonshot.cn/v1", "moonshot-v1-8k"),
    ("glm", "https://open.bigmodel.cn/api/paas/v4", "glm-4-flash"),
    ("doubao", "https://ark.cn-beijing.volces.com/api/v3", "doubao-seed-1-6-250615"),
])
@pytest.mark.asyncio
async def test_new_provider_presets(provider: str, url: str, model: str) -> None:
    client = make_client(provider, "test-key")
    assert isinstance(client, LLMClient)
    assert client.base_url == url
    assert client.model == model
    await client.close()


def test_chinese_hashing_fallback_is_nonzero_and_deterministic() -> None:
    embedder = HashingEmbedder(dim=64)
    first = embedder("用户喜欢中文回答")
    assert first == embedder("用户喜欢中文回答")
    assert any(value != 0 for value in first)


@pytest.mark.asyncio
async def test_inprocess_index_rebuilds_from_sql_after_restart(db_session) -> None:
    agent = Agent(name="reindex", provider="mock", model="mock")
    db_session.add(agent)
    await db_session.commit()
    _reset_default_store()
    store = MemoryStore(db_session, agent_id=agent.id)
    item = await store.add("咖啡 是 用户 最喜欢 的 饮品")

    # Simulate a process restart: SQL remains, the process-local index does not.
    _reset_default_store()
    restarted = MemoryStore(db_session, agent_id=agent.id)
    hits = await restarted.search("咖啡 饮品", mode="embed")
    assert item.id in {hit[0].id for hit in hits}


def test_sentence_transformer_adapter_uses_configured_local_model(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: dict[str, Any] = {}

    class FakeModel:
        def __init__(self, name: str, **kwargs: Any) -> None:
            calls["name"] = name
            calls.update(kwargs)

        def get_sentence_embedding_dimension(self) -> int:
            return 3

        def encode(self, _text: str, **_kwargs: Any) -> list[float]:
            return [0.6, 0.8, 0.0]

    monkeypatch.setenv("AGENTFORGE_EMBEDDING_PROVIDER", "sentence_transformers")
    monkeypatch.setenv("AGENTFORGE_EMBEDDING_MODEL", "/models/bge-small-zh-v1.5")
    monkeypatch.setitem(sys.modules, "sentence_transformers", SimpleNamespace(SentenceTransformer=FakeModel))
    config_module.get_settings.cache_clear()
    _reset_default_embedder()
    try:
        embedder = get_default_embedder()
        assert embedder.dim == 3
        assert embedder("中文") == [0.6, 0.8, 0.0]
        assert calls["name"] == "/models/bge-small-zh-v1.5"
        assert calls["local_files_only"] is True
    finally:
        _reset_default_embedder()
        config_module.get_settings.cache_clear()


def test_qdrant_store_upsert_search_delete_with_mock_client() -> None:
    from app.memory.vector_store import QdrantVectorStore

    class FakeQdrant:
        def __init__(self) -> None:
            self.points: dict[str, list[float]] = {}
            self.deleted: list[str] = []
            self.exists = False
            self.created = 0

        def collection_exists(self, _name: str) -> bool:
            return self.exists

        def create_collection(self, **_kwargs: Any) -> None:
            self.created += 1
            self.exists = True

        def upsert(self, *, collection_name: str, points: list[Any], wait: bool) -> None:
            assert collection_name == "test_memories" and wait
            self.points.update({str(point.id): point.vector for point in points})

        def query_points(self, *, collection_name: str, query: list[float], limit: int, with_payload: bool):
            return SimpleNamespace(points=[SimpleNamespace(id="a", score=0.99)][:limit])

        def delete(self, *, collection_name: str, points_selector: Any, wait: bool) -> None:
            self.deleted.extend(points_selector.points)

    client = FakeQdrant()
    store = QdrantVectorStore(url="http://mock", collection="test_memories", dim=2, client=client)
    store.upsert(["a"], [[1.0, 0.0]])
    assert client.points == {"a": [1.0, 0.0]}
    assert client.created == 1
    hits = store.search([1.0, 0.0], k=1)
    assert [(hit.id, hit.score) for hit in hits] == [("a", 0.99)]
    store.delete(["a"])
    assert client.deleted == ["a"]


@pytest.mark.asyncio
async def test_eval_scheduler_enforces_limit_and_priority() -> None:
    from app.eval.scheduler import EvalScheduler

    scheduler = EvalScheduler(max_concurrency=1)
    order: list[str] = []
    first_entered = asyncio.Event()
    release_first = asyncio.Event()

    async def worker(name: str, priority: int, first: bool = False) -> None:
        async with scheduler.slot(priority=priority):
            order.append(name)
            if first:
                first_entered.set()
                await release_first.wait()

    first = asyncio.create_task(worker("first", 10, True))
    await first_entered.wait()
    normal = asyncio.create_task(worker("normal", 10))
    high = asyncio.create_task(worker("high", 0))
    await asyncio.sleep(0)
    assert scheduler.queued == 2
    release_first.set()
    await asyncio.gather(first, normal, high)
    assert order == ["first", "high", "normal"]
    assert scheduler.active == 0


@pytest.mark.asyncio
async def test_cancel_endpoint_and_precancelled_runner() -> None:
    agent_id = str(uuid.uuid4())
    case_id = str(uuid.uuid4())
    eval_id = str(uuid.uuid4())
    async with async_session_factory() as session:
        session.add(Agent(id=agent_id, name="cancel-eval", provider="mock", model="mock"))
        session.add(EvalCase(id=case_id, name="case", input="hello"))
        session.add(EvalRun(id=eval_id, agent_id=agent_id, status="running", total=1))
        await session.commit()

    async with AsyncClient(
        transport=ASGITransport(app=create_app()), base_url="http://test"
    ) as client:
        response = await client.post(f"/api/agents/{agent_id}/evals/{eval_id}/cancel")
    assert response.status_code == 200, response.text
    assert response.json()["status"] in {"cancelling", "cancelled"}

    from app.eval.runner import EvalRunner

    event = asyncio.Event()
    event.set()
    async with async_session_factory() as session:
        agent = await session.get(Agent, agent_id)
        runner = EvalRunner(session, agent, cancel_event=event)
        result = await runner.run_eval([case_id], eval_run_id=eval_id)
        assert result.cases[0].status == "cancelled"
        persisted = await session.get(EvalRun, eval_id)
        assert persisted is not None and persisted.status == "cancelled"


@pytest.mark.asyncio
async def test_running_eval_cancels_agent_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.agent.loop import AgentLoop as ProductionLoop
    from app.llm.client import ChatResult
    from app.eval.runner import EvalRunner

    entered = asyncio.Event()
    release = asyncio.Event()

    class BlockingLLM:
        async def chat(self, *_args: Any, **_kwargs: Any) -> ChatResult:
            entered.set()
            await release.wait()
            return ChatResult(content="too late")

    agent_id = str(uuid.uuid4())
    case_id = str(uuid.uuid4())
    eval_id = str(uuid.uuid4())
    async with async_session_factory() as session:
        session.add(Agent(id=agent_id, name="active-cancel", provider="mock", model="mock"))
        session.add(EvalCase(id=case_id, name="case", input="hello"))
        await session.commit()

    llm = BlockingLLM()
    monkeypatch.setattr(
        "app.eval.runner.AgentLoop",
        lambda agent, session: ProductionLoop(agent, session, llm=llm),
    )
    event = asyncio.Event()
    async with async_session_factory() as session:
        agent = await session.get(Agent, agent_id)
        runner = EvalRunner(session, agent, cancel_event=event)
        task = asyncio.create_task(runner.run_eval([case_id], eval_run_id=eval_id))
        await asyncio.wait_for(entered.wait(), timeout=2)
        event.set()
        release.set()
        result = await asyncio.wait_for(task, timeout=2)
        assert result.cases[0].status == "cancelled"
        persisted = await session.get(EvalRun, eval_id)
        assert persisted is not None and persisted.status == "cancelled"
