import sqlite3
import pytest
from hermes_cli.kanban_db import init_db, upsert_session, get_session
from hermes_cli.relay_eviction import evict_idle_scopes


def test_idle_scope_evicted(monkeypatch):
    class R:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr("hermes_cli.relay_eviction.subprocess.run",
                        lambda *a, **kw: R())
    conn = sqlite3.connect(":memory:")
    init_db(conn)
    upsert_session(conn, scope_slug="old", profile="x", project="y",
                   tmux_session="t", fifo_path="/f", mcp_config_path="/m",
                   project_root="/r", scope_cwd="/c")
    conn.execute("UPDATE task_sessions SET last_used_at = 1 WHERE scope_slug = 'old'")
    conn.commit()

    evicted = evict_idle_scopes(conn, threshold_secs=10)
    assert "old" in evicted
    assert get_session(conn, "old") is None


def test_idle_eviction_leaves_db_row_when_kill_fails(monkeypatch):
    class R:
        returncode = 1
        stdout = ""
        stderr = "tmux err"

    monkeypatch.setattr("hermes_cli.relay_eviction.subprocess.run",
                        lambda *a, **kw: R())
    conn = sqlite3.connect(":memory:")
    init_db(conn)
    upsert_session(conn, scope_slug="ghost", profile="x", project="y",
                   tmux_session="t", fifo_path="/f", mcp_config_path="/m",
                   project_root="/r", scope_cwd="/c")
    conn.execute("UPDATE task_sessions SET last_used_at = 1 WHERE scope_slug = 'ghost'")
    conn.commit()

    evicted = evict_idle_scopes(conn, threshold_secs=10)
    assert evicted == []
    assert get_session(conn, "ghost") is not None  # row preserved for retry


def test_threshold_zero_is_disabled():
    conn = sqlite3.connect(":memory:")
    init_db(conn)
    evicted = evict_idle_scopes(conn, threshold_secs=0)
    assert evicted == []
