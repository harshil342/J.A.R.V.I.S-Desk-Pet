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
import math as _math
import os
import platform
import re
import shutil
import subprocess
import threading
import time
import traceback
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, List, Optional, Tuple
from urllib.parse import quote

import httpx

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
]


def create_document(doc_type: str, topic: str) -> str:
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

    def fire():
        when = datetime.now().strftime("%H:%M")
        if _bridge is not None:
            try:
                _bridge.post("notification", event="Notification",
                             title=f"⏰ Reminder: {msg}")
            except Exception:
                pass
        print(f"[tools] reminder fired at {when}: {msg}")

    timer = threading.Timer(seconds, fire)
    timer.daemon = True  # never block interpreter / server shutdown
    timer.start()
    human = f"{minutes:g} minute(s)" if minutes < 60 else f"{minutes / 60:g} hour(s)"
    return f"Reminder set for {human} from now: '{msg}'. I will ping you."


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


def _item_body(line: str) -> str:
    """Strip the checkbox prefix and the _(added ...)_ suffix from an item."""
    return line[6:].split("_(")[0].strip()


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


def _read_clipboard() -> str:
    if platform.system() == "Windows":
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command", "Get-Clipboard -Raw"],
            capture_output=True, text=True, timeout=5,
        )
        return (r.stdout or "").strip()
    r = subprocess.run(["xclip", "-selection", "clipboard", "-o"],
                       capture_output=True, text=True, timeout=5)
    return (r.stdout or "").strip()


def clipboard_assist(action: str) -> str:
    text = _read_clipboard()
    if not text:
        return "The clipboard is empty."
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
            s = httpx.get(
                "https://en.wikipedia.org/w/api.php",
                params={"action": "opensearch", "search": term, "limit": 1,
                        "format": "json"},
                timeout=8, headers={"User-Agent": "DeskPet-Jarvis/1.0"},
            )
            hits = (s.json() or [None, []])[1]
            if not hits:
                return None
            r = httpx.get(
                "https://en.wikipedia.org/api/rest_v1/page/summary/" + quote(hits[0]),
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


def remember_fact(text: str) -> str:
    text = (text or "").strip(" .!?")
    if not text:
        return "Nothing to remember — tell me the fact."
    f = _notes_file()
    if not f.exists():
        f.write_text("# DeskPet Memory\n\n", encoding="utf-8")
    with f.open("a", encoding="utf-8") as fh:
        fh.write(f"- {text}  _(saved {datetime.now():%Y-%m-%d %H:%M})_\n")
    return f"remembered: {text}"


def recall_fact(query: str) -> str:
    query = (query or "").strip().lower()
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

_RE_REMIND = re.compile(
    r"\b(?:remind me|set (?:a )?timer|timer)\b\s*"
    r"(?:in\s+)?(\d+(?:\.\d+)?)?\s*(minutes?|mins?|hours?|hrs?|seconds?|secs?)?",
    re.IGNORECASE,
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
    r"changelog|change\s+log|to-do\s+list|todo\s+list|e?-?mail)(?:\s+draft)?)",
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
    r"cpu\s+usage|ram\s+usage|memory\s+usage|battery\s+(?:status|level)|disk\s+space)\b",
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


def _parse_reminder(text: str) -> Optional[Tuple[float, str]]:
    m = _RE_REMIND.search(text)
    if not m:
        return None
    num = m.group(1)
    unit = (m.group(2) or "minutes").lower()
    if num is None:
        return None  # "remind me" without a duration → ignore
    value = float(num)
    if unit.startswith("hour") or unit.startswith("hr"):
        minutes = value * 60
    elif unit.startswith("sec"):
        minutes = max(value / 60, 0.05)
    else:
        minutes = value
    msg = text[m.end():]
    msg = re.sub(r"^\s*(?:to|about|that|for)\s+", "", msg, flags=re.IGNORECASE)
    return minutes, _clean(msg) or "your reminder"


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


def route_tools(text: str) -> List[Tuple[str, str]]:
    """Scan the user's latest message, run matched tools, return
    (label, result) pairs for the gateway to relay or speak canned.

    Pure keyword routing (F2 Phase A). Deterministic, fast, and honest —
    the 1B model then composes the spoken reply from real tool output.
    """
    text = _clean(text or "")
    if not text:
        return []
    results: List[Tuple[str, str]] = []

    def run(func, *args, label: str):
        ok, out = safe_tool_call(func, *args)
        results.append((label, out))

    # 1 — reminders (checked first: "remind me in 1 minute to X")
    rem = _parse_reminder(text)
    if rem:
        run(set_reminder, rem[0], rem[1], label="set_reminder")
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
        run(todo_add, m.group(1) or m.group(2), label="todo_add")
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
        if city and city.lower() in ("the", "a", "it", "this"):
            city = None
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

    # 7e — open a website (must run before launch_app grabs the domain)
    m = _RE_URL.search(text)
    if m:
        run(open_url, m.group(1) or m.group(2), label="open_url")
        return results

    # 8 — launch app
    m = _RE_LAUNCH.search(text)
    if m:
        name = _clean(m.group(1))
        for tail in (" for me", " please", " app", " application", " on my pc", " now"):
            if name.endswith(tail):
                name = name[: -len(tail)].strip()
        if name and name.lower() not in _LAUNCH_STOPLIST and len(name.split()) <= 3:
            run(launch_app, name, label="launch_app")
            return results

    # 9 — who/what questions: try Wikipedia first, then web search
    m = _RE_WHOIS.search(text)
    if m:
        res = wikipedia_summary(_clean(m.group(1)))
        if res:
            results.append(("wikipedia", res))
            return results

    m = _RE_SEARCH.search(text)
    if m:
        query = _clean(m.group(1)) or text
        run(web_search, query, label="web_search")
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
    return None
