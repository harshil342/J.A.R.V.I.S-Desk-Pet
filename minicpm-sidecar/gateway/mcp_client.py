"""Model Context Protocol (MCP) Client Engine for DeskPet Jarvis.

Enables DeskPet to connect to external MCP servers via standard JSON-RPC 2.0
stdio transport, discover tools dynamically, and execute tools on demand.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .log_setup import get_logger
from .tool_registry import ToolRegistry, default_registry
from .tools import docs_dir

log = get_logger("mcp_client")

MCP_PROTOCOL_VERSION = "2024-11-05"


@dataclass
class MCPServerConfig:
    name: str
    command: str
    args: List[str] = field(default_factory=list)
    env: Optional[Dict[str, str]] = None
    cwd: Optional[str] = None
    enabled: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "command": self.command,
            "args": self.args,
            "env": self.env or {},
            "cwd": self.cwd or "",
            "enabled": self.enabled,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "MCPServerConfig":
        return cls(
            name=data.get("name", ""),
            command=data.get("command", ""),
            args=data.get("args") or [],
            env=data.get("env") or None,
            cwd=data.get("cwd") or None,
            enabled=data.get("enabled", True),
        )


class MCPServerProcess:
    """Manages an active MCP subprocess over stdio JSON-RPC 2.0."""

    def __init__(self, config: MCPServerConfig, registry: Optional[ToolRegistry] = None):
        self.config = config
        self.registry = registry or default_registry
        self.proc: Optional[asyncio.subprocess.Process] = None
        self._request_id = 0
        self._pending_requests: Dict[int, asyncio.Future] = {}
        self._reader_task: Optional[asyncio.Task] = None
        self._connected = False
        self._server_info: Dict[str, Any] = {}
        self._discovered_tools: List[Dict[str, Any]] = []

    @property
    def connected(self) -> bool:
        return self._connected and self.proc is not None and self.proc.returncode is None

    @property
    def server_info(self) -> Dict[str, Any]:
        return self._server_info

    @property
    def tools(self) -> List[Dict[str, Any]]:
        return self._discovered_tools

    async def start(self) -> bool:
        """Spawn the MCP server process, complete handshake, and list tools."""
        if self.connected:
            return True

        cmd = self.config.command
        # Resolve command path if needed (e.g. node, npx, python)
        full_cmd = shutil.which(cmd) or cmd
        args = [full_cmd] + self.config.args

        # Inherit system env and merge custom env
        spawn_env = os.environ.copy()
        if self.config.env:
            spawn_env.update(self.config.env)

        try:
            log.info("Starting MCP server '%s': %s", self.config.name, " ".join(args))
            self.proc = await asyncio.create_subprocess_exec(
                *args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=spawn_env,
                cwd=self.config.cwd or None,
            )

            # Start background stdout reader
            self._reader_task = asyncio.create_task(self._read_stdout())

            # Perform MCP Handshake
            ok = await self._initialize()
            if not ok:
                await self.stop()
                return False

            # Discover and register tools
            await self._discover_tools()
            self._connected = True
            log.info(
                "MCP server '%s' initialized successfully (%d tool(s) discovered)",
                self.config.name,
                len(self._discovered_tools),
            )
            return True

        except Exception as exc:
            log.error("Failed to start MCP server '%s': %s", self.config.name, exc)
            await self.stop()
            return False

    async def stop(self) -> None:
        """Terminate the server process and unregister its tools."""
        self._connected = False
        self.registry.unregister_server_tools(self.config.name)
        self._discovered_tools = []

        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass

        # Cancel any pending futures
        for req_id, fut in list(self._pending_requests.items()):
            if not fut.done():
                fut.set_exception(ConnectionError(f"MCP server '{self.config.name}' terminated."))
        self._pending_requests.clear()

        if self.proc:
            try:
                if self.proc.returncode is None:
                    if self.proc.stdin:
                        self.proc.stdin.close()
                    self.proc.terminate()
                    try:
                        await asyncio.wait_for(self.proc.wait(), timeout=3.0)
                    except asyncio.TimeoutError:
                        self.proc.kill()
            except Exception as e:
                log.warning("Error stopping MCP server '%s': %s", self.config.name, e)
            finally:
                self.proc = None

    async def _send_request(self, method: str, params: Optional[Dict[str, Any]] = None, timeout: float = 15.0) -> Dict[str, Any]:
        """Send JSON-RPC request and await response."""
        if not self.proc or self.proc.stdin is None or self.proc.returncode is not None:
            raise ConnectionError(f"MCP server '{self.config.name}' is not running.")

        self._request_id += 1
        req_id = self._request_id
        req_payload = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
            "params": params or {},
        }

        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        self._pending_requests[req_id] = fut

        line = json.dumps(req_payload) + "\n"
        self.proc.stdin.write(line.encode("utf-8"))
        await self.proc.stdin.drain()

        try:
            res = await asyncio.wait_for(fut, timeout=timeout)
            return res
        except asyncio.TimeoutError:
            self._pending_requests.pop(req_id, None)
            raise TimeoutError(f"MCP request '{method}' to '{self.config.name}' timed out after {timeout}s")

    async def _send_notification(self, method: str, params: Optional[Dict[str, Any]] = None) -> None:
        """Send JSON-RPC notification (no id, no reply expected)."""
        if not self.proc or self.proc.stdin is None or self.proc.returncode is not None:
            return
        notif_payload = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params or {},
        }
        line = json.dumps(notif_payload) + "\n"
        self.proc.stdin.write(line.encode("utf-8"))
        await self.proc.stdin.drain()

    async def _read_stdout(self) -> None:
        """Continuously read newline-delimited JSON-RPC messages from stdout."""
        if not self.proc or not self.proc.stdout:
            return

        while True:
            try:
                line = await self.proc.stdout.readline()
                if not line:
                    break  # EOF

                line_str = line.decode("utf-8", errors="replace").strip()
                if not line_str:
                    continue

                try:
                    msg = json.loads(line_str)
                except Exception:
                    # Skip non-JSON or debug log lines printed to stdout
                    continue

                req_id = msg.get("id")
                if req_id is not None and req_id in self._pending_requests:
                    fut = self._pending_requests.pop(req_id)
                    if not fut.done():
                        if "error" in msg:
                            err = msg["error"]
                            fut.set_exception(RuntimeError(f"MCP Error {err.get('code')}: {err.get('message')}"))
                        else:
                            fut.set_result(msg.get("result", {}))

            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error("Error reading stdout from MCP server '%s': %s", self.config.name, e)
                break

    async def _initialize(self) -> bool:
        """MCP handshake."""
        try:
            init_params = {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {
                    "name": "DeskPet-Jarvis",
                    "version": "1.0.0",
                },
            }
            res = await self._send_request("initialize", init_params, timeout=10.0)
            self._server_info = res.get("serverInfo", {})
            # Send initialized notification
            await self._send_notification("notifications/initialized")
            return True
        except Exception as e:
            log.error("MCP handshake failed with '%s': %s", self.config.name, e)
            return False

    async def _discover_tools(self) -> None:
        """Query tools/list and register tools into the global ToolRegistry."""
        try:
            res = await self._send_request("tools/list", {}, timeout=10.0)
            tools_list = res.get("tools", [])
            self._discovered_tools = tools_list

            # Clear old registrations from this server before re-adding
            self.registry.unregister_server_tools(self.config.name)

            for t in tools_list:
                t_name = t.get("name", "")
                t_desc = t.get("description", "")
                t_schema = t.get("inputSchema") or {"type": "object", "properties": {}, "required": []}

                # Build a dynamic async handler closure for this specific tool
                async def make_handler(tool_target_name: str):
                    async def handler(**kwargs) -> str:
                        return await self.call_tool(tool_target_name, kwargs)
                    return handler

                handler_fn = await make_handler(t_name)

                self.registry.register_mcp_tool(
                    server_name=self.config.name,
                    name=t_name,
                    description=t_desc,
                    parameters=t_schema,
                    handler=handler_fn,
                )

        except Exception as e:
            log.error("Failed to list tools from MCP server '%s': %s", self.config.name, e)

    async def call_tool(self, tool_name: str, arguments: Optional[Dict[str, Any]] = None) -> str:
        """Execute a tool via MCP tools/call."""
        if not self.connected:
            raise ConnectionError(f"MCP server '{self.config.name}' is not connected.")

        params = {
            "name": tool_name,
            "arguments": arguments or {},
        }
        res = await self._send_request("tools/call", params, timeout=30.0)

        # Handle tool response content
        is_error = res.get("isError", False)
        content_items = res.get("content", [])

        extracted: List[str] = []
        for item in content_items:
            if isinstance(item, dict):
                if item.get("type") == "text":
                    extracted.append(item.get("text", ""))
                elif item.get("type") == "image":
                    extracted.append("[Image output received]")
                elif item.get("type") == "resource":
                    extracted.append(f"[Resource: {item.get('resource', {}).get('uri', '')}]")
            else:
                extracted.append(str(item))

        result_text = "\n".join(extracted).strip() or "(empty response)"
        if is_error:
            return f"(MCP tool error: {result_text})"
        return result_text


class MCPManager:
    """Coordinates multiple MCP servers, handles persistence and auto-connection."""

    def __init__(self, registry: Optional[ToolRegistry] = None, config_path: Optional[Path] = None):
        self.registry = registry or default_registry
        self.config_path = config_path or (docs_dir() / "mcp_servers.json")
        self._servers: Dict[str, MCPServerProcess] = {}

    def get_server(self, name: str) -> Optional[MCPServerProcess]:
        return self._servers.get(name)

    def load_configs(self) -> List[MCPServerConfig]:
        """Read saved MCP server configs from disk."""
        if not self.config_path.exists():
            return []
        try:
            raw = self.config_path.read_text(encoding="utf-8")
            data = json.loads(raw)
            servers_data = data.get("mcpServers", data) if isinstance(data, dict) else []
            if isinstance(servers_data, dict):
                # Standard claude_desktop / antigravity format: {"server-name": {"command": ..., "args": ...}}
                configs = []
                for name, cfg in servers_data.items():
                    if isinstance(cfg, dict):
                        configs.append(MCPServerConfig(
                            name=name,
                            command=cfg.get("command", ""),
                            args=cfg.get("args") or [],
                            env=cfg.get("env"),
                            cwd=cfg.get("cwd"),
                            enabled=cfg.get("enabled", True),
                        ))
                return configs
            elif isinstance(servers_data, list):
                return [MCPServerConfig.from_dict(item) for item in servers_data if isinstance(item, dict)]
        except Exception as exc:
            log.error("Failed to read MCP config from %s: %s", self.config_path, exc)
        return []

    def save_configs(self, configs: List[MCPServerConfig]) -> None:
        """Write MCP configs back to disk in standard format."""
        try:
            mcp_dict = {
                "mcpServers": {
                    cfg.name: {
                        "command": cfg.command,
                        "args": cfg.args,
                        "env": cfg.env or {},
                        "cwd": cfg.cwd or "",
                        "enabled": cfg.enabled,
                    }
                    for cfg in configs
                }
            }
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            self.config_path.write_text(json.dumps(mcp_dict, indent=2), encoding="utf-8")
        except Exception as exc:
            log.error("Failed to save MCP config to %s: %s", self.config_path, exc)

    async def start_all(self) -> Dict[str, bool]:
        """Load configs and start all enabled servers."""
        configs = self.load_configs()
        results: Dict[str, bool] = {}
        for cfg in configs:
            if not cfg.enabled:
                continue
            proc = MCPServerProcess(cfg, registry=self.registry)
            self._servers[cfg.name] = proc
            ok = await proc.start()
            results[cfg.name] = ok
        return results

    async def stop_all(self) -> None:
        """Stop all running MCP servers."""
        for name, proc in list(self._servers.items()):
            await proc.stop()
        self._servers.clear()

    async def add_server(self, config: MCPServerConfig) -> bool:
        """Add and connect a new MCP server, updating persisted config."""
        if config.name in self._servers:
            await self.remove_server(config.name)

        configs = [c for c in self.load_configs() if c.name != config.name]
        configs.append(config)
        self.save_configs(configs)

        if config.enabled:
            proc = MCPServerProcess(config, registry=self.registry)
            self._servers[config.name] = proc
            return await proc.start()
        return True

    async def remove_server(self, name: str) -> bool:
        """Disconnect and remove an MCP server."""
        proc = self._servers.pop(name, None)
        if proc:
            await proc.stop()

        configs = [c for c in self.load_configs() if c.name != name]
        self.save_configs(configs)
        return True

    async def reload_server(self, name: str) -> bool:
        """Restart and refresh tools for an MCP server."""
        configs = {c.name: c for c in self.load_configs()}
        cfg = configs.get(name)
        if not cfg:
            return False

        proc = self._servers.get(name)
        if proc:
            await proc.stop()

        new_proc = MCPServerProcess(cfg, registry=self.registry)
        self._servers[name] = new_proc
        return await new_proc.start()

    def get_status_report(self) -> List[Dict[str, Any]]:
        """Return status and tool lists for all configured servers."""
        configs = self.load_configs()
        report: List[Dict[str, Any]] = []

        for cfg in configs:
            proc = self._servers.get(cfg.name)
            connected = proc.connected if proc else False
            server_info = proc.server_info if proc else {}
            tools = proc.tools if proc else []

            report.append({
                "name": cfg.name,
                "command": cfg.command,
                "args": cfg.args,
                "enabled": cfg.enabled,
                "connected": connected,
                "serverInfo": server_info,
                "toolCount": len(tools),
                "tools": tools,
            })
        return report


# Global default MCP manager instance
default_mcp_manager = MCPManager(registry=default_registry)
