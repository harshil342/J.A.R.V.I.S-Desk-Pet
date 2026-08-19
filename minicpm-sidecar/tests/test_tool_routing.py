"""Lock down the keyword router's currency parsing (F2 Phase A).

The parse stage is offline and deterministic, so it is tested directly.
Live API behaviour (open.er-api.com) is exercised manually via
scripts/smoke-chat.ps1 — we do not hit the network from unit tests.
"""

from __future__ import annotations

import pytest

from gateway import tools


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("convert 1 usd to inr", (1.0, "USD", "INR")),
        ("convert 1 dollar to inr", (1.0, "USD", "INR")),
        ("what is 100 yen in dollars", (100.0, "JPY", "USD")),
        ("500 rupees to usd", (500.0, "INR", "USD")),
        ("usd/inr rate", (1.0, "USD", "INR")),
        ("exchange 250 euros into pounds", (250.0, "EUR", "GBP")),
        ("how much is 1,000 inr in usd", (1000.0, "INR", "USD")),
        ("$50 to inr", (50.0, "USD", "INR")),
    ],
)
def test_currency_parse_positive(text, expected):
    assert tools._parse_currency(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "hello there",
        "remind me in 5 minutes to stretch",
        "meet me at 5 in the cafe",
        "what's the weather in London",
        "convert my notes to markdown",  # unknown currency words
        "usd to usd",                    # same currency both sides
    ],
)
def test_currency_parse_negative(text):
    assert tools._parse_currency(text) is None


def test_plain_greeting_routes_nothing():
    assert tools.route_tools("hello there") == []


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("add to my todo: buy milk", "buy milk"),
        ("add buy milk to my todo", "buy milk"),
        ("add call mum on my todos", "call mum"),
    ],
)
def test_todo_add_parses_both_word_orders(text, expected):
    m = tools._RE_TODO_ADD.search(text)
    assert m is not None
    assert (m.group(1) or m.group(2)) == expected


@pytest.mark.parametrize(
    "text",
    [
        "clear my todo list",
        "clear my todos",
        "wipe my todo list",
        "empty the task list",
        "reset my todos",
        "delete all my todos",
        "remove all tasks from my list",
    ],
)
def test_todo_clear_routes_before_list(text):
    """'clear my todo list' must NOT fall through to the show-list branch."""
    assert tools._RE_TODO_CLEAR.search(text) is not None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("mark buy milk as done", "buy milk"),
        ("check off buy milk", "buy milk"),
        ("complete buy milk", "buy milk"),
        ("finish call mum", "call mum"),
    ],
)
def test_todo_done_parses_item(text, expected):
    m = tools._RE_TODO_DONE.search(text)
    assert m is not None
    assert tools._clean(m.group(1) or m.group(2) or m.group(3)) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("remove buy milk from my todo", "buy milk"),
        ("delete call mum from my todo list", "call mum"),
    ],
)
def test_todo_remove_parses_item(text, expected):
    m = tools._RE_TODO_REMOVE.search(text)
    assert m is not None
    assert tools._clean(m.group(1)) == expected


def test_todo_lifecycle_add_done_remove_clear(tmp_path, monkeypatch):
    monkeypatch.setenv("DESKPET_DOCS_DIR", str(tmp_path))
    assert "Added" in tools.todo_add("buy milk")
    assert "Added" in tools.todo_add("call mum")

    assert "Marked as done: 'buy milk'" in tools.todo_done("buy milk")
    listing = tools.todo_list()
    assert "1 open item" in listing
    assert "1 completed" in listing

    assert "Removed from your to-do list: 'call mum'" in tools.todo_remove("call mum")
    assert "No task matching 'ghost'" in tools.todo_remove("ghost")

    assert "removed 1 item" in tools.todo_clear()
    assert "already empty" in tools.todo_clear()


def test_canned_replies_cover_mechanical_tools():
    r = tools.canned_reply("todo_clear", "Cleared your to-do list: removed 3 item(s).")
    assert r is not None and "3 item(s) removed" in r
    r = tools.canned_reply("todo_clear", "Your to-do list is already empty — nothing to clear.")
    assert r is not None and "already empty" in r
    r = tools.canned_reply("todo_add", "Added to your to-do list: 'buy milk'.")
    assert r is not None and "'buy milk'" in r
    r = tools.canned_reply("todo_done", "Marked as done: 'buy milk'.")
    assert r is not None and "marked complete" in r
    r = tools.canned_reply("todo_remove", "Removed from your to-do list: 'call mum'.")
    assert r is not None and "'call mum'" in r
    r = tools.canned_reply(
        "convert_currency",
        "1 USD = 95.53 INR (live rate: 1 USD = 95.5300 INR, updated recently).",
    )
    assert r is not None and "live rate" in r
    # Tools needing free-form shaping stay model-composed.
    assert tools.canned_reply("get_weather", "sunny") is None
    assert tools.canned_reply("web_search", "x") is None


# ── Batch 2: calculator, units, memory, safety ─────────────────────────


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("what is 27 times 43", "27*43 = 1161"),
        ("calculate 2 + 3 * 4", "2+3*4 = 14"),
        ("what is 10 divided by 4", "10/4 = 2.5"),
        ("what is 2 ^ 10", "2**10 = 1024"),
        ("what is 15 percent of 240", "15% of 240 = 36"),
        ("square root of 144", "sqrt(144) = 12"),
        ("how much is 1,000 plus 250", "1000+250 = 1250"),
    ],
)
def test_math_parse(text, expected):
    assert tools._parse_math(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "hello there",
        "convert 1 usd to inr",       # currency, not arithmetic
        "remind me in 5 minutes to stretch",
        "my phone number is 555 plus 12",  # word ops without a calc hint
    ],
)
def test_math_parse_negative(text):
    assert tools._parse_math(text) is None


def test_safe_eval_rejects_code():
    assert tools._safe_eval("__import__('os')") is None
    assert tools._safe_eval("open('x')") is None
    assert tools._safe_eval("1/0") is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("convert 5 km to miles", "5 km = 3.107 miles"),
        ("how many feet in 2 meters", "2 meters = 6.562 feet"),
        ("convert 100 celsius to fahrenheit", "100 celsius = 212.00 fahrenheit"),
        ("convert 1 kg to pounds", "1 kg = 2.205 pounds"),
        ("convert 1 gb to mb", "1 gb = 1,024 mb"),
        ("convert 100 km/h to mph", "100 km/h = 62.14 mph"),
    ],
)
def test_unit_parse(text, expected):
    assert tools._parse_units(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "hello there",
        "convert 1 usd to inr",
        "meet me at 5 in the cafe",  # ambiguous 'in' without units
    ],
)
def test_unit_parse_negative(text):
    assert tools._parse_units(text) is None


def test_destructive_requests_route_to_safety_refusal():
    hits = tools.route_tools("delete my system32 folder")
    assert hits and hits[0][0] == "safety_refusal"
    hits = tools.route_tools("format my hard drive")
    assert hits and hits[0][0] == "safety_refusal"
    # Harmless deletes stay on their normal branches.
    hits = tools.route_tools("remove buy milk from my todo")
    assert hits and hits[0][0] == "todo_remove"


def test_memory_remember_and_recall(tmp_path, monkeypatch):
    monkeypatch.setenv("DESKPET_DOCS_DIR", str(tmp_path))
    assert "remembered" in tools.remember_fact("my favorite tea is earl grey")
    out = tools.recall_fact("tea")
    assert "earl grey" in out
    assert "no memory matching" in tools.recall_fact("quantum flux")


def test_new_router_triggers_match():
    assert tools._RE_VOLUME.search("turn the volume up")
    assert tools._RE_VOLUME.search("mute")
    assert tools._RE_MEDIA.search("play some music")
    assert tools._RE_MEDIA.search("next song")
    assert tools._RE_SHOT.search("take a screenshot")
    assert tools._RE_LOCK.search("lock my screen")
    assert tools._RE_REMEMBER.search("remember that the wifi is slow")
    assert tools._RE_RECALL.search("what do you remember about wifi")
    m = tools._RE_URL.search("open github.com")
    assert m and m.group(2) == "github.com"
    m = tools._RE_URL.search("read https://example.com/page for me")
    assert m and m.group(1) == "https://example.com/page"
