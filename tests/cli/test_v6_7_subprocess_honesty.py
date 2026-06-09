"""Tests for v6.7 Part 3 — gateway/dispatcher subprocess-honesty fixes.

Closes:
- hermes-jarvis#33 (GH_TOKEN propagation at worker spawn)
- hermes-jarvis#34 (respawn_guarded active_pr exempts review tasks)
- hermes-jarvis#65 (fabricated github-auth block claims rejected)

See hermes-jarvis#61 for the bootstrap-paradox case study.
"""
from __future__ import annotations

import sqlite3
import subprocess
from typing import Any
from unittest.mock import patch

import pytest

import hermes_cli.kanban_db as kb
from hermes_cli.kanban_db import (
    FabricatedAuthClaimError,
    _dispatcher_gh_is_authed,
    _inject_gh_token_into_env,
    _reason_claims_missing_gh_auth,
    block_task,
    check_respawn_guard,
)


# =====================================================================
# #33 — _inject_gh_token_into_env
# =====================================================================


class TestInjectGhToken:
    def test_existing_gh_token_unchanged(self) -> None:
        env = {"GH_TOKEN": "preexisting-value", "PATH": "/usr/bin"}
        with patch("subprocess.run") as mock_run:
            _inject_gh_token_into_env(env)
        assert env["GH_TOKEN"] == "preexisting-value"
        mock_run.assert_not_called()  # short-circuit when GH_TOKEN set

    def test_existing_github_token_unchanged(self) -> None:
        env = {"GITHUB_TOKEN": "ghp_existing", "PATH": "/usr/bin"}
        with patch("subprocess.run") as mock_run:
            _inject_gh_token_into_env(env)
        assert "GH_TOKEN" not in env, "Don't double-write when GITHUB_TOKEN exists"
        mock_run.assert_not_called()

    def test_injects_when_gh_authed_and_env_clean(self) -> None:
        env: dict[str, str] = {"PATH": "/usr/bin"}
        fake = subprocess.CompletedProcess(
            args=["gh", "auth", "token"], returncode=0, stdout="gho_fake_token\n",
            stderr="",
        )
        with patch("shutil.which", return_value="/usr/local/bin/gh"), \
             patch("subprocess.run", return_value=fake):
            _inject_gh_token_into_env(env)
        assert env["GH_TOKEN"] == "gho_fake_token"

    def test_no_inject_when_gh_not_installed(self) -> None:
        env: dict[str, str] = {"PATH": "/usr/bin"}
        with patch("shutil.which", return_value=None):
            _inject_gh_token_into_env(env)
        assert "GH_TOKEN" not in env

    def test_no_inject_when_gh_returns_nonzero(self) -> None:
        env: dict[str, str] = {"PATH": "/usr/bin"}
        fake = subprocess.CompletedProcess(
            args=["gh", "auth", "token"], returncode=1, stdout="", stderr="not logged in",
        )
        with patch("shutil.which", return_value="/usr/local/bin/gh"), \
             patch("subprocess.run", return_value=fake):
            _inject_gh_token_into_env(env)
        assert "GH_TOKEN" not in env

    def test_no_inject_when_gh_returns_empty_token(self) -> None:
        env: dict[str, str] = {"PATH": "/usr/bin"}
        fake = subprocess.CompletedProcess(
            args=["gh", "auth", "token"], returncode=0, stdout="\n", stderr="",
        )
        with patch("shutil.which", return_value="/usr/local/bin/gh"), \
             patch("subprocess.run", return_value=fake):
            _inject_gh_token_into_env(env)
        assert "GH_TOKEN" not in env

    def test_timeout_is_silent_no_op(self) -> None:
        env: dict[str, str] = {"PATH": "/usr/bin"}
        with patch("shutil.which", return_value="/usr/local/bin/gh"), \
             patch(
                 "subprocess.run",
                 side_effect=subprocess.TimeoutExpired(cmd="gh", timeout=5),
             ):
            _inject_gh_token_into_env(env)  # must not raise
        assert "GH_TOKEN" not in env


# =====================================================================
# #34 — check_respawn_guard exempts review roles
# =====================================================================


@pytest.fixture
def board_conn(tmp_path, monkeypatch):
    """A minimal initialized kanban DB on disk."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "k.db"))
    conn = kb.connect(board="default")
    yield conn
    conn.close()


def _seed_task_with_pr_comment(conn, *, assignee: str, task_id: str) -> None:
    """Create a task and stamp a recent comment containing a PR URL."""
    import time
    now = int(time.time())
    conn.execute(
        "INSERT INTO tasks (id, title, status, assignee, created_at, "
        "  workspace_kind, workspace_path) "
        "VALUES (?, ?, 'ready', ?, ?, 'scratch', NULL)",
        (task_id, "Re-review Block C", assignee, now),
    )
    conn.execute(
        "INSERT INTO task_comments (task_id, author, body, created_at) "
        "VALUES (?, 'kaipo', ?, ?)",
        (
            task_id,
            "Captured `gh pr diff 1` from host shell. "
            "Proceed with verdict using "
            "https://github.com/1Team-Engineering/agent-dashboard/pull/1 as the diff source.",
            now,
        ),
    )
    conn.commit()


class TestRespawnGuardExemption:
    def test_tchalla_with_pr_url_in_comment_not_guarded(self, board_conn) -> None:
        """The 2026-06-07 incident: Tchalla re-review blocked 18 ticks on
        the active_pr guard because Kaipo's unblock comment included the
        PR URL as legitimate evidence."""
        _seed_task_with_pr_comment(board_conn, assignee="tchalla", task_id="t_67a")
        assert check_respawn_guard(board_conn, "t_67a") is None

    def test_tony_review_with_pr_url_not_guarded(self, board_conn) -> None:
        _seed_task_with_pr_comment(board_conn, assignee="tony", task_id="t_67b")
        assert check_respawn_guard(board_conn, "t_67b") is None

    def test_vision_review_with_pr_url_not_guarded(self, board_conn) -> None:
        _seed_task_with_pr_comment(board_conn, assignee="vision", task_id="t_67c")
        assert check_respawn_guard(board_conn, "t_67c") is None

    def test_non_review_role_with_pr_url_still_guarded(self, board_conn) -> None:
        """JARVIS / Friday / Pepper with a PR URL in comments DO still
        trigger the guard — the exemption is narrowly scoped to reviewers
        whose entire job is to operate on PRs."""
        _seed_task_with_pr_comment(board_conn, assignee="jarvis", task_id="t_67d")
        assert check_respawn_guard(board_conn, "t_67d") == "active_pr"

    def test_friday_with_pr_url_still_guarded(self, board_conn) -> None:
        _seed_task_with_pr_comment(board_conn, assignee="friday", task_id="t_67e")
        assert check_respawn_guard(board_conn, "t_67e") == "active_pr"

    def test_review_role_without_pr_url_returns_none(self, board_conn) -> None:
        """No comments → no guard regardless of role."""
        import time
        now = int(time.time())
        board_conn.execute(
            "INSERT INTO tasks (id, title, status, assignee, created_at, "
            "  workspace_kind, workspace_path) "
            "VALUES (?, ?, 'ready', 'tony', ?, 'scratch', NULL)",
            ("t_67f", "Clean code review", now),
        )
        board_conn.commit()
        assert check_respawn_guard(board_conn, "t_67f") is None


# =====================================================================
# #65 — block_task rejects fabricated github-auth claims
# =====================================================================


class TestReasonClaimsMissingGhAuth:
    """Pure-function checks on the claim-matcher (no DB needed)."""

    def test_missing_github_auth_phrase_matches(self) -> None:
        assert _reason_claims_missing_gh_auth(
            "missing-github-auth: cannot run gh pr diff"
        ) is not None

    def test_gh_auth_login_required_matches(self) -> None:
        assert _reason_claims_missing_gh_auth(
            "infra: gh auth login required (worker subprocess can't see token)"
        ) is not None

    def test_GITHUB_TOKEN_required_matches(self) -> None:
        assert _reason_claims_missing_gh_auth(
            "GITHUB_TOKEN required to verify PR"
        ) is not None

    def test_gh_CLI_not_authenticated_matches(self) -> None:
        assert _reason_claims_missing_gh_auth(
            "gh CLI is not authenticated; cannot proceed"
        ) is not None

    def test_unrelated_reason_does_not_match(self) -> None:
        assert _reason_claims_missing_gh_auth(
            "remediation: still need Friday to update integration test"
        ) is None

    def test_empty_reason_returns_none(self) -> None:
        assert _reason_claims_missing_gh_auth(None) is None
        assert _reason_claims_missing_gh_auth("") is None


@pytest.fixture
def running_task_board(tmp_path, monkeypatch):
    """A board with a single running task ready to be blocked."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "k.db"))
    conn = kb.connect(board="default")
    import time
    now = int(time.time())
    conn.execute(
        "INSERT INTO tasks (id, title, status, assignee, created_at, "
        "  workspace_kind, workspace_path) "
        "VALUES ('t_b1', 'Block C re-review', 'running', 'tchalla', ?, "
        "        'scratch', NULL)",
        (now,),
    )
    conn.commit()
    yield conn
    conn.close()


class TestBlockGate:
    def test_fabricated_claim_rejected_when_dispatcher_is_authed(
        self, running_task_board,
    ) -> None:
        """The 2026-06-09 Tchalla case: blocked with
        'gh CLI not authenticated; cannot run gh pr diff 42'
        when the dispatcher's shell IS authed (and PR 42 doesn't exist).
        Gate should reject the block so the worker has to surface the
        real cause."""
        with patch(
            "hermes_cli.kanban_db._dispatcher_gh_is_authed",
            return_value=True,
        ), pytest.raises(FabricatedAuthClaimError):
            block_task(
                running_task_board, "t_b1",
                reason="missing-github-auth: cannot run gh pr diff 42",
            )
        # Task state is unchanged
        row = running_task_board.execute(
            "SELECT status FROM tasks WHERE id = 't_b1'"
        ).fetchone()
        assert row["status"] == "running"

    def test_genuine_claim_accepted_when_dispatcher_not_authed(
        self, running_task_board,
    ) -> None:
        """When the dispatcher ALSO can't reach gh, the worker's claim
        is plausible (genuine subprocess auth gap from #33); we accept
        and block normally."""
        with patch(
            "hermes_cli.kanban_db._dispatcher_gh_is_authed",
            return_value=False,
        ):
            ok = block_task(
                running_task_board, "t_b1",
                reason="missing-github-auth: cannot run gh pr diff 42",
            )
        assert ok
        row = running_task_board.execute(
            "SELECT status FROM tasks WHERE id = 't_b1'"
        ).fetchone()
        assert row["status"] == "blocked"

    def test_non_auth_claim_block_passes_through(self, running_task_board) -> None:
        """A block reason that doesn't claim auth doesn't trip the gate."""
        with patch(
            "hermes_cli.kanban_db._dispatcher_gh_is_authed",
            return_value=True,
        ):
            ok = block_task(
                running_task_board, "t_b1",
                reason="remediation: need Friday to fix the integration test "
                       "before this re-review can proceed",
            )
        assert ok

    def test_empty_reason_skips_gate(self, running_task_board) -> None:
        """``reason=None`` is rare but valid (e.g. orchestrator-initiated
        block) and the gate must not crash on it."""
        with patch(
            "hermes_cli.kanban_db._dispatcher_gh_is_authed",
            return_value=True,
        ):
            ok = block_task(running_task_board, "t_b1", reason=None)
        assert ok

    def test_event_recorded_on_gate_rejection(self, running_task_board) -> None:
        with patch(
            "hermes_cli.kanban_db._dispatcher_gh_is_authed",
            return_value=True,
        ), pytest.raises(FabricatedAuthClaimError):
            block_task(
                running_task_board, "t_b1",
                reason="gh auth login required for this worker",
            )
        events = running_task_board.execute(
            "SELECT kind FROM task_events WHERE task_id = 't_b1' "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert events is not None
        assert events["kind"] == "block_blocked_fabricated_auth_claim"
