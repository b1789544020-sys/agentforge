"""Tests for the tool-metadata plugin layer (0.1.3 #3).

Exercises the ``tool_metadata`` table, the merge logic in
:mod:`app.api.tools_metadata`, and the HTTP CRUD endpoints.
"""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from app.api.tools_metadata import get_enabled_tool_names, merge_metadata
from app.models import ToolMetadata


# ---------------------------------------------------------------------------
# Pure-function tests (no HTTP)
# ---------------------------------------------------------------------------


def _make_spec_dict(name: str = "echo") -> dict:
    """Build a canonical spec dict for the merge tests."""
    return {
        "name": name,
        "description": f"echo {name}",
        "risk": "safe",
        "scope": "general",
        "parameters": {"type": "object", "properties": {}},
    }


def test_merge_registry_only():
    """Without a DB row, the merge returns the spec's defaults."""
    out = merge_metadata(_make_spec_dict(), None)
    assert out["name"] == "echo"
    assert out["description"] == "echo echo"
    assert out["source"] == "registry"
    assert out["enabled"] is True


def test_merge_override_replaces_fields():
    """A DB row that explicitly sets a field wins over the spec."""
    row = ToolMetadata(
        name="echo",
        description="A friendlier description",
        category="utility",
        enabled=True,
    )
    out = merge_metadata(_make_spec_dict(), row)
    assert out["description"] == "A friendlier description"
    assert out["category"] == "utility"
    assert out["source"] == "override"
    # risk was not overridden, the registry default should be returned
    assert out["risk"] == "safe"


def test_merge_disable_flag():
    """A row with ``enabled=False`` flips the merged enabled field."""
    row = ToolMetadata(name="echo", enabled=False)
    out = merge_metadata(_make_spec_dict(), row)
    assert out["enabled"] is False


# ---------------------------------------------------------------------------
# DB-backed tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_enabled_tool_names_default(db_session):
    """Without a row, every requested tool is enabled."""
    names = await get_enabled_tool_names(db_session, ["echo", "calculator"])
    assert "echo" in names
    assert "calculator" in names


@pytest.mark.asyncio
async def test_get_enabled_tool_names_with_disabled_row(db_session):
    """A row with ``enabled=False`` removes the tool from the set."""
    db_session.add(ToolMetadata(name="shell_exec", enabled=False))
    await db_session.commit()
    names = await get_enabled_tool_names(
        db_session, ["echo", "shell_exec", "calculator"]
    )
    assert "echo" in names
    assert "shell_exec" not in names
    assert "calculator" in names


@pytest.mark.asyncio
async def test_get_enabled_tool_names_unknown_tool(db_session):
    """Unknown tools are silently filtered out."""
    names = await get_enabled_tool_names(db_session, ["definitely_not_a_tool"])
    assert names == set()


# ---------------------------------------------------------------------------
# HTTP round-trip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_http_list_tools_merges_metadata(app):
    """``GET /api/tools`` includes the metadata row's overrides."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        # First call: nothing overridden, every tool reports source=registry.
        r = await ac.get("/api/tools")
        assert r.status_code == 200
        body = r.json()
        echo = next(t for t in body if t["name"] == "echo")
        assert echo["source"] == "registry"
        assert echo["enabled"] is True

        # Disable ``echo`` via the metadata endpoint.
        put = await ac.put(
            "/api/tools/echo/metadata",
            json={"enabled": False, "category": "test"},
        )
        assert put.status_code == 200, put.text
        assert put.json()["enabled"] is False
        assert put.json()["category"] == "test"
        assert put.json()["source"] == "override"

        # The next ``GET /api/tools`` shows the override.
        r2 = await ac.get("/api/tools")
        assert r2.status_code == 200
        echo2 = next(t for t in r2.json() if t["name"] == "echo")
        assert echo2["enabled"] is False
        assert echo2["category"] == "test"
        assert echo2["source"] == "override"

        # DELETE resets back to the registry default.
        d = await ac.delete("/api/tools/echo/metadata")
        assert d.status_code == 204
        r3 = await ac.get("/api/tools")
        echo3 = next(t for t in r3.json() if t["name"] == "echo")
        assert echo3["source"] == "registry"
        assert echo3["enabled"] is True


@pytest.mark.asyncio
async def test_http_upsert_unknown_tool_returns_404(app):
    """A PUT for a non-registered tool surfaces a clear 404."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        r = await ac.put(
            "/api/tools/definitely_not_a_tool/metadata",
            json={"description": "nope"},
        )
        assert r.status_code == 404
        assert "not registered" in r.json()["detail"]


@pytest.mark.asyncio
async def test_http_get_single_tool(app):
    """``GET /api/tools/{name}`` returns the merged payload for one tool."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        r = await ac.get("/api/tools/calculator")
        assert r.status_code == 200
        body = r.json()
        assert body["name"] == "calculator"
        assert "parameters" in body


@pytest.mark.asyncio
async def test_http_get_unknown_tool_returns_404(app):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        r = await ac.get("/api/tools/no_such_tool_xyz")
        assert r.status_code == 404


@pytest.mark.asyncio
async def test_http_delete_idempotent(app):
    """Deleting a non-existent override is a no-op 204."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        r = await ac.delete("/api/tools/echo/metadata")
        assert r.status_code == 204
