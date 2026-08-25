"""Durable reminder store: persistence, boot restore, cancellation.

Reminders must survive gateway restarts — a daemon thread alone loses
them. These tests use a temp store file, a stubbed bridge, and a fake
Timer so no real waiting happens.
"""

from __future__ import annotations

import threading

import pytest

from gateway import tools


class FakeTimer:
    """Captures (delay, fn); never auto-starts. Tests invoke fn manually."""

    instances: list["FakeTimer"] = []

    def __init__(self, delay, fn, *args, **kwargs):
        self.delay = delay
        self.fn = fn
        self.started = False
        self.cancelled = False
        FakeTimer.instances.append(self)

    def start(self):
        self.started = True

    def cancel(self):
        self.cancelled = True

    def fire(self):
        self.fn()


class StubBridge:
    def __init__(self):
        self.posts = []

    def post(self, state, **kwargs):
        self.posts.append((state, kwargs))


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(tools, "_reminders_file", lambda: tmp_path / "pending-reminders.json")
    monkeypatch.setattr(tools, "_native_toast", lambda title: None)
    bridge = StubBridge()
    monkeypatch.setattr(tools, "_bridge", bridge)
    FakeTimer.instances = []
    monkeypatch.setattr(tools.threading, "Timer", FakeTimer)
    return bridge


def test_set_reminder_persists_and_arms(env):
    reply = tools.set_reminder(2.0, "stretch")
    assert "Reminder set" in reply
    data = tools._load_reminders_unlocked()
    assert len(data) == 1
    assert data[0]["message"] == "stretch"
    assert data[0]["fire_at"] > 0
    t = FakeTimer.instances[-1]
    assert t.started and 119 <= t.delay <= 121


def test_fire_removes_entry_and_pushes(env):
    tools.set_reminder(1.0, "drink water")
    entry = tools._load_reminders_unlocked()[0]
    assert len(FakeTimer.instances) == 1
    FakeTimer.instances[0].fire()  # simulate the timer expiring

    assert tools._load_reminders_unlocked() == []
    state, kwargs = env.posts[-1]
    assert state == "notification"
    assert "drink water" in kwargs["title"]


def test_restore_fires_overdue_and_rearms_future(env, monkeypatch):
    import time as _time

    now = _time.time()
    monkeypatch.setattr(tools.time, "time", lambda: now)
    tools._save_reminders_unlocked([
        {"id": "old", "fire_at": now - 300, "message": "missed ping"},
        {"id": "new", "fire_at": now + 600, "message": "future ping"},
    ])

    stats = tools.restore_reminders(now=now)

    assert stats == {"fired": 1, "rearmed": 1}
    delays = sorted(t.delay for t in FakeTimer.instances)
    # overdue fires within ~1s; future one keeps its remaining delay
    assert delays[0] <= 1.5
    assert abs(delays[1] - 600) < 2
    # both entries still on disk until their timers fire
    assert {r["id"] for r in tools._load_reminders_unlocked()} == {"old", "new"}

    # firing the overdue one removes it and labels the push
    overdue_timer = next(t for t in FakeTimer.instances if t.delay <= 1.5)
    overdue_timer.fire()
    state, kwargs = env.posts[-1]
    assert "Overdue" in kwargs["title"]
    assert [r["id"] for r in tools._load_reminders_unlocked()] == ["new"]


def test_cancel_clears_store(env):
    tools.set_reminder(3.0, "tea")
    tools._save_reminders_unlocked(tools._load_reminders_unlocked())  # no-op sanity

    reply = tools.cancel_reminders()
    assert "Cancelled 1" in reply
    assert tools._load_reminders_unlocked() == []
    assert "No pending reminders" in tools.cancel_reminders()


def test_router_routes_cancel_phrase(env):
    hits = tools.route_tools("cancel my reminders")
    assert hits and hits[0][0] == "cancel_reminders"
    assert "Cancelled" in hits[0][1] or "No pending" in hits[0][1]


def test_malformed_entries_are_dropped_not_crashing(env):
    tools._save_reminders_unlocked([{"junk": True}, "not-a-dict"])
    stats = tools.restore_reminders(now=tools.time.time())
    assert stats["fired"] == 0 and stats["rearmed"] == 0
