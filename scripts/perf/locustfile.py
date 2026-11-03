"""AgentForge short/ramp/spike/soak load scenarios.

Example: LOAD_SHAPE=ramp locust -f scripts/perf/locustfile.py --host http://localhost:8000
"""
from __future__ import annotations

import os

from locust import HttpUser, LoadTestShape, between, task


class AgentForgeUser(HttpUser):
    wait_time = between(0.05, 0.2)

    def on_start(self) -> None:
        token = os.environ.get("PERF_TOKEN", "")
        if token:
            self.client.headers["Authorization"] = f"Bearer {token}"

    @task(5)
    def health(self) -> None:
        self.client.get("/api/health", name="/api/health")

    @task(1)
    def agents(self) -> None:
        self.client.get("/api/agents", name="/api/agents")


_SCENARIOS = {
    "short": [(30, 2, 1)],
    "ramp": [(30, 2, 1), (60, 5, 1), (120, 10, 1), (180, 0, 1)],
    "spike": [(30, 20, 20), (60, 0, 20)],
    "soak": [(300, 10, 2), (330, 0, 2)],
}


class ScenarioShape(LoadTestShape):
    """Select one bounded scenario with LOAD_SHAPE (default short)."""

    def tick(self):
        run_time = self.get_run_time()
        elapsed = 0
        scenario = os.environ.get("LOAD_SHAPE", "short")
        if scenario not in _SCENARIOS:
            raise ValueError(f"unknown LOAD_SHAPE={scenario!r}")
        for duration, users, spawn_rate in _SCENARIOS[scenario]:
            elapsed += duration
            if run_time < elapsed:
                return users, spawn_rate
        return None
