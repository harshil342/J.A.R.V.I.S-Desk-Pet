"""Unit tests for mcp_client.py.

Verifies MCP configuration persistence, JSON-RPC 2.0 stdio protocol handshake,
tool discovery, tool invocation, and manager lifecycle.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.mcp_client import MCPServerConfig, MCPServerProcess, MCPManager
from gateway.tool_registry import ToolRegistry


def test_mcp_config_serialization():
    cfg = MCPServerConfig(
        name="test-fs",
        command="npx",
        args=["-y", "@modelcontextprotocol/server-filesystem", "C:\\test"],
        env={"CUSTOM_KEY": "VAL"},
        enabled=True,
    )
    d = cfg.to_dict()
    assert d["name"] == "test-fs"
    assert d["command"] == "npx"
    assert len(d["args"]) == 3
    assert d["env"]["CUSTOM_KEY"] == "VAL"

    restored = MCPServerConfig.from_dict(d)
    assert restored.name == cfg.name
    assert restored.args == cfg.args
    assert restored.env == cfg.env


def test_mcp_manager_persistence(tmp_path):
    config_file = tmp_path / "mcp_servers.json"
    mgr = MCPManager(config_path=config_file)

    assert mgr.load_configs() == []

    cfg1 = MCPServerConfig(name="srv1", command="node", args=["index.js"])
    cfg2 = MCPServerConfig(name="srv2", command="python", args=["server.py"])

    mgr.save_configs([cfg1, cfg2])

    loaded = mgr.load_configs()
    assert len(loaded) == 2
    assert loaded[0].name == "srv1"
    assert loaded[1].name == "srv2"


@pytest.mark.asyncio
async def test_mcp_process_mock_handshake_and_tool_call():
    registry = ToolRegistry()
    cfg = MCPServerConfig(name="mock_server", command="echo", args=[])
    proc_mgr = MCPServerProcess(cfg, registry=registry)

    # Mock subprocess streams
    mock_proc = MagicMock()
    mock_proc.returncode = None
    mock_stdin = MagicMock()
    mock_stdin.drain = AsyncMock()
    mock_proc.stdin = mock_stdin

    proc_mgr.proc = mock_proc

    # Simulate JSON-RPC reader responses
    async def mock_send_request(method, params=None, timeout=15.0):
        if method == "initialize":
            return {"serverInfo": {"name": "mock-mcp", "version": "1.0"}}
        elif method == "tools/list":
            return {
                "tools": [
                    {
                        "name": "echo_tool",
                        "description": "Echoes back input",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"msg": {"type": "string"}},
                        },
                    }
                ]
            }
        elif method == "tools/call":
            args = params.get("arguments", {})
            return {
                "content": [{"type": "text", "text": f"Echo: {args.get('msg', '')}"}],
                "isError": False,
            }
        return {}

    with patch.object(proc_mgr, "_send_request", side_effect=mock_send_request), \
         patch.object(proc_mgr, "_send_notification", new=AsyncMock()):

        ok = await proc_mgr._initialize()
        assert ok is True
        assert proc_mgr.server_info.get("name") == "mock-mcp"
        proc_mgr._connected = True

        await proc_mgr._discover_tools()
        assert len(proc_mgr.tools) == 1
        assert proc_mgr.tools[0]["name"] == "echo_tool"

        # Verify tool was registered in registry
        reg_tool = registry.get_tool("mcp_mock_server_echo_tool")
        assert reg_tool is not None

        # Execute registered MCP tool
        res = await registry.execute_tool_async("mcp_mock_server_echo_tool", {"msg": "hello world"})
        assert res.success is True
        assert res.result == "Echo: hello world"
        assert res.source == "mock_server"

        # Cleanup
        await proc_mgr.stop()
        assert registry.get_tool("mcp_mock_server_echo_tool") is None
