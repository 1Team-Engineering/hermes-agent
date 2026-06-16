"""Tests for the /compact between-task hook."""
import sqlite3
import pytest

from hermes_cli.kanban_db import (
    init_db, complete_task, upsert_session,
)


def _seed_task(conn, task_id, provider="claude-code-relay", assignee="friday",
               workspace_kind="scratch"):
    conn.execute("""
        INSERT INTO tasks (id, title, status, assignee, created_at,
                           workspace_kind, provider)
        VALUES (?, 'x', 'running', ?, 0, ?, ?)
    """, (task_id, assignee, workspace_kind, provider))
    conn.commit()


def test_compact_called_after_relay_task_completes(monkeypatch):
    called = []
    monkeypatch.setattr(
        "hermes_cli.kanban_db._run_relay_compact",
        lambda slug: called.append(slug),
    )
    conn = sqlite3.connect(":memory:")
    init_db(conn)
    _seed_task(conn, "t_test")
    upsert_session(conn, scope_slug="friday-scratch", profile="friday", project="scratch",
                   tmux_session="claude-friday-scratch", fifo_path="/tmp/f",
                   mcp_config_path="/tmp/m", project_root="/r", scope_cwd="/c")

    complete_task(conn, "t_test", summary="done", result="ok")

    assert "friday-scratch" in called


def test_compact_failure_does_not_block_completion(monkeypatch):
    def boom(slug): raise RuntimeError("compact crashed")
    monkeypatch.setattr("hermes_cli.kanban_db._run_relay_compact", boom)
    conn = sqlite3.connect(":memory:")
    init_db(conn)
    _seed_task(conn, "t_test2", assignee="cap")
    upsert_session(conn, scope_slug="cap-scratch", profile="cap", project="scratch",
                   tmux_session="t", fifo_path="/f", mcp_config_path="/m",
                   project_root="/r", scope_cwd="/c")

    # Should NOT raise (B10: /compact failures don't block next task)
    result = complete_task(conn, "t_test2", summary="done", result="ok")
    assert result is True


def test_compact_skipped_for_non_relay_provider(monkeypatch):
    called = []
    monkeypatch.setattr(
        "hermes_cli.kanban_db._run_relay_compact",
        lambda slug: called.append(slug),
    )
    conn = sqlite3.connect(":memory:")
    init_db(conn)
    _seed_task(conn, "t_test3", provider="anthropic")  # NOT relay
    complete_task(conn, "t_test3", summary="done", result="ok")
    assert called == []  # no compact for non-relay
