"""Unit tests for tool_registry.py.

Verifies schema generation, native tool coverage, typed argument execution,
dynamic MCP tool registration, and error handling.
"""

from __future__ import annotations

import pytest

from gateway.tool_registry import ToolRegistry, ToolCallResult


@pytest.fixture
def registry():
    return ToolRegistry()


def test_native_tools_registered(registry):
    tools = registry.list_tools(source="native")
    tool_names = {t.name for t in tools}

    expected_tools = {
        "get_weather",
        "get_time",
        "web_search",
        "convert_currency",
        "calculate",
        "convert_units",
        "launch_app",
        "create_document",
        "set_reminder",
        "todo_add",
        "todo_list",
        "todo_done",
        "todo_remove",
        "todo_clear",
        "system_status",
        "clipboard_assist",
        "fetch_page",
        "wikipedia_summary",
        "remember_fact",
        "recall_fact",
        "open_url",
        "media_control",
        "take_screenshot",
        "lock_workstation",
    }

    assert expected_tools.issubset(tool_names), f"Missing tools: {expected_tools - tool_names}"


def test_openai_schemas_format(registry):
    schemas = registry.get_openai_schemas()
    assert len(schemas) > 0

    for s in schemas:
        assert s.get("type") == "function"
        fn = s.get("function", {})
        assert "name" in fn
        assert "description" in fn
        assert "parameters" in fn
        params = fn["parameters"]
        assert params.get("type") == "object"
        assert "properties" in params


@pytest.mark.asyncio
async def test_execute_native_tool_calculate(registry):
    res = await registry.execute_tool_async("calculate", {"expr": "12 * 8"})
    assert isinstance(res, ToolCallResult)
    assert res.success is True
    assert "96" in res.result
    assert res.source == "native"
    assert res.error is None


@pytest.mark.asyncio
async def test_execute_native_tool_unit_convert(registry):
    res = await registry.execute_tool_async("convert_units", {"amount": 5, "from_u": "km", "to_u": "m"})
    assert res.success is True
    assert "5,000 m" in res.result


@pytest.mark.asyncio
async def test_execute_unknown_tool(registry):
    res = await registry.execute_tool_async("nonexistent_tool", {})
    assert res.success is False
    assert "not registered" in (res.error or "")


@pytest.mark.asyncio
async def test_dynamic_mcp_tool_lifecycle(registry):
    async def mock_mcp_handler(query: str = ""):
        return f"MCP result for {query}"

    registry.register_mcp_tool(
        server_name="test_server",
        name="custom_query",
        description="A mock MCP tool",
        parameters={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
        handler=mock_mcp_handler,
    )

    tool = registry.get_tool("mcp_test_server_custom_query")
    assert tool is not None
    assert tool.source == "test_server"
    assert "[test_server]" in tool.description

    # Execute dynamic tool
    res = await registry.execute_tool_async("mcp_test_server_custom_query", {"query": "hello"})
    assert res.success is True
    assert res.result == "MCP result for hello"
    assert res.source == "test_server"

    # Unregister
    removed = registry.unregister_server_tools("test_server")
    assert removed == 1
    assert registry.get_tool("mcp_test_server_custom_query") is None
