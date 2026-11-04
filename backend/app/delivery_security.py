"""Abuse controls for token-authenticated public delivery endpoints.

Redis fixed windows are shared by every API worker. The in-memory backend is
intentionally limited to development/single-process installations.
"""
from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import logging
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any, Literal

from fastapi import Request
from fastapi.responses import JSONResponse

logger = logging.getLogger("agentforge.delivery")

_REDIS_FIXED_WINDOW = """
local count = redis.call('INCR', KEYS[1])
if count == 1 then
  redis.call('EXPIRE', KEYS[1], ARGV[1])
end
local ttl = redis.call('TTL', KEYS[1])
if ttl < 0 then
  redis.call('EXPIRE', KEYS[1], ARGV[1])
  ttl = tonumber(ARGV[1])
end
return {count, ttl}
"""


class RateLimiterUnavailable(RuntimeError):
    """The configured limiter cannot provide a trustworthy decision."""


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    limit: int
    remaining: int
    reset_after: int
    retry_after: int = 0
    degraded: bool = False


class DeliveryRateLimiter:
    """Apply independent per-token and per-client fixed-window limits."""

    def __init__(
        self,
        *,
        requests: int = 60,
        window_seconds: int = 60,
        backend: Literal["auto", "memory", "redis"] = "auto",
        control_backend: str = "memory",
        fail_closed: bool = True,
        trusted_proxies: Iterable[str] = (),
        redis_factory: Callable[[], Any] | None = None,
        clock: Callable[[], float] = time.time,
        memory_max_keys: int = 10_000,
    ) -> None:
        if requests < 0 or window_seconds < 1 or memory_max_keys < 1:
            raise ValueError("invalid delivery rate-limit configuration")
        self.requests = requests
        self.window_seconds = window_seconds
        self.backend = "redis" if backend == "auto" and control_backend == "redis" else (
            "memory" if backend == "auto" else backend
        )
        self.fail_closed = fail_closed
        self._redis_factory = redis_factory
        self._clock = clock
        self._memory_max_keys = memory_max_keys
        self._windows: dict[str, tuple[int, int]] = {}
        self._lock = asyncio.Lock()
        self._trusted_networks = tuple(
            ipaddress.ip_network(value, strict=False) for value in trusted_proxies
        )

    @classmethod
    def from_settings(cls, settings: Any) -> "DeliveryRateLimiter":
        return cls(
            requests=settings.delivery_rate_limit_requests,
            window_seconds=settings.delivery_rate_limit_window_seconds,
            backend=settings.delivery_rate_limit_backend,
            control_backend=settings.control_backend,
            fail_closed=settings.delivery_rate_limit_fail_closed,
            trusted_proxies=settings.delivery_trusted_proxies,
        )

    def client_ip(self, request: Request) -> str:
        """Unwrap XFF only across explicitly trusted proxy hops."""
        direct = request.client.host if request.client else "unknown"
        candidate = direct
        forwarded = [part.strip() for part in request.headers.get("x-forwarded-for", "").split(",") if part.strip()]
        for hop in reversed(forwarded):
            try:
                address = ipaddress.ip_address(candidate)
            except ValueError:
                break
            if not any(address in network for network in self._trusted_networks):
                break
            candidate = hop
        return candidate[:255]

    @staticmethod
    def _scope(kind: str, value: str) -> str:
        digest = hashlib.sha256(f"{kind}:{value}".encode("utf-8")).hexdigest()
        return f"{kind}:{digest}"

    async def check(self, token: str, client_ip: str) -> RateLimitDecision:
        if self.requests == 0:
            return RateLimitDecision(True, 0, 0, 0)
        scopes = (self._scope("token", token), self._scope("ip", client_ip))
        try:
            decisions = await self._check_memory(scopes) if self.backend == "memory" else [
                await self._check_redis(scope) for scope in scopes
            ]
        except Exception as exc:
            logger.error("Delivery rate limiter unavailable; fail_closed=%s", self.fail_closed)
            if self.fail_closed:
                raise RateLimiterUnavailable("delivery rate limiter unavailable") from exc
            return RateLimitDecision(True, self.requests, self.requests, self.window_seconds, degraded=True)
        allowed = all(item.allowed for item in decisions)
        retry = max((item.retry_after for item in decisions), default=0) if not allowed else 0
        return RateLimitDecision(
            allowed, self.requests, min(item.remaining for item in decisions),
            max(item.reset_after for item in decisions), retry,
        )

    async def _check_memory(self, scopes: tuple[str, ...]) -> list[RateLimitDecision]:
        now = self._clock()
        window = int(now // self.window_seconds)
        reset = max(1, int(self.window_seconds - (now % self.window_seconds)))
        async with self._lock:
            new_count = sum(
                1 for scope in scopes
                if self._windows.get(scope, (window - 1, 0))[0] != window
            )
            if len(self._windows) + new_count > self._memory_max_keys:
                self._windows = {key: value for key, value in self._windows.items() if value[0] == window}
            if len(self._windows) + new_count > self._memory_max_keys:
                raise RateLimiterUnavailable("in-memory delivery rate-limit capacity exceeded")
            decisions = []
            for scope in scopes:
                old_window, count = self._windows.get(scope, (window, 0))
                count = count + 1 if old_window == window else 1
                self._windows[scope] = (window, count)
                allowed = count <= self.requests
                decisions.append(RateLimitDecision(
                    allowed, self.requests, max(0, self.requests - count), reset, 0 if allowed else reset,
                ))
            return decisions

    async def _check_redis(self, scope: str) -> RateLimitDecision:
        if self._redis_factory is None:
            from app.infra.redis import get_redis
            self._redis_factory = get_redis
        window = int(self._clock() // self.window_seconds)
        key = f"agentforge:delivery:{window}:{scope}"
        raw = await self._redis_factory().eval(_REDIS_FIXED_WINDOW, 1, key, self.window_seconds)
        count, ttl = (int(value) for value in raw)
        reset = max(1, ttl)
        allowed = count <= self.requests
        return RateLimitDecision(
            allowed, self.requests, max(0, self.requests - count), reset, 0 if allowed else reset,
        )


async def delivery_security_middleware(request: Request, call_next):
    path = request.url.path
    is_chat = request.method == "POST" and path.startswith("/api/deliver/") and path.endswith("/chat")
    rate_headers: dict[str, str] = {}
    response = None
    if is_chat:
        token = path[len("/api/deliver/"):-len("/chat")].strip("/")
        limiter = request.app.state.delivery_rate_limiter
        try:
            decision = await limiter.check(token, limiter.client_ip(request))
        except RateLimiterUnavailable:
            response = JSONResponse(
                {"detail": "delivery rate limiter unavailable"}, status_code=503,
                headers={"Retry-After": "5"},
            )
        else:
            if decision.limit:
                rate_headers = {
                    "X-RateLimit-Limit": str(decision.limit),
                    "X-RateLimit-Remaining": str(decision.remaining),
                    "X-RateLimit-Reset": str(decision.reset_after),
                }
            if not decision.allowed:
                response = JSONResponse(
                    {"detail": "delivery rate limit exceeded"}, status_code=429,
                    headers={**rate_headers, "Retry-After": str(max(1, decision.retry_after))},
                )
    if response is None:
        response = await call_next(request)
    if path.startswith("/api/deliver/"):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        for name, value in rate_headers.items():
            response.headers[name] = value
    return response


__all__ = [
    "DeliveryRateLimiter", "RateLimitDecision", "RateLimiterUnavailable",
    "delivery_security_middleware",
]
