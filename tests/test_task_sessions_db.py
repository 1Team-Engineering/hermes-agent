"""Tests for the task_sessions table + helpers."""
import sqlite3
import time

import pytest

from hermes_cli.kanban_db import (
    init_db,
    upsert_session,
    get_session,
    touch_session,
    mark_compacted,
    evict_session,
    list_idle_sessions,
)


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    init_db(c)
    yield c
    c.close()


def test_upsert_and_get(conn):
    upsert_session(conn, scope_slug="friday-coachvision",
                   profile="friday", project="coachvision",
                   tmux_session="claude-friday-coachvision",
                   fifo_path="/tmp/fifo", mcp_config_path="/tmp/mcp.json",
                   project_root="/repo", scope_cwd="/tmp/cwd")
    row = get_session(conn, "friday-coachvision")
    assert row is not None
    assert row["profile"] == "friday"
    assert row["project"] == "coachvision"
    assert row["task_count"] == 0


def test_touch_increments_task_count(conn):
    upsert_session(conn, scope_slug="cap-hermes-agent",
                   profile="cap", project="hermes-agent",
                   tmux_session="t", fifo_path="/tmp/f", mcp_config_path="/tmp/m",
                   project_root="/r", scope_cwd="/c")
    touch_session(conn, "cap-hermes-agent")
    touch_session(conn, "cap-hermes-agent")
    row = get_session(conn, "cap-hermes-agent")
    assert row["task_count"] == 2


def test_mark_compacted_sets_timestamp(conn):
    upsert_session(conn, scope_slug="x-y", profile="x", project="y",
                   tmux_session="t", fifo_path="/f", mcp_config_path="/m",
                   project_root="/r", scope_cwd="/c")
    assert get_session(conn, "x-y")["last_compacted_at"] is None
    mark_compacted(conn, "x-y")
    assert get_session(conn, "x-y")["last_compacted_at"] is not None


def test_evict_removes_row(conn):
    upsert_session(conn, scope_slug="e-x", profile="e", project="x",
                   tmux_session="t", fifo_path="/tmp/f", mcp_config_path="/tmp/m",
                   project_root="/r", scope_cwd="/c")
    evict_session(conn, "e-x")
    assert get_session(conn, "e-x") is None


def test_list_idle_finds_old_sessions(conn):
    upsert_session(conn, scope_slug="old-x", profile="o", project="x",
                   tmux_session="t", fifo_path="/tmp/f", mcp_config_path="/tmp/m",
                   project_root="/r", scope_cwd="/c")
    conn.execute("UPDATE task_sessions SET last_used_at = 1 WHERE scope_slug = 'old-x'")
    conn.commit()
    idle = list_idle_sessions(conn, threshold_secs=100)
    assert any(r["scope_slug"] == "old-x" for r in idle)
