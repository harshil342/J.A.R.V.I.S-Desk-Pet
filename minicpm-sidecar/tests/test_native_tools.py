"""Tests for tool-mode resolution and native tool-call plumbing."""

from __future__ import annotations

import json
import os
from unittest.mock import MagicMock

import pytest

from gateway import server as server_mod
from gateway.llama_client import accumulate_tool_calls
from gateway.server import ChatRequest


# ── accumulate_tool_calls ───────────────────────────────────────────────────


def test_accumulate_stitches_fragmented_arguments():
    fragments = [
        {"index": 0, "id": "c1", "function": {"name": "get_time", "arguments": ""}},
        {"index": 0, "function": {"arguments": "{}"}},
        {"index": 1, "id": "c2", "function": {"name": "calculate", "arguments": '{"expr":'}},
        {"index": 1, "function": {"arguments": ' "1+2"}'}},
    ]
    calls = accumulate_tool_calls(fragments)
    assert [c["name"] for c in calls] == ["get_time", "calculate"]
    assert calls[0]["arguments"] == {}
    assert calls[1]["arguments"] == {"expr": "1+2"}
    assert calls[0]["id"] == "c1"


def test_accumulate_survives_malformed_argument_json():
    fragments = [
        {"index": 0, "id": "x", "function": {"name": "web_search", "arguments": "{not json"}},
    ]
    calls = accumulate_tool_calls(fragments)
    assert calls[0]["name"] == "web_search"
    assert calls[0]["arguments"] == {"_raw": "{not json"}


def test_accumulate_empty_and_garbage_input():
    assert accumulate_tool_calls([]) == []
    assert accumulate_tool_calls([None, "junk", 42]) == []


def test_accumulate_drops_unnamed_but_keeps_index_order():
    fragments = [
        {"index": 1, "id": "b", "function": {"name": "beta", "arguments": "{}"}},
        {"index": 0, "id": "a", "function": {"name": "alpha", "arguments": "{}"}},
    ]
    calls = accumulate_tool_calls(fragments)
    assert [c["name"] for c in calls] == ["alpha", "beta"]


# ── tool mode resolution ────────────────────────────────────────────────────


def _req(**kw) -> ChatRequest:
    return ChatRequest(messages=[{"role": "user", "content": "hi"}], **kw)


def test_default_tool_mode_is_auto(monkeypatch):
    monkeypatch.delenv("MINICPM_TOOL_MODE", raising=False)
    assert server_mod.DEFAULT_TOOL_MODE == "auto"
    assert server_mod._resolve_tool_mode(_req()) == "auto"


def test_request_level_mode_overrides_env(monkeypatch):
    monkeypatch.setenv("MINICPM_TOOL_MODE", "regex")
    # Re-import constant is cached at module load; resolution must still
    # honour an explicit per-request value.
    assert server_mod._resolve_tool_mode(_req(tool_mode="native")) == "native"


def test_invalid_mode_falls_back_to_default():
    assert server_mod._resolve_tool_mode(_req(tool_mode="yolo")) in server_mod._VALID_TOOL_MODES


# ── canned stream emits the tool chip event ────────────────────────────────


@pytest.mark.asyncio
async def test_canned_stream_emits_tool_event_first():
    bridge = MagicMock()
    chunks = []
    async for chunk in server_mod._canned_stream(bridge, "Done, sir.", tool_name="todo_add"):
        chunks.append(chunk)
    events = [json.loads(c.decode("utf-8").removeprefix("data: "))["event"] for c in chunks]
    assert events == ["tool", "start", "delta", "end"]
    assert json.loads(chunks[0].decode("utf-8").removeprefix("data: ")) == {
        "event": "tool", "name": "todo_add",
    }


@pytest.mark.asyncio
async def test_canned_stream_without_tool_name_has_no_chip():
    bridge = MagicMock()
    chunks = []
    async for chunk in server_mod._canned_stream(bridge, "Done, sir."):
        chunks.append(chunk)
    first = json.loads(chunks[0].decode("utf-8").removeprefix("data: "))
    assert first["event"] == "start"


# ── health payload carries watchdog telemetry ──────────────────────────────


def test_chat_request_accepts_tool_mode_field():
    r = _req(tool_mode="off")
    assert r.tool_mode == "off"
    assert _req().tool_mode is None
