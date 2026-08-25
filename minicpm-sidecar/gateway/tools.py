"""DeskPet Jarvis tool layer.

Nine generalized micro-task tools (F1 of PROJECT_PLAN.md) plus the
keyword router that implements F2 Phase A: before the user's message
hits llama-server, we scan it for tool triggers, execute the matched
tools, and hand the results back to the model as injected context so
the final reply is grounded in live data instead of hallucination.

Every tool goes through `safe_tool_call` (docs §29) — a failing tool
degrades into a text note in the context, never a crashed chat.
"""

from __future__ import annotations

import ast
import json
import math as _math
import os
import platform
import re
import shutil
import subprocess
import threading
import time
import traceback
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, List, Optional, Tuple
from urllib.parse import quote

import httpx

from .log_setup import get_logger

log = get_logger("tools")

# ── Pet bridge (reminders push a notification state when they fire) ─────────

_bridge = None  # ClawdBridge, injected by server.build_app()


def bind_bridge(bridge) -> None:
    """Let tools (reminders) push pet states without importing server."""
    global _bridge
    _bridge = bridge


# ── Storage locations ────────────────────────────────────────────────────────


def docs_dir() -> Path:
    """Where created documents / todo.md live. Configurable via env."""
    raw = os.environ.get("DESKPET_DOCS_DIR")
    if raw:
        p = Path(raw).expanduser()
    else:
        p = Path.home() / "Documents" / "DeskPet"
    p.mkdir(parents=True, exist_ok=True)
    return p


# ── safe_tool_call (docs §29) ────────────────────────────────────────────────


def safe_tool_call(func: Callable, *args, **kwargs) -> Tuple[bool, str]:
    """Run a tool; return (ok, result_text). Errors become text, not crashes."""
    try:
        result = func(*args, **kwargs)
        return True, str(result)
    except Exception as exc:
        detail = f"{func.__name__}: {exc}"
        try:
            print(f"[tools] tool error: {detail}\n{traceback.format_exc()}")
        except Exception:
            pass
        return False, f"(tool {func.__name__} failed: {exc})"


# ── Tool 1: weather (open-meteo, no API key) ────────────────────────────────

_WMO = {
    0: "clear sky", 1: "mostly clear", 2: "partly cloudy", 3: "overcast",
    45: "fog", 48: "freezing fog", 51: "light drizzle", 53: "drizzle",
    55: "heavy drizzle", 61: "light rain", 63: "rain", 65: "heavy rain",
    66: "freezing rain", 67: "heavy freezing rain", 71: "light snow",
    73: "snow", 75: "heavy snow", 77: "snow grains", 80: "light showers",
    81: "showers", 82: "violent showers", 85: "snow showers",
    86: "heavy snow showers", 95: "thunderstorm", 96: "thunderstorm with hail",
    99: "thunderstorm with heavy hail",
}


def _geocode(city: str) -> Optional[dict]:
    r = httpx.get(
        "https://geocoding-api.open-meteo.com/v1/search",
        params={"name": city, "count": 1, "language": "en", "format": "json"},
        timeout=6,
    )
    r.raise_for_status()
    results = (r.json() or {}).get("results") or []
    if not results:
        return None
    top = results[0]
    return {
        "lat": top.get("latitude"),
        "lon": top.get("longitude"),
        "name": top.get("name", city),
        "country": top.get("country", ""),
    }


def _geolocate_ip() -> Optional[dict]:
    """Last-resort city detection when the user didn't name one."""
    try:
        r = httpx.get("https://ipapi.co/json/", timeout=5)
        r.raise_for_status()
        d = r.json()
        return {"lat": d.get("latitude"), "lon": d.get("longitude"),
                "name": d.get("city") or "your location", "country": d.get("country_name") or ""}
    except Exception:
        return None


def get_weather(city: Optional[str] = None) -> str:
    loc = None
    if city:
        loc = _geocode(city)
    if loc is None:
        loc = _geolocate_ip()
    if loc is None or loc.get("lat") is None:
        return ("I couldn't determine the city for the weather request. "
                "Ask me with a city name, e.g. 'weather in London'.")
    r = httpx.get(
        "https://api.open-meteo.com/v1/forecast",
        params={
            "latitude": loc["lat"],
            "longitude": loc["lon"],
            "current": "temperature_2m,relative_humidity_2m,apparent_temperature,weather_code,wind_speed_10m",
        },
        timeout=6,
    )
    r.raise_for_status()
    cur = (r.json() or {}).get("current") or {}
    code = int(cur.get("weather_code", 0) or 0)
    desc = _WMO.get(code, f"weather code {code}")
    place = loc["name"] + (f", {loc['country']}" if loc.get("country") else "")
    return (f"{place}: {cur.get('temperature_2m', '?')}°C, {desc} "
            f"(feels like {cur.get('apparent_temperature', '?')}°C, "
            f"humidity {cur.get('relative_humidity_2m', '?')}%, "
            f"wind {cur.get('wind_speed_10m', '?')} km/h).")


# ── Tool 2: time (offline) ───────────────────────────────────────────────────


def get_time() -> str:
    now = datetime.now()
    return f"{now.strftime('%A, %d %B %Y, %H:%M')} (local time)."


# ── Tool 3: web search (DuckDuckGo instant answers, no key) ─────────────────


def web_search(query: str) -> str:
    r = httpx.get(
        "https://api.duckduckgo.com/",
        params={"q": query, "format": "json", "no_html": 1, "no_redirect": 1},
        timeout=6,
    )
    r.raise_for_status()
    d = r.json() or {}
    parts: List[str] = []
    if d.get("Answer"):
        parts.append(str(d["Answer"]))
    abstract = d.get("AbstractText") or ""
    if abstract:
        src = d.get("AbstractURL") or ""
        parts.append(abstract + (f" (source: {src})" if src else ""))
    if not parts:
        topics = d.get("RelatedTopics") or []
        for t in topics[:3]:
            txt = t.get("Text") if isinstance(t, dict) else None
            if txt:
                parts.append(txt)
    if not parts:
        # DDG instant-answer miss — fall back to Wikipedia before giving
        # up, so "search for X" gets a real answer instead of "rephrase
        # or be more specific". _wiki_topic_lookup strips interrogative
        # framing and trims progressively; wikipedia_summary now resolves
        # misses via relevance-ranked search. Returned without the
        # "Web search:" prefix so canned_reply relays wiki prose verbatim.
        wiki = _wiki_topic_lookup(query) or (
            wikipedia_summary(query.strip()) if len(query.split()) == 1 else None
        )
        if wiki:
            return wiki
        return f"No instant answer found for '{query}'. Rephrase or be more specific."
    return "Web search: " + " | ".join(parts)[:1200]


# ── Tool 3b: currency conversion (open.er-api.com, no key) ─────────────────

_CUR_ALIASES = {
    "$": "USD", "us$": "USD", "usd": "USD", "dollar": "USD", "dollars": "USD",
    "us dollar": "USD", "us dollars": "USD", "bucks": "USD", "buck": "USD",
    "₹": "INR", "rs": "INR", "inr": "INR", "rupee": "INR", "rupees": "INR",
    "indian rupee": "INR", "indian rupees": "INR",
    "€": "EUR", "eur": "EUR", "euro": "EUR", "euros": "EUR",
    "£": "GBP", "gbp": "GBP", "pound": "GBP", "pounds": "GBP", "sterling": "GBP",
    "jpy": "JPY", "yen": "JPY",
    "cny": "CNY", "yuan": "CNY", "chinese yuan": "CNY",
    "aud": "AUD", "cad": "CAD", "chf": "CHF", "sgd": "SGD", "aed": "AED",
    "dirham": "AED", "dirhams": "AED",
    "btc": "BTC", "bitcoin": "BTC", "eth": "ETH", "ethereum": "ETH",
}
# Words that are only valid as aliases (a bare 3-letter code on either side
# is not enough to trigger routing — it would match ordinary sentences).
_CUR_KNOWN_CODES = {
    "USD", "INR", "EUR", "GBP", "JPY", "CNY", "AUD", "CAD", "CHF", "SGD",
    "AED", "BTC", "ETH",
}


def _cur_code(word: str) -> Optional[str]:
    w = (word or "").strip().lower().rstrip(".")
    if w in _CUR_ALIASES:
        return _CUR_ALIASES[w]
    if re.fullmatch(r"[a-z]{3}", w):
        return w.upper()
    return None


def convert_currency(amount: float, base: str, target: str) -> str:
    r = httpx.get(f"https://open.er-api.com/v6/latest/{base}", timeout=8)
    r.raise_for_status()
    d = r.json() or {}
    if d.get("result") != "success":
        return f"'{base}' is not a recognised currency code."
    rates = d.get("rates") or {}
    if target not in rates:
        return f"'{target}' is not a recognised currency code."
    rate = float(rates[target])
    value = amount * rate
    updated = d.get("time_last_update_utc") or "recently"
    return (f"{amount:g} {base} = {value:,.2f} {target} "
            f"(live rate: 1 {base} = {rate:,.4f} {target}, updated {updated}).")


# ── Tool 4: launch_app (Windows registry-aware, docs §27) ────────────────────

_APP_ALIASES = {
    "notepad": "notepad", "calculator": "calc", "calc": "calc",
    "explorer": "explorer", "file explorer": "explorer",
    "paint": "mspaint", "mspaint": "mspaint",
    "chrome": "chrome", "google chrome": "chrome",
    "firefox": "firefox", "edge": "msedge", "vscode": "code",
    "code": "code", "visual studio code": "code",
    "terminal": "wt", "windows terminal": "wt", "powershell": "powershell",
    "cmd": "cmd", "excel": "excel", "word": "winword",
    "onenote": "onenote", "one note": "onenote", "one-note": "onenote",
    "powerpoint": "powerpnt", "power point": "powerpnt", "outlook": "outlook",
    "spotify": "spotify", "discord": "discord", "steam": "steam",
    "settings": "ms-settings:", "task manager": "taskmgr",
    "snipping tool": "snippingtool", "snip": "snippingtool",
    "control panel": "control", "photos": "ms-photos:",
    "camera": "microsoft.windows.camera:", "store": "ms-windows-store:",
}

# Protocol URIs tried as a last resort when no binary/shortcut is found
# (covers Store-packaged apps that register a URI scheme).
_PROTOCOL_FALLBACKS = {
    "onenote": "onenote:",
    "spotify": "spotify:",
    "discord": "discord:",
    "steam": "steam:",
}

# Web services with canonical hosts — "open youtube" opens the site when
# no local app matches. Checked after every install/protocol path fails.
_WEB_SERVICES = {
    "youtube": "https://www.youtube.com",
    "yt": "https://www.youtube.com",
    "google": "https://www.google.com",
    "gmail": "https://mail.google.com",
    "maps": "https://maps.google.com",
    "google maps": "https://maps.google.com",
    "github": "https://github.com",
    "reddit": "https://www.reddit.com",
    "twitter": "https://x.com",
    "x": "https://x.com",
    "wikipedia": "https://www.wikipedia.org",
    "netflix": "https://www.netflix.com",
    "amazon": "https://www.amazon.com",
    "chatgpt": "https://chat.openai.com",
    "whatsapp": "https://web.whatsapp.com",
    "instagram": "https://www.instagram.com",
    "linkedin": "https://www.linkedin.com",
    "stack overflow": "https://stackoverflow.com",
}

# Human-friendly names echoed back to the model/user.
_PRETTY_NAMES = {
    "onenote": "OneNote", "winword": "Word", "excel": "Excel",
    "powerpnt": "PowerPoint", "outlook": "Outlook", "msedge": "Edge",
    "mspaint": "Paint", "calc": "Calculator", "wt": "Windows Terminal",
    "code": "VS Code", "taskmgr": "Task Manager", "snippingtool": "Snipping Tool",
}


def _norm_app(name: str) -> str:
    """'One Note' / 'one-note' / 'onenote' → 'onenote' for fuzzy matching."""
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def _find_start_menu_shortcut(name: str) -> Optional[str]:
    """Find a Start Menu .lnk whose name matches `name` (Store apps too)."""
    if platform.system() != "Windows":
        return None
    wanted = _norm_app(name)
    if not wanted:
        return None
    roots = [
        Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu",
        Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "Microsoft" / "Windows" / "Start Menu",
    ]
    best: Optional[str] = None
    for root in roots:
        if not root.is_dir():
            continue
        for lnk in root.rglob("*.lnk"):
            stem = _norm_app(lnk.stem)
            if stem == wanted:
                return str(lnk)
            if wanted in stem or stem in wanted:
                if best is None or len(lnk.stem) < len(Path(best).stem):
                    best = str(lnk)
    return best


def _winreg_app_path(name: str) -> Optional[str]:
    if platform.system() != "Windows":
        return None
    try:
        import winreg
        for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            try:
                with winreg.OpenKey(
                    hive,
                    r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths",
                ) as key:
                    with winreg.OpenKey(key, f"{name}.exe") as sub:
                        return winreg.QueryValue(sub, None)
            except OSError:
                continue
    except Exception:
        return None
    return None


def launch_app(name: str) -> str:
    name = (name or "").strip().lower()
    if not name:
        return "No application name given."
    target = _APP_ALIASES.get(name, name)
    display = _PRETTY_NAMES.get(target, name)

    # ms-settings: and other protocol URIs go straight to the shell.
    if target.endswith(":") or "://" in target:
        try:
            os.startfile(target)  # noqa: S606 — user-requested protocol
            return f"Launched {display}."
        except Exception as exc:
            return f"Could not open {display}: {exc}"

    candidates: List[str] = []
    reg = _winreg_app_path(target)
    if reg:
        candidates.append(reg)
    which = shutil.which(target) or shutil.which(f"{target}.exe")
    if which:
        candidates.append(which)
    for base in (r"C:\Program Files", r"C:\Program Files (x86)",
                 str(Path.home() / "AppData" / "Local" / "Programs")):
        for cand in (Path(base) / target / f"{target}.exe",
                     Path(base) / f"{target}.exe"):
            if cand.is_file():
                candidates.append(str(cand))

    for path in candidates:
        try:
            subprocess.Popen(
                [path],
                creationflags=getattr(subprocess, "DETACHED_PROCESS", 0),
                close_fds=True,
            )
            return f"Launched {display} from {path}."
        except Exception:
            continue

    # Start Menu shortcut match — catches Office/Store apps (OneNote,
    # Snipping Tool, ...) that have no App Paths entry or PATH binary.
    lnk = _find_start_menu_shortcut(name) or _find_start_menu_shortcut(target)
    if lnk:
        try:
            os.startfile(lnk)  # noqa: S606 — user-requested launch
            return f"Launched {display} from {lnk}."
        except Exception:
            pass

    # Registered protocol URI (onenote:, spotify:, ...).
    proto = _PROTOCOL_FALLBACKS.get(target)
    if proto:
        try:
            os.startfile(proto)  # noqa: S606 — user-requested protocol
            return f"Launched {display}."
        except Exception:
            pass

    # Shell association fallback (handles notepad, calc, urls, docs...).
    if platform.system() == "Windows":
        try:
            os.startfile(target)  # noqa: S606 — user-requested launch
            return f"Launched {display} via shell association."
        except Exception:
            pass

    # Web-service fallback: "open youtube" on a machine without a YouTube
    # app should open the site, not apologise. Curated hosts first (their
    # canonical domains), then a last-resort https://<name>.com guess.
    host = _WEB_SERVICES.get(target)
    if not host and re.fullmatch(r"[a-z0-9]{2,20}", target):
        host = f"https://{target}.com"
    if host:
        try:
            import webbrowser
            webbrowser.open(host)
            return f"Opened {display} in your browser ({host})."
        except Exception:
            pass
    return (f"Could not find an application called '{display}' on this machine; "
            f"it does not appear to be installed. Apologise briefly and say it is "
            f"not installed. Do not claim to have opened it, and do not offer or "
            f"perform any substitute action.")


# ── Tool 5: create_document (template-driven, F1.1 flagship) ─────────────────


def _slug(text: str, maxlen: int = 40) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return (slug or "draft")[:maxlen].rstrip("-") or "draft"


_DOC_TEMPLATES: dict[str, Callable[[str, str], str]] = {}


def _doc_template(kind: str):
    def wrap(fn):
        _DOC_TEMPLATES[kind] = fn
        return fn
    return wrap


@_doc_template("meeting_notes")
def _tpl_meeting(topic: str, stamp: str) -> str:
    return f"""# Meeting Notes — {topic or "Untitled"}

> {stamp} · drafted by DeskPet Jarvis

## Attendees
- 

## Agenda
1. {topic or 'Open discussion'}

## Key Decisions
- 

## Action Items
- [ ] Owner: ___ — Due: ___
- [ ] Owner: ___ — Due: ___

## Next Steps
- 
"""


@_doc_template("readme")
def _tpl_readme(topic: str, stamp: str) -> str:
    return f"""# {topic or "Project Name"}

> {stamp} · drafted by DeskPet Jarvis

Short one-paragraph description of what {topic or 'the project'} does and why it exists.

## Features
- 
- 

## Installation
```bash
# TODO: install commands
```

## Usage
```bash
# TODO: quick-start command
```

## License
MIT
"""


@_doc_template("video_script")
def _tpl_video(topic: str, stamp: str) -> str:
    return f"""# Video Script / Outline — {topic or "Untitled"}

> {stamp} · drafted by DeskPet Jarvis

## Hook (0:00–0:15)
Open with the problem: 

## Intro (0:15–0:45)
What this video delivers: 

## Main Points
1. 
2. 
3. 

## Demo / Show Section
- 

## Call to Action (last 20s)
- Like / subscribe / link

## B-roll & Assets Needed
- 
"""


@_doc_template("changelog")
def _tpl_changelog(topic: str, stamp: str) -> str:
    return f"""# Changelog — {topic or "Project"}

> {stamp} · drafted by DeskPet Jarvis

## [Unreleased]
### Added
- 

### Changed
- 

### Fixed
- 
"""


@_doc_template("todo_list")
def _tpl_todo(topic: str, stamp: str) -> str:
    return f"""# To-Do List — {topic or "This Week"}

> {stamp} · drafted by DeskPet Jarvis

## Priority
- [ ] 
- [ ] 

## Later
- [ ] 
- [ ] 

## Done
- [x] Started this list
"""


@_doc_template("email")
def _tpl_email(topic: str, stamp: str) -> str:
    return f"""# Email Draft — {topic or "Untitled"}

> {stamp} · drafted by DeskPet Jarvis

**Subject:** {topic or '(subject)'}

Dear (name),

I hope this message finds you well. (Opening sentence about {topic or 'the matter'}.)

(Body: 2–3 short paragraphs with the key details.)

(Ask / next step: what you need from them.)

Kind regards,
(Your name)
"""


_DOC_KEYWORDS = [
    ("meeting notes", "meeting_notes"), ("meeting note", "meeting_notes"),
    ("readme", "readme"), ("read me", "readme"),
    ("video script", "video_script"), ("script", "video_script"),
    ("outline", "video_script"),
    ("changelog", "changelog"), ("change log", "changelog"),
    ("to-do list", "todo_list"), ("todo list", "todo_list"),
    ("email", "email"), ("e-mail", "email"),
    # Generic catch-all LAST — "write a document" with no type still routes.
    ("document", "document"), ("doc", "document"),
]


@_doc_template("document")
def _tpl_document(topic: str, stamp: str) -> str:
    return f"""# {topic or "Untitled Document"}

> {stamp} · drafted by DeskPet Jarvis

## Summary

(One-paragraph overview of {topic or "the topic"}.)

## Details

-

## Next Steps

-
"""


def create_document(doc_type: str, topic: str) -> str:
    topic = (topic or "").strip()
    if not topic:
        # Tool-level guard: catches BOTH the regex-router path and a native
        # model call — never silently create a file named after a placeholder.
        kind_label = (doc_type or "document").replace("_", " ")
        return (f"Certainly, sir — what should the {kind_label} cover? "
                "Give me the topic and I shall draft it at once.")
    kind = doc_type if doc_type in _DOC_TEMPLATES else "meeting_notes"
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    body = _DOC_TEMPLATES[kind](topic, stamp)
    fname = f"{_slug(kind)}-{_slug(topic) or 'draft'}-{datetime.now():%Y%m%d-%H%M}.md"
    path = docs_dir() / fname
    path.write_text(body, encoding="utf-8")
    return (f"Drafted a {kind.replace('_', ' ')} document about "
            f"'{topic or 'the topic'}' and saved it to {path}.")


# ── Tool 6: reminders (local scheduler → pet notification) ──────────────────


def set_reminder(minutes: float, message: str) -> str:
    seconds = max(1.0, minutes * 60.0)
    msg = (message or "your reminder").strip()
    entry = {
        "id": uuid.uuid4().hex[:10],
        "fire_at": round(time.time() + seconds, 3),
        "message": msg,
    }
    with _REMINDERS_LOCK:
        items = _load_reminders_unlocked()
        items.append(entry)
        _save_reminders_unlocked(items)
    _arm_reminder(entry, seconds)
    human = f"{minutes:g} minute(s)" if minutes < 60 else f"{minutes / 60:g} hour(s)"
    return f"Reminder set for {human} from now: '{msg}'. I will ping you."


# ── Durable reminder store ───────────────────────────────────────────────────
#
# Reminders used to live only in a daemon thread: a gateway restart or
# app close silently ate them. Each pending timer is now persisted to
# pending-reminders.json; restore_reminders() re-arms future entries at
# boot and fires overdue ones immediately with an "Overdue" marker.

_REMINDERS_LOCK = threading.Lock()


def _reminders_file() -> Path:
    return docs_dir() / "pending-reminders.json"


def _load_reminders_unlocked() -> list:
    f = _reminders_file()
    if not f.exists():
        return []
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except Exception as exc:
        log.warning("could not read %s: %s", f.name, exc)
        return []
    if not isinstance(data, list):
        return []
    return [
        r for r in data
        if isinstance(r, dict) and r.get("id") and r.get("fire_at") and r.get("message")
    ]


def _save_reminders_unlocked(items: list) -> None:
    f = _reminders_file()
    try:
        f.parent.mkdir(parents=True, exist_ok=True)
        tmp = f.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(f)
    except Exception as exc:
        log.warning("could not persist reminders to %s: %s", f.name, exc)


def _native_toast(title: str) -> None:
    """OS-level Windows toast (winotify), independent of the pet UI.

    The bridge path only narrates in-bubble; if Electron is busy/hidden
    the user misses the reminder. winotify fires a real Action Center
    toast from this process. Lazy import: missing lib must never break
    reminder delivery.
    """
    try:
        from winotify import Notification, audio

        n = Notification(app_id="com.deskpet.assistant", title="DeskPet", msg=title)
        n.set_audio(audio.Silent, loop=False)  # chime handled by the pet
        n.show()
    except Exception as exc:
        log.warning("native toast failed for %r: %s", title, exc)


def _push_reminder(entry: dict, *, overdue: bool = False) -> None:
    msg = str(entry.get("message") or "your reminder")
    when = datetime.now().strftime("%H:%M")
    title = f"⏰ {'Overdue reminder' if overdue else 'Reminder'}: {msg}"
    _native_toast(title)
    if _bridge is None:
        log.warning("reminder fired but no pet bridge bound: %s", msg)
        return
    try:
        _bridge.post("notification", event="Notification", title=title)
        log.info(
            "reminder fired at %s and pushed to pet: %s%s",
            when, msg, " (overdue)" if overdue else "",
        )
    except Exception as exc:
        log.warning("reminder push failed for %r: %s", msg, exc)


def _arm_reminder(entry: dict, delay_s: float, *, overdue: bool = False) -> None:
    rid = str(entry.get("id"))

    def fire():
        # Remove from the store first so a crash right after the push
        # never double-fires the same reminder.
        with _REMINDERS_LOCK:
            items = [r for r in _load_reminders_unlocked() if str(r.get("id")) != rid]
            _save_reminders_unlocked(items)
        _push_reminder(entry, overdue=overdue)

    timer = threading.Timer(max(0.5, delay_s), fire)
    timer.daemon = True  # never block interpreter / server shutdown
    timer.start()


def restore_reminders(now: Optional[float] = None) -> dict:
    """Boot-time recovery: re-arm future reminders, fire overdue ones.

    `now` lets tests inject a clock. Returns counters for logging.
    """
    now_ts = time.time() if now is None else float(now)
    fired = rearmed = dropped = 0
    with _REMINDERS_LOCK:
        for entry in _load_reminders_unlocked():
            # Dispatcher-owned entries (drawer /api/tasks) are restored by
            # task_dispatcher.restore() — skipping here prevents double-fire.
            if entry.get("via") == "task":
                continue
            try:
                fire_at = float(entry["fire_at"])
            except (KeyError, TypeError, ValueError):
                dropped += 1
                continue
            if fire_at <= now_ts:
                # Stagger pushes slightly so several overdue reminders
                # don't collide into one toast burst.
                _arm_reminder(entry, 0.5 + fired * 1.0, overdue=True)
                fired += 1
            else:
                _arm_reminder(entry, fire_at - now_ts)
                rearmed += 1
    if dropped:
        log.warning("dropped %d malformed reminder entries", dropped)
    return {"fired": fired, "rearmed": rearmed}


def cancel_reminders() -> str:
    with _REMINDERS_LOCK:
        items = _load_reminders_unlocked()
        n = len(items)
        _save_reminders_unlocked([])
    if n == 0:
        return "No pending reminders to cancel, sir."
    return f"Cancelled {n} pending reminder(s), sir."


# ── Tool 7: todo capture (todo.md) ───────────────────────────────────────────


def _todo_file() -> Path:
    return docs_dir() / "todo.md"


def todo_add(text: str) -> str:
    text = (text or "").strip()
    if not text:
        return "Nothing to add — tell me the task text."
    f = _todo_file()
    line = f"- [ ] {text}  _(added {datetime.now():%Y-%m-%d %H:%M})_\n"
    if not f.exists():
        f.write_text("# DeskPet To-Do\n\n", encoding="utf-8")
    with f.open("a", encoding="utf-8") as fh:
        fh.write(line)
    return f"Added to your to-do list: '{text}'."


def todo_list() -> str:
    f = _todo_file()
    if not f.exists():
        return "Your to-do list is empty (no todo.md yet)."
    lines = [ln.rstrip() for ln in f.read_text(encoding="utf-8").splitlines()
             if ln.startswith("- [")]
    if not lines:
        return "Your to-do list has no items."
    open_items = [ln for ln in lines if ln.startswith("- [ ]")]
    done = [ln for ln in lines if ln.startswith("- [x]")]
    out = [f"You have {len(open_items)} open item(s):"]
    out.extend(open_items[:15])
    if done:
        out.append(f"({len(done)} completed item(s) on file.)")
    return "\n".join(out)


def compose_daily_briefing() -> str:
    """One-line proactive briefing: greeting + open to-dos.

    ponytail: todos only — reminders/schedule woven in when there is a
    real source for them (dispatcher tasks are one-shot timers, not a day
    plan).
    """
    hour = datetime.now().hour
    greet = "Good morning" if hour < 12 else "Good afternoon" if hour < 18 else "Good evening"
    ok, out = safe_tool_call(todo_list)
    items: list[str] = []
    if ok:
        items = [_item_body(ln) for ln in str(out).splitlines() if ln.startswith("- [ ]")]
    if not items:
        return f"{greet}, sir. Your to-do list is clear."
    tail = f" Top of the list: {items[0]}." if items[0] else ""
    more = f" ({len(items) - 1} more)" if len(items) > 1 else ""
    return f"{greet}, sir. {len(items)} open to-do item(s).{tail}{more}"


def _item_body(line: str) -> str:
    """Strip the checkbox prefix and the _(added ...)_ suffix from an item."""
    return line[6:].split("_(")[0].strip()


def compose_evening_recap() -> str:
    """Evening wind-down line: what got checked off, what's still open.

    ponytail: "- [x]" lines carry no completion timestamp, so the
    done-count is a today-proxy over the whole todo.md file.
    """
    lines: list[str] = []
    try:
        f = _todo_file()
        if f.exists():
            lines = [ln.rstrip() for ln in f.read_text(encoding="utf-8").splitlines()]
    except Exception:
        lines = []
    done = [ln for ln in lines if ln.startswith("- [x]")]
    open_items = [ln for ln in lines if ln.startswith("- [ ]")]
    parts = ["Good evening, sir."]
    if done:
        parts.append(f"You wrapped up {len(done)} task(s) today.")
    else:
        parts.append("No tasks checked off today.")
    if open_items:
        parts.append(f"{len(open_items)} task(s) still open.")
        first = _item_body(open_items[0])
        if first:
            parts.append(f"Still open: {first}.")
    parts.append("Rest well, sir.")
    return " ".join(parts)


def _norm_item(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", (s or "").lower()).strip()


def todo_clear() -> str:
    f = _todo_file()
    lines = []
    if f.exists():
        lines = [ln for ln in f.read_text(encoding="utf-8").splitlines()
                 if ln.startswith("- [")]
    if not lines:
        return "Your to-do list is already empty — nothing to clear."
    f.write_text("# DeskPet To-Do\n\n", encoding="utf-8")
    return f"Cleared your to-do list: removed {len(lines)} item(s)."


def todo_done(item: str) -> str:
    item = (item or "").strip()
    if not item:
        return "Nothing to complete — tell me the task name."
    f = _todo_file()
    if not f.exists():
        return "Your to-do list is empty — nothing to mark as done."
    lines = f.read_text(encoding="utf-8").splitlines()
    needle = _norm_item(item)
    for i, ln in enumerate(lines):
        if ln.startswith("- [ ]"):
            body = _item_body(ln)
            nb = _norm_item(body)
            if needle and nb and (needle in nb or nb in needle):
                lines[i] = "- [x]" + ln[5:]
                f.write_text("\n".join(lines) + "\n", encoding="utf-8")
                return f"Marked as done: '{body}'."
    return f"No open task matching '{item}' on your to-do list."


def todo_remove(item: str) -> str:
    item = (item or "").strip()
    if not item:
        return "Nothing to remove — tell me the task name."
    f = _todo_file()
    if not f.exists():
        return "Your to-do list is empty — nothing to remove."
    lines = f.read_text(encoding="utf-8").splitlines()
    needle = _norm_item(item)
    kept, removed = [], None
    for ln in lines:
        if removed is None and (ln.startswith("- [ ]") or ln.startswith("- [x]")):
            body = _item_body(ln)
            nb = _norm_item(body)
            if needle and nb and (needle in nb or nb in needle):
                removed = body
                continue
        kept.append(ln)
    if removed is None:
        return f"No task matching '{item}' on your to-do list."
    f.write_text("\n".join(kept) + "\n", encoding="utf-8")
    return f"Removed from your to-do list: '{removed}'."


# ── Tool 8: system_status (psutil, offline) ──────────────────────────────────


def system_status() -> str:
    import psutil
    cpu = psutil.cpu_percent(interval=0.5)
    vm = psutil.virtual_memory()
    parts = [
        f"CPU {cpu:.0f}%",
        f"RAM {vm.percent:.0f}% used ({vm.used // (1024 ** 2)} MB of {vm.total // (1024 ** 2)} MB)",
    ]
    try:
        bat = psutil.sensors_battery()
        if bat is not None:
            plug = "charging" if bat.power_plugged else "on battery"
            parts.append(f"battery {bat.percent:.0f}% ({plug})")
    except Exception:
        pass
    try:
        du = psutil.disk_usage(str(Path.home()))
        parts.append(f"disk {du.percent:.0f}% used")
    except Exception:
        pass
    up = timedelta(seconds=int(time.time() - psutil.boot_time()))
    parts.append(f"uptime {up.seconds // 3600}h {(up.seconds // 60) % 60}m")
    return "System status: " + "; ".join(parts) + "."


# ── Tool 9: clipboard_assist (read clipboard, model does the work) ──────────


# Sentinel returned by _read_clipboard when the clipboard holds an image but
# no text. The text-only model cannot ingest images; surfacing this clearly
# beats forwarding image bytes (which makes llama-server throw
# "this model does not support image input").
_CLIPBOARD_IMAGE = "__CLIPBOARD_IMAGE_ONLY__"


def _clipboard_has_image() -> bool:
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-Clipboard -Format Image) -ne $null"],
            capture_output=True, text=True, timeout=5,
        )
        return r.stdout.strip().lower() == "true"
    except Exception:
        return False


def _read_clipboard() -> str:
    if platform.system() == "Windows":
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command", "Get-Clipboard -Raw"],
            capture_output=True, text=True, timeout=5,
        )
        text = (r.stdout or "").strip()
        # Empty text but an image present: report the image case so the
        # model never receives image data it can't process.
        if not text and _clipboard_has_image():
            return _CLIPBOARD_IMAGE
        return text
    r = subprocess.run(["xclip", "-selection", "clipboard", "-o"],
                       capture_output=True, text=True, timeout=5)
    return (r.stdout or "").strip()


def clipboard_assist(action: str) -> str:
    text = _read_clipboard()
    if not text:
        return "The clipboard is empty."
    if text == _CLIPBOARD_IMAGE:
        return ("The clipboard currently holds an image, which I can't view "
                "with the text-only model. Copy some text and I'll help.")
    snippet = text[:1500]
    verb = (action or "show").strip().lower()
    if verb == "show":
        return f"Clipboard content: {snippet}"
    return (f"The user's clipboard contains the text below. "
            f"{verb.capitalize()} it as requested.\n---\n{snippet}\n---")


# ── Tool 10: calculator (safe AST evaluator, fully offline) ─────────────

_MATH_FUNCS = {
    "sqrt": _math.sqrt, "abs": abs, "round": round, "min": min, "max": max,
    "pow": pow, "sin": _math.sin, "cos": _math.cos, "tan": _math.tan,
    "log": _math.log, "log10": _math.log10, "floor": _math.floor, "ceil": _math.ceil,
}
_MATH_CONSTS = {"pi": _math.pi, "e": _math.e}


def _safe_eval(expr: str) -> Optional[float]:
    """Evaluate an arithmetic expression via AST — no eval(), no builtins."""
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError:
        return None

    def _eval(node):
        if isinstance(node, ast.Expression):
            return _eval(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return node.value
        if isinstance(node, ast.BinOp):
            ops = {ast.Add: lambda a, b: a + b, ast.Sub: lambda a, b: a - b,
                   ast.Mult: lambda a, b: a * b, ast.Div: lambda a, b: a / b,
                   ast.FloorDiv: lambda a, b: a // b, ast.Mod: lambda a, b: a % b,
                   ast.Pow: lambda a, b: a ** b}
            fn = ops.get(type(node.op))
            if fn is None:
                raise ValueError("operator not allowed")
            return fn(_eval(node.left), _eval(node.right))
        if isinstance(node, ast.UnaryOp):
            v = _eval(node.operand)
            return -v if isinstance(node.op, ast.USub) else v
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            fn = _MATH_FUNCS.get(node.func.id)
            if fn is None:
                raise ValueError("function not allowed")
            return fn(*[_eval(a) for a in node.args])
        if isinstance(node, ast.Name):
            if node.id in _MATH_CONSTS:
                return _MATH_CONSTS[node.id]
            raise ValueError("name not allowed")
        raise ValueError("node not allowed")

    try:
        val = _eval(tree)
        if isinstance(val, (int, float)) and val == val and abs(val) != float("inf"):
            return float(val)
    except Exception:
        return None
    return None


def calculate(expr: str) -> str:
    expr = (expr or "").strip()
    if not expr:
        return "Nothing to calculate — give me an expression."
    val = _safe_eval(expr)
    if val is None:
        return f"I could not evaluate '{expr}'. Give me plain arithmetic, sir."
    pretty = f"{val:g}"
    return f"{expr} = {pretty}"


# ── Tool 11: unit conversion (offline, deterministic) ───────────────────

# category → {canonical unit → factor to base unit}
_UNIT_TABLES = {
    "length": {"m": 1.0, "km": 1000.0, "cm": 0.01, "mm": 0.001,
               "mi": 1609.344, "ft": 0.3048, "in": 0.0254, "yd": 0.9144},
    "weight": {"kg": 1.0, "g": 0.001, "lb": 0.45359237,
               "oz": 0.028349523125, "t": 1000.0},
    "data": {"mb": 1.0, "kb": 1 / 1024, "gb": 1024.0, "tb": 1048576.0,
             "b": 1 / 1048576},
    "speed": {"kmh": 1.0, "mph": 1.609344, "kn": 1.852, "ms": 3.6},
}
_UNIT_ALIASES = {}
for _cat, _units in _UNIT_TABLES.items():
    for _u in _units:
        _UNIT_ALIASES[_u] = (_cat, _u)
_UNIT_ALIASES.update({
    "meter": ("length", "m"), "meters": ("length", "m"), "metre": ("length", "m"),
    "metres": ("length", "m"), "kilometer": ("length", "km"), "kilometers": ("length", "km"),
    "kilometre": ("length", "km"), "kilometres": ("length", "km"),
    "centimeter": ("length", "cm"), "centimeters": ("length", "cm"),
    "millimeter": ("length", "mm"), "millimeters": ("length", "mm"),
    "mile": ("length", "mi"), "miles": ("length", "mi"),
    "foot": ("length", "ft"), "feet": ("length", "ft"),
    "inch": ("length", "in"), "inches": ("length", "in"),
    "yard": ("length", "yd"), "yards": ("length", "yd"),
    "kilogram": ("weight", "kg"), "kilograms": ("weight", "kg"), "kilos": ("weight", "kg"),
    "kilo": ("weight", "kg"), "gram": ("weight", "g"), "grams": ("weight", "g"),
    "pound": ("weight", "lb"), "pounds": ("weight", "lb"), "lbs": ("weight", "lb"),
    "ounce": ("weight", "oz"), "ounces": ("weight", "oz"),
    "tonne": ("weight", "t"), "tonnes": ("weight", "t"), "ton": ("weight", "t"),
    "kilobyte": ("data", "kb"), "kilobytes": ("data", "kb"),
    "megabyte": ("data", "mb"), "megabytes": ("data", "mb"),
    "gigabyte": ("data", "gb"), "gigabytes": ("data", "gb"),
    "terabyte": ("data", "tb"), "terabytes": ("data", "tb"),
    "byte": ("data", "b"), "bytes": ("data", "b"),
    "km/h": ("speed", "kmh"), "kmph": ("speed", "kmh"), "kph": ("speed", "kmh"),
    "m/s": ("speed", "ms"), "knot": ("speed", "kn"), "knots": ("speed", "kn"),
    "celsius": ("temp", "c"), "fahrenheit": ("temp", "f"), "kelvin": ("temp", "k"),
    "c": ("temp", "c"), "f": ("temp", "f"), "k": ("temp", "k"),
})
_UNIT_UNAMBIGUOUS = {
    "km", "cm", "mm", "mi", "ft", "yd", "kg", "lb", "lbs", "oz", "kb", "mb",
    "gb", "tb", "kmh", "kmph", "mph", "knots", "celsius", "fahrenheit", "kelvin",
    "meter", "meters", "metre", "metres", "mile", "miles", "foot", "feet",
    "inch", "inches", "yard", "yards", "kilometer", "kilometers", "kilometre",
    "kilometres", "centimeter", "centimeters", "millimeter", "millimeters",
    "kilogram", "kilograms", "kilo", "kilos", "gram", "grams", "pound",
    "pounds", "ounce", "ounces", "ton", "tonne", "tonnes", "byte", "bytes",
    "kilobyte", "kilobytes", "megabyte", "megabytes", "gigabyte", "gigabytes",
    "terabyte", "terabytes", "knot",
}
_UNIT_HINT = re.compile(r"\b(?:convert|conversion|how many|how much|equals?|is)\b", re.IGNORECASE)


def _unit_lookup(word: str) -> Optional[Tuple[str, str]]:
    return _UNIT_ALIASES.get((word or "").strip().lower())


def convert_units(amount: float, from_u: str, to_u: str) -> str:
    fu, tu = _unit_lookup(from_u), _unit_lookup(to_u)
    if not fu or not tu:
        return "One of those units is not recognised, sir."
    if fu[0] != tu[0]:
        return f"I cannot convert {from_u} to {to_u} — they measure different things."
    cat = fu[0]
    if cat == "temp":
        v = amount
        if fu[1] == "f":
            v = (v - 32) * 5 / 9
        elif fu[1] == "k":
            v = v - 273.15
        # v is now celsius
        if tu[1] == "f":
            out = v * 9 / 5 + 32
        elif tu[1] == "k":
            out = v + 273.15
        else:
            out = v
        return f"{amount:g} {from_u} = {out:,.2f} {to_u}"
    factor = _UNIT_TABLES[cat][fu[1]] / _UNIT_TABLES[cat][tu[1]]
    out = amount * factor
    return f"{amount:g} {from_u} = {out:,.4g} {to_u}"


# ── Tool 12: fetch_page (mini-RAG — fetch a URL, model summarises) ───────


def fetch_page(url: str) -> str:
    url = (url or "").strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    try:
        r = httpx.get(url, timeout=10, follow_redirects=True,
                      headers={"User-Agent": "Mozilla/5.0 (DeskPet-Jarvis)"})
        r.raise_for_status()
    except Exception as exc:
        return f"Could not fetch {url}: {type(exc).__name__}."
    html = r.text or ""
    html = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", html)
    text = re.sub(r"(?s)<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return f"The page at {url} returned no readable text."
    return (f"The page at {url} contains the following text. "
            f"Summarise or answer from it as the user asked.\n---\n"
            f"{text[:3000]}\n---")


# ── Tool 13: wikipedia summary ──────────────────────────────────────────


def wikipedia_summary(term: str) -> Optional[str]:
    term = (term or "").strip()
    if not term:
        return None
    try:
        r = httpx.get(
            "https://en.wikipedia.org/api/rest_v1/page/summary/" + quote(term),
            timeout=8, headers={"User-Agent": "DeskPet-Jarvis/1.0"},
        )
        if r.status_code == 404:
            # Relevance-ranked full-text search beats opensearch here:
            # opensearch fuzzy-matches near-miss titles ('nikola tesla
            # die' → 'Nikola Tesla in popular culture') or misses
            # multi-word phrases entirely; list=search ranks the right
            # article first ('Nikola Tesla', 'Fuzzy set', ...).
            s = httpx.get(
                "https://en.wikipedia.org/w/api.php",
                params={"action": "query", "list": "search", "srsearch": term,
                        "srlimit": 1, "format": "json"},
                timeout=8, headers={"User-Agent": "DeskPet-Jarvis/1.0"},
            )
            hits = ((s.json() or {}).get("query") or {}).get("search") or []
            if not hits:
                return None
            r = httpx.get(
                "https://en.wikipedia.org/api/rest_v1/page/summary/"
                + quote(hits[0].get("title") or ""),
                timeout=8, headers={"User-Agent": "DeskPet-Jarvis/1.0"},
            )
        if r.status_code != 200:
            return None
        d = r.json() or {}
        extract = (d.get("extract") or "").strip()
        if not extract:
            return None
        title = d.get("title") or term
        return f"From Wikipedia on '{title}': {extract}"
    except Exception:
        return None


# ── Tool 14: quick memory (notes.md) ─────────────────────────────────────


def _notes_file() -> Path:
    return docs_dir() / "notes.md"


def remember_fact(text: str = "", fact: str = "", note: str = "", **_alt) -> str:
    # 1B models hallucinate argument names ("fact", "note", "information");
    # accept common synonyms instead of crashing the call.
    text = (text or fact or note or next(iter(_alt.values()), "") or "").strip(" .!?")
    if not text:
        return "Nothing to remember — tell me the fact."
    f = _notes_file()
    if not f.exists():
        f.write_text("# DeskPet Memory\n\n", encoding="utf-8")
    with f.open("a", encoding="utf-8") as fh:
        fh.write(f"- {text}  _(saved {datetime.now():%Y-%m-%d %H:%M})_\n")
    try:
        from .semantic_memory import default_memory_store
        default_memory_store.add(text)
    except Exception:
        pass
    return f"remembered: {text}"


def recall_fact(query: str = "", key: str = "", topic: str = "", term: str = "", **_alt) -> str:
    # Same tolerance: models emit "key"/"topic"/"term" instead of "query".
    query = (query or key or topic or term or next(iter(_alt.values()), "") or "").strip().lower()
    try:
        from .semantic_memory import default_memory_store
        matches = default_memory_store.search(query, limit=5)
        if matches:
            lines = [f"- {item.text}" for item, _ in matches]
            return "From my memory:\n" + "\n".join(lines)
    except Exception:
        pass

    f = _notes_file()
    if not f.exists():
        return "I have nothing saved in my memory yet, sir."
    lines = [ln for ln in f.read_text(encoding="utf-8").splitlines()
             if ln.startswith("- ")]
    if not lines:
        return "I have nothing saved in my memory yet, sir."
    if not query:
        return "My memory contains:\n" + "\n".join(lines[-10:])
    words = [w for w in re.sub(r"[^a-z0-9 ]", " ", query).split() if len(w) > 2]
    hits = [ln for ln in lines
            if any(w in ln.lower() for w in words) or query in ln.lower()]
    if not hits:
        return f"I have no memory matching '{query}'."
    return "From my memory:\n" + "\n".join(hits[:8])


# ── Tool 15: open URL in the default browser ─────────────────────────────


def open_url(url: str) -> str:
    url = (url or "").strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    import webbrowser
    webbrowser.open(url)
    host = re.sub(r"^https?://", "", url).split("/")[0]
    return f"opened: {host}"


# ── Tool 16: volume & media keys (Windows SendKeys) ──────────────────

_MEDIA_KEYS = {
    "volume_up": "{VOLUME_UP}", "volume_down": "{VOLUME_DOWN}",
    "mute": "{VOLUME_MUTE}", "play_pause": "{MEDIA_PLAY_PAUSE}",
    "next": "{MEDIA_NEXT_TRACK}", "prev": "{MEDIA_PREV_TRACK}",
}


def media_control(action: str) -> str:
    key = _MEDIA_KEYS.get((action or "").strip().lower())
    if not key:
        return "That media control is not recognised."
    if platform.system() != "Windows":
        return "Media keys are only wired up on Windows for now."
    subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         f"(New-Object -ComObject WScript.Shell).SendKeys('{key}')"],
        capture_output=True, text=True, timeout=5,
    )
    return f"sent: {action}"


# ── Tool 17: screenshot (saved to Documents/DeskPet) ──────────────────


def take_screenshot() -> str:
    if platform.system() != "Windows":
        return "Screenshots are only wired up on Windows for now."
    path = docs_dir() / f"screenshot-{datetime.now():%Y%m%d-%H%M%S}.png"
    ps = (
        "Add-Type -AssemblyName System.Windows.Forms,System.Drawing;"
        "$b=[System.Windows.Forms.SystemInformation]::VirtualScreen;"
        "$bmp=New-Object System.Drawing.Bitmap $b.Width,$b.Height;"
        "$g=[System.Drawing.Graphics]::FromImage($bmp);"
        "$g.CopyFromScreen($b.X,$b.Y,0,0,$bmp.Size);"
        f"$bmp.Save('{path}');$g.Dispose();$bmp.Dispose()"
    )
    r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                       capture_output=True, text=True, timeout=15)
    if not path.exists():
        return f"Screenshot capture failed: {(r.stderr or 'unknown error')[:120]}"
    return f"saved: {path}"


# ── Tool 18: lock workstation ──────────────────────────────────────────


def lock_workstation() -> str:
    if platform.system() == "Windows":
        subprocess.run(["rundll32.exe", "user32.dll,LockWorkStation"], timeout=5)
        return "locked: workstation"
    return "Locking is only wired up on Windows for now."


# ── Keyword router (F2 Phase A) ──────────────────────────────────────────────

_RE_REMIND = re.compile(r"\b(?:remind(?:\s*me)?|reminder|timer)\b", re.IGNORECASE)
_RE_REMIND_CANCEL = re.compile(
    r"\b(?:cancel|clear|remove|delete|stop)\b[^.?!]{0,30}\b(?:reminder|timer)s?\b"
    r"|\b(?:reminder|timer)s?\b[^.?!]{0,20}\b(?:cancel|off)\b",
    re.IGNORECASE,
)

# Asked when the user clearly wants a reminder but left out the details.
REMINDER_ASK = (
    "Certainly, sir — what shall I remind you about, and in how many minutes?"
)

# Asked when weather intent has no usable city.
WEATHER_ASK = (
    "Certainly, sir — which city shall I check the weather for?"
)
_RE_TODO_LIST = re.compile(
    r"\b(?:show|list|read)\b.*\b(?:my\s+)?(?:todo|todos|to-dos|tasks?)\b|"
    r"\b(?:my\s+)?(?:todo\s+list|todos|tasks)\b\s*\??$",
    re.IGNORECASE,
)
_TODO_WORDS = r"(?:to-do\s+list|todo\s*list|to-dos|todos|to-do|todo|task\s*list|tasks)"
_RE_TODO_CLEAR = re.compile(
    # "clear my todo list" | "wipe my todos" | "delete all my tasks"
    r"\b(?:clear|wipe|empty|reset|erase)\b.*?\b" + _TODO_WORDS + r"\b|"
    r"\b(?:delete|remove)\s+all\b.*?\b" + _TODO_WORDS + r"\b",
    re.IGNORECASE,
)
_RE_TODO_DONE = re.compile(
    # "mark buy milk as done" | "check off buy milk" | "complete buy milk"
    r"\b(?:mark|tick(?:\s+off)?)\s+(.+?)\s+(?:as\s+)?"
    r"(?:done|complete|completed|finished)\b|"
    r"\bcheck\s+off\s+(.+?)\s*$|"
    r"\b(?:complete|finish)\s+(?:the\s+(?:task|item)\s+)?(.+?)\s*$",
    re.IGNORECASE,
)
_RE_TODO_REMOVE = re.compile(
    # "remove buy milk from my todo" | "delete buy milk from my todo list"
    r"\b(?:remove|delete|drop)\s+(.+?)\s+from\s+(?:my\s+)?" + _TODO_WORDS + r"\b",
    re.IGNORECASE,
)
_RE_TODO_ADD = re.compile(
    # "add to my todo: buy milk" | "add buy milk to my todo"
    r"\badd\s+(?:to\s+)?(?:my\s+)?(?:todo|to-do|todos|task\s*list)\b[:\s]*(.+)"
    r"|\badd\s+(.+?)\s+(?:to|on)\s+(?:my\s+)?(?:todo|to-do|todos|task\s*list|tasks)\b",
    re.IGNORECASE,
)
_RE_DOC = re.compile(
    r"\b(?:write|draft|create|make|prepare)\b(?:\s+(?:me\s+)?(?:a|an|some|the))?"
    r"\s*([a-z\- ]*?(?:meeting\s+notes?|read\s*me|readme|video\s+script|script|outline|"
    r"changelog|change\s+log|to-do\s+list|todo\s+list|e?-?mail|documents?)(?:\s+draft)?)",
    re.IGNORECASE,
)
_RE_TOPIC = re.compile(r"\b(?:about|on|for|regarding)\s+(.+?)(?:\?|!|\.|$)", re.IGNORECASE)
_RE_WEATHER = re.compile(r"\b(?:weather|forecast|temperature)\b", re.IGNORECASE)
_RE_CITY = re.compile(r"\b(?:in|for|at)\s+([A-Za-z][A-Za-z .'-]{1,30}?)(?:\s*(?:today|tomorrow|now|\?|!|\.|$))")
_RE_TIME = re.compile(
    r"\b(?:"
    # time questions in any common phrasing
    r"(?:what(?:'s|s| is)|tell me|show me|check|got|do you (?:have|know)|can you tell me)\s+"
    r"(?:the\s+)?(?:current\s+|right\s+now\s+|local\s+)?time\b"
    r"|time\s+(?:is\s+it|now|please|right\s+now)"
    r"|(?:current|local|exact)\s+time\b"
    # date questions
    r"|(?:what(?:'s|s| is)|tell me|show me)\s+(?:the\s+|today'?s\s+)?date\b"
    r"|today'?s\s+date"
    r"|what\s+day\s+is\s+(?:it|today)"
    r"|(?:what(?:'s|s| is)|tell me)\s+(?:the\s+)?(?:day|date)\s+(?:today|is\s+(?:it|today))"
    r")",
    re.IGNORECASE,
)
_RE_STATUS = re.compile(
    r"\b(?:system\s+status|pc\s+status|computer\s+status|how(?:'s| is) my (?:pc|computer|laptop)|"
    r"cpu\s+usage|ram\s+usage|memory\s+usage|battery\s+(?:status|level)|disk\s+space|"
    r"(?:system|pc|computer)\s+(?:check|health|report|diagnostics?)|"
    r"check\s+(?:the\s+|my\s+)?(?:system|pc|computer)\b|"
    r"run\s+an?\s*(?:system|full)\s+(?:check|diagnostics?))\b",
    re.IGNORECASE,
)
_RE_CLIP = re.compile(r"\bclipboard\b", re.IGNORECASE)
_RE_CURRENCY = re.compile(
    r"([$€£₹])?\s*(\d+(?:[.,]\d+)*)?\s*([$€£₹])?\s*"
    r"(us\s*dollars?|dollars?|rupees?|euros?|pounds?|sterling|yen|yuan|"
    r"bitcoin|ethereum|dirhams?|bucks|[a-z]{3})?\s*"
    r"(?:to|in|into|=|/)\s*"
    r"(us\s*dollars?|dollars?|rupees?|euros?|pounds?|sterling|yen|yuan|"
    r"bitcoin|ethereum|dirhams?|bucks|[a-z]{3})\b",
    re.IGNORECASE,
)
_CUR_HINT = re.compile(r"\b(?:convert|exchange|conversion|rate|value of)\b", re.IGNORECASE)
_SYMBOL_CODES = {"$": "USD", "€": "EUR", "£": "GBP", "₹": "INR"}
_RE_LAUNCH = re.compile(r"\b(?:launch|open|start|run)\s+([a-z0-9][a-z0-9 .'+-]{0,30})", re.IGNORECASE)
_RE_SEARCH = re.compile(
    r"\b(?:search(?:\s+for)?|look\s+up|google|who\s+(?:is|was)|wikipedia)\b\s*(.*)",
    re.IGNORECASE,
)

# ── New-tool triggers (batch 2) ─────────────────────────────────────────────
_RE_PERCENT_OF = re.compile(
    r"(\d+(?:\.\d+)?)\s*(?:%|percent)\s+of\s+(\d+(?:\.\d+)?)", re.IGNORECASE)
_RE_MATHFN = re.compile(
    r"\b(?:sqrt|square\s+root(?:\s+of)?)\s*[(:]?\s*(\d+(?:\.\d+)?)\)?", re.IGNORECASE)
_RE_ARITH = re.compile(
    r"(\d[\d,]*(?:\.\d+)?(?:\s*(?:[-+*/×÷^]|\*\*|plus|minus|times|"
    r"multiplied\s+by|divided\s+by|over|x)\s*\d[\d,]*(?:\.\d+)?)+)",
    re.IGNORECASE)
_RE_CALC_HINT = re.compile(
    r"\b(?:calculate|compute|evaluate|what(?:'s| is)|how much is|solve)\b", re.IGNORECASE)
_UNIT_WORD = (
    r"kilometres?|kilometers?|km|centimetres?|centimeters?|cm|millimetres?|millimeters?|mm|"
    r"meters?|metres?|miles?|mi|feet|foot|ft|inches?|inch|yards?|yd|"
    r"kilograms?|kilos?|kg|grams?|g|pounds?|lbs|lb|ounces?|oz|tonnes?|tons?|"
    r"celsius|fahrenheit|kelvin|km/?h|kmph|kph|mph|knots?|knot|m/?s|"
    r"terabytes?|tb|gigabytes?|gb|megabytes?|mb|kilobytes?|kb|bytes?|b|c|f|k")
_RE_UNITS = re.compile(
    r"(\d+(?:[.,]\d+)?)\s*(?:degrees\s+|°\s*)?(" + _UNIT_WORD + r")\s*"
    r"(?:to|in|into|=)\s*(?:degrees\s+|°\s*)?(" + _UNIT_WORD + r")\b",
    re.IGNORECASE)
_RE_UNITS_REV = re.compile(
    r"how\s+many\s+(" + _UNIT_WORD + r")\s+(?:in|are in|is in)\s+(\d+(?:[.,]\d+)?)\s*"
    r"(?:degrees\s+|°\s*)?(" + _UNIT_WORD + r")\b",
    re.IGNORECASE)
_RE_URL = re.compile(
    r"\b((?:https?://|www\.)[^\s]+)\b|"
    r"\b(?:open|go\s+to|visit|browse\s+to)\s+([a-z0-9][a-z0-9-]*(?:\.[a-z0-9-]+)+)\b",
    re.IGNORECASE)
_RE_WIKI = re.compile(r"\bwiki(?:pedia)?\b\s*(?:about|for|on)?\s*(.+)", re.IGNORECASE)
_RE_WHOIS = re.compile(r"^(?:who|what)\s+(?:is|was|are)\s+(.+?)[?.!]*$", re.IGNORECASE)
_RE_VOLUME = re.compile(
    r"\b(?:volume\s+(?:up|down)|turn\s+(?:the\s+volume|it)\s+(?:up|down)|"
    r"mute|unmute|louder|quieter|sound\s+(?:up|down))\b", re.IGNORECASE)
_RE_MEDIA = re.compile(
    r"\b(?:play|pause|resume)\b(?:\s+(?:the\s+)?(?:music|song|track|audio|video))?|"
    r"\b(?:next|previous|prev|skip)\s+(?:song|track)\b", re.IGNORECASE)
_RE_SHOT = re.compile(
    r"\b(?:take|grab|capture|snap)\s+(?:a\s+)?(?:screen\s*shot|screenshot|screen\s+capture)\b|"
    r"\bscreenshot\b", re.IGNORECASE)
_RE_LOCK = re.compile(
    r"\block\s+(?:my\s+|the\s+)?(?:screen|pc|computer|workstation|machine|laptop)\b", re.IGNORECASE)
_RE_REMEMBER = re.compile(
    r"\b(?:remember|note\s+down|keep\s+in\s+mind)\s+(?:that\s+)?(.+)", re.IGNORECASE)
# Declarative personal facts worth storing even without "remember": a
# possessive + stable-attribute noun + "is" + value. Question words and
# verbs of state disqualify (that's chitchat, not a fact to store).
_RE_PERSONAL_FACT = re.compile(
    r"^\s*(?:hey\s+\w+,?\s*)?(?:please\s+)?my\s+"
    r"(codename|code\s*name|callsign|call\s*sign|nickname|name|password|"
    r"passcode|pin|birthday|anniversary|email(?:\s*address)?|phone(?:\s*number)?|"
    r"address|timezone|favourite|favorite)\s+(?:is|=)\s+(.{2,80}?)[.!?]*\s*$",
    re.IGNORECASE,
)
_RE_MY_ATTR_Q = re.compile(
    r"^\s*what(?:'s| is|\s+was)\s+(?:my|our)\s+"
    r"(?:codename|code\s*name|callsign|call\s*sign|nickname|name|password|"
    r"passcode|pin|birthday|anniversary|email(?:\s*address)?|phone(?:\s*number)?|"
    r"address|timezone|favourite|favorite)\b[?.!]*\s*$",
    re.IGNORECASE,
)
_RE_RECALL = re.compile(
    r"\b(?:what\s+do\s+you\s+(?:remember|know)|do\s+you\s+remember|recall|"
    r"remind\s+me\s+what)\b\s*(?:about\s+)?(.*)", re.IGNORECASE)
_RE_DESTRUCTIVE = re.compile(
    r"\b(?:delete|remove|erase|format|wipe|destroy|nuke)\b.*"
    r"\b(?:system\s*32|windows\s+folder|hard\s*drive|ssd|disk|drive|"
    r"all\s+(?:my\s+)?files|all\s+(?:my\s+)?data|my\s+(?:files|documents|photos|pics)|"
    r"everything|c:\\|d:\\)\b|\brm\s+-rf\b|\bdel\s+/[sqf]\b|"
    r"\bformat\s+(?:my\s+)?(?:pc|computer|disk|drive)\b",
    re.IGNORECASE)

_RE_ACTIVE_WIN = re.compile(
    r"\b(?:what (?:app|application|window|file) am i (?:on|in|using|editing)|"
    r"what(?:'s| is) (?:my |the )?(?:active|current) (?:window|app|application|file)|"
    r"active window|current window|active app|current app)\b",
    re.IGNORECASE,
)
_RE_SCREEN_INSPECT = re.compile(
    r"\b(?:what(?:'s| is) on my screen|look at my screen|read my screen|"
    r"read (?:the )?screen|inspect (?:my |the )?screen|"
    r"what (?:error|stack trace|bug) is on (?:my )?screen|"
    r"explain (?:the |this )?(?:error|code|message) on (?:my )?screen|"
    r"what does (?:my |the )?screen say)\b",
    re.IGNORECASE,
)
_RE_RUNNING_APPS = re.compile(
    r"\b(?:what (?:apps|applications) are (?:open|running)|"
    r"running (?:apps|applications)|list (?:open|running) (?:apps|applications))\b",
    re.IGNORECASE,
)
_LAUNCH_STOPLIST = {
    "chat", "the chat", "bubble", "the bubble", "settings", "a file", "files",
    "my todo", "my todos", "todo", "todos", "a document", "document", "documents",
    "meeting notes", "notes", "the door", "the pod", "it", "this", "that",
    "a new", "my", "your", "the", "up", "the app", "an app",
}


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip(" ?!.,").strip()


def _parse_currency(text: str) -> Optional[Tuple[float, str, str]]:
    """Parse 'convert 1 usd to inr', '500 yen in dollars', 'usd/inr', etc."""
    m = _RE_CURRENCY.search(text)
    if not m:
        return None
    sym_pre, amount_s, sym_post, w_base, w_target = m.groups()
    base = _cur_code(w_base) if w_base else None
    target = _cur_code(w_target)
    symbol = sym_pre or sym_post
    if symbol and symbol in _SYMBOL_CODES:
        base = base or _SYMBOL_CODES[symbol]
    if not base or not target or base == target:
        return None
    # Guard against matching ordinary sentences: require an explicit
    # convert/exchange/rate keyword or at least one known currency word.
    if not _CUR_HINT.search(text) and \
            base not in _CUR_KNOWN_CODES and target not in _CUR_KNOWN_CODES:
        return None
    amount = float(amount_s.replace(",", "")) if amount_s else 1.0
    return amount, base, target


def _wiki_topic_lookup(term: str) -> Optional[str]:
    """Wikipedia summary for a topic phrase, ignoring interrogative
    framing ('how did nikola tesla die' → 'nikola tesla')."""
    stop_lead = {
        "who", "what", "when", "where", "why", "how", "which",
        "did", "do", "does", "is", "was", "are", "were",
        "tell", "me", "please",
    }
    words = [w.strip(".,?!'\"") for w in term.split() if w.strip(".,?!'\"")]
    while words and words[0].lower() in stop_lead:
        words.pop(0)
    while len(words) >= 2:
        res_text = wikipedia_summary(" ".join(words))
        if res_text:
            globals()["_LAST_TOPIC"] = " ".join(w.lower() for w in words)
            return res_text
        words.pop()
    return None


def _wiki_fulltext_sentences(title: str) -> Optional[str]:
    """Up to 2 article-body sentences matching an intent word, or None."""
    try:
        s = httpx.get(
            "https://en.wikipedia.org/w/api.php",
            params={"action": "query", "prop": "extracts", "explaintext": 1,
                    "redirects": 1, "titles": title, "format": "json"},
            timeout=8, headers={"User-Agent": "DeskPet-Jarvis/1.0"},
        )
        pages = ((s.json() or {}).get("query") or {}).get("pages") or {}
        extract = next(iter(pages.values()), {}).get("extract") or ""
        hits = [
            sn.strip() for sn in re.split(r"(?<=[.!?])\s+", extract)
            if _RE_INTENT.search(sn) and 30 < len(sn.strip()) < 400
        ]
        return " ".join(hits[:2]) or None
    except Exception:
        return None


# Death/birth-style intents the short intro summary usually cannot answer.
_RE_INTENT = re.compile(
    r"\b(?:die|died|dies|dying|death|deaths|dead|killed|kill|murder|"
    r"born|birth)\b",
    re.IGNORECASE,
)


def _wiki_intent_lookup(q: str) -> Optional[str]:
    """Topic lookup that digs into the article body when the question asks
    something the intro summary does not cover ('how did tesla die')."""
    res_text = _wiki_topic_lookup(q)
    m = re.search(r"^From Wikipedia on '([^']+)':\s*(.*)$", res_text or "")
    if not (res_text and m):
        return res_text
    if not _RE_INTENT.search(q) or _RE_INTENT.search(m.group(2)):
        return res_text
    extra = _wiki_fulltext_sentences(m.group(1))
    if not extra:
        return res_text
    return f"From Wikipedia on '{m.group(1)}': {extra}"


def _parse_reminder(text: str) -> Optional[Tuple[float, str]]:
    """Extract (minutes, task) from reminder/timer intent.

    The duration may sit anywhere in the sentence and may be a word
    amount — 'set a timer for two minutes to stretch' parses exactly
    like 'remind me in 5 mins to hydrate'. Returns None when the intent
    is present but no usable duration is found (caller asks once).
    """
    if not _RE_REMIND.search(text or ""):
        return None
    dur = _RE_LENIENT_DURATION.search(text)
    if not dur:
        return None
    if dur.group(1) is not None:
        value = float(dur.group(1))
    else:
        head = dur.group(0).split()[0].lower().strip(".,")
        head = head.removesuffix("s") if head not in ("half",) else head
        value = _WORD_NUMBERS.get(head)
        if value is None:
            return None
    unit = (dur.group(2) or "minutes").lower()
    if unit.startswith("h"):
        minutes = value * 60.0
    elif unit.startswith("s"):
        minutes = max(value / 60.0, 0.05)
    else:
        minutes = value
    msg = text[: dur.start()] + " " + text[dur.end():]
    msg = re.sub(
        r"\b(?:please|jarvis|hey|ok(?:ay)?|set|start|a|an|the|timer|reminder|"
        r"remind\s+me|remind|for|in|to)\b",
        " ", msg, flags=re.IGNORECASE,
    )
    return minutes, _clean(re.sub(r"\s+", " ", msg)) or "your reminder"


def _parse_math(text: str) -> Optional[str]:
    """Deterministic arithmetic: percent-of, sqrt, then plain expressions.

    Returns a final 'expr = value' string only when evaluation succeeds;
    otherwise None so the message falls through to other branches.
    """
    m = _RE_PERCENT_OF.search(text)
    if m:
        a, b = float(m.group(1)), float(m.group(2))
        return f"{a:g}% of {b:g} = {a * b / 100:g}"
    m = _RE_MATHFN.search(text)
    if m:
        v = _math.sqrt(float(m.group(1)))
        return f"sqrt({m.group(1)}) = {v:g}"
    m = _RE_ARITH.search(text)
    if not m:
        return None
    expr = m.group(1)
    # Word-form operators need an explicit question hint so that ordinary
    # sentences containing 'x' or 'plus' never reach the evaluator.
    if not re.search(r"[-+*/^÷×]", expr) and not _RE_CALC_HINT.search(text):
        return None
    expr = expr.replace(",", "").replace("×", "*").replace("÷", "/")
    expr = re.sub(r"\s*plus\s*", " + ", expr, flags=re.IGNORECASE)
    expr = re.sub(r"\s*minus\s*", " - ", expr, flags=re.IGNORECASE)
    expr = re.sub(r"\s*(?:times|multiplied\s+by|x)\s*", " * ", expr, flags=re.IGNORECASE)
    expr = re.sub(r"\s*(?:divided\s+by|over)\s*", " / ", expr, flags=re.IGNORECASE)
    expr = expr.replace("^", "**")
    expr = re.sub(r"\s+", "", expr)
    val = _safe_eval(expr)
    if val is None:
        return None
    return f"{expr} = {val:g}"


def _parse_units(text: str) -> Optional[str]:
    """Parse 'convert 5 km to miles' / 'how many feet in 2 meters'."""
    m = _RE_UNITS.search(text)
    if m:
        amount_s, u1, u2 = m.groups()
    else:
        m = _RE_UNITS_REV.search(text)
        if not m:
            return None
        # "how many feet in 2 meters" → convert 2 meters INTO feet.
        u1, amount_s, u2 = m.group(3), m.group(2), m.group(1)
    fu, tu = _unit_lookup(u1), _unit_lookup(u2)
    if not fu or not tu or fu[0] != tu[0]:
        return None
    # Ambiguous short units (c, f, g, m...) need an explicit convert hint.
    ambiguous = (u1.strip().lower() not in _UNIT_UNAMBIGUOUS or
                 u2.strip().lower() not in _UNIT_UNAMBIGUOUS)
    if ambiguous and not _UNIT_HINT.search(text):
        return None
    try:
        amount = float(amount_s.replace(",", ""))
    except ValueError:
        return None
    return convert_units(amount, u1, u2)


_RE_AFFIRM = re.compile(
    r"^\s*(?:yes|yeah|yep|yup|ok|okay|sure|proceed|go ahead|go on|do it|"
    r"confirm|please do|affirmative|sounds good)\b",
    re.IGNORECASE,
)
_RE_DECLINE = re.compile(
    r"^\s*(?:no\b|nope|nah\b|cancel|stop\b|don't|do not|never ?mind|negative)",
    re.IGNORECASE,
)

# Pending action awaiting user confirmation under clarify="confirm_all".
# Single-user desktop app: one slot is enough.
_PENDING_CONFIRM: dict | None = None

# Set right after a REMINDER_ASK so the NEXT message ("two minutes to
# stretch") is interpreted as the missing duration/task instead of
# falling through unmatched. Single-shot: cleared on the next turn.
_REMIND_PENDING: bool = False

# Same pattern for the weather ask: next bare place name completes it.
_WEATHER_PENDING: bool = False

# Last entity a knowledge lookup succeeded on ('who is nikola tesla' →
# 'nikola tesla'). Pronoun follow-ups ('how did he die') substitute it;
# without this the router has no subject to act on and the model freewheels.
_LAST_TOPIC: Optional[str] = None

# Chit-chat / command words that disqualify a phrase from being treated
# as an encyclopedic topic lookup. Question words are deliberately NOT
# here ("how did nikola tesla die" is a knowledge request); first/second
# person pronouns, greetings and imperatives are.
_RE_TOPIC_BLOCKLIST = re.compile(
    r"\b(?:hi|hello|hey|good|morning|evening|night|thanks|thank|"
    r"sorry|please|yes|no|ok(?:ay)?|jarvis|i|you|we|my|your|me|us|"
    r"this|that|it|they|he|she|them|his|her|their|"
    r"write|draft|create|read|delete|remove|clear|add|mark|done|"
    r"summar\w+|translate|rewrite|convert|calculat\w+|comput\w+|"
    r"search|find|check|remember|recall|note|take|capture|lock|"
    r"mute|unmute|increase|decrease|turn|send|schedule|list)\b",
    re.IGNORECASE,
)

_WORD_NUMBERS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
    "twelve": 12, "fifteen": 15, "twenty": 20, "thirty": 30,
    "forty-five": 45, "half": 0.5,
}
_RE_LENIENT_DURATION = re.compile(
    r"\b(?:(\d+(?:\.\d+)?)|one|two|three|four|five|six|seven|eight|nine|ten|"
    r"eleven|twelve|fifteen|twenty|thirty|forty[ -]?five|an? half|half)"
    r"\s*(?:an?\s+)?"
    r"(seconds?|secs?|minutes?|mins?|hours?|hrs?)\b",
    re.IGNORECASE,
)
_RE_LENIENT_FILLER = re.compile(
    r"^(?:please|jarvis|hey|hi|ok(?:ay)?|set|start|a|an|the|timer|reminder|"
    r"remind\s+me|for|in|to)\b[\s,:-]*",
    re.IGNORECASE,
)


def _parse_lenient_reminder(text: str) -> tuple[float, str] | None:
    """Best-effort (minutes, task) from a follow-up like 'two minutes to
    stretch'. Returns None when no usable duration is present."""
    m = _RE_LENIENT_DURATION.search(text or "")
    if not m:
        return None
    amount = m.group(1)
    if amount is not None:
        value = float(amount)
    else:
        word = (m.group(0).split() or [""])[0].lower().replace(" ", "-")
        word = word.removesuffix("s")
        value = _WORD_NUMBERS.get(word, _WORD_NUMBERS.get(m.group(0).strip().lower(), None))
        if value is None:
            return None
    unit = m.group(2).lower()
    if unit.startswith("h"):
        minutes = value * 60.0
    elif unit.startswith("s"):
        minutes = value / 60.0
    else:
        minutes = value
    task = (text[: m.start()] + " " + text[m.end():]).strip()
    while True:
        stripped = _RE_LENIENT_FILLER.sub("", task).strip(" ,.-")
        if stripped == task:
            break
        task = stripped
    return (minutes, task or "your reminder")


def route_tools(
    text: str,
    prior_turns: int = 0,
    clarify: str = "ambiguous",
) -> List[Tuple[str, str]]:
    """Scan the user's latest message, run matched tools, return
    (label, result) pairs for the gateway to relay or speak canned.

    Pure keyword routing (F2 Phase A). Deterministic, fast, and honest —
    the 1B model then composes the spoken reply from real tool output.

    `prior_turns` is the number of messages before this one in the live
    conversation. When > 0 the router grows conservative: broad
    "who is / what is" lookups are skipped entirely (they are usually
    follow-ups whose subject lives in history), and the gateway reserves
    canned replies for opening turns so follow-up answers always flow
    through the model WITH conversation context.

    `clarify` is the runtime-config confirmation strength:
      - "off"         — never emit ("clarify", ...) hits; under-specified
                        requests fall through to the model instead.
      - "ambiguous"   — default behaviour: ask only when key details are
                        missing (no reminder duration, no document topic).
      - "confirm_all" — ask "Shall I proceed with: ...?" BEFORE running
                        any state-changing branch (set_reminder,
                        todo_add, create_document, launch_app) and return
                        without executing it.
    """
    text = _clean(text or "")
    if not text:
        return []
    results: List[Tuple[str, str]] = []

    global _PENDING_CONFIRM, _REMIND_PENDING, _WEATHER_PENDING
    had_remind_pending = _REMIND_PENDING
    _REMIND_PENDING = False
    had_weather_pending = _WEATHER_PENDING
    _WEATHER_PENDING = False

    def run(func, *args, label: str):
        ok, out = safe_tool_call(func, *args)
        results.append((label, out))

    # 0 — resolve a pending confirm_all action from the previous turn.
    if _PENDING_CONFIRM is not None:
        pending = _PENDING_CONFIRM
        if _RE_DECLINE.match(text):
            _PENDING_CONFIRM = None
            results.append(("confirm_cancel", "Very good, sir - cancelled."))
            return results
        if _RE_AFFIRM.match(text):
            _PENDING_CONFIRM = None
            run(pending["func"], *pending["args"], label=pending["label"])
            return results
        # Anything else reads as a new topic; abandon the stale action.
        _PENDING_CONFIRM = None

    # 1 — reminders (checked first: "remind me in 1 minute to X")
    if _RE_REMIND_CANCEL.search(text):
        run(cancel_reminders, label="cancel_reminders")
        return results
    rem = _parse_reminder(text)
    if rem:
        if clarify == "confirm_all":
            _PENDING_CONFIRM = {
                "label": "set_reminder", "func": set_reminder,
                "args": (rem[0], rem[1]),
            }
            results.append(("clarify",
                f"Shall I proceed with: a reminder in {rem[0]:g} minute(s) "
                f"for '{rem[1]}', sir?"))
            return results
        run(set_reminder, rem[0], rem[1], label="set_reminder")
        return results
    # Follow-up to a previous "what shall I remind you about?" ask:
    # "two minutes to stretch" now completes the reminder instead of
    # falling through unmatched (which made the assistant ask twice).
    if had_remind_pending and clarify != "confirm_all":
        lenient = _parse_lenient_reminder(text)
        if lenient is not None:
            run(set_reminder, lenient[0], lenient[1], label="set_reminder")
            return results
    # Follow-up to a previous weather ask: a bare place name ("Mumbai")
    # completes the request instead of re-asking forever.
    if had_weather_pending and clarify != "confirm_all":
        guess = re.sub(
            r"\b(?:weather|temperature|forecast|the|in|at|for|today|now|please)\b",
            " ", text, flags=re.IGNORECASE,
        ).strip(" ,.?!")
        if (
            guess
            and 1 <= len(guess.split()) <= 3
            and not re.search(
                r"\b(?:hi|hello|hey|thanks|thank|yes|no|ok(?:ay)?|"
                r"never\s*mind|cancel|forget|stop|nothing)\b",
                guess, re.IGNORECASE,
            )
        ):
            run(get_weather, guess.title(), label="get_weather")
            return results
    if _RE_REMIND.search(text):
        # Intent without a usable duration/task → ask instead of guessing.
        # ("remind me to stretch" must not silently become a 5-minute timer.)
        # clarify="off" skips the canned ask — the message falls through to
        # the model instead.
        if clarify != "off":
            _REMIND_PENDING = True
            results.append(("clarify", REMINDER_ASK))
            return results

    # 2 — todo family: clear → remove → done → list → add
    if _RE_TODO_CLEAR.search(text):
        run(todo_clear, label="todo_clear")
        return results
    m = _RE_TODO_REMOVE.search(text)
    if m:
        run(todo_remove, _clean(m.group(1)), label="todo_remove")
        return results
    m = _RE_TODO_DONE.search(text)
    if m:
        run(todo_done, _clean(m.group(1) or m.group(2) or m.group(3)), label="todo_done")
        return results
    if _RE_TODO_LIST.search(text) and not _RE_TODO_ADD.search(text) and not _RE_DOC.search(text):
        run(todo_list, label="todo_list")
        return results
    m = _RE_TODO_ADD.search(text)
    if m:
        item = m.group(1) or m.group(2)
        if clarify == "confirm_all":
            _PENDING_CONFIRM = {
                "label": "todo_add", "func": todo_add, "args": (item,),
            }
            results.append(("clarify",
                f"Shall I proceed with: adding '{item}' to your to-do list, sir?"))
            return results
        run(todo_add, item, label="todo_add")
        return results

    # 2b — destructive requests: refuse firmly, run nothing
    if _RE_DESTRUCTIVE.search(text):
        results.append(("safety_refusal",
            "I am afraid I cannot do that, sir. Destructive operations on this "
            "machine are outside my remit — and I would respectfully suggest "
            "they remain so."))
        return results

    # 2c — arithmetic & unit conversion (deterministic, offline)
    mres = _parse_math(text)
    if mres:
        results.append(("calculate", mres))
        return results
    ures = _parse_units(text)
    if ures:
        results.append(("unit_convert", ures))
        return results

    # 3 — document drafting
    m = _RE_DOC.search(text)
    if m:
        raw_type = m.group(1).lower()
        kind = "meeting_notes"
        for kw, k in _DOC_KEYWORDS:
            if kw in raw_type:
                kind = k
                break
        topic_m = _RE_TOPIC.search(text)
        topic = _clean(topic_m.group(1)) if topic_m else ""
        if not topic:
            # Missing subject → ask rather than create a file named
            # after a generic placeholder. clarify="off" lets the model
            # handle the under-specified request instead.
            if clarify != "off":
                results.append(("clarify", f"Certainly, sir — what should the {kind} cover?"))
                return results
        elif clarify == "confirm_all":
            _PENDING_CONFIRM = {
                "label": "create_document", "func": create_document,
                "args": (kind, topic),
            }
            results.append(("clarify",
                f"Shall I proceed with: drafting a {kind.replace('_', ' ')} "
                f"document about '{topic}', sir?"))
            return results
        else:
            run(create_document, kind, topic, label="create_document")
            return results

    # 3b — currency conversion ("convert 1 usd to inr")
    cur = _parse_currency(text)
    if cur:
        run(convert_currency, cur[0], cur[1], cur[2], label="convert_currency")
        return results

    # 3c — explicit URL → fetch page (mini-RAG: model summarises the text)
    m = _RE_URL.search(text)
    if m and m.group(1):
        run(fetch_page, m.group(1), label="fetch_page")
        return results

    # 3d — wikipedia lookup
    m = _RE_WIKI.search(text)
    if m:
        term = _clean(m.group(1))
        res = wikipedia_summary(term)
        results.append(("wikipedia", res or f"No Wikipedia article found for '{term}'."))
        return results

    # 4 — weather
    if _RE_WEATHER.search(text):
        cm = _RE_CITY.search(text)
        city = _clean(cm.group(1)) if cm else None
        if city and city.lower() in ("the", "a", "it", "this", "my", "here"):
            city = None
        if city is None:
            if clarify != "off":
                # Ask once; the next bare place name completes the request.
                _WEATHER_PENDING = True
                results.append(("clarify", WEATHER_ASK))
                return results
        else:
            run(get_weather, city, label="get_weather")
            return results

    # 5 — time / date
    if _RE_TIME.search(text):
        run(get_time, label="get_time")
        return results

    # 6 — system status
    if _RE_STATUS.search(text):
        run(system_status, label="system_status")
        return results

    # 7 — clipboard
    if _RE_CLIP.search(text):
        action = "summarize" if re.search(r"\bsummar\w+", text, re.I) else \
                 "rewrite" if re.search(r"\b(rewrite|rephrase|improve)\b", text, re.I) else \
                 "translate" if re.search(r"\btranslat\w+", text, re.I) else "show"
        run(clipboard_assist, action, label="clipboard_assist")
        return results

    # 7b — volume & media keys
    if _RE_VOLUME.search(text):
        act = "mute" if re.search(r"\b(?:mute|unmute)\b", text, re.I) else \
              "volume_down" if re.search(r"\b(?:down|quieter)\b", text, re.I) else \
              "volume_up"
        run(media_control, act, label="media_control")
        return results
    if _RE_MEDIA.search(text):
        act = "next" if re.search(r"\b(?:next|skip)\b", text, re.I) else \
              "prev" if re.search(r"\b(?:previous|prev)\b", text, re.I) else \
              "play_pause"
        run(media_control, act, label="media_control")
        return results

    # 7c — screenshot / lock
    if _RE_SHOT.search(text):
        run(take_screenshot, label="screenshot")
        return results
    if _RE_LOCK.search(text):
        run(lock_workstation, label="lock")
        return results

    # 7d — quick memory: recall before remember (longer trigger first)
    m = _RE_RECALL.search(text)
    if m:
        run(recall_fact, _clean(m.group(1)), label="recall")
        return results
    m = _RE_REMEMBER.search(text)
    if m:
        run(remember_fact, _clean(m.group(1)), label="remember")
        return results

    # 7d — implicit personal-fact statements ("my codename is Blue Falcon").
    # No "remember" keyword, so this used to fall to the native round where
    # the model would web-search the noun instead of saving the fact.
    # The FULL sentence is stored (m.group(0)), not just the attribute.
    m = _RE_PERSONAL_FACT.search(text)
    if m:
        run(remember_fact, _clean(m.group(0)), label="remember")
        return results

    # 7d' — personal-attribute questions recall from notes instead of
    # searching the world for the noun.
    m = _RE_MY_ATTR_Q.search(text)
    if m:
        run(recall_fact, _clean(m.group(0)), label="recall")
        return results

    # 7e — open a website (must run before launch_app grabs the domain)
    m = _RE_URL.search(text)
    if m:
        run(open_url, m.group(1) or m.group(2), label="open_url")
        return results

    # 7f — screen & active window perception
    if _RE_ACTIVE_WIN.search(text):
        from . import screen_context
        run(screen_context.get_active_window, label="active_window")
        return results

    if _RE_SCREEN_INSPECT.search(text):
        from . import screen_context
        run(screen_context.inspect_screen, label="inspect_screen")
        return results

    if _RE_RUNNING_APPS.search(text):
        from . import screen_context
        run(screen_context.get_running_apps_summary, label="running_apps")
        return results

    # 7g — vocalize / speak aloud — TTS removed from the project

    # 8 — launch app
    m = _RE_LAUNCH.search(text)
    if m:
        name = _clean(m.group(1))
        for tail in (" for me", " please", " app", " application", " on my pc", " now"):
            if name.endswith(tail):
                name = name[: -len(tail)].strip()
        if name and name.lower() not in _LAUNCH_STOPLIST and len(name.split()) <= 3:
            if clarify == "confirm_all":
                _PENDING_CONFIRM = {
                    "label": "launch_app", "func": launch_app, "args": (name,),
                }
                results.append(("clarify",
                    f"Shall I proceed with: launching {name}, sir?"))
                return results
            run(launch_app, name, label="launch_app")
            return results

    # 9 — who/what questions: try Wikipedia first, then web search.
    # Referential mid-chat forms ("what is it / who is he") still skip —
    # Wikipedia-ing a literal pronoun is exactly how follow-ups used to
    # break. First/second-person possessives skip too: "what is my
    # codename" must never become an encyclopedia search for the noun.
    m = _RE_WHOIS.search(text)
    if m:
        subject = _clean(m.group(1))
        if subject and not re.match(
            r"^(?:he|she|it|they|that|this|his|her|their|them|my|our|your)\b",
            subject, re.IGNORECASE,
        ):
            res = wikipedia_summary(subject)
            if res:
                globals()["_LAST_TOPIC"] = subject.lower()
                results.append(("wikipedia", res))
                return results

    m = _RE_SEARCH.search(text)
    if m:
        # "search for X" / "look up X" are explicit commands — always route.
        implicit_whois = re.match(r"^\s*who\s+(?:is|was)\b", text, re.IGNORECASE)
        subject_after = text[implicit_whois.end():].strip() if implicit_whois else ""
        referential = bool(re.match(
            r"^(?:he|she|it|they|that|this|his|her|their|them)\b",
            subject_after, re.IGNORECASE,
        ))
        if not referential:
            query = _clean(m.group(1)) or text
            run(web_search, query, label="web_search")
            return results

    # 9a — pronoun follow-ups ("how did he die", "what happened to her").
    # The subject is referential and the topic blocklist rightly rejects a
    # literal pronoun, so resolve against the last topic a knowledge lookup
    # succeeded on. Without this the text routes nowhere and the armed-tools
    # native round freewheels refusals + fabrications.
    if re.search(r"\b(?:he|she|him|her)\b", text, re.IGNORECASE) and _LAST_TOPIC:
        words = [
            w for w in re.findall(r"[a-z']+", text.lower())
            if w not in {
                "how", "what", "when", "where", "why", "which", "who", "whom",
                "did", "do", "does", "was", "were", "is", "are",
                "he", "she", "him", "her", "his", "hers", "their",
                "the", "a", "an", "to", "of", "in", "at", "on",
            } and len(w) > 1
        ]
        # Bare referentials ("who is he?") carry nothing new — the model
        # answers from history. Only questions with real content route.
        if words:
            q = _LAST_TOPIC + " " + " ".join(words)
            res_text = _wiki_intent_lookup(q)
            if res_text:
                results.append(("wikipedia", res_text))
                return results
            run(web_search, q, label="web_search")
            return results

    # 9b — knowledge lookup: bare topics AND open questions that name a
    # thing ("nikola tesla death", "how did nikola tesla die"). These are
    # requests for facts, not chat — Wikipedia (progressively trimmed)
    # then web search, instead of letting the armed-tools native round
    # freewheel refusals.
    m = re.match(r"^([A-Za-z][A-Za-z'.]*(?:\s+[A-Za-z][A-Za-z'.]*){1,5})[?.!]*$", text)
    if m:
        low = " " + m.group(1).lower() + " "
        if not re.search(_RE_TOPIC_BLOCKLIST, low):
            res_text = _wiki_intent_lookup(_clean(m.group(1)))
            if res_text:
                results.append(("wikipedia", res_text))
                return results
            run(web_search, _clean(m.group(1)), label="web_search")
            return results

    return results


# ── Canned replies (deterministic Jarvis voice for mechanical tools) ────────
#
# 1B models relay facts fine but freewheel narration they were never asked
# for ("I used the web search tool..."). For purely mechanical results we
# therefore skip the model entirely and speak a fixed line built from the
# real tool output. The model still composes replies for tools whose output
# needs shaping (weather, search, documents, clipboard, status).

_CANNED_DONE = re.compile(r"Marked as done: '(.+)'")
_CANNED_REMOVED = re.compile(r"Removed from your to-do list: '(.+)'")
_CANNED_CLEARED = re.compile(r"removed (\d+) item")
_CANNED_RATE = re.compile(r"(\S+ \S+ = .+?)\s*\(live rate: (.+), updated")
_CANNED_REMIND = re.compile(r"Reminder set for (.+) from now: '(.+)'")


def canned_reply(label: str, result: str) -> Optional[str]:
    """Fixed Jarvis-voice line for a tool result, or None → model composes."""
    if label == "cancel_reminders":
        result = (result or "").strip()
        return result if result.startswith(("Cancelled", "No pending")) else None
    if label == "get_weather":
        # Ready-made factual line ("Mumbai, India: 26.9°C, light drizzle
        # (...)"). Relay verbatim — the 1B sometimes "honestly" claims it
        # cannot access weather even when handed the reading.
        if result and not re.match(r"^(?:no |could not|weather unavailable)", result, re.I):
            return result.strip()
        return None
    if label == "wikipedia":
        # Ready-made prose — relay verbatim. The 1B model sometimes
        # "honestly" claims it lacks access even when handed a full
        # summary; Wikipedia text needs no shaping anyway.
        if result and "No Wikipedia article" not in result:
            return result.strip()
        return None
    if label == "web_search":
        # Wiki-backed fallback results are ready-made prose — relay them
        # like wikipedia hits. Genuine DDG snippets stay model-composed.
        if result and result.startswith("From Wikipedia"):
            return result.strip()
        return None
    if label == "todo_clear":
        if "already empty" in result:
            return "Your to-do list is already empty, sir — nothing to clear."
        m = _CANNED_CLEARED.search(result)
        n = m.group(1) if m else "the"
        return f"Very good, sir. Your to-do list has been cleared — {n} item(s) removed."
    if label == "todo_add":
        m = re.search(r"to-do list: '(.+)'", result)
        if m:
            return f"Noted, sir. '{m.group(1)}' is on your to-do list."
    if label == "todo_done":
        m = _CANNED_DONE.search(result)
        if m:
            return f"Done, sir. '{m.group(1)}' is marked complete."
        return result.rstrip(".") + ", sir."
    if label == "todo_remove":
        m = _CANNED_REMOVED.search(result)
        if m:
            return f"Removed '{m.group(1)}' from your to-do list, sir."
        return result.rstrip(".") + ", sir."
    if label == "convert_currency":
        m = _CANNED_RATE.search(result)
        if m:
            return f"At the live rate, {m.group(1)} — {m.group(2)}."
        return result
    if label == "get_time":
        return result.rstrip(".") + ", sir."
    if label == "set_reminder":
        m = _CANNED_REMIND.search(result)
        if m:
            return f"Reminder set, sir — '{m.group(2)}' in {m.group(1)}. I will ping you."
    if label == "calculate":
        return f"As I compute it, {result}, sir."
    if label == "unit_convert":
        return f"By my reckoning, {result}, sir."
    if label == "open_url":
        host = result.replace("opened: ", "")
        return f"Opening {host} in your browser, sir."
    if label == "media_control":
        lines = {
            "volume_up": "Turning the volume up, sir.",
            "volume_down": "Turning the volume down, sir.",
            "mute": "Toggling the mute, sir.",
            "play_pause": "Toggling playback, sir.",
            "next": "Skipping to the next track, sir.",
            "prev": "Going back to the previous track, sir.",
        }
        return lines.get(result.replace("sent: ", ""), "Done, sir.")
    if label == "screenshot":
        if result.startswith("saved: "):
            return f"Screenshot captured and saved to {result[7:]}, sir."
        return result
    if label == "lock":
        return "Locking the workstation now, sir."
    if label == "remember":
        return f"Noted, sir. I shall remember: '{result.replace('remembered: ', '')}'."
    if label == "recall":
        return result
    if label == "safety_refusal":
        return result
    if label == "active_window":
        return f"You are currently working in {result}, sir."
    if label == "running_apps":
        return f"{result}, sir."
    if label == "speak":
        return "Spoken aloud, sir."
    return None
