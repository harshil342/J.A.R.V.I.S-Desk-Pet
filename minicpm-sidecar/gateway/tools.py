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
import socket
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, List, Optional, Tuple
from urllib.parse import quote

import httpx

from .log_setup import get_logger

log = get_logger("tools")

# ── Internet connectivity probing (Butler dual-mode) ──────────────────────────

_ONLINE_CACHE_TTL = 5.0  # seconds
_last_online_check: float = 0.0
_is_currently_online: bool = True
_online_override: Optional[bool] = None


def set_internet_connection_override(status: Optional[bool]) -> None:
    """Manually override internet connectivity for testing or offline enforcement."""
    global _online_override
    _online_override = status


def check_internet_connection(force: bool = False, timeout: float = 0.5) -> bool:
    """Fast probe to determine if the system has active internet connectivity.
    Results are cached for 5 seconds to prevent latency spikes during multi-turn chats.
    """
    global _last_online_check, _is_currently_online
    if _online_override is not None:
        return _online_override

    env_force = os.environ.get("DESKPET_FORCE_OFFLINE")
    if env_force == "1":
        return False
    if env_force == "0":
        return True

    now = time.monotonic()
    if not force and (now - _last_online_check < _ONLINE_CACHE_TTL):
        return _is_currently_online

    online = False
    for host in ("1.1.1.1", "8.8.8.8"):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.settimeout(timeout)
                sock.connect((host, 53))
                online = True
                break
        except Exception:
            continue

    _last_online_check = now
    _is_currently_online = online
    return online

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
    for url in ("http://ip-api.com/json", "https://ipwhois.app/json/"):
        try:
            r = httpx.get(url, timeout=3.5)
            if r.status_code == 200:
                d = r.json()
                lat = d.get("lat") or d.get("latitude")
                lon = d.get("lon") or d.get("longitude")
                if lat is not None and lon is not None:
                    return {
                        "lat": float(lat),
                        "lon": float(lon),
                        "name": d.get("city") or "your location",
                        "country": d.get("country") or d.get("country_name") or "",
                    }
        except Exception:
            continue
    return None


def get_weather(city: Optional[str] = None) -> str:
    if not check_internet_connection():
        return "Weather lookup unavailable: network is offline (air-gapped mode)."
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


# ── Tool 2: clock & time (offline, any timezone) ─────────────────────────────

_TIMEZONES = {
    # UTC / GMT
    "utc": (0, "UTC", "UTC"),
    "gmt": (0, "GMT", "GMT"),
    # Europe
    "london": (1, "London", "BST/GMT"),
    "uk": (1, "United Kingdom", "BST/GMT"),
    "paris": (2, "Paris", "CEST"),
    "france": (2, "France", "CEST"),
    "berlin": (2, "Berlin", "CEST"),
    "germany": (2, "Germany", "CEST"),
    "rome": (2, "Rome", "CEST"),
    "italy": (2, "Italy", "CEST"),
    "madrid": (2, "Madrid", "CEST"),
    "spain": (2, "Spain", "CEST"),
    "amsterdam": (2, "Amsterdam", "CEST"),
    # Middle East & Africa
    "cairo": (3, "Cairo", "EEST"),
    "egypt": (3, "Egypt", "EEST"),
    "moscow": (3, "Moscow", "MSK"),
    "russia": (3, "Moscow", "MSK"),
    "dubai": (4, "Dubai", "GST"),
    "uae": (4, "UAE", "GST"),
    # Asia
    "karachi": (5, "Karachi", "PKT"),
    "pakistan": (5, "Pakistan", "PKT"),
    "india": (5.5, "India", "IST"),
    "ist": (5.5, "India", "IST"),
    "delhi": (5.5, "Delhi", "IST"),
    "new delhi": (5.5, "New Delhi", "IST"),
    "mumbai": (5.5, "Mumbai", "IST"),
    "bangalore": (5.5, "Bangalore", "IST"),
    "bengaluru": (5.5, "Bengaluru", "IST"),
    "kolkata": (5.5, "Kolkata", "IST"),
    "chennai": (5.5, "Chennai", "IST"),
    "bangkok": (7, "Bangkok", "ICT"),
    "thailand": (7, "Thailand", "ICT"),
    "jakarta": (7, "Jakarta", "WIB"),
    "singapore": (8, "Singapore", "SGT"),
    "hong kong": (8, "Hong Kong", "HKT"),
    "beijing": (8, "Beijing", "CST"),
    "shanghai": (8, "Shanghai", "CST"),
    "china": (8, "China", "CST"),
    "taipei": (8, "Taipei", "CST"),
    "perth": (8, "Perth", "AWST"),
    "tokyo": (9, "Tokyo", "JST"),
    "japan": (9, "Japan", "JST"),
    "seoul": (9, "Seoul", "KST"),
    "korea": (9, "South Korea", "KST"),
    # Australia & Pacific
    "sydney": (10, "Sydney", "AEST"),
    "melbourne": (10, "Melbourne", "AEST"),
    "brisbane": (10, "Brisbane", "AEST"),
    "australia": (10, "Sydney", "AEST"),
    "auckland": (12, "Auckland", "NZST"),
    "new zealand": (12, "Auckland", "NZST"),
    # Americas
    "honolulu": (-10, "Honolulu", "HST"),
    "hawaii": (-10, "Hawaii", "HST"),
    "anchorage": (-8, "Anchorage", "AKDT"),
    "alaska": (-8, "Alaska", "AKDT"),
    "los angeles": (-7, "Los Angeles", "PDT"),
    "la": (-7, "Los Angeles", "PDT"),
    "san francisco": (-7, "San Francisco", "PDT"),
    "seattle": (-7, "Seattle", "PDT"),
    "vancouver": (-7, "Vancouver", "PDT"),
    "california": (-7, "California", "PDT"),
    "pst": (-8, "Pacific Standard Time", "PST"),
    "pdt": (-7, "Pacific Daylight Time", "PDT"),
    "denver": (-6, "Denver", "MDT"),
    "colorado": (-6, "Colorado", "MDT"),
    "mst": (-7, "Mountain Standard Time", "MST"),
    "mdt": (-6, "Mountain Daylight Time", "MDT"),
    "chicago": (-5, "Chicago", "CDT"),
    "dallas": (-5, "Dallas", "CDT"),
    "houston": (-5, "Houston", "CDT"),
    "cst": (-6, "Central Standard Time", "CST"),
    "cdt": (-5, "Central Daylight Time", "CDT"),
    "new york": (-4, "New York", "EDT"),
    "nyc": (-4, "New York City", "EDT"),
    "boston": (-4, "Boston", "EDT"),
    "washington": (-4, "Washington, D.C.", "EDT"),
    "dc": (-4, "Washington, D.C.", "EDT"),
    "miami": (-4, "Miami", "EDT"),
    "toronto": (-4, "Toronto", "EDT"),
    "est": (-5, "Eastern Standard Time", "EST"),
    "edt": (-4, "Eastern Daylight Time", "EDT"),
    "sao paulo": (-3, "São Paulo", "BRT"),
    "brazil": (-3, "São Paulo", "BRT"),
    "buenos aires": (-3, "Buenos Aires", "ART"),
    "argentina": (-3, "Buenos Aires", "ART"),
}


def get_time(location: Optional[str] = None) -> str:
    from datetime import timezone
    if location:
        norm = location.strip().lower().rstrip("?.!,")
        if norm in _TIMEZONES:
            offset_hours, name, code = _TIMEZONES[norm]
            tz = timezone(timedelta(hours=offset_hours))
            dt = datetime.now(timezone.utc).astimezone(tz)
            return f"{dt.strftime('%A, %B %d, %Y, %I:%M %p').lstrip('0')} in {name} ({code})."
        for k, (offset_hours, name, code) in _TIMEZONES.items():
            if k == norm or k in norm.split():
                tz = timezone(timedelta(hours=offset_hours))
                dt = datetime.now(timezone.utc).astimezone(tz)
                return f"{dt.strftime('%A, %B %d, %Y, %I:%M %p').lstrip('0')} in {name} ({code})."

    now = datetime.now()
    # ponytail: 12-hour format with AM/PM
    return f"{now.strftime('%A, %B %d, %Y, %I:%M %p').lstrip('0')} (local time)."


# ── Tool 3: web search (DuckDuckGo instant answers, no key) ─────────────────


def web_search(query: str) -> str:
    if not check_internet_connection():
        return "I am currently running in offline (air-gapped) mode without internet access, sir. I cannot browse the web or look up external information right now."
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
        # First fallback: query DuckDuckGo HTML search for live search snippets
        # to ground the butler with actual web results on technical/specific queries.
        try:
            r_html = httpx.get(
                "https://html.duckduckgo.com/html/",
                params={"q": query},
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) DeskPet/1.0"},
                timeout=5,
            )
            raw_html = getattr(r_html, "text", "") or ""
            if r_html.status_code == 200 and raw_html:
                raw_snippets = re.findall(r'result__snippet.*?>(.*?)</a>', raw_html, re.DOTALL)
                clean_snippets = [
                    re.sub(r'<[^>]+>', '', s).strip() for s in raw_snippets if s.strip()
                ]
                if clean_snippets:
                    return "Web search: " + " | ".join(clean_snippets[:3])[:1200]
        except Exception:
            pass

        # Second fallback: Wikipedia topic lookup if live search yielded nothing
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
    if not check_internet_connection():
        return "Live currency conversion is unavailable while offline, sir."
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


def _clean_doc_topic(topic: str) -> str:
    topic = (topic or "").strip()
    topic = re.sub(r"^(?:about|on|for|regarding)\s+", "", topic, flags=re.IGNORECASE).strip()
    return topic


def _fetch_topic_knowledge(topic: str) -> Tuple[str, List[str]]:
    """Returns (canonical_title, list_of_factual_sentences)."""
    clean = _clean_doc_topic(topic)
    core = re.sub(r"^(?:the|a|an)\s+", "", clean, flags=re.IGNORECASE).strip()
    candidates = [clean]
    if core and core != clean:
        candidates.append(core)

    for cand in candidates:
        try:
            res = wikipedia_summary(cand)
            if res and not res.startswith("No Wikipedia article"):
                m = re.match(r"^From Wikipedia on '(.+?)':\s*(.+)$", res, re.DOTALL)
                if m:
                    wiki_title = m.group(1).strip()
                    extract = m.group(2).strip()
                    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", extract) if len(s.strip()) > 5]
                    if sentences:
                        return (wiki_title, sentences)
        except Exception:
            pass

    disp_title = clean.title() if clean.islower() else (clean[0].upper() + clean[1:] if clean else "Untitled Document")
    return (disp_title, [])


@_doc_template("meeting_notes")
def _tpl_meeting(topic: str, stamp: str) -> str:
    clean = _clean_doc_topic(topic)
    title, sentences = _fetch_topic_knowledge(clean)
    display_title = title or clean.title() or "Untitled Meeting"

    if sentences:
        ctx = " ".join(sentences[:2])
        findings = "\n".join(f"- {s}" for s in sentences[2:6]) if len(sentences) > 2 else f"- Explored core background and implications of {display_title}."
        return f"""# Meeting Notes — {display_title}

> {stamp} · drafted by DeskPet Jarvis

## Overview & Background
{ctx}

## Discussion Points & Findings
{findings}

## Key Decisions
- Confirmed understanding and operational scope for {display_title}.
- Aligned on priority takeaways and knowledge archiving.

## Action Items
- [ ] Synthesize findings into the final project dossier — Due: End of week
- [ ] Share documented brief with core stakeholders — Due: Next check-in

## Next Steps
- Incorporate follow-up feedback from the team.
- Retain notes in DeskPet local document archive.
"""

    return f"""# Meeting Notes — {display_title}

> {stamp} · drafted by DeskPet Jarvis

## Attendees
- Project Lead
- Core Team Contributors

## Agenda
1. Review objectives and current status of {display_title}
2. Align on functional requirements, timeline, and dependencies
3. Identify roadblocks and assign action items

## Key Decisions
- Confirmed project scope and priority milestones for {display_title}.
- Approved initial architecture and implementation roadmap.

## Action Items
- [ ] Finalize technical specification for {display_title} — Due: End of week
- [ ] Review resource allocation and deliverable schedule — Due: Next sprint

## Next Steps
- Follow up on outstanding action items in next check-in.
- Distribute meeting notes to all attendees.
"""


@_doc_template("readme")
def _tpl_readme(topic: str, stamp: str) -> str:
    clean = _clean_doc_topic(topic)
    title = clean.title() if clean.islower() else (clean[0].upper() + clean[1:] if clean else "Project")
    project_slug = re.sub(r"[^a-zA-Z0-9_-]", "-", title.lower()).strip("-") or "project"
    return f"""# {title}

> {stamp} · drafted by DeskPet Jarvis

Overview of {title}: high-performance, modular system designed for reliable execution and seamless user experience.

## Features
- Modular and extensible architecture
- High-efficiency local and cloud processing pipelines
- Robust error recovery, observability, and logging
- Clean, cross-platform interface

## Installation
```bash
git clone <repository-url>
cd {project_slug}
npm install # or pip install -r requirements.txt
```

## Usage
```bash
npm start # or python main.py
```

## License
MIT
"""


@_doc_template("video_script")
def _tpl_video(topic: str, stamp: str) -> str:
    clean = _clean_doc_topic(topic)
    title = clean.title() if clean.islower() else (clean[0].upper() + clean[1:] if clean else "Untitled")
    return f"""# Video Script / Outline — {title}

> {stamp} · drafted by DeskPet Jarvis

## Hook (0:00–0:15)
- Attention-grabbing opening highlighting the core premise of {title}.

## Intro (0:15–0:45)
- What viewers will learn and why {title} matters today.

## Main Points
1. Background and foundational concepts of {title}.
2. Step-by-step walkthrough of core mechanics.
3. Practical applications, best practices, and common pitfalls.

## Key Takeaways (last 30s)
- Summary of core insights.
- Call to action: subscribe, comment feedback, and review accompanying links.
"""


@_doc_template("changelog")
def _tpl_changelog(topic: str, stamp: str) -> str:
    clean = _clean_doc_topic(topic)
    title = clean.title() if clean.islower() else (clean[0].upper() + clean[1:] if clean else "Project")
    return f"""# Changelog — {title}

> {stamp} · drafted by DeskPet Jarvis

## [Unreleased]
### Added
- Core implementation and initial architecture for {title}
- Automated testing and verification suite

### Changed
- Refined configuration and performance tuning

### Fixed
- Resolved edge cases and stability issues
"""


@_doc_template("todo_list")
def _tpl_todo(topic: str, stamp: str) -> str:
    clean = _clean_doc_topic(topic)
    title = clean.title() if clean.islower() else (clean[0].upper() + clean[1:] if clean else "Priority Tasks")
    return f"""# To-Do List — {title}

> {stamp} · drafted by DeskPet Jarvis

## Priority
- [ ] Initial scoping and requirements definition for {title}
- [ ] Core milestone implementation and testing

## Later
- [ ] Extended optimization and edge-case validation
- [ ] Documentation and team review

## Done
- [x] Initialized task checklist for {title}
"""


@_doc_template("email")
def _tpl_email(topic: str, stamp: str) -> str:
    clean = _clean_doc_topic(topic)
    title = clean.title() if clean.islower() else (clean[0].upper() + clean[1:] if clean else "Update")
    return f"""# Email Draft — {title}

> {stamp} · drafted by DeskPet Jarvis

**Subject:** Update regarding {title}

Dear Team,

I hope this message finds you well. I am writing to share a brief update regarding {title}.

We have outlined the core objectives, deliverables, and next steps for this initiative. Please review the attached points at your earliest convenience and let me know if you have any questions or feedback.

Thank you for your ongoing collaboration and support.

Kind regards,
DeskPet Jarvis
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
    clean = _clean_doc_topic(topic)
    title, sentences = _fetch_topic_knowledge(clean)
    display_title = title or clean.title() or "Untitled Document"

    if sentences:
        summary = " ".join(sentences[:2])
        details = sentences[2:6] if len(sentences) > 2 else [f"Core background and significance recorded for {display_title}."]
        details_md = "\n".join(f"- {d}" for d in details)
        return f"""# {display_title}

> {stamp} · drafted by DeskPet Jarvis

## Overview
{summary}

## Key Facts & Background
{details_md}

## Reference Notes
- Concise factual summary sourced from verified reference archive.
- Retained in DeskPet local document library for offline access.

## Next Steps
- Review draft content and retain in DeskPet document archive.
"""

    return f"""# {display_title}

> {stamp} · drafted by DeskPet Jarvis

## Overview
Brief overview and reference notes regarding {display_title}.

## Key Points
- Core background, operational scope, and foundational context for {display_title}.
- Deliverables, dependencies, and reference notes.

## Next Steps
- Review draft content and retain in DeskPet document archive.
"""


_LAST_CREATED_DOC: Optional[str] = None


def get_document_location(filename: str = "") -> str:
    global _LAST_CREATED_DOC
    directory = str(docs_dir())
    if _LAST_CREATED_DOC and Path(_LAST_CREATED_DOC).exists():
        return (f"Your documents are stored in: {directory}\n"
                f"The latest generated document is: {_LAST_CREATED_DOC}")
    try:
        md_files = sorted(
            [p for p in docs_dir().glob("*.md") if p.name not in ("notes.md", "todo.md")],
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if md_files:
            return (f"Your documents are stored in: {directory}\n"
                    f"The latest generated document is: {md_files[0]}")
    except Exception:
        pass
    return f"Your documents are saved in: {directory}"


def open_document(target: str = "") -> str:
    global _LAST_CREATED_DOC
    directory = docs_dir()
    target_clean = (target or "").strip().lower()
    folder_label = "documents"

    # Known system folder recognition
    if any(w in target_clean for w in ("download", "downloads")):
        to_open = Path.home() / "Downloads"
        folder_label = "downloads"
    elif any(w in target_clean for w in ("desktop",)):
        to_open = Path.home() / "Desktop"
        folder_label = "desktop"
    elif any(w in target_clean for w in ("picture", "pictures", "photo", "photos")):
        to_open = Path.home() / "Pictures"
        folder_label = "pictures"
    elif any(w in target_clean for w in ("music", "songs")):
        to_open = Path.home() / "Music"
        folder_label = "music"
    elif any(w in target_clean for w in ("video", "videos", "movie", "movies")):
        to_open = Path.home() / "Videos"
        folder_label = "videos"
    elif any(w in target_clean for w in ("home", "user")):
        to_open = Path.home()
        folder_label = "home"
    elif any(w in target_clean for w in ("folder", "dir", "directory", "my documents", "documents folder")):
        to_open = directory
        folder_label = "documents"
    else:
        to_open = None
        clean_name = re.sub(r"^(?:open|view|show|the|this|that|my|a|an)\s+", "", target_clean).strip()
        clean_name = re.sub(r"^(?:document|doc|file|draft)\s*", "", clean_name).strip()
        if clean_name and clean_name not in ("it", "here", "location", "path", ""):
            slug = _slug(clean_name)
            for p in directory.glob("*.md"):
                if slug in p.name.lower() or clean_name in p.name.lower():
                    to_open = p
                    break
        if not to_open:
            if _LAST_CREATED_DOC and Path(_LAST_CREATED_DOC).exists():
                to_open = Path(_LAST_CREATED_DOC)
            else:
                try:
                    md_files = sorted(
                        [p for p in directory.glob("*.md") if p.name not in ("notes.md", "todo.md")],
                        key=lambda p: p.stat().st_mtime,
                        reverse=True,
                    )
                    if md_files:
                        to_open = md_files[0]
                except Exception:
                    pass
        if not to_open or not to_open.exists():
            to_open = directory
            folder_label = "documents"

    try:
        to_open.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass

    try:
        if platform.system() == "Windows":
            if to_open.is_dir():
                subprocess.Popen(["explorer.exe", os.path.normpath(str(to_open))])
            else:
                try:
                    os.startfile(str(to_open))
                except OSError:
                    subprocess.Popen(["notepad.exe", str(to_open)])
        elif platform.system() == "Darwin":
            subprocess.Popen(["open", str(to_open)])
        else:
            subprocess.Popen(["xdg-open", str(to_open)])
        if to_open.is_dir():
            return f"Opened {folder_label} folder at {to_open}, sir."
        return f"Opened {to_open.name}, sir."
    except Exception as exc:
        return f"Could not open {to_open}: {exc}"


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
    global _LAST_CREATED_DOC
    _LAST_CREATED_DOC = str(path)
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


def list_reminders() -> str:
    # ponytail: read existing persistent reminders file
    with _REMINDERS_LOCK:
        items = _load_reminders_unlocked()
    if not items:
        return "You have no active reminders scheduled, sir."
    now = time.time()
    lines = []
    for r in items:
        diff = max(0, int(r.get("fire_at", now) - now))
        mins, secs = divmod(diff, 60)
        t_str = f"{mins}m {secs}s" if mins else f"{secs}s"
        lines.append(f"• '{r.get('text', 'Reminder')}' (in ~{t_str})")
    return f"Active reminders ({len(items)}), sir:\n" + "\n".join(lines)


# ── Tool 7: todo capture (todo.md) ───────────────────────────────────────────


def _todo_file() -> Path:
    return docs_dir() / "todo.md"


_LAST_TODO_ITEM: Optional[str] = None
_ANAPHORIC_TASKS = {
    "it", "that", "this", "the task", "the item", "task", "item",
    "the last one", "last one", "the last task", "last task", "the last item", "last item",
}


def todo_add(text: str) -> str:
    global _LAST_TODO_ITEM
    text = (text or "").strip()
    if not text:
        return "Nothing to add — tell me the task text."
    f = _todo_file()
    line = f"- [ ] {text}  _(added {datetime.now():%Y-%m-%d %H:%M})_\n"
    if not f.exists():
        f.write_text("# DeskPet To-Do\n\n", encoding="utf-8")
    with f.open("a", encoding="utf-8") as fh:
        fh.write(line)
    _LAST_TODO_ITEM = text
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
    global _LAST_TODO_ITEM
    _LAST_TODO_ITEM = None
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
    global _LAST_TODO_ITEM
    item = (item or "").strip()
    if not item:
        return "Nothing to complete — tell me the task name."
    f = _todo_file()
    if not f.exists():
        return "Your to-do list is empty — nothing to mark as done."
    lines = f.read_text(encoding="utf-8").splitlines()

    item_norm = _norm_item(item)
    if item_norm in _ANAPHORIC_TASKS or item_norm == "it":
        if _LAST_TODO_ITEM:
            item = _LAST_TODO_ITEM
        else:
            open_tasks = [ln for ln in lines if ln.startswith("- [ ]")]
            if open_tasks:
                item = _item_body(open_tasks[-1])

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
    global _LAST_TODO_ITEM
    item = (item or "").strip()
    if not item:
        return "Nothing to remove — tell me the task name."
    f = _todo_file()
    if not f.exists():
        return "Your to-do list is empty — nothing to remove."
    lines = f.read_text(encoding="utf-8").splitlines()

    item_norm = _norm_item(item)
    if item_norm in _ANAPHORIC_TASKS or item_norm == "it":
        if _LAST_TODO_ITEM:
            item = _LAST_TODO_ITEM
        else:
            open_tasks = [ln for ln in lines if ln.startswith("- [ ]")]
            if open_tasks:
                item = _item_body(open_tasks[-1])

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
    if _LAST_TODO_ITEM and _norm_item(_LAST_TODO_ITEM) == needle:
        _LAST_TODO_ITEM = None
    f.write_text("\n".join(kept) + "\n", encoding="utf-8")
    return f"Removed from your to-do list: '{removed}'."


# ── Tool 8: system_status (psutil, offline) ──────────────────────────────────


def system_status() -> str:
    parts = []
    try:
        import psutil
        cpu = psutil.cpu_percent(interval=0.2)
        vm = psutil.virtual_memory()
        parts.append(f"CPU {cpu:.0f}%")
        parts.append(f"RAM {vm.percent:.0f}% used ({vm.used // (1024 ** 2)} MB of {vm.total // (1024 ** 2)} MB)")
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
    except Exception:
        # 100% Python standard library fallback (zero external dependencies)
        if platform.system() == "Windows":
            try:
                import ctypes
                class MEMORYSTATUSEX(ctypes.Structure):
                    _fields_ = [
                        ("dwLength", ctypes.c_ulong),
                        ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong),
                        ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong),
                        ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong),
                        ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                    ]
                stat = MEMORYSTATUSEX()
                stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
                if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
                    total_mb = stat.ullTotalPhys // (1024 ** 2)
                    used_mb = (stat.ullTotalPhys - stat.ullAvailPhys) // (1024 ** 2)
                    parts.append(f"RAM {stat.dwMemoryLoad}% used ({used_mb} MB of {total_mb} MB)")
                ms = ctypes.windll.kernel32.GetTickCount64()
                up = timedelta(seconds=int(ms // 1000))
                parts.append(f"uptime {up.days * 24 + up.seconds // 3600}h {(up.seconds // 60) % 60}m")
            except Exception:
                pass
        try:
            import shutil
            du = shutil.disk_usage(str(Path.home()))
            disk_pct = int((du.used / du.total) * 100) if du.total else 0
            parts.append(f"disk {disk_pct}% used")
        except Exception:
            pass
        cpus = os.cpu_count()
        if cpus:
            parts.insert(0, f"CPU ({cpus} cores)")
    online = check_internet_connection()
    parts.append("network " + ("online (connected)" if online else "offline (air-gapped)"))
    return "System status: " + "; ".join(parts) + "."


# ── Tool 8b: network_status (connectivity check, butler status) ──────────────


def network_status() -> str:
    """Explicitly check and report live network connectivity status."""
    online = check_internet_connection(force=True)
    if online:
        return "Network status: Online (Connected). Web search and live data feeds are fully operational, sir."
    return "Network status: Offline (Air-gapped mode). The system has no internet connectivity, sir."


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


def clipboard_write(text: str) -> str:
    # ponytail: native Windows clip.exe or xclip
    text = (text or "").strip()
    if not text:
        return "No text provided to copy, sir."
    try:
        if platform.system() == "Windows":
            subprocess.run(["clip"], input=text.encode("utf-16"), check=True, timeout=2)
        else:
            subprocess.run(["xclip", "-selection", "clipboard"], input=text.encode("utf-8"), check=True, timeout=2)
        return f"Copied '{text[:60]}' to your clipboard, sir."
    except Exception as exc:
        return f"Could not copy to clipboard: {exc}"


def find_file(query: str, folder: Optional[str] = None) -> str:
    # ponytail: stdlib os.walk over standard user folders, max depth 2
    q = (query or "").strip().lower()
    if not q:
        return "Please specify a file name or keyword to search for, sir."
    roots = []
    if folder and folder.lower() in ("downloads", "download"):
        roots = [Path.home() / "Downloads"]
    elif folder and folder.lower() in ("documents", "document", "docs"):
        roots = [Path.home() / "Documents" / "DeskPet", Path.home() / "Documents"]
    elif folder and folder.lower() in ("desktop",):
        roots = [Path.home() / "Desktop"]
    else:
        roots = [
            Path.home() / "Documents" / "DeskPet",
            Path.home() / "Documents",
            Path.home() / "Downloads",
            Path.home() / "Desktop",
        ]

    matches = []
    seen_paths = set()
    for root in roots:
        if not root.is_dir():
            continue
        try:
            for cur, dirs, files in os.walk(root):
                dirs[:] = [d for d in dirs if d not in {".git", "node_modules", "__pycache__", "AppData", "$RECYCLE.BIN"}]
                rel_parts = Path(cur).relative_to(root).parts
                if len(rel_parts) > 2:
                    del dirs[:]
                    continue
                for f in files:
                    if q in f.lower():
                        fp = Path(cur) / f
                        if str(fp) in seen_paths:
                            continue
                        seen_paths.add(str(fp))
                        try:
                            sz_kb = max(1, int(fp.stat().st_size / 1024))
                            matches.append(f"- {f} ({sz_kb} KB) - {fp}")
                        except Exception:
                            matches.append(f"- {f} - {fp}")
                        if len(matches) >= 5:
                            break
                if len(matches) >= 5:
                    break
        except Exception:
            continue
        if len(matches) >= 5:
            break

    if not matches:
        return f"I could not find any files matching '{query}' in your Documents, Downloads, or Desktop folders, sir."
    return f"Found {len(matches)} matching file(s), sir:\n" + "\n".join(matches)


def git_status(repo_dir: Optional[str] = None) -> str:
    # ponytail: native git CLI
    target = repo_dir
    if not target:
        # Check current directory and parents
        p = Path(os.getcwd()).resolve()
        while p != p.parent:
            if (p / ".git").is_dir():
                target = str(p)
                break
            p = p.parent
    if not target:
        # Check active window for open workspace or project
        try:
            from . import screen_context
            info = screen_context.get_active_window_info()
            if info and info.title:
                parts = info.title.split(" - ")
                if len(parts) >= 2:
                    proj = parts[-2].strip()
                    for base in [Path("H:/apps"), Path.home() / "Documents", Path.home() / "source", Path.home() / "repos", Path.home() / "Desktop"]:
                        cand = base / proj
                        if (cand / ".git").is_dir():
                            target = str(cand)
                            break
        except Exception:
            pass
    if not target:
        if Path("H:/apps/Deskpet/.git").is_dir():
            target = "H:/apps/Deskpet"
        else:
            target = os.getcwd()

    try:
        branch = subprocess.check_output(
            ["git", "branch", "--show-current"],
            cwd=target, text=True, timeout=3, stderr=subprocess.DEVNULL,
        ).strip()
        st = subprocess.check_output(
            ["git", "status", "--short"],
            cwd=target, text=True, timeout=3, stderr=subprocess.DEVNULL,
        ).strip()
        lines = [l for l in st.splitlines() if l.strip()]
        repo_name = Path(target).name
        if not lines:
            return f"In repository '{repo_name}' on branch '{branch}': Working tree clean, sir."
        summary = "\n".join(lines[:5]) + (f"\n...and {len(lines)-5} more" if len(lines) > 5 else "")
        return f"In repository '{repo_name}' on branch '{branch}', sir. {len(lines)} uncommitted change(s):\n{summary}"
    except Exception:
        return "I am not currently running inside a Git repository, sir."


def wifi_info() -> str:
    # ponytail: native netsh on Windows, socket for IP
    try:
        ip = socket.gethostbyname(socket.gethostname())
    except Exception:
        ip = "Unknown"
    if platform.system() != "Windows":
        return f"Local IP: {ip}, sir."
    ssid = "Ethernet / Disconnected"
    sig_info = ""
    try:
        out = subprocess.check_output(["netsh", "wlan", "show", "interfaces"], text=True, timeout=3)
        m_ssid = re.search(r"^\s*SSID\s*:\s*(.+)$", out, re.MULTILINE)
        if m_ssid:
            ssid = m_ssid.group(1).strip()
        m_sig = re.search(r"^\s*Signal\s*:\s*(.+)$", out, re.MULTILINE)
        if m_sig:
            sig_info = f" (Signal: {m_sig.group(1).strip()})"
    except Exception:
        pass
    return f"Connected to {ssid}{sig_info}. Local IP: {ip}, sir."


_HOLIDAYS = {
    "christmas": (12, 25),
    "new year": (1, 1),
    "new years": (1, 1),
    "new year's": (1, 1),
    "halloween": (10, 31),
    "valentine": (2, 14),
    "valentines": (2, 14),
    "valentine's": (2, 14),
}


def date_math(query: str) -> Optional[str]:
    # ponytail: stdlib datetime delta math
    q = (query or "").lower().strip()
    now = datetime.now()

    # "how many days until <holiday/date>"
    m_until = re.search(r"\b(?:how many days (?:until|to|till)|days (?:until|to|till))\s+([a-z0-9 '\-]+)", q)
    if m_until:
        target_name = m_until.group(1).strip().rstrip("?.!")
        for hol, (m, d) in _HOLIDAYS.items():
            if hol in target_name:
                target = datetime(now.year, m, d)
                if target.date() < now.date():
                    target = datetime(now.year + 1, m, d)
                diff = (target.date() - now.date()).days
                return f"There are {diff} day(s) until {target_name.title()} ({target.strftime('%B %d, %Y')}), sir."

    # "what date is in <N> days / weeks / months"
    # ponytail: stdlib datetime + simple regex handles all common date offset queries
    m_future = re.search(r"\b(?:what (?:is the )?date\s+(?:is\s+)?(?:in\s+)?|date in\s+)(\d+)\s+(day|days|week|weeks|month|months)\b", q)
    if m_future:
        num = int(m_future.group(1))
        unit = m_future.group(2)
        if "week" in unit:
            future = now + timedelta(weeks=num)
        elif "month" in unit:
            future = now + timedelta(days=num * 30)
        else:
            future = now + timedelta(days=num)
        return f"In {num} {unit}, the date will be {future.strftime('%A, %B %d, %Y')}, sir."

    return None


# ── Tool 10: calculator (safe AST evaluator, fully offline) ─────────────

_MATH_FUNCS = {
    "sqrt": _math.sqrt, "abs": abs, "round": round, "min": min, "max": max,
    "pow": pow, "sin": _math.sin, "cos": _math.cos, "tan": _math.tan,
    "log": _math.log, "log10": _math.log10, "floor": _math.floor, "ceil": _math.ceil,
}
_MATH_CONSTS = {"pi": _math.pi, "e": _math.e}


def _safe_eval(expr: str, raise_zero_div: bool = False) -> Optional[float]:
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
    except ZeroDivisionError:
        if raise_zero_div:
            raise
        return None
    except Exception:
        return None
    return None


def calculate(expr: str) -> str:
    expr = (expr or "").strip()
    if not expr:
        return "Nothing to calculate — give me an expression."
    expr_clean = expr.replace(",", "").replace("×", "*").replace("÷", "/")
    expr_clean = re.sub(r"\s*plus\s*", " + ", expr_clean, flags=re.IGNORECASE)
    expr_clean = re.sub(r"\s*minus\s*", " - ", expr_clean, flags=re.IGNORECASE)
    expr_clean = re.sub(r"\s*(?:times|multiplied\s+by|x)\s*", " * ", expr_clean, flags=re.IGNORECASE)
    expr_clean = re.sub(r"\s*(?:divided\s+by|over)\s*", " / ", expr_clean, flags=re.IGNORECASE)
    expr_clean = expr_clean.replace("^", "**")
    expr_clean = re.sub(r"\s+", "", expr_clean)
    try:
        val = _safe_eval(expr_clean, raise_zero_div=True)
    except ZeroDivisionError:
        return "Division by zero is undefined, sir."
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
    if not check_internet_connection():
        return "Cannot fetch webpage: network is offline (air-gapped mode)."
    url = (url or "").strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    try:
        r = httpx.get(
            url,
            timeout=10,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) DeskPet-Jarvis/1.0"},
        )
        r.raise_for_status()
    except Exception as exc:
        return f"Could not fetch {url}: {type(exc).__name__}."
    html = r.text or ""
    # Strip script, style, nav, footer, header, svg tags
    cleaned = re.sub(r"(?is)<(script|style|noscript|header|footer|nav|svg)[^>]*>.*?</\1>", " ", html)
    # Convert block/break elements into linebreaks
    cleaned = re.sub(r"(?i)<(?:p|br|div|h[1-6]|li|tr)[^>]*>", "\n", cleaned)
    # Strip remaining HTML tags
    text = re.sub(r"<[^>]+>", " ", cleaned)
    import html as _html
    text = _html.unescape(text)
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()]
    clean_text = "\n".join(line for line in lines if line)
    if not clean_text:
        return f"The page at {url} returned no readable text."
    excerpt = clean_text[:2500] + ("\n...(content truncated)" if len(clean_text) > 2500 else "")
    return (f"The page at {url} contains the following text. "
            f"Summarise or answer from it as the user asked.\n---\n"
            f"{excerpt}\n---")


fetch_url = fetch_page


# ── Tool 13: wikipedia summary ──────────────────────────────────────────


def wikipedia_summary(term: str) -> Optional[str]:
    if not check_internet_connection():
        return None
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
        if not query:
            if not default_memory_store._items:
                return "I have nothing saved in my memory yet, sir."
            recent = list(default_memory_store._items.values())[-10:]
            lines = [f"- {item.text}" for item in recent]
            return "From my memory:\n" + "\n".join(lines)

        matches = default_memory_store.search(query, limit=5)
        if matches:
            lines = [f"- {item.text}" for item, _ in matches]
            return "From my memory:\n" + "\n".join(lines)
        return f"I have no memory matching '{query}'."
    except Exception:
        pass

    f = _notes_file()
    if not f.exists():
        return f"I have no memory matching '{query}'." if query else "I have nothing saved in my memory yet, sir."
    lines = [ln for ln in f.read_text(encoding="utf-8").splitlines()
             if ln.startswith("- ")]
    if not lines:
        return f"I have no memory matching '{query}'." if query else "I have nothing saved in my memory yet, sir."
    if not query:
        return "My memory contains:\n" + "\n".join(lines[-10:])
    stop = {
        "what", "when", "where", "which", "who", "whom", "whose", "why", "how",
        "is", "are", "was", "were", "the", "a", "an", "and", "or", "in", "on", "at",
        "to", "for", "with", "about", "our", "your", "my", "tell", "show", "give",
        "does", "did", "can", "could", "would", "should", "from", "into", "any", "anything",
        "he", "she", "it", "they", "them", "him", "her", "his", "their", "me", "i", "we",
        "you", "say", "said", "note", "notes", "remember", "recall", "know",
    }
    words = [w for w in re.sub(r"[^a-z0-9 ]", " ", query).split() if len(w) > 2 and w not in stop]
    hits = [ln for ln in lines
            if (words and any(w in ln.lower() for w in words)) or (query and query in ln.lower())]
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


# ── Tool 15b: AI web chat automation (Gemini, Claude, ChatGPT, DeepSeek, Qwen) ──

_AI_DISPLAY_NAMES = {
    "chatgpt": "ChatGPT",
    "claude": "Claude",
    "gemini": "Gemini",
    "deepseek": "DeepSeek",
    "qwen": "Qwen",
    "perplexity": "Perplexity",
    "copilot": "Copilot",
    "grok": "Grok",
}


def _norm_ai_provider(name: str) -> str:
    n = re.sub(r"[^a-z0-9]", "", (name or "").lower())
    if "chatgpt" in n or "gpt" in n or "openai" in n:
        return "chatgpt"
    if "claude" in n or "anthropic" in n:
        return "claude"
    if "gemini" in n:
        return "gemini"
    if "deepseek" in n:
        return "deepseek"
    if "qwen" in n or "tongyi" in n:
        return "qwen"
    if "perplexity" in n:
        return "perplexity"
    if "copilot" in n or "bing" in n:
        return "copilot"
    if "grok" in n or "xai" in n:
        return "grok"
    return "chatgpt"


def rebuild_ai_prompt(raw_query: str) -> str:
    """Clean conversational wrappers, capitalize, and punctuate prompt for web models."""
    p = (raw_query or "").strip(" :,-")
    p = re.sub(r"^(?:(?:it|them|him|her)\s+)?to\s+", "", p, flags=re.IGNORECASE)
    p = re.sub(r"^(?:(?:can|could|would)\s+you\s+)?(?:please\s+)?", "", p, flags=re.IGNORECASE)
    p = re.sub(r"^(?:about|that|for)\s+", "", p, flags=re.IGNORECASE)
    p = p.strip(" :,-")
    if not p:
        return ""
    p = p[0].upper() + p[1:]
    is_q = re.match(
        r"^(?:what|why|how|when|where|who|which|whose|whom|is|are|can|could|would|will|do|does|did|should)\b",
        p,
        re.IGNORECASE,
    )
    if is_q:
        if not p.endswith(("?", "!", ".")):
            p += "?"
    elif not p.endswith((".", "?", "!", '"', "'")):
        p += "."
    return p


def _send_webchat_input(needs_paste: bool = True, delay: float = 2.5):
    """Simulate Windows keystrokes (Ctrl+V and/or Enter) after the browser window opens."""
    if platform.system() != "Windows":
        return
    import threading

    def _worker():
        time.sleep(delay)
        try:
            import ctypes
            u32 = ctypes.windll.user32
            VK_CONTROL = 0x11
            VK_V = 0x56
            VK_RETURN = 0x0D
            KEYEVENTF_KEYUP = 0x0002

            if needs_paste:
                # Ctrl+V
                u32.keybd_event(VK_CONTROL, 0, 0, 0)
                u32.keybd_event(VK_V, 0, 0, 0)
                time.sleep(0.06)
                u32.keybd_event(VK_V, 0, KEYEVENTF_KEYUP, 0)
                u32.keybd_event(VK_CONTROL, 0, KEYEVENTF_KEYUP, 0)
                time.sleep(0.18)

            # Enter
            u32.keybd_event(VK_RETURN, 0, 0, 0)
            time.sleep(0.06)
            u32.keybd_event(VK_RETURN, 0, KEYEVENTF_KEYUP, 0)
        except Exception as exc:
            try:
                print(f"[tools] webchat input simulation error: {exc}")
            except Exception:
                pass

    t = threading.Thread(target=_worker, daemon=True)
    t.start()


def open_ai_webchat(provider_raw: str, prompt_raw: str) -> str:
    """Rebuild prompt, copy to clipboard, launch browser, and submit via Enter."""
    provider = _norm_ai_provider(provider_raw)
    display = _AI_DISPLAY_NAMES.get(provider, provider.capitalize())
    prompt = rebuild_ai_prompt(prompt_raw) or (prompt_raw or "").strip()
    if not prompt:
        return f"No prompt provided to send to {display}, sir."

    # 1. Copy rebuilt prompt to clipboard as an immediate, foolproof safeguard
    clipboard_write(prompt)

    # 2. Formulate target URL
    import urllib.parse
    encoded_q = urllib.parse.quote_plus(prompt)

    if provider == "chatgpt":
        target_url = f"https://chatgpt.com/?q={encoded_q}"
        needs_paste = False
    elif provider == "claude":
        target_url = f"https://claude.ai/new?q={encoded_q}"
        needs_paste = False
    elif provider == "perplexity":
        target_url = f"https://www.perplexity.ai/search?q={encoded_q}"
        needs_paste = False
    elif provider == "copilot":
        target_url = f"https://copilot.microsoft.com/?q={encoded_q}"
        needs_paste = False
    elif provider == "gemini":
        target_url = "https://gemini.google.com/app"
        needs_paste = True
    elif provider == "deepseek":
        target_url = "https://chat.deepseek.com"
        needs_paste = True
    elif provider == "qwen":
        target_url = "https://chat.qwen.ai"
        needs_paste = True
    elif provider == "grok":
        target_url = "https://grok.com"
        needs_paste = True
    else:
        target_url = f"https://chatgpt.com/?q={encoded_q}"
        needs_paste = False

    # 3. Open in default browser
    open_url(target_url)

    # 4. Asynchronously send paste and enter keystrokes on Windows
    if provider != "perplexity":
        delay = 2.5 if needs_paste else 2.0
        _send_webchat_input(needs_paste=needs_paste, delay=delay)

    return f"Opened {display} with your prompt: '{prompt}'"


# ── Tool 16: volume & media keys (Windows SendKeys) ──────────────────

_MEDIA_KEYS = {
    "volume_up": "{VOLUME_UP}", "volume_down": "{VOLUME_DOWN}",
    "mute": "{VOLUME_MUTE}", "play_pause": "{MEDIA_PLAY_PAUSE}",
    "next": "{MEDIA_NEXT_TRACK}", "prev": "{MEDIA_PREV_TRACK}",
}


def media_control(action: str) -> str:
    act = (action or "").strip().lower()
    if platform.system() == "Windows":
        vk_map = {
            "mute": 0xAD,          # VK_VOLUME_MUTE
            "volume_down": 0xAE,   # VK_VOLUME_DOWN
            "volume_up": 0xAF,     # VK_VOLUME_UP
            "next": 0xB0,          # VK_MEDIA_NEXT_TRACK
            "prev": 0xB1,          # VK_MEDIA_PREV_TRACK
            "play_pause": 0xB3,    # VK_MEDIA_PLAY_PAUSE
        }
        vk = vk_map.get(act)
        if not vk:
            return "That media control is not recognised."
        try:
            import ctypes
            ctypes.windll.user32.keybd_event(vk, 0, 0, 0)
            ctypes.windll.user32.keybd_event(vk, 0, 2, 0)  # KEYEVENTF_KEYUP
            return f"sent: {act}"
        except Exception as exc:
            return f"Media control failed: {exc}"
    return "Media keys are only wired up on Windows for now."


def set_volume_percent(level: int) -> str:
    level = max(0, min(100, int(level)))
    if platform.system() == "Windows":
        try:
            import ctypes
            from ctypes import wintypes, Structure, c_float, c_void_p, POINTER, byref, HRESULT
            ole32 = ctypes.windll.ole32
            ole32.CoInitialize(None)

            class GUID(Structure):
                _fields_ = [('Data1', wintypes.DWORD), ('Data2', wintypes.WORD), ('Data3', wintypes.WORD), ('Data4', wintypes.BYTE * 8)]

            def _pg(guid_str):
                g = GUID()
                ole32.CLSIDFromString(guid_str, byref(g))
                return g

            CLSID_MMDeviceEnumerator = _pg('{BCDE0395-E52F-467C-8E3D-C4579291692E}')
            IID_IMMDeviceEnumerator = _pg('{A95664D2-9614-4F35-A746-DE8DB63617E6}')
            IID_IAudioEndpointVolume = _pg('{5CDF2C82-841E-4546-9722-0CF74078229A}')

            class IMMDeviceEnumeratorVtbl(Structure):
                _fields_ = [
                    ('QueryInterface', c_void_p), ('AddRef', c_void_p), ('Release', c_void_p), ('EnumAudioEndpoints', c_void_p),
                    ('GetDefaultAudioEndpoint', ctypes.WINFUNCTYPE(HRESULT, c_void_p, wintypes.DWORD, wintypes.DWORD, POINTER(c_void_p))),
                    ('GetDevice', c_void_p), ('RegisterEndpointNotificationCallback', c_void_p), ('UnregisterEndpointNotificationCallback', c_void_p)
                ]
            class IMMDeviceEnumerator(Structure):
                _fields_ = [('lpVtbl', POINTER(IMMDeviceEnumeratorVtbl))]

            class IMMDeviceVtbl(Structure):
                _fields_ = [
                    ('QueryInterface', c_void_p), ('AddRef', c_void_p), ('Release', c_void_p),
                    ('Activate', ctypes.WINFUNCTYPE(HRESULT, c_void_p, POINTER(GUID), wintypes.DWORD, c_void_p, POINTER(c_void_p))),
                ]
            class IMMDevice(Structure):
                _fields_ = [('lpVtbl', POINTER(IMMDeviceVtbl))]

            class IAudioEndpointVolumeVtbl(Structure):
                _fields_ = [
                    ('QueryInterface', c_void_p), ('AddRef', c_void_p), ('Release', c_void_p),
                    ('RegisterControlChangeNotify', c_void_p), ('UnregisterControlChangeNotify', c_void_p),
                    ('GetChannelCount', c_void_p), ('SetMasterVolumeLevel', c_void_p),
                    ('SetMasterVolumeLevelScalar', ctypes.WINFUNCTYPE(HRESULT, c_void_p, c_float, c_void_p)),
                    ('GetMasterVolumeLevel', c_void_p),
                    ('GetMasterVolumeLevelScalar', ctypes.WINFUNCTYPE(HRESULT, c_void_p, POINTER(c_float))),
                    ('SetMute', ctypes.WINFUNCTYPE(HRESULT, c_void_p, wintypes.BOOL, c_void_p)),
                    ('GetMute', ctypes.WINFUNCTYPE(HRESULT, c_void_p, POINTER(wintypes.BOOL))),
                ]
            class IAudioEndpointVolume(Structure):
                _fields_ = [('lpVtbl', POINTER(IAudioEndpointVolumeVtbl))]

            device_enumerator = c_void_p()
            hr_enum = ole32.CoCreateInstance(byref(CLSID_MMDeviceEnumerator), None, 1, byref(IID_IMMDeviceEnumerator), byref(device_enumerator))
            if hr_enum != 0 or not device_enumerator.value:
                return f"Failed to initialize audio endpoint enumerator: hr={hr_enum}"
            enum = ctypes.cast(device_enumerator, POINTER(IMMDeviceEnumerator))
            updated = False
            for role in (0, 1):
                try:
                    default_device = c_void_p()
                    hr_dev = enum.contents.lpVtbl.contents.GetDefaultAudioEndpoint(device_enumerator, 0, role, byref(default_device))
                    if hr_dev == 0 and default_device.value:
                        dev = ctypes.cast(default_device, POINTER(IMMDevice))
                        endpoint_volume = c_void_p()
                        hr_act = dev.contents.lpVtbl.contents.Activate(default_device, byref(IID_IAudioEndpointVolume), 1, None, byref(endpoint_volume))
                        if hr_act == 0 and endpoint_volume.value:
                            vol = ctypes.cast(endpoint_volume, POINTER(IAudioEndpointVolume))
                            hr_set = vol.contents.lpVtbl.contents.SetMasterVolumeLevelScalar(endpoint_volume, c_float(level / 100.0), None)
                            if hr_set == 0:
                                updated = True
                except Exception:
                    pass
            if updated:
                return f"volume set to {level}%"
            return "Failed to set volume: no default audio endpoint found."
        except Exception as exc:
            return f"Failed to set volume: {exc}"
    elif platform.system() == "Darwin":
        subprocess.run(["osascript", "-e", f"set volume output volume {level}"], check=False)
        return f"volume set to {level}%"
    else:
        subprocess.run(["amixer", "-D", "pulse", "sset", "Master", f"{level}%"], check=False)
        return f"volume set to {level}%"


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
        try:
            import ctypes
            res = ctypes.windll.user32.LockWorkStation()
            if res != 0:
                return "locked: workstation"
        except Exception:
            pass
        subprocess.run(["rundll32.exe", "user32.dll,LockWorkStation"], timeout=5)
        return "locked: workstation"
    elif platform.system() == "Darwin":
        subprocess.Popen(["pmset", "displaysleepnow"])
        return "locked: workstation"
    else:
        subprocess.Popen(["xdg-screensaver", "lock"])
        return "locked: workstation"


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
    r"\b(?:show|list|read|view|check|display|get)\b.*\b(?:(?:my|the)\s+)?(?:to[- ]?do|to[- ]?dos|tasks?)\b|"
    r"^\s*(?:what(?:'s| is)\s+(?:on\s+)?)?(?:(?:my|the)\s+)?(?:to[- ]?do\s+list|todos|to-dos|tasks)\s*\??$",
    re.IGNORECASE,
)
_TODO_WORDS = r"(?:to[- ]?do\s+list|to[- ]?dos|to[- ]?do|todos|todo|task\s*list|tasks)"
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
    # "remove buy milk from my todo" | "delete buy milk from the to do list" | "remove it from the to do list"
    r"\b(?:remove|delete|drop)\s+(.+?)\s+from\s+(?:(?:my|the|this)\s+)?" + _TODO_WORDS + r"\b",
    re.IGNORECASE,
)
_RE_TODO_ADD = re.compile(
    # "add to my todo: buy milk" | "add buy milk to my todo"
    r"\badd\s+(?:to\s+)?(?:(?:my|the)\s+)?(?:to[- ]?do|todos|task\s*list)\b[:\s]*(.+)"
    r"|\badd\s+(.+?)\s+(?:to|on)\s+(?:(?:my|the)\s+)?(?:to[- ]?do|todos|task\s*list|tasks)\b",
    re.IGNORECASE,
)
_RE_DOC_LOCATION = re.compile(
    r"\b(?:"
    r"where\s+(?:is|are|was)\s+(?:the|my|this|that|a)?\s*(?:documents?|docs?|files?|notes?)(?:\s+(?:saved|stored|located|location))?|"
    r"(?:what\s+is|tell\s+me|show\s+me|check)?\s*(?:the|my)?\s*(?:documents?|docs?|files?)\s+(?:location|path|directory|folder)|"
    r"where\s+did\s+you\s+(?:save|put|store)\s+(?:the|it|that|this|my)(?:\s+(?:document|doc|file))?|"
    r"where\s+(?:was|is)\s+it\s+saved|"
    r"document\s+location|"
    r"documents?\s+(?:directory|folder|path)|"
    r"where\s+are\s+(?:my\s+)?documents"
    r")\b",
    re.IGNORECASE,
)
_RE_OPEN_DOC = re.compile(
    r"\b(?:"
    r"(?:open|view|show)(?:\s+me)?\s+(?:the\s+|this\s+|that\s+|my\s+)?(?:created\s+|drafted\s+|recent\s+|last\s+)?(?:documents?|docs?|files?|drafts?|notes?)\b(?:\s+(?:folder|directory|\S+.*))?|"
    r"(?:open|view|show|browse)\s+(?:the\s+|my\s+)?(?:downloads?|desktop|pictures?|photos?|music|videos?|documents?|docs?|home)?\s*(?:folder|dir|directory)\b|"
    r"open\s+(?:the\s+|my\s+)?(?:downloads?|desktop|pictures?|documents)\b|"
    r"open\s+it\b"
    r")",
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
    r"|\btime\s+(?:in|at|for)\s+([A-Za-z .'-]{2,25})"
    r"|\bclock\b"
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
_RE_NET_STATUS = re.compile(
    r"\b(?:"
    r"(?:check\s+(?:the\s+)?)?(?:internet|network|connection)\s+status|"
    r"(?:are\s+you|are\s+we)\s+(?:connected\s+to\s+(?:the\s+)?internet|online|offline)|"
    r"is\s+(?:the\s+)?(?:internet|network|connection)\s+(?:working|connected|active|online|up)|"
    r"do\s+you\s+have\s+(?:an?\s+)?internet\s+connection|"
    r"check\s+(?:the\s+)?(?:internet|network|connection)"
    r")\b",
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
    r"(\(?\s*\d[\d,]*(?:\.\d+)?(?:\s*[\)\(]*\s*(?:[-+*/×÷^]|\*\*|plus|minus|times|"
    r"multiplied\s+by|divided\s+by|over|x)\s*[\)\(]*\s*\d[\d,]*(?:\.\d+)?[\)\(]*)+)",
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
_SITE_URLS = {
    # Music & Audio
    "spotify": "https://open.spotify.com",
    "spotify web": "https://open.spotify.com",
    "youtube music": "https://music.youtube.com",
    "yt music": "https://music.youtube.com",
    "ytmusic": "https://music.youtube.com",
    "soundcloud": "https://soundcloud.com",

    # Video & Streaming
    "youtube": "https://www.youtube.com",
    "yt": "https://www.youtube.com",
    "netflix": "https://www.netflix.com",
    "twitch": "https://www.twitch.tv",

    # Social & Community
    "reddit": "https://www.reddit.com",
    "twitter": "https://twitter.com",
    "x": "https://x.com",
    "facebook": "https://www.facebook.com",
    "fb": "https://www.facebook.com",
    "instagram": "https://www.instagram.com",
    "ig": "https://www.instagram.com",
    "linkedin": "https://www.linkedin.com",
    "pinterest": "https://www.pinterest.com",
    "tiktok": "https://www.tiktok.com",

    # Search & Knowledge
    "google": "https://www.google.com",
    "google maps": "https://maps.google.com",
    "maps": "https://maps.google.com",
    "wikipedia": "https://www.wikipedia.org",
    "wiki": "https://www.wikipedia.org",
    "github": "https://github.com",
    "stackoverflow": "https://stackoverflow.com",
    "stack overflow": "https://stackoverflow.com",
    "duckduckgo": "https://duckduckgo.com",
    "ddg": "https://duckduckgo.com",
    "bing": "https://www.bing.com",

    # Shopping & Entertainment
    "amazon": "https://www.amazon.com",
    "ebay": "https://www.ebay.com",
    "imdb": "https://www.imdb.com",

    # AI Web Chats (direct open)
    "chatgpt": "https://chatgpt.com",
    "chat gpt": "https://chatgpt.com",
    "claude": "https://claude.ai",
    "claude ai": "https://claude.ai",
    "gemini": "https://gemini.google.com/app",
    "google gemini": "https://gemini.google.com/app",
    "deepseek": "https://chat.deepseek.com",
    "deep seek": "https://chat.deepseek.com",
    "qwen": "https://chat.qwen.ai",
    "tongyi": "https://chat.qwen.ai",
    "perplexity": "https://www.perplexity.ai",
    "perplexity ai": "https://www.perplexity.ai",
    "copilot": "https://copilot.microsoft.com",
    "grok": "https://grok.com",
}

_SITE_SEARCH_URLS = {
    # Music & Audio
    "spotify": "https://open.spotify.com/search/{q}",
    "spotify web": "https://open.spotify.com/search/{q}",
    "youtube music": "https://music.youtube.com/search?q={q}",
    "yt music": "https://music.youtube.com/search?q={q}",
    "ytmusic": "https://music.youtube.com/search?q={q}",
    "soundcloud": "https://soundcloud.com/search?q={q}",

    # Video & Streaming
    "youtube": "https://www.youtube.com/results?search_query={q}",
    "yt": "https://www.youtube.com/results?search_query={q}",
    "netflix": "https://www.netflix.com/search?q={q}",
    "twitch": "https://www.twitch.tv/search?term={q}",

    # Social & Community
    "reddit": "https://www.reddit.com/search/?q={q}",
    "twitter": "https://twitter.com/search?q={q}",
    "x": "https://x.com/search?q={q}",
    "facebook": "https://www.facebook.com/search/top/?q={q}",
    "fb": "https://www.facebook.com/search/top/?q={q}",
    "instagram": "https://www.instagram.com/explore/tags/{q}/",
    "ig": "https://www.instagram.com/explore/tags/{q}/",
    "linkedin": "https://www.linkedin.com/search/results/all/?keywords={q}",
    "pinterest": "https://www.pinterest.com/search/pins/?q={q}",
    "tiktok": "https://www.tiktok.com/search?q={q}",

    # Search & Knowledge
    "google": "https://www.google.com/search?q={q}",
    "google maps": "https://www.google.com/maps/search/{q}",
    "maps": "https://www.google.com/maps/search/{q}",
    "wikipedia": "https://en.wikipedia.org/w/index.php?search={q}",
    "wiki": "https://en.wikipedia.org/w/index.php?search={q}",
    "github": "https://github.com/search?q={q}",
    "stackoverflow": "https://stackoverflow.com/search?q={q}",
    "stack overflow": "https://stackoverflow.com/search?q={q}",
    "duckduckgo": "https://duckduckgo.com/?q={q}",
    "ddg": "https://duckduckgo.com/?q={q}",
    "bing": "https://www.bing.com/search?q={q}",

    # Shopping & Entertainment
    "amazon": "https://www.amazon.com/s?k={q}",
    "ebay": "https://www.ebay.com/sch/i.html?_nkw={q}",
    "imdb": "https://www.imdb.com/find/?q={q}",

    # AI Search
    "perplexity": "https://www.perplexity.ai/search?q={q}",
    "perplexity ai": "https://www.perplexity.ai/search?q={q}",
}


def _norm_site_name(name: str) -> str:
    n = re.sub(r"\s+", " ", (name or "").strip().lower())
    alias_map = {
        "yt": "youtube",
        "yt music": "youtube music",
        "ytmusic": "youtube music",
        "fb": "facebook",
        "ig": "instagram",
        "ddg": "duckduckgo",
        "wiki": "wikipedia",
        "spotify web": "spotify",
        "google map": "google maps",
        "maps": "google maps",
        "stack overflow": "stackoverflow",
        "claude ai": "claude",
        "google gemini": "gemini",
        "deep seek": "deepseek",
        "qwen ai": "qwen",
        "tongyi": "qwen",
        "perplexity ai": "perplexity",
    }
    return alias_map.get(n, n)


_SITE_NAMES_RE = (
    r"youtube\s+music|yt\s*music|spotify\s+web|google\s+maps|stack\s*overflow|"
    r"youtube|spotify|reddit|twitter|facebook|instagram|linkedin|pinterest|twitch|soundcloud|"
    r"netflix|github|wikipedia|amazon|ebay|imdb|google|duckduckgo|bing|tiktok|maps|wiki|ddg|fb|ig|yt|x"
)

_RE_SITE_SEARCH = re.compile(
    rf"\b(?:"
    rf"open\s+({_SITE_NAMES_RE})\s+and\s+search\s+(?:for\s+)?(.+)|"
    rf"search\s+(?:on\s+)?({_SITE_NAMES_RE})\s+for\s+(.+)|"
    rf"search\s+for\s+(.+?)\s+on\s+({_SITE_NAMES_RE})|"
    rf"search\s+(.+?)\s+on\s+({_SITE_NAMES_RE})|"
    rf"look\s+up\s+(.+?)\s+on\s+({_SITE_NAMES_RE})|"
    rf"find\s+(.+?)\s+on\s+({_SITE_NAMES_RE})"
    rf")\b",
    re.IGNORECASE,
)

_RE_SITE_PLAY = re.compile(
    r"\bplay\s+(.+?)\s+on\s+(spotify(?:\s+web)?|youtube\s+music|yt\s*music|youtube|yt|soundcloud)\b",
    re.IGNORECASE,
)

_OPEN_SITE_NAMES_RE = (
    _SITE_NAMES_RE +
    r"|chatgpt|chat\s+gpt|claude(?:\s+ai)?|gemini|google\s+gemini|deepseek|deep\s+seek|qwen(?:\s+ai)?|tongyi|perplexity(?:\s+ai)?|copilot|grok"
)

_RE_OPEN_SITE = re.compile(
    rf"\b(?:open|launch|go\s+to|visit)\s+({_OPEN_SITE_NAMES_RE})\b",
    re.IGNORECASE,
)

_AI_CHAT_NAMES_RE = (
    r"chatgpt|chat\s+gpt|claude(?:\s+ai)?|gemini|google\s+gemini|deepseek|deep\s+seek|"
    r"qwen(?:\s+ai)?|tongyi|perplexity(?:\s+ai)?|copilot|grok"
)

_RE_AI_CHAT_ASK = re.compile(
    rf"\b(?:"
    rf"open\s+({_AI_CHAT_NAMES_RE})\s+and\s+ask\s+(?:it\s+)?(?:to\s+|for\s+)?(.+)|"
    rf"ask\s+({_AI_CHAT_NAMES_RE})\s+(?:to\s+|for\s+|about\s+|that\s+)?(.+)|"
    rf"query\s+({_AI_CHAT_NAMES_RE})\s+(?:for\s+|about\s+)?(.+)|"
    rf"send\s+(?:a\s+)?(?:prompt\s+)?(?:to\s+)({_AI_CHAT_NAMES_RE})\s*[:,-]?\s*(.+)"
    rf")\b",
    re.IGNORECASE,
)

_RE_URL = re.compile(
    r"\b((?:https?://|www\.)[^\s]+)\b|"
    r"\b(?:open|go\s+to|visit|browse\s+to)\s+([a-z0-9][a-z0-9-]*(?:\.[a-z0-9-]+)+)\b",
    re.IGNORECASE)
_RE_WIKI = re.compile(r"\bwiki(?:pedia)?\b\s*(?:about|for|on)?\s*(.+)", re.IGNORECASE)
_RE_WHOIS = re.compile(r"^(?:who|what)\s+(?:is|was|are)\s+(.+?)[?.!]*$", re.IGNORECASE)
_RE_SET_VOLUME = re.compile(
    r"\b(?:set|turn|change|put|adjust)\s+(?:the\s+)?(?:sound\s+|audio\s+)?(?:volume|sound|audio)\s+(?:(?:up|down)\s+)?(?:to|at|on)?\s*(\d{1,3})\s*%?|"
    r"\b(?:volume|sound|audio)\s+(?:to|at|on|=)?\s*(\d{1,3})\s*%?|"
    r"\b(?:turn\s+(?:it\s+)?(?:up|down)\s+to)\s*(\d{1,3})\s*%?",
    re.IGNORECASE,
)
_RE_VOLUME = re.compile(
    r"\b(?:volume\s+(?:up|down)|turn\s+(?:the\s+volume|the|it)?\s*(?:up|down)|"
    r"mute|unmute|louder|quieter|sound\s+(?:up|down))\b", re.IGNORECASE)
_RE_MEDIA = re.compile(
    r"\b(?:play|pause|resume)\b(?:\s+(?:the\s+)?(?:music|song|track|audio|video))?|"
    r"\b(?:next|previous|prev|skip)\s+(?:song|track)\b", re.IGNORECASE)
_RE_SHOT = re.compile(
    r"\b(?:take|grab|capture|snap)\s+(?:a\s+)?(?:screen\s*shot|screenshot|screen\s+capture)\b|"
    r"\bscreenshot\b", re.IGNORECASE)
_RE_LOCK = re.compile(
    r"\b(?:lock\s+(?:(?:down|the|my)\s+)*(?:sys(?:tem)?|screen|pc|computer|workstation|machine|laptop|desktop)|lock\s+down)\b",
    re.IGNORECASE,
)
_RE_REMEMBER = re.compile(
    r"\b(?:remember|note\s+down|keep\s+in\s+mind)\s+(?:that\s+)?(.+)", re.IGNORECASE)
_RE_SENSITIVE_SECRET = re.compile(
    r"\b(?:password|passcode|pin|api\s*key|secret\s*key|private\s*key|auth\s*token)\b",
    re.IGNORECASE,
)
_RE_LIST_REMINDERS = re.compile(
    r"\b(?:what\s+(?:are\s+)?(?:all\s+|my\s+|the\s+)?reminders?|"
    r"list\s+(?:all\s+|my\s+|the\s+)*reminders?|"
    r"show\s+(?:all\s+|my\s+|the\s+)*reminders?|"
    r"check\s+(?:all\s+|my\s+|the\s+)*reminders?|"
    r"view\s+(?:all\s+|my\s+|the\s+)*reminders?|"
    r"active\s+reminders?|any\s+reminders?)\b",
    re.IGNORECASE,
)
_RE_COPY_CLIPBOARD = re.compile(
    r"\bcopy\s+(.+?)\s+(?:to|into|in|onto)\s+(?:my\s+|the\s+)?clipboard\b|"
    r"\b(?:copy\s+(?:to|into|in|onto)\s+(?:the\s+|my\s+)?clipboard|put\s+(?:on|in|into)\s+(?:the\s+|my\s+)?clipboard)\b",
    re.IGNORECASE,
)
_RE_FIND_FILE = re.compile(
    r"\b(?:find|locate|search\s+for|look\s+for)\s+(?:all\s+)?(?:the\s+|my\s+)?(?:files?|documents?)\s+(?:called|named|with|matching|for)\s+(.+?)[?.!]*$|"
    r"\b(?:where\s+is|where\s+are)\s+(?:the\s+|my\s+)?(?:files?|documents?)\s+(?:called|named|with|matching|for)\s+(.+?)[?.!]*$|"
    r"\bfind\s+(?:my\s+|the\s+)?file\s+(.+?)[?.!]*$|"
    r"\b(?:find|locate|search\s+for|look\s+for)\s+(?:my\s+|the\s+)?(.+?)\s+file[s]?[?.!]*$|"
    r"\b(?:find|locate|where\s+is|where\s+are)\s+(?:the\s+|my\s+)?([a-z0-9_\-.]+\.[a-z0-9]{2,5})\b",
    re.IGNORECASE,
)
_RE_GIT_STATUS = re.compile(
    r"\b(?:git\s+status|what\s+git\s+branch|what\s+branch\s+am\s+i\s+on|git\s+branch|git\s+changes|git\s+diff\s+summary)\b",
    re.IGNORECASE,
)
_RE_WIFI_INFO = re.compile(
    r"\b(?:what\s+wi-?fi|wi-?fi\s+(?:info|status|network|details)|what\s+is\s+my\s+local\s+ip|my\s+ip\s+address|network\s+ip)\b",
    re.IGNORECASE,
)
_RE_DATE_MATH = re.compile(
    r"\b(?:how\s+many\s+days\s+(?:until|to|till)|days\s+(?:until|to|till)|what\s+date\s+is\s+(?:it\s+)?in\s+\d+\s+(?:days?|weeks?|months?))\b",
    re.IGNORECASE,
)
# Declarative personal facts worth storing even without "remember": a
# possessive + stable-attribute noun + "is" + value. Question words and
# verbs of state disqualify (that's chitchat, not a fact to store).
_RE_PERSONAL_FACT = re.compile(
    r"^\s*(?:hey\s+\w+,?\s*)?(?:please\s+)?my\s+"
    r"(codename|code\s*name|callsign|call\s*sign|nickname|name|"
    r"birthday|anniversary|email(?:\s*address)?|phone(?:\s*number)?|"
    r"address|timezone|favourite|favorite)\s+(?:is|=)\s+(.{2,80}?)[.!?]*\s*$",
    re.IGNORECASE,
)
_RE_MY_ATTR_Q = re.compile(
    r"^\s*what(?:'s| is|\s+was)\s+(?:my|our)\s+"
    r"(?:codename|code\s*name|callsign|call\s*sign|nickname|name|"
    r"birthday|anniversary|email(?:\s*address)?|phone(?:\s*number)?|"
    r"address|timezone|favourite|favorite)\b[?.!]*\s*$",
    re.IGNORECASE,
)
_RE_RESEARCH_PDF = re.compile(
    r"\b(?:generate|create|research|compile|make|build)\s+(?:an?\s+)?(?:executive\s+|research\s+)?pdf\s+(?:briefing\s+|report\s+)?(?:on|about|for)\s+(.+)",
    re.IGNORECASE,
)
_RE_SPEAK = re.compile(
    r"^\s*(?:speak|say\s+aloud|say|vocalize|voice)\s+[\"']?(.+?)[\"']?\s*$",
    re.IGNORECASE,
)
_RE_RECALL = re.compile(
    r"\b(?:"
    r"what\s+do\s+you\s+(?:remember|know)|"
    r"do\s+you\s+(?:remember|know)|"
    r"what\s+did\s+i\s+(?:say|tell\s+you|mention|note|write)|"
    r"did\s+i\s+(?:say|tell\s+you|mention)\s+anything|"
    r"recall|"
    r"remind\s+me\s+what|"
    r"(?:search|check)\s+(?:my\s+)?(?:notes|memory)\s+for"
    r")\b\s*(?:about\s+)?(.*)",
    re.IGNORECASE,
)
_RE_DESTRUCTIVE = re.compile(
    r"\b(?:delete|remove|erase|format|wipe|destroy|nuke)\b.*"
    r"\b(?:system\s*32|windows\s+folder|hard\s*drive|ssd|disk|drive|"
    r"all\s+(?:my\s+)?files|all\s+(?:my\s+)?data|my\s+(?:files|documents|photos|pics)|"
    r"everything|c:\\|d:\\)\b|\brm\s+-rf\b|\bdel\s+/[sqf]\b|"
    r"\bformat\s+(?:my\s+)?(?:pc|computer|disk|drive)\b",
    re.IGNORECASE)

_RE_ACTIVE_WIN = re.compile(
    r"\b(?:"
    r"(?:what|which)\s+(?:app|application|window|file|program)\s+(?:am\s+i\s+(?:on|in|using|editing|looking\s+at)|is\s+(?:currently\s+)?(?:open|active|in\s+use|focused|running))(?:\s+(?:right\s+now|now|currently|at\s+the\s+moment))?|"
    r"(?:what|which)\s+(?:app|application|window|file|program)\s+am\s+i\s+(?:on|in|using|editing|looking\s+at)?(?:\s+(?:right\s+now|now|currently|at\s+the\s+moment))|"
    r"(?:what|which)\s+(?:am\s+i|are\s+we)\s+(?:using|on|in|editing|looking\s+at)(?:\s+(?:right\s+now|now|currently|at\s+the\s+moment))?|"
    r"what(?:'s| is)\s+(?:my\s+|the\s+)?(?:active|current|foreground)\s+(?:window|app|application|program|file)|"
    r"active\s+window|current\s+window|active\s+app|current\s+app|foreground\s+window|foreground\s+app"
    r")\b",
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
    fn_m = _RE_MATHFN.search(text)
    if fn_m:
        fn_val = _math.sqrt(float(fn_m.group(1)))
        # Check compound arithmetic: e.g. "what is sqrt(144) plus 50" -> "what is 12 plus 50"
        replaced = text[:fn_m.start()] + f" {fn_val:g} " + text[fn_m.end():]
        m_arith = _RE_ARITH.search(replaced)
        if m_arith:
            expr = m_arith.group(1)
            if re.search(r"[-+*/^÷×]", expr) or _RE_CALC_HINT.search(text):
                expr_clean = expr.replace(",", "").replace("×", "*").replace("÷", "/")
                expr_clean = re.sub(r"\s*plus\s*", " + ", expr_clean, flags=re.IGNORECASE)
                expr_clean = re.sub(r"\s*minus\s*", " - ", expr_clean, flags=re.IGNORECASE)
                expr_clean = re.sub(r"\s*(?:times|multiplied\s+by|x)\s*", " * ", expr_clean, flags=re.IGNORECASE)
                expr_clean = re.sub(r"\s*(?:divided\s+by|over)\s*", " / ", expr_clean, flags=re.IGNORECASE)
                expr_clean = expr_clean.replace("^", "**")
                expr_clean = re.sub(r"\s+", "", expr_clean)
                try:
                    val = _safe_eval(expr_clean, raise_zero_div=True)
                except ZeroDivisionError:
                    return f"{expr_clean}: division by zero is undefined"
                if val is not None:
                    return f"{expr_clean} = {val:g}"
        return f"sqrt({fn_m.group(1)}) = {fn_val:g}"
    m = _RE_ARITH.search(text)
    if not m:
        return None
    expr = m.group(1)
    # Word-form operators need an explicit question hint so that ordinary
    # sentences containing 'x' or 'plus' never reach the evaluator.
    if not re.search(r"[-+*/^÷×]|divided\s+by|multiplied\s+by", expr, re.I) and not _RE_CALC_HINT.search(text):
        return None
    expr = expr.replace(",", "").replace("×", "*").replace("÷", "/")
    expr = re.sub(r"\s*plus\s*", " + ", expr, flags=re.IGNORECASE)
    expr = re.sub(r"\s*minus\s*", " - ", expr, flags=re.IGNORECASE)
    expr = re.sub(r"\s*(?:times|multiplied\s+by|x)\s*", " * ", expr, flags=re.IGNORECASE)
    expr = re.sub(r"\s*(?:divided\s+by|over)\s*", " / ", expr, flags=re.IGNORECASE)
    expr = expr.replace("^", "**")
    expr = re.sub(r"\s+", "", expr)
    try:
        val = _safe_eval(expr, raise_zero_div=True)
    except ZeroDivisionError:
        return f"{expr}: division by zero is undefined"
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
    r"^\s*(?:yes|yeah|yep|yup|ok|okay|sure|proceed|go ahead|go on|do it|do that|"
    r"confirm|please do|please|yes please|affirmative|sounds good|draft it|create it|y)\b",
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
    r"sorry|please|yes|no|ok(?:ay)?|jarvis|i|you|we|my|your|me|us|yourself|"
    r"introduce|intro|status|diagnostic|diagnostics|system|systems|"
    r"this|that|it|they|he|she|them|his|her|their|"
    r"write|draft|create|read|delete|remove|clear|add|mark|done|"
    r"summar\w+|translate|rewrite|convert|calculat\w+|comput\w+|"
    r"search|find|check|remember|recall|note|take|capture|lock|"
    r"mute|unmute|increase|decrease|turn|send|schedule|list|"
    r"documents?|docs?|files?|folders?|location|directory|saved|path)\b",
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


def _check_local_memory(text: str) -> Optional[str]:
    """Check if the question matches any fact stored in notes.md."""
    # Never hijack pronoun follow-ups or referential questions
    if re.search(r"\b(?:he|she|him|her|they|them|his|hers)\b", text, re.I):
        return None
    f = _notes_file()
    if not f.exists():
        return None
    try:
        content = f.read_text(encoding="utf-8")
    except Exception:
        return None
    lines = [ln for ln in content.splitlines() if ln.startswith("- ")]
    if not lines:
        return None

    stop = {
        "what", "when", "where", "which", "who", "whom", "whose", "why", "how",
        "is", "are", "was", "were", "the", "a", "an", "and", "or", "in", "on", "at",
        "to", "for", "with", "about", "our", "your", "my", "tell", "show", "give",
        "does", "did", "can", "could", "would", "should", "from", "into",
        "he", "she", "it", "they", "them", "him", "her", "his", "their",
    }
    words = [w for w in re.sub(r"[^a-z0-9 ]", " ", text.lower()).split()
             if len(w) > 2 and w not in stop]
    if not words:
        return None

    hits = []
    for ln in lines:
        low = ln.lower()
        matched = [w for w in words if w in low]
        if len(matched) >= 2 or (len(words) == 1 and words[0] in low):
            hits.append((len(matched), ln))

    if hits:
        hits.sort(key=lambda x: x[0], reverse=True)
        clean_lines = [h[1] for h in hits[:5]]
        return "From my memory:\n" + "\n".join(clean_lines)
    return None


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
    if _RE_LIST_REMINDERS.search(text):
        run(list_reminders, label="list_reminders")
        return results
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

    # 2d-ai — AI web chat prompt routing ("ask gemini why is the sky blue", "ask claude to write ...")
    m_ai = _RE_AI_CHAT_ASK.search(text)
    if m_ai:
        vals = [g.strip() for g in m_ai.groups() if g is not None]
        if len(vals) >= 2:
            run(open_ai_webchat, vals[0], vals[1], label="ai_webchat")
            return results

    # 2d — open document / file / folder ("open the file", "open the document", "open it", "open documents folder")
    m = _RE_OPEN_DOC.search(text)
    if m:
        run(open_document, text, label="open_document")
        return results

    # 2d' — document location query ("where is the document location", "where was it saved", "where did you save the document")
    if _RE_DOC_LOCATION.search(text):
        run(get_document_location, label="document_location")
        return results

    # 2d'' — find file across user folders
    m_find = _RE_FIND_FILE.search(text)
    if m_find:
        target = next((g.strip() for g in m_find.groups() if g and g.strip()), "")
        if target:
            run(find_file, _clean(target), label="find_file")
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
        elif kind == "meeting_notes":
            clean_top = _clean_doc_topic(topic)
            wiki_title, sentences = _fetch_topic_knowledge(clean_top)
            if sentences:
                disp_title = wiki_title or clean_top.title()
                _PENDING_CONFIRM = {
                    "label": "create_document",
                    "func": create_document,
                    "args": ("document", topic),
                }
                results.append((
                    "clarify",
                    f"I have no record of a meeting regarding '{disp_title}', sir. Would you like me to draft a concise document on {disp_title} instead?"
                ))
                return results
            else:
                run(create_document, kind, topic, label="create_document")
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

    # 3c — URL handling:
    # Explicit open/visit/launch command → open_url (launches browser)
    # Read/summarize/inspect or question with URL → fetch_page (extracts text)
    m_url = _RE_URL.search(text)
    if m_url:
        target_url = m_url.group(1) or m_url.group(2)
        is_open_intent = bool(re.search(r"\b(?:open|launch|visit|go\s+to|browse\s+to)\b", text, re.IGNORECASE))
        if is_open_intent:
            run(open_url, target_url, label="open_url")
            return results
        if m_url.group(1):
            run(fetch_page, m_url.group(1), label="fetch_page")
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
        if city and city.lower() in ("the", "a", "it", "this", "my", "here", "near me", "outside"):
            city = None
        run(get_weather, city, label="get_weather")
        return results

    # 5 — date delta math
    if _RE_DATE_MATH.search(text):
        dm = date_math(text)
        if dm:
            results.append(("date_math", dm))
            return results

    # 5b — time / date / clock (any timezone)
    if _RE_TIME.search(text):
        m_loc = re.search(r"\b(?:in|at|for)\s+([A-Za-z .'-]{2,25})(?:\s*(?:right\s+now|now|\?|!|\.|$))", text, re.IGNORECASE)
        loc = m_loc.group(1).strip() if m_loc else None
        if loc and loc.lower() in ("the morning", "the evening", "the afternoon", "the moment", "the night", "my time", "here", "local time"):
            loc = None
        run(get_time, loc, label="get_time")
        return results

    # 6 — system status & git status
    if _RE_GIT_STATUS.search(text):
        run(git_status, label="git_status")
        return results
    if _RE_STATUS.search(text):
        run(system_status, label="system_status")
        return results

    # 6b — network status & wifi info
    if _RE_WIFI_INFO.search(text):
        run(wifi_info, label="wifi_info")
        return results
    if _RE_NET_STATUS.search(text):
        run(network_status, label="network_status")
        return results

    # 7 — clipboard (read and write)
    m_copy = _RE_COPY_CLIPBOARD.search(text)
    if m_copy:
        to_copy = ""
        m_x = re.search(r"\bcopy\s+(.+?)\s+(?:to|into|in|onto)\s+(?:my\s+|the\s+)?clipboard\b", text, re.I)
        if m_x:
            to_copy = m_x.group(1).strip()
        elif m_copy.group(1):
            to_copy = m_copy.group(1).strip()
        if not to_copy:
            to_copy = text
        run(clipboard_write, to_copy, label="clipboard_write")
        return results
    if _RE_CLIP.search(text):
        action = "summarize" if re.search(r"\bsummar\w+", text, re.I) else \
                 "rewrite" if re.search(r"\b(rewrite|rephrase|improve)\b", text, re.I) else \
                 "translate" if re.search(r"\btranslat\w+", text, re.I) else "show"
        run(clipboard_assist, action, label="clipboard_assist")
        return results

    # 7b — volume & media keys
    m_vol_set = _RE_SET_VOLUME.search(text)
    if m_vol_set:
        val_s = m_vol_set.group(1) or m_vol_set.group(2) or m_vol_set.group(3)
        if val_s:
            run(set_volume_percent, int(val_s), label="set_volume")
            return results
    if _RE_VOLUME.search(text):
        act = "mute" if re.search(r"\b(?:mute|unmute)\b", text, re.I) else \
              "volume_down" if re.search(r"\b(?:down|quieter)\b", text, re.I) else \
              "volume_up"
        run(media_control, act, label="media_control")
        return results
    m_site_play = _RE_SITE_PLAY.search(text)
    if m_site_play:
        p_query = m_site_play.group(1).strip()
        p_site = _norm_site_name(m_site_play.group(2))
        template = _SITE_SEARCH_URLS.get(p_site)
        if template:
            import urllib.parse
            url = template.format(q=urllib.parse.quote_plus(p_query))
            run(open_url, url, label="site_search")
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

    # 7d0 — security guard: refuse to write cleartext passwords, PINs, or API keys to notes
    if _RE_SENSITIVE_SECRET.search(text) and (
        _RE_REMEMBER.search(text) or re.search(r"\b(?:my|save|store|keep|record)\s+(?:password|passcode|pin|api\s*key|secret)", text, re.I)
    ):
        refusal = "For your security, sir, I do not store sensitive credentials such as passwords, PINs, or API keys in plain text notes."
        results.append(("security_refusal", refusal))
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

    # 7e0 — specific site search ("open youtube and search for ryan trahan", "search spotify for ...")
    m_site_search = _RE_SITE_SEARCH.search(text)
    if m_site_search:
        vals = [g.strip() for g in m_site_search.groups() if g is not None]
        if len(vals) >= 2:
            s_name1 = _norm_site_name(vals[0])
            s_name2 = _norm_site_name(vals[1])
            if s_name1 in _SITE_SEARCH_URLS:
                s_name, s_query = s_name1, vals[1]
            elif s_name2 in _SITE_SEARCH_URLS:
                s_name, s_query = s_name2, vals[0]
            else:
                s_name, s_query = s_name1, vals[1]
            template = _SITE_SEARCH_URLS.get(s_name)
            if template:
                import urllib.parse
                url = template.format(q=urllib.parse.quote_plus(s_query))
                run(open_url, url, label="site_search")
                return results

    # 7e1 — open known website ("open youtube", "open reddit", "launch spotify web", "open gemini")
    m_site = _RE_OPEN_SITE.search(text)
    if m_site:
        s_name = _norm_site_name(m_site.group(1))
        if s_name in _SITE_URLS:
            run(open_url, _SITE_URLS[s_name], label="open_site")
            return results

    # 7e1b — autonomous research & PDF briefing generation
    m_pdf = _RE_RESEARCH_PDF.search(text)
    if m_pdf:
        topic = _clean(m_pdf.group(1)).rstrip(".?!")
        if topic:
            run(research_and_generate_pdf, topic, label="research_and_generate_pdf")
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

    # 7g — vocalize / speak aloud — genuine Paul Bettany soundboard
    m_spk = _RE_SPEAK.search(text)
    if m_spk:
        phrase = _clean(m_spk.group(1)).rstrip(".?!")
        if phrase:
            run(speak, phrase, label="speak")
            return results

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
            r"^(?:he|she|it|they|that|this|his|her|their|them|my|our|your|you|yourself|jarvis|deskpet)\b",
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

    # 9a' — questions about facts stored in local memory (notes.md).
    # Intercept queries matching saved facts before falling through to open search.
    mem_hit = _check_local_memory(text)
    if mem_hit:
        results.append(("recall", mem_hit))
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
    if label == "network_status":
        return result
    if label == "cancel_reminders":
        result = (result or "").strip()
        return result if result.startswith(("Cancelled", "No pending")) else None
    if label == "get_weather":
        if result and ("offline" in result.lower() or "air-gapped" in result.lower()):
            return "I cannot retrieve live weather reports right now, sir, as the system is currently offline in air-gapped mode."
        # Ready-made factual line ("Mumbai, India: 26.9°C, light drizzle
        # (...)"). Relay verbatim — the 1B sometimes "honestly" claims it
        # cannot access weather even when handed the reading.
        if result and not re.match(r"^(?:no |could not|weather unavailable)", result, re.I):
            return result.strip()
        return None
    if label == "convert_currency":
        if result and ("offline" in result.lower() or "air-gapped" in result.lower()):
            return "Live currency conversion requires an active internet connection, sir, and we are currently operating offline."
        m = _CANNED_RATE.search(result)
        if m:
            return f"At the live rate, {m.group(1)} — {m.group(2)}."
        return result
    if label == "wikipedia":
        # Ready-made prose — relay verbatim. The 1B model sometimes
        # "honestly" claims it lacks access even when handed a full
        # summary; Wikipedia text needs no shaping anyway.
        if result and "No Wikipedia article" not in result:
            return result.strip()
        return None
    if label == "web_search":
        if result and ("offline (air-gapped) mode" in result or "cannot browse the web" in result):
            return result.strip()
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
        if "division by zero is undefined" in result.lower():
            return "Division by zero is undefined, sir."
        return f"As I compute it, {result}, sir."
    if label == "document_location":
        return result
    if label == "open_document":
        return result
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
        if "I have no memory matching" in result or "nothing saved in my memory" in result:
            m = re.search(r"matching '(.+?)'", result)
            raw = m.group(1).strip() if m else "that"
            target = re.sub(r"^(?:my|your|the|a|an)\s+", "", raw, flags=re.IGNORECASE).strip()
            return f"I don't have any notes saved about {target or raw}, sir."
        return result
    if label in ("safety_refusal", "security_refusal"):
        return result
    if label in ("fetch_page", "fetch_url"):
        if result and ("offline" in result.lower() or "could not fetch" in result.lower()):
            return result.strip()
        return None
    if label == "active_window":
        if not result or "No active foreground window" in result:
            return "I could not detect an active window at the moment, sir."
        clean_res = result.rstrip(".")
        if "Web Page: " in clean_res:
            m_web = re.search(r"Application:\s*(.+?),\s*Web Page:\s*'(.+?)'", clean_res)
            if m_web:
                app, page = m_web.group(1).strip(), m_web.group(2).strip()
                return f"You are currently viewing '{page}' in {app}, sir."
        if clean_res.startswith("Application: "):
            if ", Window Title: " in clean_res:
                app_part, rest = clean_res.split(", Window Title: ", 1)
                app = app_part.replace("Application: ", "").strip()
                title = rest.split(", Active File: ")[0].strip("'\" ")
                return f"You are currently using {app} ('{title}'), sir."
            else:
                app = clean_res.replace("Application: ", "").strip()
                return f"You are currently using {app}, sir."
        return f"You are currently in {clean_res}, sir."
    from . import runtime_config
    addr = runtime_config.get().get("assistant_address", "sir")
    if label == "running_apps":
        return f"{result}, {addr}."
    if label == "speak":
        return f"Spoken aloud, {addr}."
    if label == "launch_app":
        if result and result.lower().startswith("could not find"):
            return result
        return result.rstrip(".") + f", {addr}."
    if label == "system_status":
        return result
    if label == "todo_list":
        return result
    if label == "create_document":
        return result
    if label == "set_volume":
        m = re.search(r"(\d+)%", result)
        pct = m.group(1) if m else ""
        return f"Setting the volume to {pct}%, {addr}." if pct else f"Volume adjusted, {addr}."
    if label in ("list_reminders", "clipboard_write", "find_file", "git_status", "wifi_info", "date_math"):
        return result
    if label == "clipboard_assist":
        if not result or result == "The clipboard is empty.":
            return f"Your clipboard is currently empty, {addr}."
        if result.startswith("Clipboard content: "):
            val = result.replace("Clipboard content: ", "").strip()
            return f"Your clipboard contains: '{val}', {addr}."
        return result
    if label == "ai_webchat":
        return f"{result}, {addr}."
    if label == "site_search":
        return f"Opening that search in your browser, {addr}."
    if label == "open_site":
        return f"Opening that website in your browser, {addr}."
    if label == "open_url":
        host = result.replace("opened: ", "").strip()
        return f"Opening {host} in your browser, {addr}."
    if label == "research_and_generate_pdf":
        return result
    return None


def research_and_generate_pdf(topic: str, target_pages: str = "optimal") -> str:
    """Autonomously research a topic online, compile an executive PDF briefing with ReportLab, and open it."""
    from . import pdf_engine
    return pdf_engine.research_and_generate_pdf(topic, target_pages)


def speak(phrase: str) -> str:
    """Play dense J.A.R.V.I.S. voice audio matching the requested phrase (0ms soundboard or dense neural synthesis)."""
    from . import jarvis_soundboard
    clip = jarvis_soundboard.find_best_clip(phrase)
    if clip:
        jarvis_soundboard.play_audio_file(clip["path"], async_play=True)
        return f"Spoken aloud (soundboard): '{clip['text']}'"
    ok = jarvis_soundboard.play_clip_for_phrase(phrase)
    if ok:
        return f"Spoken aloud (dense neural synthesis): '{phrase}'"
    return "Voice synthesis unavailable, sir."



