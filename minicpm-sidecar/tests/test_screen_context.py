"""Unit tests for screen_context.py.

Verifies active window inspection, filename parsing, running application scans,
and screen context extraction.
"""

from __future__ import annotations

import platform
from unittest.mock import patch

import pytest

from gateway.screen_context import (
    ActiveWindowInfo,
    _extract_filename_from_title,
    get_active_window_info,
    get_running_apps_summary,
    inspect_screen,
)
from gateway.tool_registry import default_registry


def test_active_window_info_formatting():
    info = ActiveWindowInfo(
        title="server.py - DeskPet - Visual Studio Code",
        app_name="Visual Studio Code",
        pid=1234,
        filename="server.py",
    )
    formatted = info.formatted()
    assert "Application: Visual Studio Code" in formatted
    assert "Window Title: 'server.py - DeskPet - Visual Studio Code'" in formatted
    assert "Active File: 'server.py'" in formatted

    d = info.to_dict()
    assert d["filename"] == "server.py"
    assert d["pid"] == 1234


def test_extract_filename_from_title():
    # VS Code / Cursor
    assert _extract_filename_from_title("app.tsx - my-app - Visual Studio Code", "Visual Studio Code") == "app.tsx"
    assert _extract_filename_from_title("● main.py - test - Cursor", "Cursor") == "main.py"
    assert _extract_filename_from_title("README.md - Notepad", "Notepad") == "README.md"
    assert _extract_filename_from_title("Google Chrome", "Google Chrome") is None


def test_get_active_window_info_smoke():
    info = get_active_window_info()
    assert isinstance(info, ActiveWindowInfo)
    assert info.formatted() is not None


def test_get_running_apps_summary_smoke():
    summary = get_running_apps_summary()
    assert isinstance(summary, str)
    assert len(summary) > 0


def test_screen_tools_registered_in_registry():
    tools = {t.name for t in default_registry.list_tools(source="native")}
    assert "get_active_window" in tools
    assert "read_screen_text" in tools
    assert "inspect_screen" in tools
    assert "get_running_apps" in tools


@pytest.mark.asyncio
async def test_execute_active_window_via_registry():
    res = await default_registry.execute_tool_async("get_active_window", {})
    assert res.success is True
    assert isinstance(res.result, str)
    assert res.source == "native"


def test_extract_browser_tab():
    from gateway.screen_context import _extract_browser_tab, ActiveWindowInfo

    tab = _extract_browser_tab("FastAPI Documentation - Google Chrome", "Google Chrome")
    assert tab == "FastAPI Documentation"

    tab_edge = _extract_browser_tab("GitHub: Where the world builds software - Personal - Microsoft Edge", "Microsoft Edge")
    assert tab_edge == "GitHub: Where the world builds software"

    tab_firefox = _extract_browser_tab("DeskPet / J.A.R.V.I.S — Mozilla Firefox", "Firefox")
    assert tab_firefox == "DeskPet / J.A.R.V.I.S"

    # Non-browser app should return None
    assert _extract_browser_tab("main.py - DeskPet - Visual Studio Code", "Visual Studio Code") is None

    # Formatted ActiveWindowInfo with web_page
    info = ActiveWindowInfo(title="FastAPI - Google Chrome", app_name="Google Chrome", web_page="FastAPI")
    assert "Web Page: 'FastAPI'" in info.formatted()
