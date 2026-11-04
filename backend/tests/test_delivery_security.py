"""Public delivery rate limiting, proxy trust, and HTTP boundary tests."""
from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from starlette.requests import Request

from app.delivery_security import DeliveryRateLimiter, RateLimiterUnavailable
from app.database import async_session_factory
from app.main import create_app


@pytest.mark.asyncio
async def test_memory_limits_are_independent_and_reset() -> None:
    now = [0.0]
    limiter = DeliveryRateLimiter(requests=2, window_seconds=60, clock=lambda: now[0])
    assert (await limiter.check("token-a", "192.0.2.1")).remaining == 1
    assert (await limiter.check("token-b", "192.0.2.2")).allowed
    assert (await limiter.check("token-a", "192.0.2.1")).remaining == 0
    blocked = await limiter.check("token-a", "192.0.2.1")
    assert not blocked.allowed and blocked.retry_after == 60
    now[0] = 60.0
    assert (await limiter.check("token-a", "192.0.2.1")).allowed


class _SharedRedis:
    """Small deterministic Redis-script test double shared by two workers."""

    def __init__(self) -> None:
        self.counts: dict[str, int] = {}

    async def eval(self, _script: str, _keys: int, key: str, window: int):
        self.counts[key] = self.counts.get(key, 0) + 1
        return [self.counts[key], window]


@pytest.mark.asyncio
async def test_redis_token_limit_is_shared_between_workers() -> None:
    redis = _SharedRedis()
    first = DeliveryRateLimiter(requests=1, backend="redis", redis_factory=lambda: redis)
    second = DeliveryRateLimiter(requests=1, backend="redis", redis_factory=lambda: redis)
    assert (await first.check("shared-token", "192.0.2.1")).allowed
    blocked = await second.check("shared-token", "192.0.2.2")
    assert not blocked.allowed and blocked.retry_after == 60
    assert (await second.check("other-token", "192.0.2.3")).allowed


class _BrokenRedis:
    async def eval(self, *_args):
        raise ConnectionError("redis unavailable")


@pytest.mark.asyncio
async def test_redis_failure_is_closed_by_default_and_explicitly_open() -> None:
    closed = DeliveryRateLimiter(backend="redis", redis_factory=_BrokenRedis)
    with pytest.raises(RateLimiterUnavailable):
        await closed.check("token", "192.0.2.1")
    opened = DeliveryRateLimiter(
        backend="redis", redis_factory=_BrokenRedis, fail_closed=False,
    )
    decision = await opened.check("token", "192.0.2.1")
    assert decision.allowed and decision.degraded


def _proxy_request() -> Request:
    return Request({
        "type": "http", "http_version": "1.1", "method": "POST", "path": "/",
        "headers": [(b"x-forwarded-for", b"203.0.113.7, 10.0.0.1")],
        "client": ("10.0.0.5", 12345), "server": ("test", 80), "scheme": "http",
    })


def test_forwarded_for_requires_a_trusted_proxy_chain() -> None:
    trusted = DeliveryRateLimiter(trusted_proxies=["10.0.0.0/8"])
    untrusted = DeliveryRateLimiter()
    assert trusted.client_ip(_proxy_request()) == "203.0.113.7"
    assert untrusted.client_ip(_proxy_request()) == "10.0.0.5"


async def _published_token(client: AsyncClient) -> str:
    created = await client.post("/api/agents", json={
        "name": f"limited-{uuid.uuid4()}", "provider": "mock", "model": "mock",
        "system_prompt": "rate-limit test", "max_steps": 2, "tools": [],
        "config": {"mock_replies": {"question": "ok"}},
    })
    assert created.status_code == 201, created.text
    agent_id = created.json()["id"]
    versions = await client.get(f"/api/agents/{agent_id}/versions")
    version_id = versions.json()[0]["id"]
    async with async_session_factory() as session:
        from app.models import AgentVersion
        version = await session.get(AgentVersion, version_id)
        version.gate_status = "passed"
        await session.commit()
    published = await client.post("/api/deployments", json={
        "agent_version_id": version_id, "name": "rate-limited",
    })
    assert published.status_code == 201, published.text
    return published.json()["token"]


@pytest.mark.asyncio
async def test_public_api_returns_429_headers_and_rejects_oversized_input() -> None:
    app = create_app()
    app.state.delivery_rate_limiter = DeliveryRateLimiter(requests=4, window_seconds=60)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        token = await _published_token(client)
        path = f"/api/deliver/{token}/chat"
        oversized = await client.post(path, json={"input": "x" * 32_001})
        assert oversized.status_code == 422
        assert oversized.headers["x-ratelimit-remaining"] == "3"
        too_long_session = await client.post(path, json={
            "input": "question", "session_id": "s" * 129,
        })
        assert too_long_session.status_code == 422
        assert too_long_session.headers["x-ratelimit-remaining"] == "2"
        first = await client.post(path, json={"input": "question"})
        assert first.status_code == 200, first.text
        assert first.headers["x-ratelimit-limit"] == "4"
        assert first.headers["x-ratelimit-remaining"] == "1"
        assert first.headers["cache-control"] == "no-store"
        assert first.headers["x-content-type-options"] == "nosniff"
        assert first.headers["referrer-policy"] == "no-referrer"
        assert (await client.post(path, json={"input": "question"})).status_code == 200
        blocked = await client.post(path, json={"input": "question"})
        assert blocked.status_code == 429, blocked.text
        assert 1 <= int(blocked.headers["retry-after"]) <= 60
        assert blocked.headers["x-ratelimit-remaining"] == "0"
        assert blocked.headers["x-request-id"]

        app.state.delivery_rate_limiter = DeliveryRateLimiter(
            backend="redis", redis_factory=_BrokenRedis,
        )
        unavailable = await client.post(path, json={"input": "question"})
        assert unavailable.status_code == 503
        assert unavailable.headers["retry-after"] == "5"
        assert unavailable.headers["cache-control"] == "no-store"
        assert unavailable.headers["x-request-id"]
