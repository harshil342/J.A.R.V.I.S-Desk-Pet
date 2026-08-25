"""Unit and integration tests for semantic memory and task dispatcher.

Tests categorical memory persistence, fuzzy semantic search, proactive task
scheduling, and REST endpoints.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.testclient import TestClient

from gateway.semantic_memory import MemoryItem, SemanticMemoryStore, default_memory_store
from gateway.task_dispatcher import ScheduledTask, TaskDispatcher, default_task_dispatcher
from gateway.tool_registry import default_registry


def test_memory_item_dict_conversion():
    item = MemoryItem(id="abc1", text="Favorite drink is Earl Grey", category="preference", tags=["tea", "food"])
    d = item.to_dict()
    assert d["id"] == "abc1"
    assert d["text"] == "Favorite drink is Earl Grey"
    assert d["category"] == "preference"

    item2 = MemoryItem.from_dict(d)
    assert item2.text == item.text
    assert item2.tags == ["tea", "food"]


def test_semantic_memory_store_crud(tmp_path):
    store_file = tmp_path / "memory_store.json"
    store = SemanticMemoryStore(storage_path=store_file)

    # 1. Add
    item = store.add("My server runs on port 18765", category="dev")
    assert item.id is not None
    assert len(store.list_all()) == 1

    # 2. Duplicate update
    dup = store.add("My server runs on port 18765", category="infra")
    assert dup.id == item.id
    assert dup.category == "infra"
    assert len(store.list_all()) == 1

    # 3. Search
    hits = store.search("what port does the server run on")
    assert len(hits) >= 1
    assert hits[0][0].id == item.id
    assert hits[0][1] > 0.15

    # 4. Delete
    ok = store.delete(item.id)
    assert ok is True
    assert len(store.list_all()) == 0


def test_semantic_memory_fuzzy_matching(tmp_path):
    store = SemanticMemoryStore(storage_path=tmp_path / "mem.json")
    store.add("My dog Buster loves fetch", category="personal")
    store.add("Meeting with Alice tomorrow at 3pm", category="work")

    # Query with word stems/synonyms
    res = store.search("Buster the puppy")
    assert len(res) >= 1
    assert "Buster" in res[0][0].text


@pytest.mark.asyncio
async def test_task_dispatcher_scheduling_and_cancellation():
    dispatcher = TaskDispatcher()

    # Schedule short task
    task = dispatcher.schedule_task("test_reminder", 10.0, payload="hello")
    assert task.status == "pending"
    assert len(dispatcher.list_tasks()) == 1

    # Cancel task
    cancelled = dispatcher.cancel_task(task.id)
    assert cancelled is True
    assert task.status == "cancelled"


def test_tools_registered_in_registry():
    tools = {t.name for t in default_registry.list_tools(source="native")}
    assert "remember_fact" in tools
    assert "recall_fact" in tools
    assert "schedule_task" in tools


@pytest.fixture
def test_client(tmp_path, monkeypatch):
    import gateway.server as server_mod
    monkeypatch.setenv("DESKPET_DOCS_DIR", str(tmp_path))
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


def test_memory_endpoints(test_client):
    # 1. Add
    res = test_client.post("/api/memory", json={"text": "Project DeskPet is awesome", "category": "projects"})
    assert res.status_code == 200
    data = res.json()
    assert data.get("ok") is True
    mem_id = data["memory"]["id"]

    # 2. List
    res = test_client.get("/api/memory")
    assert res.status_code == 200
    assert res.json().get("count") >= 1

    # 3. Search
    res = test_client.post("/api/memory/search", json={"query": "DeskPet project", "limit": 3})
    assert res.status_code == 200
    matches = res.json().get("matches", [])
    assert len(matches) >= 1
    assert "DeskPet" in matches[0]["memory"]["text"]

    # 4. Delete
    res = test_client.delete(f"/api/memory/{mem_id}")
    assert res.status_code == 200
    assert res.json().get("ok") is True


def test_tasks_endpoints(test_client):
    # 1. Schedule
    res = test_client.post("/api/tasks", json={"name": "Check deploy", "delay_seconds": 30.0, "payload": "status"})
    assert res.status_code == 200
    data = res.json()
    assert data.get("ok") is True
    tid = data["task"]["id"]

    # 2. List
    res = test_client.get("/api/tasks")
    assert res.status_code == 200
    assert res.json().get("count") >= 1

    # 3. Cancel
    res = test_client.delete(f"/api/tasks/{tid}")
    assert res.status_code == 200
    assert res.json().get("ok") is True
