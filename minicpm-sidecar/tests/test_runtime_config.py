"""Live-reloadable runtime config (gateway.runtime_config) + /api/config.

Covers the config store's validation/clamping, the {ADDRESS} placeholder
in the Jarvis system prompt, the clarify-strength consumption inside the
keyword router, and the /api/config HTTP contract.
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from gateway import runtime_config, server as server_mod, tools
from gateway.persona import JARVIS_SYSTEM_PROMPT, jarvis_system_prompt, prune_history


@pytest.fixture(autouse=True)
def _restore_runtime():
    """Keep RUNTIME mutations from leaking between tests."""
    snapshot = dict(runtime_config.RUNTIME)
    yield
    runtime_config.RUNTIME.clear()
    runtime_config.RUNTIME.update(snapshot)


# ── Config store: validation / clamping ────────────────────────────────


def test_update_applies_and_returns_state():
    out = runtime_config.update(
        {"assistant_address": "boss", "clarify_strength": "confirm_all",
         "auto_memory": False, "briefing_hour": 6}
    )
    assert out == {
        "assistant_address": "boss",
        "clarify_strength": "confirm_all",
        "auto_memory": False,
        "briefing_hour": 6,
        "recap_hour": 21,
    }
    assert runtime_config.get() == out


def test_update_clamps_address_length_and_blank_to_default():
    out = runtime_config.update({"assistant_address": "b" * 40})
    assert out["assistant_address"] == "b" * runtime_config.ADDRESS_MAX_CHARS
    out = runtime_config.update({"assistant_address": "   "})
    assert out["assistant_address"] == "sir"


def test_update_clamps_briefing_hour_into_0_23():
    assert runtime_config.update({"briefing_hour": 99})["briefing_hour"] == 23
    assert runtime_config.update({"briefing_hour": -5})["briefing_hour"] == 0


def test_update_clamps_recap_hour_into_0_23():
    assert runtime_config.update({"recap_hour": 99})["recap_hour"] == 23
    assert runtime_config.update({"recap_hour": -5})["recap_hour"] == 0
    with pytest.raises(ValueError):
        runtime_config.update({"recap_hour": "evening"})
    assert runtime_config.update({"recap_hour": 22})["recap_hour"] == 22


def test_update_rejects_bad_clarify_strength():
    with pytest.raises(ValueError):
        runtime_config.update({"clarify_strength": "shout"})
    # Valid enum values pass and are normalised.
    assert runtime_config.update({"clarify_strength": "OFF"})["clarify_strength"] == "off"


def test_unknown_fields_ignored_and_get_is_a_copy():
    out = runtime_config.update({"nope": 1, "auto_memory": True})
    assert "nope" not in out
    snapshot = runtime_config.get()
    snapshot["briefing_hour"] = 99
    assert runtime_config.get()["briefing_hour"] != 99


def test_module_defaults_unchanged():
    assert runtime_config.RUNTIME["assistant_address"] == "sir"
    assert runtime_config.RUNTIME["clarify_strength"] == "ambiguous"
    assert runtime_config.RUNTIME["auto_memory"] is True
    assert runtime_config.RUNTIME["briefing_hour"] == 8
    assert runtime_config.RUNTIME["recap_hour"] == 21


# ── Daily events loop: pure scheduling helpers ──────────────────────────


def test_next_daily_event_same_day_future_stays_today():
    now = datetime(2026, 8, 22, 10, 0)
    assert server_mod._next_daily_event(now, 21) == datetime(2026, 8, 22, 21, 0)


def test_next_daily_event_past_or_now_rolls_to_tomorrow():
    now = datetime(2026, 8, 22, 10, 0)
    assert server_mod._next_daily_event(now, 8) == datetime(2026, 8, 23, 8, 0)
    assert server_mod._next_daily_event(datetime(2026, 8, 22, 8, 0), 8) == \
        datetime(2026, 8, 23, 8, 0)


def test_next_daily_event_clamps_hours_into_0_23():
    now = datetime(2026, 8, 22, 10, 0)
    assert server_mod._next_daily_event(now, 99) == datetime(2026, 8, 22, 23, 0)
    assert server_mod._next_daily_event(now, -3) == datetime(2026, 8, 23, 0, 0)


def test_next_scheduled_event_picks_sooner_and_breaks_ties():
    now = datetime(2026, 8, 22, 10, 0)
    fire_at, kind = server_mod._next_scheduled_event(now, 8, 21)
    assert kind == "recap" and fire_at == datetime(2026, 8, 22, 21, 0)
    # Both hours already past today → both roll to tomorrow; recap at 21
    # beats briefing at 23.
    late = datetime(2026, 8, 22, 23, 30)
    fire_at, kind = server_mod._next_scheduled_event(late, 23, 21)
    assert kind == "recap" and fire_at == datetime(2026, 8, 23, 21, 0)
    # Equal hours → briefing wins deterministically (listed first).
    fire_at, kind = server_mod._next_scheduled_event(now, 12, 12)
    assert kind == "briefing" and fire_at == datetime(2026, 8, 22, 12, 0)


# ── Persona: {ADDRESS} placeholder ──────────────────────────────────────


def _address_line(prompt: str) -> str:
    return next(ln for ln in prompt.splitlines() if "Address the user as" in ln)


def test_jarvis_system_prompt_substitutes_address():
    line = _address_line(jarvis_system_prompt("boss"))
    assert '"boss"' in line
    assert "sir" not in line


def test_jarvis_system_prompt_default_and_backcompat_constant():
    assert '"sir"' in _address_line(jarvis_system_prompt())
    assert jarvis_system_prompt() == JARVIS_SYSTEM_PROMPT
    # Placeholder never survives into a rendered prompt.
    assert "{ADDRESS}" not in JARVIS_SYSTEM_PROMPT


def test_build_messages_uses_runtime_address():
    runtime_config.update({"assistant_address": "boss"})
    req = server_mod.ChatRequest(
        messages=[server_mod.ChatMessage(role="user", content="hello")]
    )
    msgs = server_mod._build_messages(req)
    assert '"boss"' in _address_line(msgs[0]["content"])


def test_build_messages_drops_memory_bullet_when_auto_memory_off():
    req = server_mod.ChatRequest(
        messages=[server_mod.ChatMessage(role="user", content="hello")]
    )
    runtime_config.update({"auto_memory": False})
    off = server_mod._build_messages(req)[0]["content"]
    assert "remember_fact" not in off
    runtime_config.update({"auto_memory": True})
    on = server_mod._build_messages(req)[0]["content"]
    assert "remember_fact" in on


def test_prune_history_untouched():
    msgs = [
        {"role": "user", "content": "x" * 6000},
        {"role": "assistant", "content": "y" * 6000},
        {"role": "user", "content": "question"},
    ]
    kept = prune_history(msgs)
    assert kept[-1]["content"] == "question"
    assert len(kept[-2]["content"]) <= 520


# ── Router: clarify strength consumption ────────────────────────────────


def test_route_tools_clarify_off_skips_canned_ask(monkeypatch):
    monkeypatch.setattr(tools, "set_reminder", lambda m, msg: "ok")
    # Under-specified reminder: no canned ask — falls through to model.
    assert tools.route_tools("remind me to stretch", clarify="off") == []
    # Default (ambiguous) keeps today's behaviour.
    hits = tools.route_tools("remind me to stretch")
    assert hits and hits[0][0] == "clarify"
    # Under-specified document request behaves the same way.
    assert tools.route_tools("write a meeting notes document", clarify="off") == []
    # A fully-specified reminder still executes with clarify="off".
    hits = tools.route_tools("remind me in 5 minutes to stretch", clarify="off")
    assert hits and hits[0][0] == "set_reminder"


def test_route_tools_confirm_all_asks_before_launch(monkeypatch):
    launched = []
    monkeypatch.setattr(tools, "launch_app", lambda name: launched.append(name))
    hits = tools.route_tools("open notepad", clarify="confirm_all")
    assert len(hits) == 1
    label, text = hits[0]
    assert label == "clarify"
    assert "Shall I proceed" in text
    assert "notepad" in text
    assert launched == []


def test_route_tools_confirm_all_asks_before_reminder_and_todo(monkeypatch):
    ran = []
    monkeypatch.setattr(tools, "set_reminder", lambda *a: ran.append(a))
    monkeypatch.setattr(tools, "todo_add", lambda text: ran.append(text))
    hits = tools.route_tools("remind me in 5 minutes to stretch", clarify="confirm_all")
    assert len(hits) == 1 and hits[0][0] == "clarify"
    assert "Shall I proceed" in hits[0][1]
    hits = tools.route_tools("add to my todo: buy milk", clarify="confirm_all")
    assert len(hits) == 1 and hits[0][0] == "clarify"
    assert "Shall I proceed" in hits[0][1]
    assert ran == []
    # Ambiguous default still executes immediately.
    hits = tools.route_tools("add to my todo: buy milk")
    assert hits and hits[0][0] == "todo_add"
    assert ran == ["buy milk"]


# ── HTTP contract ────────────────────────────────────────────────────────


@pytest.fixture
def test_client(tmp_path, monkeypatch):
    """Test client for FastAPI with a mocked LlamaServer."""
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


def test_api_config_post_roundtrip(test_client):
    res = test_client.post(
        "/api/config",
        json={"assistant_address": "boss", "briefing_hour": 6},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["assistant_address"] == "boss"
    assert data["briefing_hour"] == 6
    # Untouched fields keep their values; GET mirrors the state.
    assert data["clarify_strength"] == "ambiguous"
    assert test_client.get("/api/config").json() == data


def test_api_config_partial_patch_and_unknown_fields(test_client):
    res = test_client.post("/api/config", json={"nope": 1, "auto_memory": False})
    assert res.status_code == 200
    data = res.json()
    assert data["auto_memory"] is False
    assert "nope" not in data


def test_api_config_invalid_strength_returns_422(test_client):
    res = test_client.post("/api/config", json={"clarify_strength": "shout"})
    assert res.status_code == 422
