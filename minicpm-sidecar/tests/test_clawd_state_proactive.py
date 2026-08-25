"""Proactive reminder/briefing pushes must use the deskpet-proactive session
prefix so the pet's narrator speaks them verbatim instead of dropping them."""

import json

from gateway.clawd_state import ClawdBridge


class _Capture:
    """httpx.Client stand-in that records posted bodies."""

    def __init__(self):
        self.bodies = []

    def post(self, url, content=None, headers=None):
        self.bodies.append(json.loads(content))

        class _Resp:
            status_code = 200
            headers = {}

        return _Resp()


def test_notification_uses_proactive_session_prefix(monkeypatch):
    capture = _Capture()
    bridge = ClawdBridge(enabled=True)
    monkeypatch.setattr(bridge, "_client", capture)
    bridge.post("notification", event="Notification", title="Reminder: stretch")
    body = capture.bodies[-1]
    assert body["session_id"].startswith("deskpet-proactive-")
    assert body["session_title"] == "Reminder: stretch"


def test_chat_states_keep_rotating_session_id(monkeypatch):
    capture = _Capture()
    bridge = ClawdBridge(enabled=True)
    monkeypatch.setattr(bridge, "_client", capture)
    session_id = bridge._session_id
    bridge.post("thinking")
    assert capture.bodies[-1]["session_id"] == session_id
