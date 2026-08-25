"""Tool Registry and Schema Engine for DeskPet Jarvis.

Provides OpenAI/MCP JSON-Schema compatible definitions for all native
micro-tools, typed argument execution via safe_tool_call, and dynamic
registration of tools from external MCP servers.
"""

from __future__ import annotations

import inspect
import json
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, List, Optional, Union

# Tools the 1B model must never see in its schema and must never invoke
# natively: a hallucinated call would execute IMMEDIATELY with no
# confirmation (a stray "who is X" once locked the workstation). These
# stay reachable through the deterministic keyword router, which only
# fires on explicit user phrasing, and via /api/tools/call.
MODEL_EXCLUDED_TOOLS = frozenset({"lock_workstation"})

from . import tools
from .log_setup import get_logger

log = get_logger("tool_registry")


def _filter_known_kwargs(name: str, handler: Callable, args: Dict[str, Any]) -> Dict[str, Any]:
    """Drop model-invented kwargs the handler does not declare.

    Small models sometimes emit extra fields (e.g. ``max_results``) that
    are not in the tool schema; passing them through raises TypeError and
    fails the whole call. Unknown keys are silently ignored instead.
    """
    if not args:
        return args
    try:
        sig = inspect.signature(handler)
    except (TypeError, ValueError):
        return args
    params = sig.parameters
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return args
    allowed = {
        param_name
        for param_name, p in params.items()
        if p.kind
        in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
    }
    dropped = set(args) - allowed
    if not dropped:
        return args
    log.info("tool %s: dropping unknown argument(s) %s", name, sorted(dropped))
    return {k: v for k, v in args.items() if k in allowed}


@dataclass
class ToolDefinition:
    name: str
    description: str
    parameters: Dict[str, Any]
    source: str = "native"  # "native" or mcp server name
    handler: Optional[Callable[..., Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
            "source": self.source,
        }

    def to_openai_schema(self) -> Dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


@dataclass
class ToolCallResult:
    name: str
    success: bool
    result: str
    source: str = "native"
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "success": self.success,
            "result": self.result,
            "source": self.source,
            "error": self.error,
        }


class ToolRegistry:
    """Central registry for all native micro-tools and dynamic MCP tools."""

    def __init__(self):
        self._tools: Dict[str, ToolDefinition] = {}
        self._register_native_tools()

    def _register_native_tools(self) -> None:
        """Register all built-in micro-tools with formal JSON schemas."""

        self.register_native(
            name="get_weather",
            description="Get the current weather and forecast for a given city or the local area.",
            parameters={
                "type": "object",
                "properties": {
                    "city": {
                        "type": "string",
                        "description": "City name, e.g. 'London', 'Tokyo', 'New York'. If omitted, detects location.",
                    }
                },
                "required": [],
            },
            handler=tools.get_weather,
        )

        self.register_native(
            name="get_time",
            description="Get the current local date, time, and day of the week.",
            parameters={
                "type": "object",
                "properties": {},
                "required": [],
            },
            handler=tools.get_time,
        )

        self.register_native(
            name="web_search",
            description="Search the web using DuckDuckGo instant answers for facts, summaries, or quick lookups.",
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query or topic to look up.",
                    }
                },
                "required": ["query"],
            },
            handler=tools.web_search,
        )

        self.register_native(
            name="convert_currency",
            description="Convert monetary amounts between currencies with live exchange rates.",
            parameters={
                "type": "object",
                "properties": {
                    "amount": {
                        "type": "number",
                        "description": "The numeric amount to convert.",
                    },
                    "base": {
                        "type": "string",
                        "description": "Source 3-letter currency code (e.g. 'USD', 'EUR', 'INR', 'GBP').",
                    },
                    "target": {
                        "type": "string",
                        "description": "Target 3-letter currency code (e.g. 'EUR', 'INR', 'JPY').",
                    },
                },
                "required": ["amount", "base", "target"],
            },
            handler=tools.convert_currency,
        )

        self.register_native(
            name="calculate",
            description="Perform exact, safe arithmetic evaluation and mathematical calculations.",
            parameters={
                "type": "object",
                "properties": {
                    "expr": {
                        "type": "string",
                        "description": "Arithmetic expression, e.g. '24 * 1.15', 'sqrt(144)', '500 / 4'.",
                    }
                },
                "required": ["expr"],
            },
            handler=tools.calculate,
        )

        self.register_native(
            name="convert_units",
            description="Convert physical measurements between units (length, weight, temperature, data, speed).",
            parameters={
                "type": "object",
                "properties": {
                    "amount": {
                        "type": "number",
                        "description": "The quantity to convert.",
                    },
                    "from_u": {
                        "type": "string",
                        "description": "Source unit, e.g. 'km', 'miles', 'kg', 'lbs', 'celsius', 'fahrenheit', 'gb'.",
                    },
                    "to_u": {
                        "type": "string",
                        "description": "Target unit, e.g. 'miles', 'km', 'lbs', 'kg', 'fahrenheit', 'celsius', 'mb'.",
                    },
                },
                "required": ["amount", "from_u", "to_u"],
            },
            handler=tools.convert_units,
        )

        self.register_native(
            name="launch_app",
            description="Launch a desktop application or protocol on the host machine.",
            parameters={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Application name or alias (e.g. 'notepad', 'calc', 'chrome', 'vscode', 'spotify').",
                    }
                },
                "required": ["name"],
            },
            handler=tools.launch_app,
        )

        self.register_native(
            name="create_document",
            description="Draft a structured document template (meeting notes, README, video script, changelog, todo list, email) saved to Documents/DeskPet.",
            parameters={
                "type": "object",
                "properties": {
                    "doc_type": {
                        "type": "string",
                        "enum": ["meeting_notes", "readme", "video_script", "changelog", "todo_list", "email"],
                        "description": "Type of document to create.",
                    },
                    "topic": {
                        "type": "string",
                        "description": "Subject or title of the document.",
                    },
                },
                "required": ["doc_type", "topic"],
            },
            handler=tools.create_document,
        )

        self.register_native(
            name="set_reminder",
            description="Set a local timer/reminder that will ping the desktop pet when expired.",
            parameters={
                "type": "object",
                "properties": {
                    "minutes": {
                        "type": "number",
                        "description": "Time in minutes from now.",
                    },
                    "message": {
                        "type": "string",
                        "description": "Reminder text / reminder description.",
                    },
                },
                "required": ["minutes", "message"],
            },
            handler=tools.set_reminder,
        )

        self.register_native(
            name="todo_add",
            description="Add an item to the user's persistent to-do list (todo.md).",
            parameters={
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "Task description to add.",
                    }
                },
                "required": ["text"],
            },
            handler=tools.todo_add,
        )

        self.register_native(
            name="todo_list",
            description="Read and list active and completed items from the user's to-do list.",
            parameters={
                "type": "object",
                "properties": {},
                "required": [],
            },
            handler=tools.todo_list,
        )

        self.register_native(
            name="todo_done",
            description="Mark a specific task on the to-do list as completed.",
            parameters={
                "type": "object",
                "properties": {
                    "item": {
                        "type": "string",
                        "description": "Task text or keywords matching the item to mark done.",
                    }
                },
                "required": ["item"],
            },
            handler=tools.todo_done,
        )

        self.register_native(
            name="todo_remove",
            description="Remove a specific task from the to-do list.",
            parameters={
                "type": "object",
                "properties": {
                    "item": {
                        "type": "string",
                        "description": "Task text or keywords matching the item to remove.",
                    }
                },
                "required": ["item"],
            },
            handler=tools.todo_remove,
        )

        self.register_native(
            name="todo_clear",
            description="Clear all items from the to-do list.",
            parameters={
                "type": "object",
                "properties": {},
                "required": [],
            },
            handler=tools.todo_clear,
        )

        self.register_native(
            name="system_status",
            description="Check host system performance: CPU usage, RAM utilization, battery level, disk space, and uptime.",
            parameters={
                "type": "object",
                "properties": {},
                "required": [],
            },
            handler=tools.system_status,
        )

        self.register_native(
            name="clipboard_assist",
            description="Read the system clipboard text and perform an action on it (show, summarize, rewrite, translate).",
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["show", "summarize", "rewrite", "translate"],
                        "description": "Action to perform on the clipboard contents.",
                    }
                },
                "required": ["action"],
            },
            handler=tools.clipboard_assist,
        )

        self.register_native(
            name="fetch_page",
            description="Fetch a web page by URL, strip HTML tags, and return the cleaned text content.",
            parameters={
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "The HTTP/HTTPS URL to fetch.",
                    }
                },
                "required": ["url"],
            },
            handler=tools.fetch_page,
        )

        self.register_native(
            name="wikipedia_summary",
            description="Look up a summary article from Wikipedia for a concept, person, or term.",
            parameters={
                "type": "object",
                "properties": {
                    "term": {
                        "type": "string",
                        "description": "The topic or term to look up.",
                    }
                },
                "required": ["term"],
            },
            handler=tools.wikipedia_summary,
        )

        self.register_native(
            name="remember_fact",
            description="Save a personal fact, note, or piece of information into the persistent memory file (notes.md).",
            parameters={
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "Fact or note to remember.",
                    }
                },
                "required": ["text"],
            },
            handler=tools.remember_fact,
        )

        self.register_native(
            name="recall_fact",
            description="Search and recall saved facts or notes from persistent memory (notes.md).",
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Keyword or topic to search for in saved memory. If empty, returns recent facts.",
                    }
                },
                "required": [],
            },
            handler=tools.recall_fact,
        )

        self.register_native(
            name="open_url",
            description="Open a web address in the user's default web browser.",
            parameters={
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "Web URL to open.",
                    }
                },
                "required": ["url"],
            },
            handler=tools.open_url,
        )

        self.register_native(
            name="media_control",
            description="Control media playback and audio volume (volume_up, volume_down, mute, play_pause, next, prev).",
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["volume_up", "volume_down", "mute", "play_pause", "next", "prev"],
                        "description": "Media key action to trigger.",
                    }
                },
                "required": ["action"],
            },
            handler=tools.media_control,
        )

        self.register_native(
            name="take_screenshot",
            description="Capture a full screenshot of the desktop and save it to Documents/DeskPet.",
            parameters={
                "type": "object",
                "properties": {},
                "required": [],
            },
            handler=tools.take_screenshot,
        )

        self.register_native(
            name="lock_workstation",
            description="Lock the computer workstation.",
            parameters={
                "type": "object",
                "properties": {},
                "required": [],
            },
            handler=tools.lock_workstation,
        )

        from . import screen_context

        self.register_native(
            name="get_active_window",
            description="Get the foreground application name, window title, and active file currently focused by the user.",
            parameters={
                "type": "object",
                "properties": {},
                "required": [],
            },
            handler=screen_context.get_active_window,
        )

        self.register_native(
            name="read_screen_text",
            description="Extract and read visible text lines from the active desktop screen using local OCR.",
            parameters={
                "type": "object",
                "properties": {
                    "region": {
                        "type": "string",
                        "enum": ["screen", "window"],
                        "description": "Region to capture text from ('screen' or 'window').",
                    }
                },
                "required": [],
            },
            handler=screen_context.extract_screen_text,
        )

        self.register_native(
            name="inspect_screen",
            description="Inspect the user's current screen context, combining active window metadata and visible OCR text.",
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Optional specific question or element to look for on screen.",
                    }
                },
                "required": [],
            },
            handler=screen_context.inspect_screen,
        )

        self.register_native(
            name="get_running_apps",
            description="List major developer and productivity applications currently running on the system.",
            parameters={
                "type": "object",
                "properties": {},
                "required": [],
            },
            handler=screen_context.get_running_apps_summary,
        )

        from . import task_dispatcher

        self.register_native(
            name="schedule_task",
            description="Schedule a proactive background timer or reminder to trigger after a delay in seconds.",
            parameters={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Name or title of the reminder/task.",
                    },
                    "delay_seconds": {
                        "type": "number",
                        "description": "Seconds to wait before triggering the proactive notification.",
                    },
                    "payload": {
                        "type": "string",
                        "description": "Optional details or message to deliver when triggered.",
                    },
                },
                "required": ["name", "delay_seconds"],
            },
            handler=task_dispatcher.schedule_task_tool,
        )

    def register_native(
        self,
        name: str,
        description: str,
        parameters: Dict[str, Any],
        handler: Callable[..., Any],
    ) -> None:
        self._tools[name] = ToolDefinition(
            name=name,
            description=description,
            parameters=parameters,
            source="native",
            handler=handler,
        )

    def register_mcp_tool(
        self,
        server_name: str,
        name: str,
        description: str,
        parameters: Dict[str, Any],
        handler: Callable[..., Any],
    ) -> None:
        scoped_name = f"mcp_{server_name}_{name}" if not name.startswith("mcp_") else name
        self._tools[scoped_name] = ToolDefinition(
            name=scoped_name,
            description=f"[{server_name}] {description}",
            parameters=parameters or {"type": "object", "properties": {}, "required": []},
            source=server_name,
            handler=handler,
        )
        log.info("Registered MCP tool: %s from server '%s'", scoped_name, server_name)

    def unregister_server_tools(self, server_name: str) -> int:
        to_remove = [k for k, v in self._tools.items() if v.source == server_name]
        for k in to_remove:
            del self._tools[k]
        log.info("Unregistered %d tool(s) from server '%s'", len(to_remove), server_name)
        return len(to_remove)

    def get_tool(self, name: str) -> Optional[ToolDefinition]:
        return self._tools.get(name)

    def list_tools(self, source: Optional[str] = None) -> List[ToolDefinition]:
        if source:
            return [t for t in self._tools.values() if t.source == source]
        return list(self._tools.values())

    def get_tools_catalog(self) -> List[Dict[str, Any]]:
        return [t.to_dict() for t in self._tools.values()]

    def get_openai_schemas(self) -> List[Dict[str, Any]]:
        return [
            t.to_openai_schema()
            for t in self._tools.values()
            if t.name not in MODEL_EXCLUDED_TOOLS
        ]

    async def execute_tool_async(
        self, name: str, arguments: Optional[Dict[str, Any]] = None
    ) -> ToolCallResult:
        """Execute a tool by name with arguments (supports sync and async handlers)."""
        tool = self.get_tool(name)
        if not tool:
            return ToolCallResult(
                name=name,
                success=False,
                result="",
                source="unknown",
                error=f"Tool '{name}' is not registered.",
            )

        handler = tool.handler
        if not handler:
            return ToolCallResult(
                name=name,
                success=False,
                result="",
                source=tool.source,
                error=f"Tool '{name}' has no callable handler.",
            )
        args = _filter_known_kwargs(name, handler, arguments or {})
        try:
            if inspect.iscoroutinefunction(handler):
                result = await handler(**args)
            else:
                ok, res_str = tools.safe_tool_call(handler, **args)
                if not ok:
                    return ToolCallResult(
                        name=name,
                        success=False,
                        result=res_str,
                        source=tool.source,
                        error=res_str,
                    )
                result = res_str

            return ToolCallResult(
                name=name,
                success=True,
                result=str(result),
                source=tool.source,
                error=None,
            )
        except TypeError as te:
            err_msg = f"Invalid arguments for tool '{name}': {te}"
            log.warning("%s (provided: %r)", err_msg, args)
            return ToolCallResult(
                name=name,
                success=False,
                result="",
                source=tool.source,
                error=err_msg,
            )
        except Exception as exc:
            err_msg = f"Tool '{name}' failed with error: {exc}"
            log.exception(err_msg)
            return ToolCallResult(
                name=name,
                success=False,
                result="",
                source=tool.source,
                error=err_msg,
            )

    def execute_tool_sync(
        self, name: str, arguments: Optional[Dict[str, Any]] = None
    ) -> ToolCallResult:
        """Synchronous tool execution wrapper (for sync endpoints)."""
        import asyncio

        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor() as pool:
                    return pool.submit(
                        asyncio.run, self.execute_tool_async(name, arguments)
                    ).result()
            return loop.run_until_complete(self.execute_tool_async(name, arguments))
        except RuntimeError:
            return asyncio.run(self.execute_tool_async(name, arguments))


# Global default registry instance
default_registry = ToolRegistry()
