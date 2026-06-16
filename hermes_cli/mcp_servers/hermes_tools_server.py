"""hermes-tools-mcp — stdio JSON-RPC MCP server exposing Hermes tools.

Bound to a single (profile, project) scope via env vars:
  HERMES_PROFILE   — set at scope spawn
  HERMES_PROJECT   — set at scope spawn
  HERMES_WORKSPACE — current task workspace (may be updated per-turn via
                     hermes_set_task tool)

Registry API used (verified 2026-06-15):
  discover_builtin_tools()          — module-level fn, imports tool modules
  registry.get_all_tool_names()     — sorted list of all registered names
  registry.get_entry(name)          — ToolEntry with .schema, .description, .is_async
  registry.get_schema(name)         — raw schema dict (has 'name', 'description', 'parameters')
  registry.dispatch(name, args)     — executes tool, bridges async, returns str
"""
from __future__ import annotations

import json
import logging
import os
import sys
from typing import Any

log = logging.getLogger("hermes-tools-mcp")
PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "hermes-tools", "version": "0.1.0"}

# ---------------------------------------------------------------------------
# Tool surface filter
# ---------------------------------------------------------------------------

# Tools exposed to claude-code sessions.  Matches tools that actually exist
# in the Hermes built-in registry (verified by running discover_builtin_tools).
ALLOWED_TOOLS: set[str] = {
    "kanban_show",
    "kanban_create",
    "kanban_complete",
    "kanban_comment",
    "kanban_block",
    "kanban_list",
    "read_file",
    "write_file",
    "patch",
    "terminal",
    "memory",       # actual memory tool name in registry (gbrain_memory_* don't exist)
    "web_search",
    "hermes_set_task",  # special — updates per-turn task context
}

# Orchestration-only tools explicitly denied (belt + suspenders).
DENIED_TOOLS: set[str] = {
    "kanban_dispatch",
    "kanban_swarm",
    "kanban_research_loop",
    "delegate_task",
    "mixture_of_agents",
}

# ---------------------------------------------------------------------------
# Per-process task context (updated via hermes_set_task)
# ---------------------------------------------------------------------------

_TASK_CONTEXT: dict[str, str] = {
    "task_id": "",
    "workspace": os.environ.get("HERMES_WORKSPACE", ""),
}


# ---------------------------------------------------------------------------
# JSON-RPC helpers
# ---------------------------------------------------------------------------

def _make_response(req_id: Any, result: dict | None = None, error: dict | None = None) -> str:
    body: dict[str, Any] = {"jsonrpc": "2.0", "id": req_id}
    if error is not None:
        body["error"] = error
    else:
        body["result"] = result
    return json.dumps(body)


def _tool_result(text: str, is_error: bool = False) -> dict:
    return {
        "content": [{"type": "text", "text": text}],
        "isError": is_error,
    }


# ---------------------------------------------------------------------------
# Registry helpers
# ---------------------------------------------------------------------------

def _load_tool_registry():
    """Lazy-import and initialize the Hermes tool registry."""
    from tools.registry import discover_builtin_tools, registry  # type: ignore
    if not registry.get_all_tool_names():
        discover_builtin_tools()
    return registry


# ---------------------------------------------------------------------------
# hermes_set_task schema (synthetic tool not in Hermes registry)
# ---------------------------------------------------------------------------

def _hermes_set_task_schema() -> dict:
    return {
        "name": "hermes_set_task",
        "description": (
            "Update the current task context (task_id + workspace) the MCP server "
            "uses to enrich tool calls. Called automatically by the relay at each "
            "new kanban turn; workers can also call to switch context within a turn."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "workspace": {"type": "string"},
            },
            "required": ["task_id", "workspace"],
        },
    }


# ---------------------------------------------------------------------------
# Handler: initialize
# ---------------------------------------------------------------------------

def handle_initialize(req: dict) -> str:
    return _make_response(req.get("id"), result={
        "protocolVersion": PROTOCOL_VERSION,
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": SERVER_INFO,
    })


# ---------------------------------------------------------------------------
# Handler: tools/list  [Task 6]
# ---------------------------------------------------------------------------

def handle_tools_list(req: dict) -> str:
    registry = _load_tool_registry()
    tools = [_hermes_set_task_schema()]

    for tool_name in registry.get_all_tool_names():
        if tool_name in DENIED_TOOLS:
            continue
        if tool_name not in ALLOWED_TOOLS:
            continue
        entry = registry.get_entry(tool_name)
        if entry is None:
            continue
        # entry.schema has keys: name, description, parameters
        # MCP expects inputSchema, not parameters
        raw_schema = entry.schema
        tools.append({
            "name": tool_name,
            "description": entry.description or raw_schema.get("description", ""),
            "inputSchema": raw_schema.get("parameters", {"type": "object"}),
        })

    return _make_response(req.get("id"), result={"tools": tools})


# ---------------------------------------------------------------------------
# Handler: tools/call  [Task 7]
# ---------------------------------------------------------------------------

def _call_hermes_set_task(args: dict) -> dict:
    task_id = args.get("task_id", "").strip()
    workspace = args.get("workspace", "").strip()
    if not task_id or not workspace:
        return _tool_result("hermes_set_task requires task_id + workspace", is_error=True)
    _TASK_CONTEXT["task_id"] = task_id
    _TASK_CONTEXT["workspace"] = workspace
    return _tool_result(f"task context set: task_id={task_id} workspace={workspace}")


def _call_registered_tool(name: str, args: dict) -> dict:
    registry = _load_tool_registry()
    entry = registry.get_entry(name)
    if entry is None:
        return _tool_result(f"tool not found: {name}", is_error=True)

    # registry.dispatch handles both sync and async tools, and returns a string.
    try:
        result = registry.dispatch(name, args)
        return _tool_result(result if isinstance(result, str) else json.dumps(result))
    except Exception as e:
        log.exception("tool %s raised", name)
        return _tool_result(f"tool {name} raised: {e!r}", is_error=True)


def handle_tools_call(req: dict) -> str:
    params = req.get("params", {})
    name = params.get("name", "")
    args = params.get("arguments", {}) or {}

    if name == "hermes_set_task":
        result = _call_hermes_set_task(args)
    elif name in DENIED_TOOLS or name not in ALLOWED_TOOLS:
        result = _tool_result(f"tool denied: {name}", is_error=True)
    else:
        result = _call_registered_tool(name, args)

    return _make_response(req.get("id"), result=result)


# ---------------------------------------------------------------------------
# Message dispatcher
# ---------------------------------------------------------------------------

def handle_message(msg: dict) -> str | None:
    method = msg.get("method")
    if method == "initialize":
        return handle_initialize(msg)
    if method == "notifications/initialized":
        return None  # notification, no response
    if method == "tools/list":
        return handle_tools_list(msg)
    if method == "tools/call":
        return handle_tools_call(msg)
    return _make_response(msg.get("id"), error={
        "code": -32601,
        "message": f"Method not found: {method}",
    })


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    logging.basicConfig(
        level=os.environ.get("HERMES_MCP_LOG_LEVEL", "WARNING"),
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )
    log.info(
        "boot profile=%s project=%s workspace=%s",
        os.environ.get("HERMES_PROFILE"),
        os.environ.get("HERMES_PROJECT"),
        os.environ.get("HERMES_WORKSPACE"),
    )
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError as e:
            log.error("invalid json: %s", e)
            continue
        resp = handle_message(msg)
        if resp is not None:
            sys.stdout.write(resp + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
