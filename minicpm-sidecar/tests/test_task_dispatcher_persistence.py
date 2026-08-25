"""Unified reminder-store tests for TaskDispatcher.

The dispatcher and tools.py share pending-reminders.json. Dispatcher entries
carry via="task"; tools.restore_reminders() must skip them and
task_dispatcher.restore() must own them. These tests pin that contract.
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from gateway import task_dispatcher as td
from gateway import tools


@pytest.fixture()
def store_file(tmp_path, monkeypatch):
    """Point BOTH modules' shared file at a temp path."""
    target = tmp_path / "pending-reminders.json"
    monkeypatch.setattr(tools, "_reminders_file", lambda: target)
    return target


@pytest.fixture(autouse=True)
def fresh_dispatcher():
    td.default_task_dispatcher.clear()
    yield
    td.default_task_dispatcher.clear()


def _read_store(path):
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


def test_schedule_persists_via_task_entry(store_file):
    task = td.default_task_dispatcher.schedule_task("standup", 120)

    items = _read_store(store_file)
    assert len(items) == 1
    entry = items[0]
    assert entry["id"] == task.id
    assert entry["via"] == "task"
    assert entry["message"] == "standup"
    assert entry["fire_at"] == pytest.approx(task.fire_at)


def test_cancel_purges_store_and_memory(store_file):
    task = td.default_task_dispatcher.schedule_task("stretch", 300)
    assert len(_read_store(store_file)) == 1

    assert td.default_task_dispatcher.cancel_task(task.id) is True

    assert _read_store(store_file) == []
    assert all(t.id != task.id for t in td.default_task_dispatcher.list_tasks())


def test_recurring_tasks_stay_out_of_the_durable_store(store_file):
    td.default_task_dispatcher.schedule_task("poll", 60, recurring=True)
    assert _read_store(store_file) == []


def test_restore_rearms_future_and_leaves_chat_entries_alone(store_file):
    now = time.time()
    store_file.write_text(json.dumps([
        {"id": "chat1", "fire_at": now + 999, "message": "earl grey"},
        {"id": "disp1", "via": "task", "fire_at": now + 50,
         "name": "standup", "message": "standup", "payload": ""},
    ]), encoding="utf-8")

    async def run():
        # restore() needs a running loop so _arm_async can arm the timer.
        return td.default_task_dispatcher.restore(now=now)

    stats = asyncio.run(run())

    assert stats["rearmed"] == 1
    assert stats["fired"] == 0
    ids = {t.id for t in td.default_task_dispatcher.list_tasks()}
    assert "disp1" in ids
    # Future entries stay in the file; the chat entry is untouched.
    by_id = {e["id"]: e for e in _read_store(store_file)}
    assert set(by_id) == {"chat1", "disp1"}
    assert by_id["chat1"].get("via") != "task"


def test_restore_fires_overdue_with_toast_and_purges(store_file, monkeypatch):
    pushed = []
    monkeypatch.setattr(
        tools, "_push_reminder",
        lambda entry, *, overdue=False: pushed.append((entry["message"], overdue)),
    )
    now = time.time()
    store_file.write_text(json.dumps([
        {"id": "late1", "via": "task", "fire_at": now - 5,
         "name": "water plants", "message": "water plants", "payload": ""},
    ]), encoding="utf-8")

    async def run():
        stats = td.default_task_dispatcher.restore(now=now)
        await asyncio.sleep(0.3)  # let the runner's floor sleep elapse
        return stats

    stats = asyncio.run(run())

    assert stats["fired"] == 1
    assert pushed == [("water plants", True)]
    assert _read_store(store_file) == []
    assert all(t.id != "late1" for t in td.default_task_dispatcher.list_tasks())


def test_tools_restore_reminders_skips_dispatcher_entries(store_file, monkeypatch):
    armed = []
    monkeypatch.setattr(tools, "_arm_reminder", lambda entry, delay, **kw: armed.append(entry["id"]))
    now = time.time()
    store_file.write_text(json.dumps([
        {"id": "chat1", "fire_at": now + 100, "message": "tea"},
        {"id": "disp1", "via": "task", "fire_at": now + 100,
         "name": "standup", "message": "standup"},
    ]), encoding="utf-8")

    stats = tools.restore_reminders(now=now)

    assert stats["rearmed"] == 1
    assert armed == ["chat1"]
