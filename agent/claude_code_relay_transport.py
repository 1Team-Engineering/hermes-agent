"""Transport for the claude-code-relay provider.

Drives the existing scripts/tmux-relay/* infrastructure as subprocesses,
maintains task_sessions DB rows for restart recovery, returns OpenAI-compat
ChatCompletion responses to upstream Hermes agent loops.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

_SCOPE_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")

from agent.claude_code_relay_helpers import (
    ProviderError,
    derive_project_root,
    build_task_header,
)
from hermes_cli.kanban_db import (
    get_session,
    upsert_session,
    touch_session,
)

RELAY_BIN = Path(os.environ.get(
    "HERMES_RELAY_BIN",
    str(Path.home() / ".hermes" / "scripts" / "tmux-relay" / "bin"),
))
MCP_ENTRY = Path(os.environ.get(
    "HERMES_TOOLS_MCP_ENTRY",
    str(Path.home() / ".hermes" / "hermes-agent" / "hermes_cli" / "mcp_servers" / "bin" / "hermes-tools-mcp"),
))


@dataclass
class ScopeContext:
    profile: str
    project: str
    workspace: str

    def __post_init__(self) -> None:
        for field_name, value in (("profile", self.profile), ("project", self.project)):
            if not _SCOPE_NAME_RE.match(value):
                raise ProviderError(
                    f"invalid {field_name} {value!r}: must match ^[a-zA-Z0-9_-]+$ "
                    f"(B8: scope-name collision / path-traversal guard)"
                )

    @property
    def slug(self) -> str:
        return f"{self.profile}-{self.project}"

    @property
    def tmux_session(self) -> str:
        return f"claude-{self.slug}"


def tmux_session_alive(name: str) -> bool:
    r = subprocess.run(["tmux", "has-session", "-t", name],
                       capture_output=True)
    return r.returncode == 0


def _write_mcp_config(ctx: ScopeContext) -> Path:
    path = Path(f"/tmp/hermes-mcp-{ctx.slug}.json")
    config = {
        "hermes": {
            "command": str(MCP_ENTRY),
            "args": [],
            "env": {
                "HERMES_PROFILE": ctx.profile,
                "HERMES_PROJECT": ctx.project,
                "HERMES_WORKSPACE": ctx.workspace,
            },
        }
    }
    path.write_text(json.dumps(config))
    os.chmod(path, 0o600)  # B7-adjacent: don't leak workspace paths to other users on /tmp
    return path


def _check_stop_hook(path: str | None = None) -> None:
    """Verify the Hermes Stop hook is installed in claude settings.

    B3: Stop hook is the per-turn end signal. Without it, relay-send.sh
    hangs waiting for FIFO writes that never happen. Fail loud at boot
    instead of silently timing out per turn.
    """
    path = path or os.path.expanduser("~/.claude/settings.json")
    if not os.path.exists(path):
        raise ProviderError(
            f"claude settings.json not found at {path}. "
            "Run scripts/tmux-relay/install-hook.sh to install the Stop hook."
        )
    with open(path) as f:
        data = json.load(f)
    hooks = data.get("hooks", {}).get("Stop", [])
    found = False
    for stop in hooks:
        for h in (stop.get("hooks") or []):
            cmd = h.get("command", "")
            if "HERMES_RELAY_FIFO" in cmd:
                found = True
                break
        if found:
            break
    if not found:
        raise ProviderError(
            "claude settings.json missing the Hermes Stop hook. "
            "Run scripts/tmux-relay/install-hook.sh to install it."
        )


def _check_prereqs() -> None:
    """Fail loud if claude/tmux are missing (B2)."""
    for b in ("claude", "tmux"):
        if shutil.which(b) is None:
            raise ProviderError(f"required binary not found: {b}")
    if not (RELAY_BIN / "relay-spawn-scope.sh").exists():
        raise ProviderError(f"relay scripts missing at {RELAY_BIN}")
    _check_stop_hook()  # B3: Stop hook required for FIFO turn-end signalling


def ensure_scope(conn, ctx: ScopeContext) -> None:
    """Make sure tmux+claude is alive for this scope. Spawns if missing."""
    _check_prereqs()
    if tmux_session_alive(ctx.tmux_session):
        if get_session(conn, ctx.slug) is None:
            upsert_session(
                conn, scope_slug=ctx.slug, profile=ctx.profile, project=ctx.project,
                tmux_session=ctx.tmux_session, fifo_path="",
                mcp_config_path="", project_root=ctx.workspace, scope_cwd="",
            )
        return

    mcp_config = _write_mcp_config(ctx)
    project_root = derive_project_root(ctx.workspace)
    env = os.environ.copy()
    env["HERMES_PROJECT_ROOT"] = project_root

    r = subprocess.run(
        [str(RELAY_BIN / "relay-spawn-scope.sh"),
         ctx.profile, ctx.project, str(mcp_config)],
        env=env, capture_output=True, text=True, timeout=60,
    )
    if r.returncode != 0:
        raise ProviderError(f"relay-spawn-scope.sh failed: {r.stderr or r.stdout}")

    scope_cwd = str(Path.home() / ".hermes" / "relay-workspaces" / ctx.slug)
    fifo_path = f"/tmp/claude-relay-{ctx.slug}.fifo"

    upsert_session(
        conn, scope_slug=ctx.slug, profile=ctx.profile, project=ctx.project,
        tmux_session=ctx.tmux_session, fifo_path=fifo_path,
        mcp_config_path=str(mcp_config), project_root=project_root,
        scope_cwd=scope_cwd,
    )


def send_turn(conn, ctx: ScopeContext, *, task_id: str, user_text: str,
              timeout: int = 300) -> str:
    """Send a single user turn through the relay, return the response text."""
    ensure_scope(conn, ctx)
    header = build_task_header(task_id=task_id, workspace=ctx.workspace)
    prompt = header + user_text

    r = subprocess.run(
        [str(RELAY_BIN / "relay-send.sh"), ctx.slug, prompt],
        capture_output=True, text=True, timeout=timeout,
    )
    if r.returncode == 4:
        log.warning(
            "relay-send returned exit 4 (Stop-hook timeout) for scope %s — "
            "response may be partial", ctx.slug,
        )
    elif r.returncode != 0:
        raise ProviderError(f"relay-send.sh exit {r.returncode}: {r.stderr}")
    touch_session(conn, ctx.slug)
    return r.stdout


def chat_completion(conn, *, messages, model="sonnet",
                    profile: str, project: str, workspace: str,
                    task_id: str, timeout: int = 300) -> dict:
    """OpenAI-compat ChatCompletion response shape.

    Strategy: only forward the latest user message — prior conversation
    lives in claude's own session memory (preserved across turns within
    the scope). The task header carries the per-turn context.
    """
    ctx = ScopeContext(profile=profile, project=project, workspace=workspace)
    user_text = next(
        (m["content"] for m in reversed(messages) if m.get("role") == "user"),
        "",
    )
    response_text = send_turn(
        conn, ctx, task_id=task_id, user_text=user_text, timeout=timeout,
    )
    now = int(time.time())
    return {
        "id": f"chatcmpl-relay-{ctx.slug}-{now}",
        "object": "chat.completion",
        "created": now,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": response_text},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": None,
            "completion_tokens": None,
            "total_tokens": None,
        },
    }
