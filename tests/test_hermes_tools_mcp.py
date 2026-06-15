"""Integration tests for the hermes-tools-mcp stdio server."""
import json
import os
import subprocess
import sys
from pathlib import Path


MCP_ENTRY = Path(__file__).resolve().parents[1] / "hermes_cli" / "mcp_servers" / "bin" / "hermes-tools-mcp"


def _send(line: str) -> dict:
    """Send a single JSON-RPC line, get the response."""
    env = {
        **os.environ,
        "HERMES_PROFILE": "test",
        "HERMES_PROJECT": "smoke",
        "HERMES_WORKSPACE": "/tmp",
    }
    proc = subprocess.run(
        [sys.executable, str(MCP_ENTRY)],
        input=line + "\n",
        capture_output=True,
        timeout=10,
        env=env,
        text=True,
    )
    out = proc.stdout.strip().splitlines()
    assert out, f"no stdout. stderr: {proc.stderr}"
    return json.loads(out[0])


def test_initialize_responds_with_protocol_version():
    msg = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "0"},
        },
    })
    resp = _send(msg)
    assert resp["jsonrpc"] == "2.0"
    assert resp["id"] == 1
    assert "result" in resp
    assert resp["result"]["protocolVersion"] == "2024-11-05"
    assert "tools" in resp["result"]["capabilities"]


def test_tools_list_returns_expected_tools():
    msg = json.dumps({
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/list",
        "params": {},
    })
    resp = _send(msg)
    assert "result" in resp, resp
    names = [t["name"] for t in resp["result"]["tools"]]
    for required in ("kanban_show", "kanban_complete", "read_file", "write_file"):
        assert required in names, f"{required} missing from tools/list: {names}"
    # Filter: orchestration-only tools should NOT be exposed
    assert "kanban_dispatch" not in names


def test_tools_list_includes_hermes_set_task():
    msg = json.dumps({"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}})
    resp = _send(msg)
    names = [t["name"] for t in resp["result"]["tools"]]
    assert "hermes_set_task" in names


def test_tools_call_hermes_set_task_persists_context(tmp_path):
    msg = json.dumps({
        "jsonrpc": "2.0",
        "id": 10,
        "method": "tools/call",
        "params": {
            "name": "hermes_set_task",
            "arguments": {"task_id": "t_abc12345", "workspace": str(tmp_path)},
        },
    })
    resp = _send(msg)
    assert "result" in resp, resp
    assert resp["result"]["isError"] is False


def test_tools_call_denied_tool_returns_error():
    msg = json.dumps({
        "jsonrpc": "2.0",
        "id": 11,
        "method": "tools/call",
        "params": {"name": "kanban_dispatch", "arguments": {}},
    })
    resp = _send(msg)
    assert "result" in resp
    assert resp["result"]["isError"] is True
