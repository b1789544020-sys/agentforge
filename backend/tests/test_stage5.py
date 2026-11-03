"""Stage 5 observability, backup/restore, and delivery-script tests."""
from __future__ import annotations

import json
import logging
import sqlite3
import sys
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import create_app
from app.observability import JsonFormatter


ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.asyncio
async def test_request_id_readiness_and_instance_metrics() -> None:
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/health", headers={"X-Request-ID": "request-123"})
        assert response.status_code == 200
        assert response.headers["X-Request-ID"] == "request-123"
        ready = await client.get("/api/health/ready")
        assert ready.status_code == 200, ready.text
        assert ready.json()["components"]["database"] == "ok"
        live = await client.get("/health/live")
        assert live.status_code == 200 and live.json() == {"status": "ok"}
        metrics = await client.get("/api/metrics")
        assert metrics.status_code == 200, metrics.text
        body = metrics.json()
        assert body["requests_total"] >= 1
        assert body["duration_ms"]["p95"] >= 0
        assert {"eval_active", "eval_queued", "celery_broker"}.issubset(body["queue"])


def test_json_formatter_includes_request_id() -> None:
    record = logging.LogRecord("agentforge.test", logging.INFO, __file__, 1, "hello", (), None)
    record.request_id = "abc-123"
    payload = json.loads(JsonFormatter().format(record))
    assert payload["message"] == "hello"
    assert payload["request_id"] == "abc-123"
    assert payload["level"] == "INFO"


def test_sqlite_backup_and_restore_roundtrip(tmp_path: Path) -> None:
    sys.path.insert(0, str(ROOT / "scripts"))
    from backup import backup_database
    from restore import restore_database

    source = tmp_path / "source.db"
    backup = tmp_path / "backup.db"
    with sqlite3.connect(source) as connection:
        connection.execute("CREATE TABLE items (value TEXT)")
        connection.execute("INSERT INTO items VALUES ('original')")
    url = f"sqlite+aiosqlite:///{source.as_posix()}"
    backup_database(url, backup)
    with sqlite3.connect(source) as connection:
        connection.execute("UPDATE items SET value='changed'")
    restore_database(url, backup)
    with sqlite3.connect(source) as connection:
        assert connection.execute("SELECT value FROM items").fetchone() == ("original",)


def test_bounded_performance_scenarios_and_ops_docs_exist() -> None:
    locust = (ROOT / "scripts" / "perf" / "locustfile.py").read_text(encoding="utf-8")
    k6 = (ROOT / "scripts" / "perf" / "k6-smoke.js").read_text(encoding="utf-8")
    for scenario in ("short", "ramp", "spike", "soak"):
        assert scenario in locust and scenario in k6
    assert "return None" in locust
    assert "large_eval.py" in {path.name for path in (ROOT / "scripts" / "perf").iterdir()}
    production = (ROOT / "docs" / "PRODUCTION.md").read_text(encoding="utf-8")
    performance = (ROOT / "docs" / "PERFORMANCE.md").read_text(encoding="utf-8")
    assert "scripts/backup.py" in production and "scripts/restore.py" in production
    assert "通过测试不等于生产验收完成" in production
    assert "尚未产生实测压测结果" in performance
