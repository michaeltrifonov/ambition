"""MCP server registry + client.

The agent extends itself with real MCP servers: it installs/obtains one however
it likes (npm/pip, download, sign up + get a token), registers it by adding an
entry to config.mcp_servers (via request_mutation) and reboots. On boot the
MCPManager connects to every enabled server, lists its tools, and surfaces them
to the model namespaced as `<server>__<tool>`; tool calls are dispatched here.

A registry entry looks like:
    {"name": "playwright", "transport": "stdio",
     "command": "npx", "args": ["-y", "@playwright/mcp@latest"], "env": {...},
     "enabled": true}
or, for a hosted server:
    {"name": "linear", "transport": "http",
     "url": "https://mcp.linear.app/mcp", "authorization_token": "...",
     "enabled": true}

All connections live on one background asyncio loop in a single task, so the
async context managers are entered and exited in the same task (avoids the
anyio cross-task cancel-scope pitfall). Degrades to a no-op if the `mcp`
package isn't installed or a server fails to connect (logged, others continue).
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import os
import threading

from . import constants as C
from .log import audit, get_logger

log = get_logger("uaa.mcp", C.AGENT_LOG)

CONNECT_TIMEOUT_S = 60
CALL_TIMEOUT_S = 180


class MCPManager:
    def __init__(self, servers: list[dict] | None, heartbeat=None) -> None:
        seen: set[str] = set()
        deduped: list[dict] = []
        for s in (servers or []):
            name = s.get("name")
            if name and name not in seen and s.get("enabled", True):
                seen.add(name)
                deduped.append(s)
        self.servers = deduped
        self._heartbeat = heartbeat            # optional callable, pinged during startup
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._queue: asyncio.Queue | None = None
        self._tools: list[dict] = []           # anthropic tool schemas
        self._index: dict[str, tuple] = {}      # namespaced name -> (session, tool_name)
        self._connected: set[str] = set()       # server names that connected
        self._failed: dict[str, str] = {}       # server name -> failure reason
        self._ready = threading.Event()

    # ---- lifecycle ----
    def start(self) -> None:
        if not self.servers:
            self._ready.set()
            return
        try:
            import mcp  # noqa: F401
        except Exception:
            log.warning("`mcp` package not installed; %d MCP server(s) disabled", len(self.servers))
            for s in self.servers:
                self._failed[s["name"]] = "the `mcp` python package is not installed"
            self._ready.set()
            return
        self._thread = threading.Thread(target=self._thread_main, daemon=True)
        self._thread.start()
        self._ready.wait(timeout=CONNECT_TIMEOUT_S * max(1, len(self.servers)) + 30)

    def _thread_main(self) -> None:
        try:
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            self._loop.run_until_complete(self._serve())
        except Exception as exc:
            log.warning("MCP manager loop ended: %s", exc)
        finally:
            self._ready.set()

    async def _serve(self) -> None:
        from contextlib import AsyncExitStack

        self._queue = asyncio.Queue()
        async with AsyncExitStack() as stack:
            for s in self.servers:
                try:
                    await self._connect(stack, s)
                except Exception as exc:
                    self._failed[s.get("name", "?")] = str(exc)
                    log.warning("MCP server '%s' failed to connect: %s", s.get("name"), exc)
                    audit("mcp_connect_failed", server=s.get("name"), error=str(exc))
                if self._heartbeat:  # keep the daemon's hang monitor happy during slow startups
                    try:
                        self._heartbeat()
                    except Exception:
                        pass
            self._ready.set()  # tools (whatever connected) are now available
            while True:
                item = await self._queue.get()
                if item is None:
                    break
                name, args, fut = item
                try:
                    result = await self._do_call(name, args)
                    if not fut.done():
                        fut.set_result(result)
                except Exception as exc:
                    if not fut.done():
                        fut.set_result((f"Error calling {name}: {exc}", True))

    async def _connect(self, stack, s: dict) -> None:
        from mcp import ClientSession

        name = s["name"]
        transport = s.get("transport", "stdio")
        if transport == "stdio":
            from mcp import StdioServerParameters
            from mcp.client.stdio import stdio_client

            # Inherit the agent's full environment (PATH/HOME/...) and add the entry's
            # overrides — otherwise a registered `npx`/`uvx` command can't be resolved
            # and a token-only env dict would strip PATH entirely.
            env = {**os.environ, **(s.get("env") or {})}
            params = StdioServerParameters(
                command=s["command"], args=s.get("args", []), env=env
            )
            read, write = await stack.enter_async_context(stdio_client(params))
        else:  # "http" / streamable-http
            from mcp.client.streamable_http import streamablehttp_client

            headers = dict(s.get("headers") or {})
            if s.get("authorization_token"):
                headers["Authorization"] = f"Bearer {s['authorization_token']}"
            conn = await stack.enter_async_context(streamablehttp_client(s["url"], headers=headers))
            read, write = conn[0], conn[1]

        session = await stack.enter_async_context(ClientSession(read, write))
        await asyncio.wait_for(session.initialize(), timeout=CONNECT_TIMEOUT_S)
        listed = await asyncio.wait_for(session.list_tools(), timeout=CONNECT_TIMEOUT_S)
        for t in listed.tools:
            ns = f"{name}__{t.name}"[:64]
            if ns in self._index:  # truncation collision — disambiguate instead of overwriting
                base, i = ns[:60], 1
                while f"{base}_{i}" in self._index:
                    i += 1
                ns = f"{base}_{i}"
            self._tools.append({
                "name": ns,
                "description": (t.description or f"{t.name} (via {name})")[:1000],
                "input_schema": t.inputSchema or {"type": "object", "properties": {}},
            })
            self._index[ns] = (session, t.name)
        self._connected.add(name)
        log.info("MCP '%s' connected: %d tool(s)", name, len(listed.tools))
        audit("mcp_connected", server=name, tools=len(listed.tools))

    async def _do_call(self, ns: str, args: dict) -> tuple[str, bool]:
        entry = self._index.get(ns)
        if not entry:
            return f"Error: MCP tool '{ns}' not found.", True
        session, tool = entry
        res = await asyncio.wait_for(session.call_tool(tool, args or {}), timeout=CALL_TIMEOUT_S)
        parts = []
        for block in getattr(res, "content", None) or []:
            if getattr(block, "type", None) == "text":
                parts.append(getattr(block, "text", ""))
            else:
                parts.append(str(getattr(block, "data", block)))
        text = "\n".join(p for p in parts if p) or "(no output)"
        return text, bool(getattr(res, "isError", False))

    # ---- sync interface for the agent loop ----
    def tool_schemas(self) -> list[dict]:
        return list(self._tools)

    def status(self) -> dict:
        """Which registered servers connected vs. failed (and why) — surfaced to the agent."""
        return {"connected": sorted(self._connected), "failed": dict(self._failed)}

    def is_mcp_tool(self, name: str) -> bool:
        return name in self._index

    def call(self, name: str, args: dict) -> tuple[str, bool]:
        if self._loop is None or not self.is_mcp_tool(name):
            return f"Error: MCP tool '{name}' not available.", True
        fut: concurrent.futures.Future = concurrent.futures.Future()
        try:
            self._loop.call_soon_threadsafe(self._queue.put_nowait, (name, args, fut))
            return fut.result(timeout=CALL_TIMEOUT_S + 10)
        except Exception as exc:
            return f"Error calling {name}: {exc}", True

    def shutdown(self) -> None:
        if self._loop is None or self._queue is None:
            return
        try:  # tell the serve loop to exit -> AsyncExitStack closes all sessions/subprocesses
            self._loop.call_soon_threadsafe(self._queue.put_nowait, None)
        except Exception:
            pass
        if self._thread:
            self._thread.join(timeout=15)
