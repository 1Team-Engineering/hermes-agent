"""Pure-function helpers for the claude-code-relay provider."""
from __future__ import annotations

import os
import subprocess


SCRATCH_WORKSPACES_ROOT = os.path.expanduser("~/.hermes/kanban/workspaces")


class ProviderError(Exception):
    """Raised when the relay provider cannot proceed (config/binary missing)."""


def derive_project(workspace_path: str) -> str:
    """Map a kanban task workspace to its project key.

    Returns:
      - "scratch" for kanban scratch workspaces
      - basename(git rev-parse --show-toplevel) for git workspaces
    Raises:
      ProviderError if workspace isn't in a git repo and isn't a scratch path.
    """
    if workspace_path.startswith(SCRATCH_WORKSPACES_ROOT):
        return "scratch"
    real = os.path.realpath(workspace_path)
    try:
        root = subprocess.check_output(
            ["git", "-C", real, "rev-parse", "--show-toplevel"],
            stderr=subprocess.DEVNULL,
        ).decode().strip()
        return os.path.basename(root)
    except subprocess.CalledProcessError:
        raise ProviderError(
            f"Cannot derive project for {workspace_path}: not in a git repo "
            f"and not a scratch workspace. Place workspace in git repo or pass "
            f"--project explicitly."
        )


def derive_project_root(workspace_path: str) -> str:
    """Compute the --add-dir target for the relay scope."""
    if workspace_path.startswith(SCRATCH_WORKSPACES_ROOT):
        return SCRATCH_WORKSPACES_ROOT
    real = os.path.realpath(workspace_path)
    return subprocess.check_output(
        ["git", "-C", real, "rev-parse", "--show-toplevel"],
        stderr=subprocess.DEVNULL,
    ).decode().strip()


def build_task_header(*, task_id: str, workspace: str) -> str:
    """Prepend a one-line context header so claude knows the current task."""
    return f"[task_id={task_id} workspace={workspace}]\n"
