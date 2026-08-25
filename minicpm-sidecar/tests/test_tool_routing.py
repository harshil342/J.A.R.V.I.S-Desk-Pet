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
    # Weather lines are ready-made prose — relayed verbatim now; failure
    # text stays model-composed so it can be explained politely.
    assert tools.canned_reply("get_weather", "Mumbai, India: 27°C, sunny.") == "Mumbai, India: 27°C, sunny."
    assert tools.canned_reply("get_weather", "No weather data available.") is None
    # Tools needing free-form shaping stay model-composed.
    assert tools.canned_reply("web_search", "x") is None

def test_canned_reply_relays_wiki_backed_search_results():
    wiki = "From Wikipedia on 'Nikola Tesla': Serbian-American inventor."
    assert tools.canned_reply("web_search", wiki) == wiki
    # Genuine DDG snippets and misses remain model-composed.
    assert tools.canned_reply("web_search", "Web search: some ddg snippet") is None
    assert tools.canned_reply("web_search", "No instant answer found for 'x'.") is None


@pytest.fixture()
def _fresh_last_topic():
    old = tools._LAST_TOPIC
    yield
    tools._LAST_TOPIC = old


def test_pronoun_followup_resolves_last_topic(monkeypatch, _fresh_last_topic):
    monkeypatch.setattr(tools, "_LAST_TOPIC", "nikola tesla")
    seen = {}
    monkeypatch.setattr(
        tools, "_wiki_topic_lookup",
        lambda q: seen.update(q=q)
        or f"From Wikipedia on 'Nikola Tesla': died January 1943.",
    )
    hits = tools.route_tools("how did he die")
    assert hits and hits[0][0] == "wikipedia"
    assert seen["q"].startswith("nikola tesla")


def test_pronoun_followup_without_last_topic_stays_unrouted(_fresh_last_topic):
    tools._LAST_TOPIC = None
    assert tools.route_tools("how did he die") == []


def test_wiki_lookup_success_records_last_topic(monkeypatch, _fresh_last_topic):
    def fake_summary(term):
        return f"From Wikipedia on '{term}': bio." if term == "marie curie" else None

    monkeypatch.setattr(tools, "wikipedia_summary", fake_summary)
    hits = tools.route_tools("marie curie")
    assert hits and hits[0][0] == "wikipedia"
    assert tools._LAST_TOPIC == "marie curie"


def test_bare_who_is_he_stays_unrouted(monkeypatch, _fresh_last_topic):
    monkeypatch.setattr(tools, "_LAST_TOPIC", "nikola tesla")
    monkeypatch.setattr(
        tools, "_wiki_topic_lookup",
        lambda q: pytest.fail("should not look up a bare referential"),
    )
    assert tools.route_tools("who is he", prior_turns=4) == []
    assert tools.route_tools("what is it", prior_turns=4) == []


def test_pronoun_followup_enriches_with_article_intent(monkeypatch, _fresh_last_topic):
    monkeypatch.setattr(tools, "_LAST_TOPIC", "nikola tesla")
    monkeypatch.setattr(
        tools, "_wiki_topic_lookup",
        lambda q: "From Wikipedia on 'Nikola Tesla': Serbian-American inventor.",
    )
    # Intro lacks the intent → article body is mined for death sentences.
    monkeypatch.setattr(
        tools, "_wiki_fulltext_sentences",
        lambda title: "Tesla died of coronary thrombosis on 7 January 1943 in New York.",
    )
    hits = tools.route_tools("how did he die")
    assert hits and hits[0][0] == "wikipedia"
    assert "coronary thrombosis" in hits[0][1]


def test_intent_lookup_passthrough_when_summary_answers(monkeypatch):
    summary = "From Wikipedia on 'Nikola Tesla': he died alone in New Yorker hotel."
    monkeypatch.setattr(tools, "_wiki_topic_lookup", lambda q: summary)
    monkeypatch.setattr(
        tools, "_wiki_fulltext_sentences",
        lambda t: pytest.fail("article mining not needed"),
    )
    assert tools._wiki_intent_lookup("how did nikola tesla die") == summary


def test_intent_lookup_passthrough_without_intent(monkeypatch):
    bio = "From Wikipedia on 'Nikola Tesla': Serbian-American inventor."
    monkeypatch.setattr(tools, "_wiki_topic_lookup", lambda q: bio)
    assert tools._wiki_intent_lookup("nikola tesla") == bio


def test_web_search_falls_back_to_wikipedia_on_ddg_miss(monkeypatch):
    class _Resp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"AbstractText": "", "Answer": "", "RelatedTopics": []}

    monkeypatch.setattr(tools.httpx, "get", lambda *a, **k: _Resp())
    wiki = "From Wikipedia on 'Pawlak': Polish mathematician."
    order = []
    monkeypatch.setattr(
        tools, "_wiki_topic_lookup",
        lambda q: order.append("topic") or wiki,
    )
    assert tools.web_search("zdzislaw pawlak") == wiki
    assert order == ["topic"]

    # Topic lookup declining → single-word queries try the summary direct.
    direct = "From Wikipedia on 'tesla': disambiguation page."
    monkeypatch.setattr(tools, "_wiki_topic_lookup", lambda q: None)
    monkeypatch.setattr(tools, "wikipedia_summary", lambda q: direct)
    assert tools.web_search("tesla") == direct

    # All sources miss → honest give-up line, unchanged.
    monkeypatch.setattr(tools, "wikipedia_summary", lambda q: None)
    assert "No instant answer" in tools.web_search("xylophone jazz")


def test_wikipedia_summary_resolves_misses_via_fulltext_search(monkeypatch):
    captured = []

    class _Resp:
        def __init__(self, payload, status=200):
            self._payload = payload
            self.status_code = status

        def raise_for_status(self):
            pass

        def json(self):
            return self._payload

    def fake_get(url, params=None, timeout=None, headers=None):
        captured.append((url, params or {}))
        if "/page/summary/" in url.lower():
            return _Resp({}, status=404) if len(captured) == 1 else _Resp(
                {"title": "Nikola Tesla", "extract": "Inventor."}
            )
        if params and params.get("action") == "query":
            assert params["srsearch"] == "nikola tesla"
            assert params["list"] == "search"
            return _Resp({"query": {"search": [{"title": "Nikola Tesla"}]}})
        raise AssertionError(f"unexpected url {url}")

    monkeypatch.setattr(tools.httpx, "get", fake_get)
    out = tools.wikipedia_summary("nikola tesla")
    assert out == "From Wikipedia on 'Nikola Tesla': Inventor."
    assert any(p.get("list") == "search" for _, p in captured)
    assert not any(p.get("action") == "opensearch" for _, p in captured)

    # Search finding nothing → honest None (no crash, no wrong article).
    def no_hits(url, params=None, timeout=None, headers=None):
        if "page/summary/" in url:
            return _Resp({}, status=404)
        return _Resp({"query": {"search": []}})

    monkeypatch.setattr(tools.httpx, "get", no_hits)
    assert tools.wikipedia_summary("gibberish xyz") is None


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

    # Screen & active window triggers
    assert tools._RE_ACTIVE_WIN.search("what app am i using")
    assert tools._RE_ACTIVE_WIN.search("what is my active window")
    assert tools._RE_SCREEN_INSPECT.search("what is on my screen")
    assert tools._RE_SCREEN_INSPECT.search("explain this error on my screen")
    assert tools._RE_RUNNING_APPS.search("what apps are running")
    assert tools._RE_RUNNING_APPS.search("list open applications")


def test_reminder_without_details_asks_instead_of_guessing(monkeypatch):
    monkeypatch.setattr(tools, "set_reminder", lambda m, msg: "ok")
    hits = tools.route_tools("remind me to stretch")
    assert hits and hits[0][0] == "clarify"
    assert "how many minutes" in hits[0][1]
    # With a duration it still schedules normally.
    hits = tools.route_tools("remind me in 5 minutes to stretch")
    assert hits and hits[0][0] == "set_reminder"


def test_followup_whois_skipped_when_history_exists(monkeypatch):
    monkeypatch.setattr(tools, "wikipedia_summary", lambda term: f"summary of {term}")
    # Opening turn: who-question hits Wikipedia.
    hits = tools.route_tools("who is Nikola Tesla", prior_turns=0)
    assert hits and hits[0][0] == "wikipedia"
    # Follow-up ("who is he?" after talking about Tesla): no route —
    # the model answers from conversation history instead.
    hits = tools.route_tools("who is he", prior_turns=4)
    assert hits == []


def test_document_without_topic_asks():
    hits = tools.route_tools("write a meeting notes document")
    assert hits and hits[0][0] == "clarify"
    assert "what should the" in hits[0][1]


def test_daily_briefing_composes_from_todos(tmp_path, monkeypatch):
    monkeypatch.setenv("DESKPET_DOCS_DIR", str(tmp_path))
    # Empty list → clear-list line.
    assert "to-do list is clear" in tools.compose_daily_briefing()
    tools.todo_add("buy milk")
    tools.todo_add("file taxes")
    text = tools.compose_daily_briefing()
    assert "2 open to-do item(s)" in text
    assert "Top of the list: buy milk" in text
    assert "(1 more)" in text


def test_evening_recap_zero_done(tmp_path, monkeypatch):
    monkeypatch.setenv("DESKPET_DOCS_DIR", str(tmp_path))
    assert tools.compose_evening_recap() == (
        "Good evening, sir. No tasks checked off today. Rest well, sir."
    )


def test_evening_recap_counts_done_and_top_open(tmp_path, monkeypatch):
    monkeypatch.setenv("DESKPET_DOCS_DIR", str(tmp_path))
    tools.todo_add("buy milk")
    tools.todo_add("call mum")
    tools.todo_done("buy milk")
    assert tools.compose_evening_recap() == (
        "Good evening, sir. You wrapped up 1 task(s) today. "
        "1 task(s) still open. Still open: call mum. Rest well, sir."
    )


def test_pending_confirm_affirm_executes(monkeypatch):
    monkeypatch.setattr(tools, "launch_app", lambda name: f"Launched {name}.")
    hits = tools.route_tools("launch notepad", clarify="confirm_all")
    assert hits and hits[0][0] == "clarify" and "notepad" in hits[0][1]
    # User affirms -> stashed action runs now.
    hits = tools.route_tools("yes", clarify="confirm_all")
    assert hits and hits[0][0] == "launch_app"
    assert "Launched notepad." in hits[0][1]
    # Slot cleared.
    assert tools.route_tools("thanks", clarify="confirm_all") == []


def test_pending_confirm_decline_cancels(monkeypatch):
    calls = []
    monkeypatch.setattr(tools, "set_reminder", lambda m, msg: calls.append((m, msg)) or "ok")
    tools.route_tools("remind me in 5 minutes to stretch", clarify="confirm_all")
    hits = tools.route_tools("no", clarify="confirm_all")
    assert hits and hits[0][0] == "confirm_cancel"
    assert calls == []  # never executed


def test_pending_confirm_new_topic_abandons(monkeypatch):
    monkeypatch.setattr(tools, "launch_app", lambda name: "Launched.")
    tools.route_tools("launch notepad", clarify="confirm_all")
    # Unrelated next message abandons the stale action silently.
    assert tools.route_tools("what time is it", clarify="confirm_all")[0][0] == "get_time"


def test_recall_fact_without_args_returns_recent(monkeypatch, tmp_path):
    monkeypatch.setenv("DESKPET_DOCS_DIR", str(tmp_path))
    tools.remember_fact("my codename is Blue Falcon")
    out = tools.recall_fact()  # no args - must not raise TypeError
    assert "Blue Falcon" in out
    out = tools.recall_fact(query="codename")
    assert "Blue Falcon" in out
