"""Tests for v6.7 #30 — auto-spawn integrative architectural review at archive.

When a JARVIS goal-mode umbrella is archived, the dispatcher:
- Verifies all per-block reviews completed terminal
- Spawns a Tchalla integrative architectural review as a PEER task
  (intentionally NOT a child of the umbrella, since that would force
  it to ``todo`` and deadlock)
- Blocks the umbrella archive until that review completes with
  ``verdict: approve`` (matched as a strict line-anchored regex,
  not substring-anywhere)

Reject + re-spawn flow: a rejected review on a still-ready umbrella
triggers a fresh integrative review the next archive call (round 2,
title suffix ``:r2``).

See hermes-jarvis#61 for the bootstrap-paradox case study and
hermes-jarvis#30 for the original design.
"""
from __future__ import annotations

import time

import pytest

import hermes_cli.kanban_db as kb
from hermes_cli.kanban_db import (
    _INTEGRATIVE_REVIEW_TITLE_PREFIX,
    _v6_7_parse_verdict,
    archive_task,
)


@pytest.fixture
def board_conn(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "k.db"))
    conn = kb.connect(board="default")
    yield conn
    conn.close()


def _mk_task(
    conn,
    task_id: str,
    *,
    status: str = "running",
    assignee: str = "jarvis",
    goal_mode: bool = False,
    tenant: str = "marvel-swarm-v6-7-test",
    workspace_kind: str = "scratch",
    workspace_path=None,
    title: str = None,
) -> None:
    now = int(time.time())
    conn.execute(
        "INSERT INTO tasks (id, title, status, assignee, goal_mode, tenant, "
        "  created_at, workspace_kind, workspace_path) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            task_id, title or f"task {task_id}", status, assignee,
            1 if goal_mode else 0, tenant, now, workspace_kind,
            workspace_path,
        ),
    )


def _link(conn, parent: str, child: str) -> None:
    conn.execute(
        "INSERT INTO task_links (parent_id, child_id) VALUES (?, ?)",
        (parent, child),
    )


def _seed_canonical_umbrella(conn) -> str:
    """Build an umbrella in the canonical post-v6.6 shape:
    JARVIS goal-mode umbrella with a done build child (Friday) and
    done per-block reviews (Tony, Tchalla)."""
    _mk_task(conn, "t_umb", goal_mode=True, status="running")
    _mk_task(conn, "t_friday", assignee="friday", status="done")
    _mk_task(conn, "t_tony", assignee="tony", status="done")
    _mk_task(conn, "t_tchalla", assignee="tchalla", status="done")
    _link(conn, "t_umb", "t_friday")
    _link(conn, "t_umb", "t_tony")
    _link(conn, "t_umb", "t_tchalla")
    conn.commit()
    return "t_umb"


def _latest_integrative_review(conn, tenant="marvel-swarm-v6-7-test"):
    row = conn.execute(
        "SELECT id, status, title FROM tasks "
        " WHERE title LIKE ? AND tenant = ? "
        " ORDER BY created_at DESC LIMIT 1",
        (f"{_INTEGRATIVE_REVIEW_TITLE_PREFIX}%", tenant),
    ).fetchone()
    return row


# =====================================================================
# Verdict parser (strict line-anchored regex)
# =====================================================================


class TestVerdictParser:
    def test_simple_approve_line_matches(self) -> None:
        assert _v6_7_parse_verdict("verdict: approve\n") == "approve"

    def test_simple_reject_line_matches(self) -> None:
        assert _v6_7_parse_verdict("verdict: reject\n") == "reject"

    def test_case_insensitive(self) -> None:
        assert _v6_7_parse_verdict("Verdict: APPROVE\n") == "approve"

    def test_prose_mention_does_not_match(self) -> None:
        """Self-review #3: the old substring matcher false-approved
        on prose mentioning 'verdict: approve'."""
        assert _v6_7_parse_verdict(
            "After consideration my verdict: approve would be wrong because..."
        ) is None  # 'My verdict:' is after non-whitespace 'my', so no anchor

    def test_inline_followup_does_not_match(self) -> None:
        """``verdict: approve... but actually reject`` — strict regex
        only captures the first canonical line."""
        # The "approve" matches the regex (word-boundary after approve).
        # But the next sentence starts a new clause that wouldn't be
        # an anchored verdict line. First-match-wins.
        text = "verdict: approve\nthen actually verdict: reject for X"
        assert _v6_7_parse_verdict(text) == "approve"

    def test_reject_before_approve_wins(self) -> None:
        text = "verdict: reject\n\nverdict: approve"
        assert _v6_7_parse_verdict(text) == "reject"

    def test_no_verdict_line_returns_none(self) -> None:
        assert _v6_7_parse_verdict("just some prose, no verdict") is None

    def test_empty_returns_none(self) -> None:
        assert _v6_7_parse_verdict("") is None
        assert _v6_7_parse_verdict(None) is None


# =====================================================================
# Spawn trigger conditions
# =====================================================================


class TestSpawnTriggerConditions:
    def test_canonical_umbrella_spawns_review_as_peer(self, board_conn) -> None:
        """Happy path: spawn the integrative review, block archive,
        leave the umbrella running."""
        _seed_canonical_umbrella(board_conn)
        ok = archive_task(board_conn, "t_umb")
        assert ok is False
        rev = _latest_integrative_review(board_conn)
        assert rev is not None
        # Crucial assertion the prior PR missed: the spawned task is
        # in a CLAIMABLE state, not stuck in todo because of a parent
        # link. The peer-task design ensures `ready` here.
        assert rev["status"] == "ready"
        assert rev["title"] == _INTEGRATIVE_REVIEW_TITLE_PREFIX
        # Verify the review was NOT linked as a child of the umbrella
        link = board_conn.execute(
            "SELECT 1 FROM task_links WHERE parent_id = 't_umb' AND child_id = ?",
            (rev["id"],),
        ).fetchone()
        assert link is None

    def test_review_assignee_is_tchalla(self, board_conn) -> None:
        _seed_canonical_umbrella(board_conn)
        archive_task(board_conn, "t_umb")
        rev = _latest_integrative_review(board_conn)
        ass = board_conn.execute(
            "SELECT assignee FROM tasks WHERE id = ?", (rev["id"],)
        ).fetchone()
        assert ass["assignee"] == "tchalla"

    def test_non_goal_mode_archives_normally(self, board_conn) -> None:
        _mk_task(board_conn, "t_plain", goal_mode=False, status="done")
        board_conn.commit()
        assert archive_task(board_conn, "t_plain") is True

    def test_orphan_umbrella_archives(self, board_conn) -> None:
        _mk_task(board_conn, "t_orphan", goal_mode=True, status="done")
        board_conn.commit()
        assert archive_task(board_conn, "t_orphan") is True

    def test_review_only_umbrella_does_NOT_spawn(self, board_conn) -> None:
        """Self-review #5 fix: a chain with only review children (no
        actual build deliverable) doesn't warrant an integrative
        review."""
        _mk_task(board_conn, "t_revonly", goal_mode=True, status="running")
        _mk_task(board_conn, "t_tony2", assignee="tony", status="done")
        _link(board_conn, "t_revonly", "t_tony2")
        board_conn.commit()
        ok = archive_task(board_conn, "t_revonly")
        # Now archives normally because there's no non-review child to
        # integrate over.
        assert ok is True

    def test_umbrella_with_no_reviews_does_not_spawn(self, board_conn) -> None:
        _mk_task(board_conn, "t_noreviews", goal_mode=True, status="running")
        _mk_task(board_conn, "t_fr", assignee="friday", status="done")
        _link(board_conn, "t_noreviews", "t_fr")
        board_conn.commit()
        assert archive_task(board_conn, "t_noreviews") is True

    def test_in_flight_build_child_does_not_spawn(self, board_conn) -> None:
        _mk_task(board_conn, "t_inflight", goal_mode=True, status="running")
        _mk_task(board_conn, "t_fr_running", assignee="friday", status="running")
        _mk_task(board_conn, "t_tn_done", assignee="tony", status="done")
        _link(board_conn, "t_inflight", "t_fr_running")
        _link(board_conn, "t_inflight", "t_tn_done")
        board_conn.commit()
        # Friday's not done — archive succeeds without spawning a review
        # (the umbrella's state is the orchestrator's problem).
        ok = archive_task(board_conn, "t_inflight")
        assert ok is True

    def test_blocked_build_child_does_not_qualify_as_terminal(
        self, board_conn,
    ) -> None:
        """Self-review #7: a blocked Friday means work is INCOMPLETE,
        not done. The gate must not fire over a blocked deliverable."""
        _mk_task(board_conn, "t_blkbuild", goal_mode=True, status="running")
        _mk_task(board_conn, "t_fr_blocked", assignee="friday", status="blocked")
        _mk_task(board_conn, "t_tn_done", assignee="tony", status="done")
        _link(board_conn, "t_blkbuild", "t_fr_blocked")
        _link(board_conn, "t_blkbuild", "t_tn_done")
        board_conn.commit()
        # archive_task should pass through (no integrative review
        # spawned because Friday is blocked, not done).
        ok = archive_task(board_conn, "t_blkbuild")
        assert ok is True


# =====================================================================
# State machine + reject re-spawn (self-review #8)
# =====================================================================


class TestStateMachine:
    def test_no_double_spawn_on_in_flight_review(self, board_conn) -> None:
        _seed_canonical_umbrella(board_conn)
        archive_task(board_conn, "t_umb")
        # Move review to running (the dispatcher would do this on claim)
        rev = _latest_integrative_review(board_conn)
        board_conn.execute(
            "UPDATE tasks SET status = 'running' WHERE id = ?",
            (rev["id"],),
        )
        board_conn.commit()
        ok = archive_task(board_conn, "t_umb")
        assert ok is False
        count = board_conn.execute(
            "SELECT COUNT(*) AS c FROM tasks WHERE title LIKE ?",
            (f"{_INTEGRATIVE_REVIEW_TITLE_PREFIX}%",),
        ).fetchone()
        assert count["c"] == 1

    def test_approve_unblocks_archive(self, board_conn) -> None:
        _seed_canonical_umbrella(board_conn)
        archive_task(board_conn, "t_umb")
        rev = _latest_integrative_review(board_conn)
        board_conn.execute(
            "UPDATE tasks SET status = 'done', result = 'verdict: approve' "
            "WHERE id = ?",
            (rev["id"],),
        )
        board_conn.commit()
        ok = archive_task(board_conn, "t_umb")
        assert ok is True

    def test_reject_triggers_respawn_on_next_archive(self, board_conn) -> None:
        """Self-review #8 fix: a rejected review on a still-ready
        umbrella spawns a NEW review the next archive call. Old design
        permanently stalled the umbrella with no escape."""
        _seed_canonical_umbrella(board_conn)
        archive_task(board_conn, "t_umb")
        rev1 = _latest_integrative_review(board_conn)
        board_conn.execute(
            "UPDATE tasks SET status = 'done', "
            "  result = 'verdict: reject - performance issue at layer N' "
            "WHERE id = ?",
            (rev1["id"],),
        )
        board_conn.commit()
        # Next archive_task: rejected → spawn round 2
        ok = archive_task(board_conn, "t_umb")
        assert ok is False
        all_reviews = board_conn.execute(
            "SELECT id, title FROM tasks WHERE title LIKE ? "
            " ORDER BY created_at ASC",
            (f"{_INTEGRATIVE_REVIEW_TITLE_PREFIX}%",),
        ).fetchall()
        assert len(all_reviews) == 2
        assert all_reviews[0]["title"] == _INTEGRATIVE_REVIEW_TITLE_PREFIX
        assert all_reviews[1]["title"] == f"{_INTEGRATIVE_REVIEW_TITLE_PREFIX}:r2"

    def test_event_emitted_on_pending(self, board_conn) -> None:
        _seed_canonical_umbrella(board_conn)
        archive_task(board_conn, "t_umb")
        event = board_conn.execute(
            "SELECT kind FROM task_events WHERE task_id = 't_umb' "
            " ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert event["kind"] == "archive_blocked_pending_integrative_review"

    def test_event_emitted_on_respawn_includes_supersedes(self, board_conn) -> None:
        import json as _j
        _seed_canonical_umbrella(board_conn)
        archive_task(board_conn, "t_umb")
        rev1 = _latest_integrative_review(board_conn)
        board_conn.execute(
            "UPDATE tasks SET status = 'done', result = 'verdict: reject' "
            "WHERE id = ?",
            (rev1["id"],),
        )
        board_conn.commit()
        archive_task(board_conn, "t_umb")
        event = board_conn.execute(
            "SELECT kind, payload FROM task_events WHERE task_id = 't_umb' "
            " AND kind = 'archive_blocked_pending_integrative_review' "
            " ORDER BY id DESC LIMIT 1"
        ).fetchone()
        payload = _j.loads(event["payload"])
        assert payload["supersedes"] == rev1["id"]
        assert payload["supersedes_verdict"] == "reject"


# =====================================================================
# Spawned review body content
# =====================================================================


class TestSpawnedReviewBody:
    def test_body_includes_all_four_scope_items(self, board_conn) -> None:
        _seed_canonical_umbrella(board_conn)
        archive_task(board_conn, "t_umb")
        rev = _latest_integrative_review(board_conn)
        body = board_conn.execute(
            "SELECT body FROM tasks WHERE id = ?", (rev["id"],),
        ).fetchone()["body"]
        assert "End-to-end request trace" in body
        assert "End-to-end page render trace" in body
        assert "Adversarial enumeration" in body
        assert "Error-path audit" in body

    def test_body_includes_umbrella_id(self, board_conn) -> None:
        _seed_canonical_umbrella(board_conn)
        archive_task(board_conn, "t_umb")
        rev = _latest_integrative_review(board_conn)
        body = board_conn.execute(
            "SELECT body FROM tasks WHERE id = ?", (rev["id"],),
        ).fetchone()["body"]
        assert "t_umb" in body

    def test_body_documents_strict_verdict_format(self, board_conn) -> None:
        _seed_canonical_umbrella(board_conn)
        archive_task(board_conn, "t_umb")
        rev = _latest_integrative_review(board_conn)
        body = board_conn.execute(
            "SELECT body FROM tasks WHERE id = ?", (rev["id"],),
        ).fetchone()["body"]
        # Mention of the strict format
        assert "verdict: approve" in body
        assert "verdict: reject" in body

    def test_workspace_inherits_from_dir_umbrella(self, board_conn) -> None:
        _mk_task(
            board_conn, "t_umb_dir",
            goal_mode=True, status="running",
            workspace_kind="dir",
            workspace_path="/tmp/some-workspace",
        )
        _mk_task(board_conn, "t_fr2", assignee="friday", status="done")
        _mk_task(board_conn, "t_tn2", assignee="tony", status="done")
        _link(board_conn, "t_umb_dir", "t_fr2")
        _link(board_conn, "t_umb_dir", "t_tn2")
        board_conn.commit()
        archive_task(board_conn, "t_umb_dir")
        rev = _latest_integrative_review(board_conn)
        ws = board_conn.execute(
            "SELECT workspace_kind, workspace_path FROM tasks WHERE id = ?",
            (rev["id"],),
        ).fetchone()
        assert ws["workspace_kind"] == "dir"
        assert ws["workspace_path"] == "/tmp/some-workspace"


# =====================================================================
# Self-review #3: substring-anywhere verdict bypass closed
# =====================================================================


class TestVerdictBypassClosed:
    def test_prose_approve_does_not_unblock_archive(self, board_conn) -> None:
        """A reviewer who writes a paragraph mentioning 'verdict:
        approve' in prose (rather than the canonical line-anchored
        format) must NOT unblock the archive."""
        _seed_canonical_umbrella(board_conn)
        archive_task(board_conn, "t_umb")
        rev = _latest_integrative_review(board_conn)
        prose_result = (
            "After lengthy consideration I would say a verdict: approve "
            "would be premature because the perf issue at layer N is "
            "unresolved. So my actual verdict: reject."
        )
        board_conn.execute(
            "UPDATE tasks SET status = 'done', result = ? WHERE id = ?",
            (prose_result, rev["id"]),
        )
        board_conn.commit()
        # The line-anchored regex catches the second `verdict:` (or
        # the first if both are at line start). In this case, the
        # 'verdict: approve' appears mid-paragraph (not anchored at
        # newline+optional-whitespace), so the only match is the
        # actual reject. Archive stays blocked.
        ok = archive_task(board_conn, "t_umb")
        assert ok is False

    def test_canonical_approve_unblocks(self, board_conn) -> None:
        """Sanity check the strict regex DOES accept the canonical
        format the reviewers should be using."""
        _seed_canonical_umbrella(board_conn)
        archive_task(board_conn, "t_umb")
        rev = _latest_integrative_review(board_conn)
        board_conn.execute(
            "UPDATE tasks SET status = 'done', "
            "  result = 'verdict: approve\\n\\ntest_quality:\\n  imports_match: true' "
            "WHERE id = ?",
            (rev["id"],),
        )
        board_conn.commit()
        ok = archive_task(board_conn, "t_umb")
        assert ok is True
