"""Self-tool forge — the agent writing, registering, and deploying its own tools.

When the agent installs or builds software and wants a reusable programmatic
interface to it, it authors a small Python module (exposing `run(args: dict)
-> str`), registers a tool schema in config.active_tools, and calls it on later
turns with call_tool. The module persists in mcp/, so the capability survives
reboots — the same intent as the SRD's "self-MCP", realized as in-process tools.

(A future extension can export these modules as stdio MCP servers; the registry
shape here — name + JSON-schema + code file — is deliberately MCP-compatible.)
"""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path

from . import constants as C, state

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,40}$")


def _module_path(name: str) -> Path:
    return C.MCP_DIR / f"{name}.py"


def create_tool(name: str, description: str, input_schema: dict, code: str) -> tuple[str, bool]:
    """Author + register a tool. `code` must define run(args: dict) -> str."""
    if not _NAME_RE.match(name or ""):
        return "Error: tool name must be lower_snake_case (letters/digits/underscore).", True
    if "def run(" not in code:
        return "Error: code must define `def run(args):` returning a string.", True
    try:
        compile(code, f"<tool:{name}>", "exec")
    except SyntaxError as exc:
        return f"Error: code has a syntax error: {exc}", True

    C.MCP_DIR.mkdir(parents=True, exist_ok=True)
    _module_path(name).write_text(code)

    def _register(cfg: dict) -> None:
        tools = [t for t in cfg.get("active_tools", []) if t.get("name") != name]
        tools.append({
            "name": name,
            "description": description,
            "input_schema": input_schema or {"type": "object", "properties": {}},
        })
        cfg["active_tools"] = tools

    state.update(_register)
    return f"Registered tool '{name}'. Call it with call_tool(name='{name}', args={{...}}).", False


def schemas(cfg: dict) -> list[dict]:
    """Anthropic tool definitions for the agent-authored tools, surfaced via call_tool."""
    return list(cfg.get("active_tools", []))


def call_tool(name: str, args: dict) -> tuple[str, bool]:
    path = _module_path(name)
    if not path.exists():
        return f"Error: no registered tool '{name}'.", True
    try:
        spec = importlib.util.spec_from_file_location(f"uaa_tool_{name}", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)  # fresh load each call -> picks up edits
        if not hasattr(module, "run"):
            return f"Error: tool '{name}' has no run() function.", True
        result = module.run(args or {})
        return str(result), False
    except Exception as exc:
        return f"Error running tool '{name}': {exc}", True
