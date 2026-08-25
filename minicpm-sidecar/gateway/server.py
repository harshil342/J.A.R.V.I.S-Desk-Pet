"""FastAPI gateway in front of llama.cpp's llama-server.

Exposes the same HTTP/SSE contract the Electron app already speaks with
the legacy PyTorch sidecar, so the renderer (clawd-on-desk/src/minicpm-chat.*)
does not need to change. The actual inference happens in the subprocess
owned by `LlamaServer`; this file is just glue.
"""

from __future__ import annotations

import asyncio
import json
import os
import platform
import re
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import AsyncGenerator, List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from . import runtime_config
from .clawd_state import ClawdBridge
from .llama_client import LlamaServer, accumulate_tool_calls, detect_backend
from .log_setup import get_logger
from .mcp_client import MCPServerConfig, default_mcp_manager
from .persona import jarvis_system_prompt, prune_history
from .semantic_memory import default_memory_store
from .tag_scrubber import _RE_FUNCTION_TAG, _TagScrubber  # noqa: F401 (re-export)
from .task_dispatcher import default_task_dispatcher
from .think_filter import ThinkBlockFilter
from . import tools
from .tool_registry import MODEL_EXCLUDED_TOOLS, default_registry
from .updater import DEFAULT_SOURCE as DEFAULT_UPDATE_SOURCE
from .updater import ModelUpdater

_RE_TOOL_TAG = re.compile(
    r"\s*\[(?:set_reminder|todo_add|todo_list|todo_done|todo_remove|todo_clear|"
    r"create_document|convert_currency|get_weather|get_time|system_status|"
    r"clipboard_assist|launch_app|web_search|calculate|unit_convert|fetch_page|"
    r"wikipedia|remember|recall|open_url|media_control|screenshot|lock|"
    r"active_window|inspect_screen|read_screen_text|running_apps|speak|"
    r"schedule_task|safety_refusal)\]:?"
)


# â”€â”€ Request / response shapes â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


class ToolCallRequest(BaseModel):
    name: str
    arguments: Optional[dict] = None


class MCPServerAddRequest(BaseModel):
    name: str
    command: str
    args: List[str] = Field(default_factory=list)
    env: Optional[dict] = None
    cwd: Optional[str] = None
    enabled: bool = True


class SFXRequest(BaseModel):
    sound: str


class MemoryAddRequest(BaseModel):
    text: str
    category: str = "general"
    tags: List[str] = Field(default_factory=list)


class MemorySearchRequest(BaseModel):
    query: str
    limit: int = 5


class TaskScheduleRequest(BaseModel):
    name: str
    delay_seconds: float
    payload: str = ""
    recurring: bool = False


class RuntimeConfigRequest(BaseModel):
    """Partial assistant-preferences patch for /api/config.

    Every field is Optional: only the provided (non-None) fields are
    applied, unknown fields are ignored by the pydantic model itself.
    """

    assistant_address: Optional[str] = None
    clarify_strength: Optional[str] = None
    auto_memory: Optional[bool] = None
    briefing_hour: Optional[int] = None
    recap_hour: Optional[int] = None


class ChatMessage(BaseModel):
    role: str = Field(..., description="'system' | 'user' | 'assistant'")
    content: str


class ChatRequest(BaseModel):
    messages: List[ChatMessage]
    max_new_tokens: int = 512
    temperature: float = 0.6
    top_p: float = 0.95
    top_k: int = 0
    repetition_penalty: float = 1.05
    stream: bool = True
    system: Optional[str] = None
    thinking: bool = False
    silent: bool = False  # bypass pet state pushes (used by narrator)
    # When true the gateway sends `lora: []` to llama-server for THIS
    # request only, which disables every pre-loaded LoRA adapter for the
    # current generation without touching global scales. Used by the
    # narrator so its informational replies don't pick up the active
    # persona's stylistic bias. No-op when no adapter is currently
    # active.
    disable_adapter: bool = False
    # Tool-invocation strategy for this request:
    #   "auto"   â€” regex router first; if nothing matched, fall back to a
    #              single native function-calling round via llama-server.
    #   "regex"  â€” keyword router only (legacy behaviour).
    #   "native" â€” skip the regex router, native function calling only.
    #   "off"    â€” no tools at all (pure chat).
    # Default comes from MINICPM_TOOL_MODE (itself defaulting to "auto").
    tool_mode: Optional[str] = None


# When thinking=true the model emits a <think> block before the
# answer; both share one max_new_tokens budget. Bump the floor so reasoning
# doesn't eat the entire allowance and truncate the reply.
THINKING_MIN_MAX_NEW_TOKENS = 1280
MAX_NEW_TOKENS_CAP = 4096

_VALID_TOOL_MODES = ("auto", "regex", "native", "off")
DEFAULT_TOOL_MODE = os.environ.get("MINICPM_TOOL_MODE", "auto").strip().lower()
if DEFAULT_TOOL_MODE not in _VALID_TOOL_MODES:
    DEFAULT_TOOL_MODE = "auto"


def _resolve_tool_mode(req: ChatRequest) -> str:
    mode = (req.tool_mode or "").strip().lower()
    if mode in _VALID_TOOL_MODES:
        return mode
    return DEFAULT_TOOL_MODE


def _effective_max_new_tokens(req: ChatRequest) -> int:
    base = int(max(1, min(req.max_new_tokens, MAX_NEW_TOKENS_CAP)))
    if req.thinking:
        return min(MAX_NEW_TOKENS_CAP, max(base, THINKING_MIN_MAX_NEW_TOKENS))
    return base


# â”€â”€ Daily proactive events (briefing + recap) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


def _config_hour(key: str, default: int) -> int:
    """Live per-iteration read of an hour field from runtime config,
    clamped to 0â€“23 with a fallback default (mirrors runtime_config)."""
    try:
        return max(0, min(23, int(runtime_config.get().get(key, default))))
    except (TypeError, ValueError):
        return default


def _next_daily_event(now: datetime, hour: int) -> datetime:
    """Pure: next occurrence of a daily event scheduled at `hour`:00 local."""
    hour = max(0, min(23, int(hour)))
    nxt = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if nxt <= now:
        nxt += timedelta(days=1)
    return nxt


def _next_scheduled_event(
    now: datetime, briefing_hour: int, recap_hour: int
) -> tuple[datetime, str]:
    """Pure: the sooner of today's two daily events.

    Returns (fire_at, kind) where kind is "briefing" or "recap"; equal
    fire times resolve to "briefing" deterministically."""
    candidates = [
        (_next_daily_event(now, briefing_hour), "briefing"),
        (_next_daily_event(now, recap_hour), "recap"),
    ]
    return min(candidates, key=lambda pair: pair[0])


# â”€â”€ Model discovery â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


def discover_models(roots: List[Path]) -> List[dict]:
    """Return [{name, path}] for every *.gguf file under `roots`."""
    seen: set[Path] = set()
    out: List[dict] = []
    for root in roots:
        try:
            r = root.expanduser().resolve()
        except Exception:
            continue
        if not r.exists() or r in seen:
            continue
        seen.add(r)
        if r.is_file() and r.suffix.lower() == ".gguf":
            out.append({"name": r.name, "path": str(r)})
            continue
        if not r.is_dir():
            continue
        for p in sorted(r.rglob("*.gguf")):
            if any(part.endswith(".update-staging") or part.endswith(".bak") for part in p.parts):
                continue
            out.append({"name": p.name, "path": str(p)})
    return out


def _default_model_roots() -> List[Path]:
    """Locations to scan for *.gguf when no explicit MINICPM_MODEL_DIR
    is set. The Electron host passes `--model` explicitly so this is
    only used by direct CLI / dev runs."""
    here = Path(__file__).resolve().parent.parent
    return [
        Path.home() / "Library" / "Application Support" / "Clawd on Desk" / "models",
        Path.home() / ".local" / "share" / "Clawd on Desk" / "models",
        here / "models",
        here.parent / "models",
    ]


# â”€â”€ LoRA adapter discovery â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


# Filename-keyword â†’ persona slug. The slug is the stable identifier the
# Electron renderer keys off ("default" / "neko" / "muice" / ...) when
# deciding things like whether to flip `thinking` off (persona LoRAs don't
# carry <think> training, so reasoning collides with their style).
# Matching is substring + case-insensitive against the filename stem.
PERSONA_HINTS: dict[str, str] = {
    "nekoqa": "neko",
    "neko": "neko",
    "muice": "muice",
    "chuuni": "chuuni",
    "moyu": "moyu",
    "zhiyuan": "zhiyuan",
}


def _persona_for(path: Path) -> str:
    stem = path.stem.lower()
    parent = path.parent.name.lower()
    haystack = f"{parent}/{stem}"
    for needle, slug in PERSONA_HINTS.items():
        if needle in haystack:
            return slug
    return "custom"


def _default_adapter_roots() -> List[Path]:
    """Where to scan for `*.gguf` LoRA adapters when no `MINICPM_ADAPTER_DIR`
    env is set. The Electron host normally injects that env, so this only
    runs for direct CLI / dev / test invocations.

    The order here mirrors `_default_model_roots`: per-user app data first,
    then the dev-only repo path next to the sidecar package.
    """
    here = Path(__file__).resolve().parent.parent
    return [
        Path.home() / "Library" / "Application Support" / "Clawd on Desk" / "adapters",
        Path.home() / ".local" / "share" / "Clawd on Desk" / "adapters",
        here.parent / "adapters",   # <repo>/adapters/ in dev checkouts
    ]


def discover_adapters(roots: List[Path]) -> List[dict]:
    """Return [{name, path, persona}] for every `*.gguf` LoRA under `roots`.

    Skips electron-builder staging / backup directories the same way
    `discover_models` does, so we don't accidentally surface half-downloaded
    adapters."""
    seen_files: set[Path] = set()
    out: List[dict] = []
    for root in roots:
        try:
            r = root.expanduser().resolve()
        except Exception:
            continue
        if not r.exists() or not r.is_dir():
            continue
        for p in sorted(r.rglob("*.gguf")):
            if any(part.endswith(".update-staging") or part.endswith(".bak") for part in p.parts):
                continue
            try:
                resolved = p.resolve()
            except Exception:
                continue
            if resolved in seen_files:
                continue
            seen_files.add(resolved)
            out.append({
                "name": p.name,
                "path": str(p),
                "persona": _persona_for(p),
            })
    return out


def _resolve_adapter_root(initial_model: Optional[Path]) -> Optional[Path]:
    """Pick the canonical writable adapter dir for `/api/load-adapter`
    "open in Finder" hints. Resolution order:

    1. `MINICPM_ADAPTER_DIR` env (Electron host injects this in packaged
       mode pointing at `<userData>/adapters/`)
    2. First default root that already exists
    3. First default root regardless of existence (the caller can then
       `mkdir -p` before opening Finder)
    """
    env_dir = os.environ.get("MINICPM_ADAPTER_DIR")
    if env_dir:
        return Path(env_dir).expanduser()
    for cand in _default_adapter_roots():
        if cand.exists() and cand.is_dir():
            return cand
    defaults = _default_adapter_roots()
    return defaults[-1] if defaults else None


# Mirror file Electron writes after every manifest mutation. Lives in
# the adapter dir under a dot prefix so `discover_adapters`'s `*.gguf`
# scan misses it. Schema mirrors `<userData>/minicpm-adapters.json` 1:1
# (see clawd-on-desk/src/minicpm-chat.js).
_MANIFEST_MIRROR = ".manifest.json"


def read_adapter_manifest(adapter_root: Optional[Path]) -> dict:
    """Return the parsed `.manifest.json` from `adapter_root`, or an
    empty manifest if the file is absent / malformed.

    The gateway is a pure reader here â€” Electron owns the data and
    re-writes the mirror on every CRUD operation. Reading on every
    `/api/adapters` request keeps us a snapshot fresh without an
    explicit refresh endpoint."""
    if adapter_root is None:
        return {"version": 1, "items": []}
    try:
        mirror = Path(adapter_root) / _MANIFEST_MIRROR
        if not mirror.is_file():
            return {"version": 1, "items": []}
        with mirror.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {"version": 1, "items": []}
    if not isinstance(data, dict):
        return {"version": 1, "items": []}
    items = data.get("items") if isinstance(data.get("items"), list) else []
    return {"version": int(data.get("version") or 1), "items": items}


def _manifest_by_resolved_path(manifest: dict) -> dict[Path, dict]:
    """Index manifest items by their resolved absolute path so the
    `/api/adapters` merge step is O(1) per scanned file."""
    out: dict[Path, dict] = {}
    for entry in manifest.get("items", []) or []:
        if not isinstance(entry, dict):
            continue
        raw = entry.get("path")
        if not isinstance(raw, str) or not raw:
            continue
        try:
            out[Path(raw).expanduser().resolve()] = entry
        except Exception:
            continue
    return out


# â”€â”€ App factory â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


def build_app(
    *,
    initial_model: Optional[Path],
    update_source: str = DEFAULT_UPDATE_SOURCE,
    ctx_size: int = 4096,
    n_gpu_layers: int = -1,
    threads: Optional[int] = None,
) -> FastAPI:
    log = get_logger()
    bridge = ClawdBridge(enabled=True, debug=False)
    tools.bind_bridge(bridge)  # lets reminders ping the pet when they fire
    from .task_dispatcher import bind_bridge as bind_task_bridge
    bind_task_bridge(bridge)

    # Resolve the adapter root so /api/adapters can scan it; we still
    # show the full list to the UI even when none are loaded yet, so
    # users can browse + activate any LoRA from Settings.
    adapter_root = _resolve_adapter_root(initial_model)

    # Boot-time LoRA load is now *opt-in*: only the LoRA the Electron
    # host has persisted as the active one (env MINICPM_ACTIVE_ADAPTER)
    # gets passed to llama-server via --lora. Default behaviour is pure
    # Base â€” no third-party LoRA is preloaded just because it happens
    # to live on disk. Switching to a different LoRA later triggers
    # `LlamaServer.reload_adapters([new])`, costing one llama-server
    # restart but keeping the steady-state memory minimal.
    _env_active = os.environ.get("MINICPM_ACTIVE_ADAPTER", "").strip()
    initial_active: Optional[Path] = None
    if _env_active:
        try:
            cand = Path(_env_active).expanduser().resolve(strict=True)
            if cand.suffix.lower() == ".gguf":
                initial_active = cand
            else:
                log.warning("MINICPM_ACTIVE_ADAPTER ignored (not .gguf): %s", cand)
        except FileNotFoundError:
            log.warning("MINICPM_ACTIVE_ADAPTER points at missing file: %s", _env_active)

    server = LlamaServer(
        model_path=initial_model,
        ctx_size=ctx_size,
        n_gpu_layers=n_gpu_layers,
        threads=threads,
        adapters=[initial_active] if initial_active else [],
    )

    # In-memory adapter state. Single source of truth for what the
    # Electron app sees as "the active LoRA". Boots from the persisted
    # choice; cleared on /api/load-adapter {path:null}; updated to a
    # new path on /api/load-adapter {path:<gguf>}. The Electron host
    # is responsible for writing the latest choice back to its prefs
    # file so the next sidecar spawn boots into the same state.
    state: dict[str, Optional[Path]] = {"current_adapter": initial_active}
    startup_error: Optional[str] = None

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        nonlocal startup_error
        # Don't fail boot when the model isn't on disk yet â€” onboarding
        # downloads it via /api/update-apply and only then calls
        # /api/load-model. The pet still wants /api/health to answer 200
        # in the meantime so the bubble doesn't show a permanent error.
        if initial_model and Path(initial_model).exists():
            try:
                await server.start()
                startup_error = None
            except Exception as exc:
                startup_error = str(exc)
                log.exception("initial llama-server start failed: %s", exc)
        else:
            log.info("model not present at startup; waiting for /api/load-model")
        # Start configured MCP servers
        try:
            await default_mcp_manager.start_all()
        except Exception as exc:
            log.warning("MCP manager startup encountered an issue: %s", exc)

        # Durable reminders: re-arm future timers, fire overdue ones now.
        try:
            rstats = tools.restore_reminders()
            if rstats["fired"] or rstats["rearmed"]:
                log.info(
                    "restored reminders: %d fired (overdue), %d re-armed",
                    rstats["fired"], rstats["rearmed"],
                )
        except Exception as exc:
            log.warning("reminder restore failed: %s", exc)

        bridge.post("idle", title="MiniCPM Desk Pet")

        # Daily proactive events (local time): the morning briefing at
        # briefing_hour (default 8) and the evening recap at recap_hour
        # (default 21). One loop drives BOTH: each wake it recomputes
        # the next occurrence of each from live runtime config, sleeps
        # to the sooner, fires that one, repeats. Hours are re-read
        # EVERY iteration so a live /api/config change takes effect at
        # the next wake. Reuses the reminder bridge path â€” the pet
        # animates and the narrator speaks the line in a bubble.
        async def _daily_events_loop():
            composers = {
                "briefing": tools.compose_daily_briefing,
                "recap": tools.compose_evening_recap,
            }
            while True:
                now = datetime.now()
                fire_at, kind = _next_scheduled_event(
                    now,
                    _config_hour("briefing_hour", 8),
                    _config_hour("recap_hour", 21),
                )
                await asyncio.sleep(max(0.0, (fire_at - datetime.now()).total_seconds()))
                try:
                    text = await asyncio.to_thread(composers[kind])
                    if text:
                        bridge.post("notification", event="Notification", title=text)
                except Exception as exc:
                    log.warning("daily scheduled event (%s) failed: %s", kind, exc)

        daily_events_task = asyncio.create_task(_daily_events_loop())
        try:
            yield
        finally:
            daily_events_task.cancel()
            bridge.post("sleeping")
            try:
                await default_mcp_manager.stop_all()
            except Exception as exc:
                log.warning("MCP manager shutdown error: %s", exc)
            try:
                await server.shutdown()
            finally:
                bridge.close()

    app = FastAPI(title="MiniCPM Sidecar Gateway", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Model roots used by /api/models â€” honour the env override the
    # Electron host sets to <userData>/models/ in packaged mode.
    env_root = os.environ.get("MINICPM_MODEL_DIR")
    extra_roots: List[Path] = []
    if env_root:
        extra_roots.append(Path(env_root))
    if initial_model:
        extra_roots.append(Path(initial_model).expanduser().resolve().parent)
    extra_roots.extend(_default_model_roots())

    def _get_active_model_path() -> Path:
        if server.model_path:
            return server.model_path
        if initial_model:
            return Path(initial_model)
        # Fall back to the first discovered gguf so /api/update-check
        # always has *some* anchor to compare against.
        items = discover_models(extra_roots)
        if items:
            return Path(items[0]["path"])
        # Last resort: synthesise a stub path so updater code can still
        # compute target_dir for download staging.
        return (extra_roots[0] if extra_roots else Path.cwd()) / "minicpm.gguf"

    updater = ModelUpdater(_get_active_model_path(), source=update_source)

    # â”€â”€â”€ Health / introspection â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    @app.get("/api/health")
    async def health():
        sub_health = await server.health()
        backend = detect_backend()
        adapter = state["current_adapter"]
        current = backend.get("current") or backend["recommended"]
        return {
            "ok": True,
            "alive": server.alive,
            "backend": "llama.cpp",
            "accel": current,
            "device": current,  # alias used by older Electron code paths
            "dtype": "gguf",
            "model_dir": str(server.model_path) if server.model_path else None,
            "model_name": server.model_path.name if server.model_path else None,
            "adapter": str(adapter) if adapter else None,
            "persona": _persona_for(adapter) if adapter else "default",
            "llama_server": sub_health,
            "port": server.port,
            "startup_error": startup_error,
            # Crash-watchdog telemetry: how many times the watchdog has
            # re-spawned llama-server, and why it gave up (None = healthy).
            "llama_restarts": server.watchdog_restarts,
            "degraded": server.degraded_reason,
        }

    @app.get("/api/devices")
    def list_devices():
        info = detect_backend()
        return info

    @app.post("/api/set-device")
    async def set_device(payload: dict):
        device = str(payload.get("device") or "").strip().lower()
        if device not in ("metal", "cuda", "cpu", "vulkan", "mps", "auto", ""):
            return JSONResponse({"error": f"unknown device: {device!r}"}, status_code=400)
        # "mps" is the legacy name for Apple Silicon; transparently map
        # to metal for consistency with llama.cpp terminology.
        if device == "mps":
            device = "metal"
        if device == "vulkan" and platform.system() != "Windows":
            return JSONResponse(
                {"error": "vulkan backend is only configurable on Windows"},
                status_code=400,
            )
        if device:
            os.environ["MINICPM_DEVICE"] = device
        else:
            os.environ.pop("MINICPM_DEVICE", None)
        return {"ok": True, "device": device or "auto", "note": "restart sidecar to take effect"}

    @app.post("/api/config")
    async def set_runtime_config(req: RuntimeConfigRequest):
        try:
            return JSONResponse(runtime_config.update(req.model_dump(exclude_none=True)))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))

    @app.get("/api/config")
    async def get_runtime_config():
        return JSONResponse(runtime_config.get())

    @app.get("/api/onboarding")
    def onboarding():
        path = server.model_path or _get_active_model_path()
        present = path.exists() if path else False
        adapter = state["current_adapter"]
        backend = detect_backend()
        return {
            "model_present": present,
            "model_dir": str(path) if path else None,
            "device": backend.get("current") or backend["recommended"],
            "dtype": "gguf",
            "adapter": str(adapter) if adapter else None,
            "persona": _persona_for(adapter) if adapter else "default",
            "stage_hint": "ready" if present else "model-download",
        }

    # â”€â”€â”€ Model / adapter listing â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    @app.get("/api/models")
    def list_models():
        items = discover_models(extra_roots)
        current = str(server.model_path) if server.model_path else None
        return {
            "items": items,
            "current": current,
            "current_name": server.model_path.name if server.model_path else None,
        }

    @app.post("/api/load-model")
    async def load_model(payload: dict):
        nonlocal startup_error
        path = str(payload.get("path") or "").strip()
        if not path:
            return JSONResponse({"error": "path is required"}, status_code=400)
        target = Path(path).expanduser().resolve()
        if not target.is_file() or target.suffix.lower() != ".gguf":
            return JSONResponse({"error": f"not a .gguf file: {target}"}, status_code=400)
        bridge.post("working", event="LoadModel", title=f"åŠ è½½ {target.name}")
        try:
            await server.swap_model(target)
            updater.local_model_path = target
            startup_error = None
        except Exception as exc:
            startup_error = str(exc)
            bridge.post("error")
            return JSONResponse({"error": str(exc)}, status_code=500)
        bridge.post("idle")
        return {"ok": True, "model_dir": str(target), "model_name": target.name}

    def _scan_adapters() -> List[dict]:
        # Re-resolve the root each call so Settings â†’ "open adapter dir"
        # â†’ drop new .gguf â†’ "refresh" picks up files added at runtime
        # without restarting the sidecar. Also re-read the manifest
        # mirror on every call so rename / upload mutations show up in
        # the next /api/adapters response without any explicit refresh
        # ping from Electron.
        root = _resolve_adapter_root(server.model_path)
        if not root:
            return []
        items = discover_adapters([root])
        manifest = read_adapter_manifest(root)
        by_path = _manifest_by_resolved_path(manifest)
        for item in items:
            try:
                key = Path(item["path"]).expanduser().resolve()
            except Exception:
                continue
            entry = by_path.get(key)
            if not entry:
                continue
            # Only surface the product-layer fields; gateway's persona
            # slug already on `item` wins by default but a manifest
            # override (user typed their own) takes precedence.
            if isinstance(entry.get("displayName"), str) and entry["displayName"].strip():
                item["displayName"] = entry["displayName"].strip()
            if isinstance(entry.get("aliases"), list):
                item["aliases"] = [str(a).strip() for a in entry["aliases"] if str(a).strip()]
            if isinstance(entry.get("source"), str):
                item["source"] = entry["source"]
            if isinstance(entry.get("id"), str):
                item["id"] = entry["id"]
            if isinstance(entry.get("persona"), str) and entry["persona"].strip():
                item["persona"] = entry["persona"].strip()
        return items

    @app.get("/api/adapters")
    def list_adapters():
        items = _scan_adapters()
        current = state["current_adapter"]
        return {
            "items": items,
            "current": str(current) if current else None,
            "current_name": current.name if current else None,
            "adapter_dir": str(_resolve_adapter_root(server.model_path) or ""),
        }

    @app.post("/api/load-adapter")
    async def load_adapter(payload: dict):
        raw = payload.get("path")
        # path = null  â†’  deactivate any LoRA (back to base model)
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            # If llama-server was booted with `--lora <something>`, a
            # per-request `lora: []` is enough to force base output on
            # modern llama.cpp. We still respawn with no `--lora` here
            # so switching back to Base releases the adapter weights too.
            if server.adapter_paths:
                bridge.post("working", event="UnloadAdapter", title="å¸è½½ LoRA")
                try:
                    await server.reload_adapters([])
                except Exception as exc:
                    bridge.post("error")
                    return JSONResponse({"error": str(exc)}, status_code=500)
                bridge.post("idle")
            state["current_adapter"] = None
            return {"ok": True, "adapter": None, "persona": "default"}

        target = Path(str(raw)).expanduser()
        try:
            target = target.resolve(strict=True)
        except FileNotFoundError:
            return JSONResponse(
                {"error": f"adapter file not found: {target}"},
                status_code=400,
            )
        if target.suffix.lower() != ".gguf":
            return JSONResponse(
                {"error": f"not a .gguf adapter: {target}"},
                status_code=400,
            )

        # If the requested adapter isn't currently `--lora`-loaded,
        # restart llama-server so that ONLY this adapter is loaded.
        # We deliberately don't keep a growing list of preloaded LoRAs
        # in memory â€” that was the old behaviour, and it meant any
        # third-party `.gguf` on disk silently rode along whether the
        # user wanted it or not. The user pays one sidecar restart
        # (~3-4s) per LoRA switch, which matches the cost of switching
        # base models and is the only honest way to keep memory tight.
        if server.adapter_id_for(target) is None:
            bridge.post("working", event="LoadAdapter", title=f"åŠ è½½ {target.name}")
            try:
                await server.reload_adapters([target])
            except Exception as exc:
                bridge.post("error")
                return JSONResponse({"error": str(exc)}, status_code=500)
            bridge.post("idle")
            if server.adapter_id_for(target) is None:
                return JSONResponse(
                    {"error": f"llama-server refused adapter: {target}"},
                    status_code=500,
                )

        state["current_adapter"] = target
        return {
            "ok": True,
            "adapter": str(target),
            "persona": _persona_for(target),
        }

    @app.post("/api/classify")
    def classify_endpoint(payload: dict):
        return JSONResponse(
            {"error": "/api/classify not implemented for llama.cpp backend yet"},
            status_code=501,
        )

    # â”€â”€â”€ Updater â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    @app.get("/api/update-check")
    async def update_check():
        updater.local_model_path = server.model_path or _get_active_model_path()
        return await asyncio.to_thread(updater.check)

    @app.post("/api/update-apply")
    async def update_apply():
        nonlocal startup_error
        updater.local_model_path = server.model_path or _get_active_model_path()

        async def stream():
            nonlocal startup_error
            queue: asyncio.Queue = asyncio.Queue()
            sentinel = object()
            loop = asyncio.get_running_loop()

            def producer():
                try:
                    for ev in updater.apply():
                        loop.call_soon_threadsafe(queue.put_nowait, ev)
                finally:
                    loop.call_soon_threadsafe(queue.put_nowait, sentinel)

            import threading as _t
            _t.Thread(target=producer, daemon=True).start()

            bridge.post("working", event="UpdateApply", title="æ­£åœ¨æ›´æ–°æ¨¡åž‹")
            try:
                while True:
                    ev = await queue.get()
                    if ev is sentinel:
                        break
                    yield _sse(ev)
                    if ev.get("phase") == "complete":
                        try:
                            # Restart llama-server against the (potentially
                            # renamed) gguf so the new weights take effect
                            # without a full sidecar restart.
                            items = discover_models(extra_roots)
                            if items:
                                target = Path(items[0]["path"])
                                await server.swap_model(target)
                                updater.local_model_path = target
                                startup_error = None
                                yield _sse({"phase": "reloaded", "model": str(target)})
                        except Exception as exc:
                            startup_error = str(exc)
                            yield _sse({"phase": "reload-error", "message": str(exc)})
            finally:
                bridge.post("idle")

        return StreamingResponse(stream(), media_type="text/event-stream")

    # â”€â”€â”€ Chat â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    @app.post("/api/warmup")
    async def warmup():
        if not server.alive:
            return JSONResponse({"ok": False, "error": "llama-server not running"}, status_code=503)
        t0 = time.time()
        try:
            await server.complete_once(prompt=" ", max_tokens=1)
            return {"ok": True, "elapsed_ms": int((time.time() - t0) * 1000)}
        except Exception as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)

    def _lora_arr_for(req: ChatRequest) -> Optional[List[dict]]:
        """Compute the per-request `lora` array.

        - disable_adapter=true  â†’ []   (force base for this request)
        - active adapter set    â†’ [{id, scale: 1.0}]
        - no adapter active     â†’ []   (force base)

        Sending an empty list is intentionally explicit: llama.cpp
        treats adapters omitted from a per-request `lora` list as scale
        0.0, so base chat never depends on whatever global adapter scale
        the server happened to inherit at startup.
        """
        if req.disable_adapter:
            return []
        current = state["current_adapter"]
        if not current:
            return []
        idx = server.adapter_id_for(current)
        if idx is None:
            # State got out of sync (e.g. sidecar restarted without
            # re-registering this path). Fail open to base rather than
            # 500 â€” the user will notice the persona is gone and can
            # re-select from Settings.
            log.warning("active adapter %s missing from llama-server index", current)
            return []
        return [{"id": idx, "scale": 1.0}]

    @app.post("/api/chat")
    async def chat(req: ChatRequest):
        if not req.messages:
            return JSONResponse({"error": "messages is empty"}, status_code=400)
        if not server.alive:
            degraded = server.degraded_reason
            detail = (
                f"llama-server not running â€” {degraded}"
                if degraded
                else "llama-server not running â€” open Onboarding to download the model"
            )
            return JSONResponse({"error": detail}, status_code=503)
        lora_arr = _lora_arr_for(req)
        mode = _resolve_tool_mode(req)

        # F2 Phase A: keyword-route the latest user message through the tool
        # layer BEFORE inference, then inject the live results as context.
        # Skipped for silent calls (the renderer's intent classifier) and
        # for native/off modes.
        tool_context: Optional[str] = None
        canned: Optional[str] = None
        tools_ran: List[str] = []
        has_prior_turns = any(m.role == "assistant" for m in req.messages[:-1])
        if not req.silent and mode in ("auto", "regex"):
            last_user = next(
                (m.content for m in reversed(req.messages) if m.role == "user"), None
            )
            if last_user:
                hits = await asyncio.to_thread(
                    tools.route_tools, last_user, len(req.messages) - 1,
                    clarify=runtime_config.get()["clarify_strength"],
                )
                if hits:
                    log.info("tool routing matched %d tool(s) for: %r", len(hits), last_user[:80])
                    tools_ran = [label for label, _ in hits]
                    # Deterministic path: a single mechanical tool gets a fixed
                    # Jarvis line with NO inference â€” the 1B model cannot be
                    # trusted to relay results without narrating actions it
                    # never took ("I used the web search tool...").
                    #
                    # Opening turns only. Mid-conversation, a canned line
                    # would ignore everything said before it (the "every
                    # reply starts a new chat" failure mode), so follow-ups
                    # always flow through the model with full history plus
                    # the tool results injected as context.
                    if hits[0][0] in ("clarify", "confirm_cancel"):
                        canned = hits[0][1]
                    elif len(hits) == 1:
                        # Deterministic relays (todo/reminder/rate/weather/
                        # wikipedia) win at ANY conversation position â€” the
                        # 1B sometimes "honestly" refuses to repeat data it
                        # was just handed.
                        canned = tools.canned_reply(hits[0][0], hits[0][1])
                    if not canned:
                        # Inject the raw results (no [label] tags â€” the small
                        # model copies whatever brackets it sees).
                        clean = "\n".join(out for _, out in hits)
                        tool_context = (
                            "Freshly fetched result for the request below:\n"
                            + clean
                            + "\nReply to the LATEST user request only, in your "
                            "Jarvis voice, in 1-3 sentences. State the numbers/facts "
                            "directly as your own knowledge. Do NOT repeat previous "
                            "answers from history, do not mention tools, fetching, "
                            "'results', or these instructions. If the result reports "
                            "a failure or something not installed, say so briefly "
                            "and stop."
                        )
        if canned is not None:
            bridge.new_session()
            bridge.post("working")
            if req.stream:
                return StreamingResponse(
                    _canned_stream(bridge, canned, tool_name=tools_ran[0] if tools_ran else None),
                    media_type="text/event-stream",
                )
            bridge.post("attention")
            return JSONResponse({"content": canned, "thinking": None})
        if req.stream:
            # Native-only mode, or auto mode where the regex router found
            # nothing: let the model decide via llama-server's tools API.
            # The native round degrades to a plain generation itself when
            # the backend/template lacks tool support.
            if not req.silent and (mode == "native" or (mode == "auto" and not tools_ran)):
                cue_user = next(
                    (m.content for m in reversed(req.messages) if m.role == "user"), ""
                )
                return StreamingResponse(
                    native_tool_round(server, bridge, req, lora=lora_arr,
                                      arm_full=bool(_RE_TOOL_CUE.search(cue_user or ""))),
                    media_type="text/event-stream",
                )
            return StreamingResponse(
                _stream_chat(server, bridge, req, lora=lora_arr,
                             tool_context=tool_context, tools_ran=tools_ran),
                media_type="text/event-stream",
            )
        return JSONResponse(
            await _blocking_chat(server, bridge, req, lora=lora_arr, tool_context=tool_context)
        )

    @app.post("/api/state")
    def manual_state(payload: dict):
        state = str(payload.get("state") or "idle")
        bridge.post(state, event=payload.get("event"))
        return {"ok": True}

    @app.get("/api/tools")
    def list_tools():
        return JSONResponse({
            "ok": True,
            "tools": default_registry.get_tools_catalog(),
            "openai_schemas": default_registry.get_openai_schemas(),
        })

    @app.post("/api/tools/call")
    async def execute_tool(req: ToolCallRequest):
        res = await default_registry.execute_tool_async(req.name, req.arguments)
        return JSONResponse(res.to_dict())

    @app.get("/api/mcp/servers")
    def list_mcp_servers():
        return JSONResponse({
            "ok": True,
            "servers": default_mcp_manager.get_status_report(),
        })

    @app.post("/api/mcp/servers")
    async def add_mcp_server(req: MCPServerAddRequest):
        cfg = MCPServerConfig(
            name=req.name,
            command=req.command,
            args=req.args,
            env=req.env,
            cwd=req.cwd,
            enabled=req.enabled,
        )
        ok = await default_mcp_manager.add_server(cfg)
        return JSONResponse({"ok": ok, "name": req.name})

    @app.delete("/api/mcp/servers/{name}")
    async def remove_mcp_server(name: str):
        ok = await default_mcp_manager.remove_server(name)
        return JSONResponse({"ok": ok, "name": name})

    @app.post("/api/mcp/servers/{name}/reload")
    async def reload_mcp_server(name: str):
        ok = await default_mcp_manager.reload_server(name)
        return JSONResponse({"ok": ok, "name": name})

    @app.post("/api/audio/sfx")
    def audio_sfx(req: SFXRequest):
        bridge.post("attention" if req.sound == "alert" else "finish", event="SFX")
        return JSONResponse({"ok": True, "sound": req.sound})

    @app.get("/api/memory")
    def get_memory(category: Optional[str] = None):
        items = default_memory_store.list_all(category=category)
        return JSONResponse({"ok": True, "count": len(items), "memories": [i.to_dict() for i in items]})

    @app.post("/api/memory")
    def add_memory(req: MemoryAddRequest):
        item = default_memory_store.add(req.text, category=req.category, tags=req.tags)
        return JSONResponse({"ok": True, "memory": item.to_dict()})

    @app.delete("/api/memory/{item_id}")
    def delete_memory(item_id: str):
        ok = default_memory_store.delete(item_id)
        return JSONResponse({"ok": ok, "id": item_id})

    @app.post("/api/memory/search")
    def search_memory(req: MemorySearchRequest):
        results = default_memory_store.search(req.query, limit=req.limit)
        return JSONResponse({
            "ok": True,
            "query": req.query,
            "matches": [{"memory": item.to_dict(), "score": round(score, 3)} for item, score in results],
        })

    @app.get("/api/tasks")
    async def get_tasks():
        tasks = default_task_dispatcher.list_tasks()
        return JSONResponse({"ok": True, "count": len(tasks), "tasks": [t.to_dict() for t in tasks]})

    @app.post("/api/tasks")
    async def create_task(req: TaskScheduleRequest):
        task = default_task_dispatcher.schedule_task(
            name=req.name,
            delay_seconds=req.delay_seconds,
            payload=req.payload,
            recurring=req.recurring,
        )
        return JSONResponse({"ok": True, "task": task.to_dict()})

    @app.delete("/api/tasks/{task_id}")
    async def cancel_task(task_id: str):
        ok = default_task_dispatcher.cancel_task(task_id)
        return JSONResponse({"ok": ok, "id": task_id})

    @app.get("/")
    def index():
        return JSONResponse({
            "ok": True,
            "note": "MiniCPM sidecar gateway (llama.cpp backend)",
            "endpoints": [
                "/api/health", "/api/chat", "/api/warmup",
                "/api/models", "/api/load-model",
                "/api/devices", "/api/set-device", "/api/onboarding",
                "/api/config",
                "/api/update-check", "/api/update-apply",
                "/api/adapters", "/api/load-adapter", "/api/classify",
                "/api/state", "/api/tools", "/api/tools/call",
                "/api/mcp/servers",
                "/api/audio/sfx", "/api/memory", "/api/memory/search",
                "/api/tasks",
            ],
        })

    return app


# â”€â”€ Chat plumbing â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


async def _stream_chat(
    server: LlamaServer,
    bridge: ClawdBridge,
    req: ChatRequest,
    *,
    lora: Optional[List[dict]] = None,
    tool_context: Optional[str] = None,
    tools_ran: Optional[List[str]] = None,
) -> AsyncGenerator[bytes, None]:
    if not req.silent:
        bridge.new_session()
        bridge.post("thinking")

    # Announce which tools already ran for this turn BEFORE any model
    # output, so the bubble can show a "ðŸ”§ â€¦" activity chip while the
    # reply is being composed. Old renderers ignore unknown events.
    if tools_ran:
        for name in tools_ran:
            yield _sse({"event": "tool", "name": name})

    messages = _build_messages(req, tool_context=tool_context)

    try:
        agen = server.stream_chat(
            messages=messages,
            max_tokens=_effective_max_new_tokens(req),
            temperature=_chat_temperature(req, tool_context),
            top_p=float(req.top_p),
            top_k=int(req.top_k),
            repetition_penalty=float(req.repetition_penalty),
            enable_thinking=bool(req.thinking),
            lora=lora,
        )
    except Exception as exc:
        if not req.silent:
            bridge.post("error")
        yield _sse({"event": "error", "message": str(exc)})
        return

    yield _sse({"event": "start"})
    if not req.silent:
        bridge.post("working")

    last_pet_ping = time.time()
    # ThinkBlockFilter is the safety net: when llama-server *doesn't*
    # pre-split reasoning into reasoning_content (e.g. running against
    # a non-MiniCPM5 GGUF, or with --jinja off), <think> tags may leak
    # into the content stream. We still want to route them to the right
    # event in that case, so we run the filter only over content chunks.
    think_filter = ThinkBlockFilter(expose=req.thinking, start_inside=False)
    tag_scrub = _TagScrubber()

    try:
        async for kind, piece in agen:
            if kind == "reasoning":
                # llama.cpp already split <think>...</think> for us.
                # Surface it as "think" when the caller asked for it,
                # otherwise drop silently.
                if req.thinking:
                    yield _sse({"event": "think", "content": piece})
            else:  # "content"
                piece = tag_scrub.feed(_RE_TOOL_TAG.sub("", piece))
                for ev in think_filter.feed(piece):
                    yield _sse(ev)
            now = time.time()
            if now - last_pet_ping > 6.0:
                if not req.silent:
                    bridge.post("working")
                last_pet_ping = now
    except asyncio.CancelledError:
        if not req.silent:
            bridge.post("attention")
        raise
    except Exception as exc:
        get_logger().exception("chat stream error: %s", exc)
        if not req.silent:
            bridge.post("error")
        yield _sse({"event": "error", "message": str(exc)})
        return
    finally:
        tail = tag_scrub.flush()
        if tail:
            for ev in think_filter.feed(tail):
                yield _sse(ev)
        for ev in think_filter.flush():
            yield _sse(ev)

    yield _sse({"event": "end"})
    if not req.silent:
        bridge.post("attention")


async def _blocking_chat(
    server: LlamaServer,
    bridge: ClawdBridge,
    req: ChatRequest,
    *,
    lora: Optional[List[dict]] = None,
    tool_context: Optional[str] = None,
) -> dict:
    if not req.silent:
        bridge.new_session()
        bridge.post("thinking")
    messages = _build_messages(req, tool_context=tool_context)
    think_filter = ThinkBlockFilter(expose=req.thinking, start_inside=False)
    tag_scrub = _TagScrubber()
    content_parts: list[str] = []
    think_parts: list[str] = []
    if not req.silent:
        bridge.post("working")
    try:
        async for kind, piece in server.stream_chat(
            messages=messages,
            max_tokens=_effective_max_new_tokens(req),
            temperature=_chat_temperature(req, tool_context),
            top_p=float(req.top_p),
            top_k=int(req.top_k),
            repetition_penalty=float(req.repetition_penalty),
            enable_thinking=bool(req.thinking),
            lora=lora,
        ):
            if kind == "reasoning":
                think_parts.append(piece)
            else:
                piece = tag_scrub.feed(_RE_TOOL_TAG.sub("", piece))
                for ev in think_filter.feed(piece):
                    (think_parts if ev["event"] == "think" else content_parts).append(ev["content"])
        tail = tag_scrub.flush()
        if tail:
            for ev in think_filter.feed(tail):
                (think_parts if ev["event"] == "think" else content_parts).append(ev["content"])
        for ev in think_filter.flush():
            (think_parts if ev["event"] == "think" else content_parts).append(ev["content"])
    finally:
        if not req.silent:
            bridge.post("attention")
    return {
        "content": "".join(content_parts),
        "thinking": "".join(think_parts) if req.thinking else None,
    }


def _chat_temperature(req: ChatRequest, tool_context: Optional[str]) -> float:
    """Tool-augmented replies are relay tasks â€” dampen sampling so the
    1B model reports the live facts instead of freewheeling."""
    temp = max(0.0, float(req.temperature))
    if tool_context:
        temp = min(temp, 0.35)
    return temp


def _build_messages(req: ChatRequest, *, tool_context: Optional[str] = None) -> list[dict]:
    out: list[dict] = []
    # Default to the Jarvis persona when the caller didn't bring its own
    # system prompt (the renderer's intent classifier always sends one,
    # so this never leaks into classifier calls).
    if req.system is not None:
        system = req.system
    else:
        # Live runtime config: addressing word + whether the persona may
        # auto-save facts. Read per request so a /api/config change takes
        # effect without a restart.
        rcfg = runtime_config.get()
        prompt = jarvis_system_prompt(rcfg.get("assistant_address") or "sir")
        if not rcfg.get("auto_memory", True):
            # auto_memory off â†’ drop the Rules bullet instructing the model
            # to quietly save durable facts with remember_fact.
            prompt = "\n".join(
                ln for ln in prompt.splitlines() if "remember_fact" not in ln
            )
        # Ground the 1B model with the live clock on every request. Small
        # models parrot what they see â€” without this they happily invent
        # plausible times ("10:30 PM sir") when the router misses a
        # phrasing. Fresh per request, so it never goes stale mid-session.
        now = datetime.now().strftime("%A, %d %B %Y, %H:%M")
        system = (
            prompt
            + f"\n\nCurrent date and time (live, from the system clock): {now}. "
            "If asked about the time or date, quote this value exactly; never estimate."
        )
    out.append({"role": "system", "content": system})
    history = [{"role": m.role, "content": m.content} for m in req.messages]
    history = prune_history(history)
    # Small models obey tool context far more reliably when it sits right
    # next to the question than as a second system message.
    if tool_context and history and history[-1]["role"] == "user":
        history[-1] = dict(history[-1])
        history[-1]["content"] = tool_context + "\n\nUser request: " + history[-1]["content"]
    out.extend(history)
    return out


def _sse(payload: dict) -> bytes:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")


async def _canned_stream(
    bridge: ClawdBridge, text: str, *, tool_name: Optional[str] = None
) -> AsyncGenerator[bytes, None]:
    """Serve a deterministic canned tool reply over the same SSE shape."""
    if tool_name:
        yield _sse({"event": "tool", "name": tool_name})
    yield _sse({"event": "start"})
    yield _sse({"event": "delta", "content": text})
    yield _sse({"event": "end"})
    bridge.post("attention")


# â”€â”€ Native function-calling round (llama-server tools API) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


def _native_tool_error_reply(name: str) -> str:
    return f"Apologies, sir â€” the {name} tool failed to run just now."


_MEMORY_ONLY_TOOLS = {"remember_fact", "recall_fact"}
# Compact cue list mirroring what the keyword router covers. A miss here
# means "no obvious tool need" â€” the native round runs with only the
# memory tools armed.
_RE_TOOL_CUE = re.compile(
    r"\b(?:weather|temperature|forecast|rain|time|date|timer|remind|reminder|"
    r"todo|to-do|task|launch|open|run|play|pause|skip|next|previous|volume|"
    r"mute|louder|quieter|search|look\s*up|google|wikipedia|who|what|when|"
    r"where|why|how|calculat|comput|convert|sqrt|square|percent|%|plus|minus|"
    r"times|divided|status|check|cpu|ram|memory|battery|disk|uptime|health|"
    r"clipboard|screenshot|lock|document|notes?|email|changelog|readme|"
    r"meeting|script|summar\w+|translate|rewrite)\b|\?",
    re.IGNORECASE,
)


async def native_tool_round(
    server: LlamaServer,
    bridge: ClawdBridge,
    req: ChatRequest,
    *,
    lora: Optional[List[dict]],
    arm_full: bool = True,
) -> AsyncGenerator[bytes, None]:
    """One model-driven tool round: stream with `tools`, execute whatever
    the model calls (max 3), re-ask once with the results, stream the
    final answer. Falls back to a plain generation when the backend or
    the GGUF template doesn't support tool calling.

    Single retry by design â€” a 1B model looping tool calls burns latency
    without converging; one grounded follow-up is where the quality is.
    """
    log = get_logger()
    schemas = default_registry.get_openai_schemas() or []
    if not arm_full:
        # No actionable/lookup cue in the message: arm only the memory
        # pair. A 1B model left with the full catalogue plus llama-server's
        # injected "respond with tool_call" nudge invents calls for plain
        # statements ("my codename is X" â†’ system_status).
        schemas = [
            s for s in schemas
            if s.get("function", {}).get("name") in _MEMORY_ONLY_TOOLS
        ]
    if not schemas:
        async for chunk in _stream_chat(server, bridge, req, lora=lora):
            yield chunk
        return

    if not req.silent:
        bridge.new_session()
        bridge.post("thinking")
    messages = _build_messages(req)
    gen_kwargs = dict(
        max_tokens=_effective_max_new_tokens(req),
        temperature=_chat_temperature(req, None),
        top_p=float(req.top_p),
        top_k=int(req.top_k),
        repetition_penalty=float(req.repetition_penalty),
        enable_thinking=bool(req.thinking),
        lora=lora,
    )

    # â”€â”€ Phase 1: stream with tools armed; collect content + tool deltas â”€â”€
    fragments: list = []
    preamble_parts: list[str] = []
    think_parts: list[str] = []
    try:
        agen = server.stream_chat(messages=messages, tools=schemas, **gen_kwargs)
        async for kind, piece in agen:
            if kind == "tool_delta":
                fragments.append(piece)
            elif kind == "reasoning":
                if req.thinking:
                    think_parts.append(piece)
            else:
                preamble_parts.append(piece)
    except Exception as exc:
        # Backend rejected the request shape (template without tool
        # support, older llama.cpp, ...) â€” degrade to plain chat instead
        # of failing the turn.
        log.warning("native tool round unavailable (%s); falling back to plain chat", exc)
        if not req.silent:
            bridge.post("error")
        async for chunk in _stream_chat(server, bridge, req, lora=lora):
            yield chunk
        return

    calls = accumulate_tool_calls(fragments)
    calls = [c for c in calls if c.get("name")]  # drop empty-name ghosts

    if not calls:
        # Model answered without tools â€” replay what it streamed as a
        # normal SSE conversation so the UI contract stays identical.
        if not req.silent:
            bridge.post("working")
        for name_part in ("start",):
            yield _sse({"event": name_part})
        think_filter = ThinkBlockFilter(expose=req.thinking, start_inside=False)
        if req.thinking and think_parts:
            yield _sse({"event": "think", "content": "".join(think_parts)})
        text = "".join(preamble_parts)
        for ev in think_filter.feed(text):
            yield _sse(ev)
        for ev in think_filter.flush():
            yield _sse(ev)
        yield _sse({"event": "end"})
        if not req.silent:
            bridge.post("attention")
        return

    # â”€â”€ Phase 2: execute + one grounded re-ask â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    if not req.silent:
        bridge.post("working")
    for call in calls:
        yield _sse({"event": "tool", "name": call["name"]})

    assistant_msg: dict = {"role": "assistant", "content": "".join(preamble_parts) or None}
    assistant_msg["tool_calls"] = [
        {
            "id": c["id"],
            "type": "function",
            "function": {
                "name": c["name"],
                "arguments": json.dumps(c["arguments"], ensure_ascii=False),
            },
        }
        for c in calls
    ]
    messages.append(assistant_msg)

    MAX_CALLS = 3
    executed: list[tuple[str, str]] = []
    for call in calls[:MAX_CALLS]:
        try:
            if call["name"] in MODEL_EXCLUDED_TOOLS:
                # Defense in depth: the schema filter keeps these away from
                # the model; refuse loudly if one slips through anyway.
                result_text = (
                    f"'{call['name']}' is restricted, sir â€” ask me in plain "
                    "words and I shall route it properly."
                )
            else:
                res = await default_registry.execute_tool_async(call["name"], call["arguments"])
                result_text = res.to_dict().get("result") or res.to_dict().get("error") or ""
        except Exception as exc:
            log.exception("native tool %s crashed: %s", call["name"], exc)
            result_text = _native_tool_error_reply(call["name"])
        executed.append((call["name"], str(result_text)))
        messages.append({
            "role": "tool",
            "tool_call_id": call["id"],
            "content": str(result_text)[:2000],
        })

    log.info("native tool round executed %d call(s): %s",
             len(executed), [n for n, _ in executed])

    # Final answer streams through the same event pipeline as a normal
    # chat (think/delta/end), with the tool context already sitting in
    # the transcript as role:"tool" messages.
    if not req.silent:
        bridge.new_session()
        bridge.post("thinking")
    yield _sse({"event": "start"})
    if not req.silent:
        bridge.post("working")

    think_filter = ThinkBlockFilter(expose=req.thinking, start_inside=False)
    tag_scrub = _TagScrubber()
    last_pet_ping = time.time()
    reasoning_parts: list[str] = []
    content_chars = 0
    try:
        async for kind, piece in server.stream_chat(messages=messages, **gen_kwargs):
            if kind == "reasoning":
                if req.thinking:
                    yield _sse({"event": "think", "content": piece})
                reasoning_parts.append(piece)
            elif kind == "tool_delta":
                continue  # shouldn't happen post-execution; ignore safely
            else:
                content_chars += len(piece.strip())
                piece = tag_scrub.feed(_RE_TOOL_TAG.sub("", piece))
                for ev in think_filter.feed(piece):
                    yield _sse(ev)
            now = time.time()
            if now - last_pet_ping > 6.0 and not req.silent:
                bridge.post("working")
                last_pet_ping = now
    except asyncio.CancelledError:
        if not req.silent:
            bridge.post("attention")
        raise
    except Exception as exc:
        get_logger().exception("native tool final stream error: %s", exc)
        yield _sse({"event": "error", "message": str(exc)})
        return
    else:
        # After tool results the MiniCPM chat template sometimes wraps the
        # ENTIRE answer in <think>...</think>; llama-server then reports it
        # as reasoning and, with thinking disabled, the reply would be an
        # empty bubble. Promote reasoning-only output to content instead.
        if content_chars == 0 and any(p.strip() for p in reasoning_parts):
            get_logger().info(
                "native tool round: answer arrived as reasoning only; "
                "promoting %d chars to content",
                sum(len(p) for p in reasoning_parts),
            )
            clean = re.sub(r"</?\s*think\s*>", "", "".join(reasoning_parts), flags=re.IGNORECASE)
            clean = tag_scrub.feed(_RE_TOOL_TAG.sub("", clean)) + tag_scrub.flush()
            if clean.strip():
                yield _sse({"event": "delta", "content": clean.strip()})
        elif content_chars == 0 and executed:
            # Model went silent after the tool ran (1B models do this,
            # especially after remember_fact). Never leave a blank bubble.
            done = tools.canned_reply(executed[0][0], executed[0][1])
            yield _sse({"event": "delta", "content": (done or "Done, sir.")})
    finally:
        tail = tag_scrub.flush()
        if tail:
            for ev in think_filter.feed(tail):
                yield _sse(ev)
        for ev in think_filter.flush():
            yield _sse(ev)
    yield _sse({"event": "end"})
    if not req.silent:
        bridge.post("attention")


