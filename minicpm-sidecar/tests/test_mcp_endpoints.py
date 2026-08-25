"""Unit tests for tool and MCP endpoints in server.py.

Verifies /api/tools, /api/tools/call, and /api/mcp/servers CRUD routes.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from gateway import server as server_mod


@pytest.fixture
def test_client(tmp_path, monkeypatch):
    """Build a test client for FastAPI with mocked LlamaServer."""
    d = tmp_path / "adapters"
    d.mkdir()
    monkeypatch.setenv("MINICPM_ADAPTER_DIR", str(d))
    monkeypatch.setenv("DESKPET_DOCS_DIR", str(tmp_path / "docs"))

    with patch.object(server_mod, "LlamaServer") as MockLlama:
        instance = MockLlama.return_value
        instance.start = AsyncMock()
        instance.stop = AsyncMock()
        instance.shutdown = AsyncMock()
        instance.health = AsyncMock(return_value={"ok": True})
        instance.model_path = tmp_path / "model.gguf"
        instance.port = 12345
        instance.alive = True
        instance.adapter_paths = []
        instance.last_stderr = []

        app = server_mod.build_app(initial_model=tmp_path / "model.gguf")
        with TestClient(app) as client:
            yield client


def test_get_tools_endpoint(test_client):
    res = test_client.get("/api/tools")
    assert res.status_code == 200
    data = res.json()
    assert data.get("ok") is True
    assert "tools" in data
    assert "openai_schemas" in data
    assert len(data["tools"]) >= 18


def test_call_tool_endpoint_calculate(test_client):
    res = test_client.post("/api/tools/call", json={"name": "calculate", "arguments": {"expr": "100 + 25"}})
    assert res.status_code == 200
    data = res.json()
    assert data.get("success") is True
    assert "125" in data.get("result", "")
    assert data.get("source") == "native"


def test_call_tool_endpoint_active_window(test_client):
    res = test_client.post("/api/tools/call", json={"name": "get_active_window", "arguments": {}})
    assert res.status_code == 200
    data = res.json()
    assert data.get("success") is True
    assert isinstance(data.get("result"), str)
    assert data.get("source") == "native"


def test_call_tool_endpoint_running_apps(test_client):
    res = test_client.post("/api/tools/call", json={"name": "get_running_apps", "arguments": {}})
    assert res.status_code == 200
    data = res.json()
    assert data.get("success") is True
    assert isinstance(data.get("result"), str)
    assert data.get("source") == "native"


def test_call_tool_endpoint_unknown(test_client):
    res = test_client.post("/api/tools/call", json={"name": "missing_tool", "arguments": {}})
    assert res.status_code == 200
    data = res.json()
    assert data.get("success") is False
    assert "not registered" in data.get("error", "")


def test_mcp_servers_crud_endpoints(test_client):
    # 1. List initially
    res = test_client.get("/api/mcp/servers")
    assert res.status_code == 200
    assert res.json().get("ok") is True

    # 2. Add new server (mock start to prevent spawning real process)
    with patch("gateway.mcp_client.MCPServerProcess.start", new=AsyncMock(return_value=True)):
        add_res = test_client.post(
            "/api/mcp/servers",
            json={
                "name": "test-mock-server",
                "command": "node",
                "args": ["mock.js"],
                "enabled": True,
            },
        )
        assert add_res.status_code == 200
        assert add_res.json().get("ok") is True

        # Check list again
        list_res = test_client.get("/api/mcp/servers")
        servers = list_res.json().get("servers", [])
        names = [s["name"] for s in servers]
        assert "test-mock-server" in names

        # 3. Reload
        reload_res = test_client.post("/api/mcp/servers/test-mock-server/reload")
        assert reload_res.status_code == 200
        assert reload_res.json().get("ok") is True

        # 4. Delete
        del_res = test_client.delete("/api/mcp/servers/test-mock-server")
        assert del_res.status_code == 200
        assert del_res.json().get("ok") is True

        # Check list after deletion
        list_after = test_client.get("/api/mcp/servers")
        names_after = [s["name"] for s in list_after.json().get("servers", [])]
        assert "test-mock-server" not in names_after
