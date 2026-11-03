"""Direction 5: deployment locking, templates, and private knowledge tests."""
from __future__ import annotations

import io
import uuid
import zipfile

import pytest
from httpx import ASGITransport, AsyncClient

from app.agent.loop import AgentLoop
from app.database import async_session_factory
from app.knowledge import chinese_chunks, extract_document
from app.main import create_app
from app.models import Agent, AgentVersion, Deployment


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://test")


async def _agent(client: AsyncClient, answer: str = "v1") -> str:
    response = await client.post("/api/agents", json={
        "name": f"delivery-{uuid.uuid4()}", "provider": "mock", "model": "mock",
        "system_prompt": "delivery test", "max_steps": 2, "tools": [],
        "config": {"mock_replies": {"question": answer}},
    })
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def _versions(client: AsyncClient, agent_id: str) -> list[dict]:
    response = await client.get(f"/api/agents/{agent_id}/versions")
    assert response.status_code == 200
    return response.json()


async def _set_gate(version_id: str, status: str = "passed") -> None:
    async with async_session_factory() as session:
        version = await session.get(AgentVersion, version_id)
        version.gate_status = status
        await session.commit()


@pytest.mark.asyncio
async def test_deployment_is_version_locked_and_manageable() -> None:
    async with _client() as client:
        agent_id = await _agent(client)
        version1 = (await _versions(client, agent_id))[0]
        denied = await client.post("/api/deployments", json={"agent_version_id": version1["id"], "name": "blocked"})
        assert denied.status_code == 409
        await _set_gate(version1["id"])
        published = await client.post("/api/deployments", json={"agent_version_id": version1["id"], "name": "production"})
        assert published.status_code == 201, published.text
        body = published.json()
        token = body["token"]
        assert body["page_url"].endswith(token) and body["embed_url"].endswith(token)
        async with async_session_factory() as session:
            deployment = await session.get(Deployment, body["id"])
            assert deployment.token_hash != token and token not in deployment.runtime_spec

        response = await client.post(f"/api/deliver/{token}/chat", json={"input": "question"})
        assert response.status_code == 200 and response.json()["output"] == "v1", response.text
        assert response.json()["trace_id"]
        changed = await client.patch(f"/api/agents/{agent_id}", json={"config": {"mock_replies": {"question": "v2"}}})
        assert changed.status_code == 200
        version2 = (await _versions(client, agent_id))[0]
        old = await client.post(f"/api/deliver/{token}/chat", json={"input": "question"})
        assert old.json()["output"] == "v1"
        await _set_gate(version2["id"])
        upgraded = await client.post(f"/api/deployments/{body['id']}/publish", json={"agent_version_id": version2["id"]})
        assert upgraded.status_code == 200, upgraded.text
        current = await client.post(f"/api/deliver/{token}/chat", json={"input": "question"})
        assert current.json()["output"] == "v2"
        rollback = await client.post(f"/api/deployments/{body['id']}/rollback")
        assert rollback.json()["agent_version_id"] == version1["id"]
        rolled = await client.post(f"/api/deliver/{token}/chat", json={"input": "question"})
        assert rolled.json()["output"] == "v1"

        await client.post(f"/api/deployments/{body['id']}/stop")
        assert (await client.post(f"/api/deliver/{token}/chat", json={"input": "question"})).status_code == 403
        await client.post(f"/api/deployments/{body['id']}/start")
        await client.post(f"/api/deployments/{body['id']}/revoke")
        assert (await client.post(f"/api/deliver/{token}/chat", json={"input": "question"})).status_code == 410
        rotated = await client.post(f"/api/deployments/{body['id']}/rotate-token")
        new_token = rotated.json()["token"]
        await client.post(f"/api/deployments/{body['id']}/start")
        assert (await client.post(f"/api/deliver/{token}/chat", json={"input": "question"})).status_code == 404
        assert (await client.post(f"/api/deliver/{new_token}/chat", json={"input": "question"})).status_code == 200


@pytest.mark.asyncio
async def test_builtin_templates_instantiate_runnable_agents() -> None:
    async with _client() as client:
        templates = (await client.get("/api/templates")).json()
        assert len(templates) >= 6
        source = next(row for row in templates if row["id"] == "builtin-customer-service")
        created = await client.post("/api/templates/builtin-customer-service/instantiate", json={})
        assert created.status_code == 201, created.text
        agent_id = created.json()["agent"]["id"]
        example = source["examples"][0]
        run = await client.post(f"/api/agents/{agent_id}/runs", json={"input": example["input"]})
        assert run.status_code == 200 and run.json()["output"] == example["expected"], run.text


@pytest.mark.asyncio
async def test_chinese_knowledge_upload_search_and_runtime_citation() -> None:
    async with _client() as client:
        agent_id = await _agent(client)
        kb = await client.post("/api/knowledge-bases", json={"name": "产品手册"})
        assert kb.status_code == 201, kb.text
        kb_id = kb.json()["id"]
        text = """# 退款政策

退款申请审核需要 3 个工作日。审核通过后原路退回，到账时间以银行为准。

# 服务等级

企业版提供专属客户经理和全年技术支持。"""
        uploaded = await client.post(f"/api/knowledge-bases/{kb_id}/documents",
                                     files={"file": ("manual.md", text.encode("utf-8"), "text/markdown")})
        assert uploaded.status_code == 201, uploaded.text
        assert uploaded.json()["status"] == "ready" and uploaded.json()["chunks"] >= 2
        bound = await client.put(f"/api/agents/{agent_id}/knowledge/{kb_id}")
        assert bound.status_code == 201, bound.text
        hits = await client.get(f"/api/knowledge-bases/{kb_id}/search", params={
            "agent_id": agent_id, "q": "退款审核需要多久？",
        })
        assert hits.status_code == 200, hits.text
        assert hits.json() and "3 个工作日" in hits.json()[0]["content"]
        assert hits.json()[0]["document"] == "manual.md"
        async with async_session_factory() as session:
            agent = await session.get(Agent, agent_id)
            context = await AgentLoop(agent, session)._build_context("退款审核需要多久？")
            system_text = "\n".join(str(row.get("content")) for row in context.messages if row["role"] == "system")
            assert "3 个工作日" in system_text and "manual.md#退款政策" in system_text


def test_docx_extraction_and_chinese_sentence_boundaries() -> None:
    buffer = io.BytesIO()
    xml = """<?xml version='1.0' encoding='UTF-8' standalone='yes'?>
    <w:document xmlns:w='http://schemas.openxmlformats.org/wordprocessingml/2006/main'><w:body>
    <w:p><w:r><w:t>第一段中文。</w:t></w:r></w:p><w:p><w:r><w:t>第二段中文。</w:t></w:r></w:p>
    </w:body></w:document>"""
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("word/document.xml", xml)
    extracted = extract_document("guide.docx", buffer.getvalue())
    assert "第一段中文" in extracted and "第二段中文" in extracted
    chunks = chinese_chunks("# 标题\n\n第一句完整。第二句也完整。", max_chars=20)
    assert chunks[0]["section"] == "标题"
    assert chunks[0]["content"].endswith("。")
