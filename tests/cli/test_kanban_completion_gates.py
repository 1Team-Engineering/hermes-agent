"""Tests for hermes_cli.kanban_completion_gates — v6.7 Tranche 1.

Closes hermes-jarvis#62 (workspace-diff verification), #28 (repo hygiene
gate), and #64 (per-role runtime floor). See hermes-jarvis#61 for the
bootstrap-paradox case study where a v6.7 swarm build chain rubber-stamped
9 tasks done in ~10 minutes with zero real deliverables.

Each gate is a pure function and gets a focused test that pins the exact
failure modes the 2026-06-09 chain demonstrated.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from hermes_cli.kanban_completion_gates import (
    DocDriftViolation,
    MissingReviewerFieldViolation,
    PhantomPRViolation,
    RuntimeFloorViolation,
    StrayArtifactViolation,
    WorkspaceDiffViolation,
    verify_doc_drift,
    verify_no_stray_artifacts,
    verify_pr_urls_exist,
    verify_reviewer_fields,
    verify_runtime_floor,
    verify_workspace_diff,
)


# =====================================================================
# verify_runtime_floor — #64
# =====================================================================


class TestRuntimeFloor:
    def test_tony_20s_review_is_below_floor(self) -> None:
        """The exact case from 2026-06-09: Tony approved Wave A in 20s."""
        v = verify_runtime_floor("tony", started_at=1000, completed_at=1020)
        assert isinstance(v, RuntimeFloorViolation)
        assert v.actual_seconds == 20
        assert v.floor_seconds == 90
        assert "tony" in v.message().lower()
        assert "below" in v.message().lower()

    def test_friday_59s_implementation_is_below_floor(self) -> None:
        """Friday claimed 7 dispatcher gates implemented in 59s."""
        v = verify_runtime_floor("friday", started_at=1000, completed_at=1059)
        assert isinstance(v, RuntimeFloorViolation)
        assert v.floor_seconds == 300

    def test_tony_91s_review_passes(self) -> None:
        """One second above the floor is a pass — the floor is the floor."""
        assert verify_runtime_floor("tony", 1000, 1091) is None

    def test_jarvis_orchestration_has_no_floor(self) -> None:
        """Orchestration roles routinely complete in seconds and that's fine."""
        assert verify_runtime_floor("jarvis", 1000, 1001) is None

    def test_unknown_assignee_skips(self) -> None:
        """Don't invent floors for roles we haven't categorized."""
        assert verify_runtime_floor("rando-profile", 1000, 1001) is None

    def test_missing_assignee_skips(self) -> None:
        assert verify_runtime_floor(None, 1000, 1001) is None

    def test_missing_started_at_skips(self) -> None:
        """If the dispatcher never recorded started_at the gate can't fire."""
        assert verify_runtime_floor("tony", None, 1100) is None

    def test_allow_below_floor_opt_out(self) -> None:
        """Workers can justify fast completions via metadata."""
        assert (
            verify_runtime_floor("tony", 1000, 1020, allow_below_floor=True)
            is None
        )

    def test_completed_before_started_is_zero(self) -> None:
        """Clock skew / wrong order doesn't crash — actual=0, still below floor."""
        v = verify_runtime_floor("tony", 1100, 1000)
        assert v is not None
        assert v.actual_seconds == 0

    def test_case_insensitive_role_match(self) -> None:
        """Profile names sometimes capitalize differently — match insensitively."""
        v = verify_runtime_floor("Tony", 1000, 1020)
        assert v is not None

    # === hermes-jarvis#74 — honest-reject bypass ===

    def test_honest_reject_bypasses_floor_for_tony(self) -> None:
        """The exact 2026-06-10 case: Tony correctly identified bugs in
        20s, verdict was reject, but the 90s floor blocked the
        completion 6+ times. Now honest reject bypasses the floor."""
        v = verify_runtime_floor(
            "tony", 1000, 1020,  # 20s, way under 90s floor
            is_honest_reject=True,
        )
        assert v is None

    def test_honest_reject_bypasses_floor_for_tchalla(self) -> None:
        v = verify_runtime_floor(
            "tchalla", 1000, 1015,
            is_honest_reject=True,
        )
        assert v is None

    def test_honest_reject_bypasses_floor_for_vision(self) -> None:
        v = verify_runtime_floor(
            "vision", 1000, 1015,
            is_honest_reject=True,
        )
        assert v is None

    def test_approve_under_floor_still_rejects(self) -> None:
        """The floor is designed against rubber-stamp APPROVES.
        is_honest_reject=False (verdict is approve / no verdict / etc.)
        must NOT bypass."""
        v = verify_runtime_floor(
            "tony", 1000, 1020,
            is_honest_reject=False,
        )
        assert v is not None
        assert v.floor_seconds == 90

    def test_honest_reject_does_NOT_bypass_floor_for_build_role(self) -> None:
        """The bypass is reviewer-scoped. A friday/shuri 60-second
        'implementation' that ends in reject still warrants the
        5-minute floor — a fast reject from a build role isn't a sign
        of work done, just of work bailed on."""
        v = verify_runtime_floor(
            "friday", 1000, 1060,  # 60s, way under 5min floor
            is_honest_reject=True,
        )
        assert v is not None
        assert v.floor_seconds == 300

    def test_honest_reject_with_unknown_role_still_passes(self) -> None:
        """Unknown roles already have no floor; the bypass changes
        nothing."""
        v = verify_runtime_floor(
            "rando-profile", 1000, 1010,
            is_honest_reject=True,
        )
        assert v is None


# =====================================================================
# verify_workspace_diff — #62
# =====================================================================


@pytest.fixture
def git_workspace(tmp_path: Path) -> Path:
    """A real git repo with one committed file on main, no other changes."""
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=tmp_path, check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "test"],
        cwd=tmp_path, check=True,
    )
    (tmp_path / "src.py").write_text("print('hi')\n")
    subprocess.run(["git", "add", "src.py"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True,
    )
    return tmp_path


class TestWorkspaceDiff:
    def test_friday_empty_diff_with_implementation_claim_rejects(
        self, git_workspace: Path,
    ) -> None:
        """The exact case from 2026-06-09: Friday's branch had no new commits
        but his summary claimed "Wave A dispatcher discipline gates
        implemented; tests cover #28-#34".
        """
        v = verify_workspace_diff(
            assignee="friday",
            workspace_kind="dir",
            workspace_path=str(git_workspace),
            summary="Wave A dispatcher discipline gates implemented; tests cover #28-#34",
        )
        assert isinstance(v, WorkspaceDiffViolation)
        assert "friday" in v.message().lower()
        assert "implementation" in v.message().lower() or "implement" in v.message().lower()

    def test_real_diff_with_implementation_claim_passes(
        self, git_workspace: Path,
    ) -> None:
        """A worker who actually did work and committed it gets through."""
        # Make a second commit so HEAD differs from main's first commit
        # but we still test against HEAD's diff against base. Setup: detach,
        # add a new commit, then diff stat will be non-empty against `main`
        # if HEAD has more.
        new_file = git_workspace / "feature.py"
        new_file.write_text("def real(): pass\n")
        subprocess.run(["git", "checkout", "-q", "-b", "feature"], cwd=git_workspace, check=True)
        subprocess.run(["git", "add", "feature.py"], cwd=git_workspace, check=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "feat"], cwd=git_workspace, check=True,
        )
        v = verify_workspace_diff(
            assignee="friday",
            workspace_kind="dir",
            workspace_path=str(git_workspace),
            summary="Implemented feature module per spec",
        )
        assert v is None

    def test_review_role_skipped(self, git_workspace: Path) -> None:
        """Tony's deliverable is a verdict, not code — skip the diff gate."""
        assert (
            verify_workspace_diff(
                assignee="tony",
                workspace_kind="dir",
                workspace_path=str(git_workspace),
                summary="approve - implementation matches spec",
            )
            is None
        )

    def test_orchestration_role_skipped(self, git_workspace: Path) -> None:
        """JARVIS umbrella spawn doesn't ship code, even when body says
        'implemented chain'."""
        assert (
            verify_workspace_diff(
                assignee="jarvis",
                workspace_kind="dir",
                workspace_path=str(git_workspace),
                summary="Spawned and implemented the build chain",
            )
            is None
        )

    def test_scratch_workspace_skipped(self) -> None:
        """scratch workspaces have no diff target."""
        assert (
            verify_workspace_diff(
                assignee="friday",
                workspace_kind="scratch",
                workspace_path=None,
                summary="implemented thing",
            )
            is None
        )

    def test_build_role_with_no_diff_rejects_regardless_of_summary_verb(
        self, git_workspace: Path,
    ) -> None:
        """After the PR-#11 self-review fix: build-role + dir/worktree
        workspace REQUIRES a non-empty diff. The earlier verb-trigger
        version was bypassed by writing the summary without
        implementation verbs. Now even a verb-free summary fails when
        no diff exists.
        """
        v = verify_workspace_diff(
            assignee="friday",
            workspace_kind="dir",
            workspace_path=str(git_workspace),
            summary="Investigated the issue; recommendations in comment.",
        )
        assert v is not None

    def test_x_no_code_opt_out(self, git_workspace: Path) -> None:
        assert (
            verify_workspace_diff(
                assignee="friday",
                workspace_kind="dir",
                workspace_path=str(git_workspace),
                summary="Implemented the docs reshuffle",
                allow_no_code=True,
            )
            is None
        )

    def test_nonexistent_workspace_path_skipped(self) -> None:
        """We don't crash when workspace_path is wrong; we just skip."""
        assert (
            verify_workspace_diff(
                assignee="friday",
                workspace_kind="dir",
                workspace_path="/tmp/this-does-not-exist-v67",
                summary="implemented thing",
            )
            is None
        )

    def test_empty_summary_does_not_crash(self, git_workspace: Path) -> None:
        """Regression: empty/None summary on a build role with no diff
        previously crashed `splitlines()[0]` before the gate could
        surface the violation. Now the violation lands cleanly."""
        v_empty = verify_workspace_diff(
            assignee="friday",
            workspace_kind="dir",
            workspace_path=str(git_workspace),
            summary="",
        )
        assert isinstance(v_empty, WorkspaceDiffViolation)
        v_none = verify_workspace_diff(
            assignee="friday",
            workspace_kind="dir",
            workspace_path=str(git_workspace),
            summary=None,
        )
        assert isinstance(v_none, WorkspaceDiffViolation)


# =====================================================================
# verify_no_stray_artifacts — #28
# =====================================================================


def _git_init(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "test"], cwd=tmp_path, check=True,
    )


class TestStrayArtifacts:
    def test_pr1_all_prior_block_evidence_files(self, tmp_path: Path) -> None:
        """The literal failure mode from agent-dashboard PR #1: a file
        named 'all prior block evidence files' (no extension) committed
        because the evidence-path gate took a descriptive phrase
        literally.
        """
        _git_init(tmp_path)
        stray = tmp_path / "all prior block evidence files"
        stray.write_text("nothing\n")
        (tmp_path / "src.py").write_text("print('hi')\n")
        subprocess.run(
            ["git", "add", "all prior block evidence files", "src.py"],
            cwd=tmp_path, check=True,
        )
        v = verify_no_stray_artifacts("dir", str(tmp_path))
        assert isinstance(v, StrayArtifactViolation)
        assert "all prior block evidence files" in v.stray_paths

    def test_commit_hash_txt_stray(self, tmp_path: Path) -> None:
        _git_init(tmp_path)
        (tmp_path / "commit-hash.txt").write_text("abc123\n")
        (tmp_path / "src.py").write_text("print('hi')\n")
        subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
        v = verify_no_stray_artifacts("dir", str(tmp_path))
        assert v is not None
        assert "commit-hash.txt" in v.stray_paths

    def test_triage_dir_stray(self, tmp_path: Path) -> None:
        _git_init(tmp_path)
        td = tmp_path / "triage"
        td.mkdir()
        (td / "v6.4-report.md").write_text("notes\n")
        (tmp_path / "src.py").write_text("print('hi')\n")
        subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
        v = verify_no_stray_artifacts("dir", str(tmp_path))
        assert v is not None
        assert any("triage/" in p for p in v.stray_paths)

    def test_evidence_subdir_stray(self, tmp_path: Path) -> None:
        _git_init(tmp_path)
        ed = tmp_path / "changes" / "fix-14" / "evidence"
        ed.mkdir(parents=True)
        (ed / "out.json").write_text("{}\n")
        (tmp_path / "src.py").write_text("print('hi')\n")
        subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
        v = verify_no_stray_artifacts("dir", str(tmp_path))
        assert v is not None
        assert any("evidence" in p for p in v.stray_paths)

    def test_clean_repo_passes(self, tmp_path: Path) -> None:
        _git_init(tmp_path)
        (tmp_path / "src.py").write_text("print('hi')\n")
        (tmp_path / "README.md").write_text("# hi\n")
        subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
        assert verify_no_stray_artifacts("dir", str(tmp_path)) is None

    def test_shebang_file_without_extension_is_ok(self, tmp_path: Path) -> None:
        """Real scripts have shebangs — those aren't stray."""
        _git_init(tmp_path)
        (tmp_path / "bin").mkdir()
        script = tmp_path / "bin" / "deploy"
        script.write_text("#!/bin/bash\necho hi\n")
        (tmp_path / "src.py").write_text("print('hi')\n")
        subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
        assert verify_no_stray_artifacts("dir", str(tmp_path)) is None

    def test_scratch_workspace_skipped(self) -> None:
        assert verify_no_stray_artifacts("scratch", None) is None

    def test_x_stray_ok_opt_out(self, tmp_path: Path) -> None:
        _git_init(tmp_path)
        (tmp_path / "all prior block evidence files").write_text("x\n")
        (tmp_path / "src.py").write_text("print('hi')\n")
        subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
        assert (
            verify_no_stray_artifacts("dir", str(tmp_path), allow_stray=True)
            is None
        )

    def test_nonexistent_workspace_path_skipped(self) -> None:
        assert (
            verify_no_stray_artifacts("dir", "/tmp/does-not-exist-v67") is None
        )

    def test_tmp_prefixed_files_stray(self, tmp_path: Path) -> None:
        _git_init(tmp_path)
        (tmp_path / "tmp-scratch").write_text("x\n")
        (tmp_path / "src.py").write_text("print('hi')\n")
        subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
        v = verify_no_stray_artifacts("dir", str(tmp_path))
        assert v is not None
        assert "tmp-scratch" in v.stray_paths

    # === post-self-review fixes ===

    def test_tracked_LICENSE_and_Dockerfile_not_flagged(
        self, tmp_path: Path,
    ) -> None:
        """The PR-#11 self-review found that the original gate flagged
        every repo's tracked LICENSE / Dockerfile / Makefile as stray
        because they have no extension. Real repos legitimately track
        these — they predate the worker by years.
        """
        _git_init(tmp_path)
        (tmp_path / "LICENSE").write_text("MIT License\n")
        (tmp_path / "Dockerfile").write_text("FROM alpine\n")
        (tmp_path / "Makefile").write_text("all:\n\techo hi\n")
        (tmp_path / "Vagrantfile").write_text("config\n")
        (tmp_path / "README").write_text("project\n")
        (tmp_path / "src.py").write_text("print('hi')\n")
        subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
        assert verify_no_stray_artifacts("dir", str(tmp_path)) is None

    def test_evidence_substring_in_legitimate_filename_not_flagged(
        self, tmp_path: Path,
    ) -> None:
        """The original `evidence|.*-evidence|.*_evidence` regex was so
        broad it matched `evidence-types.md` (a legitimate doc in the
        security skills tree) and `scripts/evidence-store.py` (a
        legitimate source file). After tightening, those paths pass."""
        _git_init(tmp_path)
        d = tmp_path / "optional-skills" / "security" / "references"
        d.mkdir(parents=True)
        (d / "evidence-types.md").write_text("# Evidence types\n")
        scripts = tmp_path / "scripts"
        scripts.mkdir()
        (scripts / "evidence-store.py").write_text("def store(): pass\n")
        (tmp_path / "src.py").write_text("print('hi')\n")
        subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
        assert verify_no_stray_artifacts("dir", str(tmp_path)) is None

    def test_untracked_no_extension_file_still_flagged(
        self, tmp_path: Path,
    ) -> None:
        """Untracked files with no extension and no shebang remain
        stray. (Tracked ones we trust; untracked ones the worker added
        this run.)"""
        _git_init(tmp_path)
        (tmp_path / "src.py").write_text("print('hi')\n")
        subprocess.run(["git", "add", "src.py"], cwd=tmp_path, check=True)
        # Add the literal failure-mode file as UNTRACKED.
        (tmp_path / "all prior block evidence files").write_text("x\n")
        v = verify_no_stray_artifacts("dir", str(tmp_path))
        assert v is not None
        assert "all prior block evidence files" in v.stray_paths

    def test_evidence_dir_under_changes_still_flagged(
        self, tmp_path: Path,
    ) -> None:
        """The agent-dashboard PR #1 failure: `changes/fix-14/evidence/`
        subdirectory artifacts get flagged. Tightening the regex
        shouldn't have lost this case."""
        _git_init(tmp_path)
        ed = tmp_path / "changes" / "v6-6" / "evidence"
        ed.mkdir(parents=True)
        (ed / "block-c-test-output.json").write_text("{}\n")
        (tmp_path / "src.py").write_text("print('hi')\n")
        subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
        v = verify_no_stray_artifacts("dir", str(tmp_path))
        assert v is not None
        assert any("evidence" in p for p in v.stray_paths)

    def test_block_evidence_basename_still_flagged(
        self, tmp_path: Path,
    ) -> None:
        """Files matching ``*-evidence.<ext>`` at any depth are flagged
        even when their extension is legitimate. Catches the v6.6
        artifact basenames without false-positive-ing on
        evidence-types.md."""
        _git_init(tmp_path)
        d = tmp_path / "tests" / "data"
        d.mkdir(parents=True)
        (d / "block-c-evidence.json").write_text("{}\n")
        (tmp_path / "src.py").write_text("print('hi')\n")
        subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
        v = verify_no_stray_artifacts("dir", str(tmp_path))
        assert v is not None
        assert any("block-c-evidence.json" in p for p in v.stray_paths)


# =====================================================================
# Opt-out audit + integration through complete_task — PR-#11 self-review
# =====================================================================


import hermes_cli.kanban_db as kb
from hermes_cli.kanban_db import InvalidOptOutError


@pytest.fixture
def board_conn_with_task(tmp_path, monkeypatch):
    """A board with a single running task with a scratch workspace and
    a recently-claimed started_at so runtime-floor doesn't fire."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "k.db"))
    conn = kb.connect(board="default")
    import time
    now = int(time.time())
    started = now - 1000  # well above any floor
    conn.execute(
        "INSERT INTO tasks (id, title, status, assignee, started_at, "
        "  created_at, workspace_kind, workspace_path) "
        "VALUES ('t_int', 'integration test task', 'running', 'jarvis', ?, "
        "        ?, 'scratch', NULL)",
        (started, now),
    )
    conn.commit()
    yield conn
    conn.close()


class TestOptOutAudit:
    def test_truthy_bool_opt_out_rejected(self, board_conn_with_task) -> None:
        """``x_fast_justified: true`` (a literal bool) must NOT be a free
        bypass — the gate requires a substantive string reason."""
        with pytest.raises(InvalidOptOutError) as excinfo:
            kb.complete_task(
                board_conn_with_task, "t_int",
                summary="quick", result="done",
                metadata={"x_fast_justified": True},
            )
        assert excinfo.value.key == "x_fast_justified"

    def test_short_string_opt_out_rejected(self, board_conn_with_task) -> None:
        """Reason shorter than 20 chars (after strip) rejected."""
        with pytest.raises(InvalidOptOutError):
            kb.complete_task(
                board_conn_with_task, "t_int",
                summary="quick", result="done",
                metadata={"x_no_code": "ok"},
            )

    def test_whitespace_only_opt_out_rejected(
        self, board_conn_with_task,
    ) -> None:
        """A reason that's just whitespace can't satisfy the audit."""
        with pytest.raises(InvalidOptOutError):
            kb.complete_task(
                board_conn_with_task, "t_int",
                summary="quick", result="done",
                metadata={"x_stray_ok": "                                "},
            )

    def test_real_reason_opt_out_accepted_and_audited(
        self, board_conn_with_task,
    ) -> None:
        """A real string reason ≥20 chars is accepted and emits a
        ``completion_opt_out_used`` event with the verbatim reason."""
        ok = kb.complete_task(
            board_conn_with_task, "t_int",
            summary="docs-only reshuffle", result="done",
            metadata={"x_fast_justified":
                      "one-line rename verified by smoke test"},
        )
        assert ok
        events = board_conn_with_task.execute(
            "SELECT kind, payload FROM task_events WHERE task_id = 't_int' "
            "AND kind = 'completion_opt_out_used'"
        ).fetchall()
        assert len(events) == 1
        import json as _j
        payload = _j.loads(events[0]["payload"])
        assert (
            payload["opt_outs"]["x_fast_justified"]
            == "one-line rename verified by smoke test"
        )

    def test_false_opt_out_not_rejected_no_event(
        self, board_conn_with_task,
    ) -> None:
        """``False`` and ``None`` are the natural absent values — they
        skip validation and emit no opt-out event."""
        ok = kb.complete_task(
            board_conn_with_task, "t_int",
            summary="done", result="done",
            metadata={"x_fast_justified": False, "x_no_code": None},
        )
        assert ok
        events = board_conn_with_task.execute(
            "SELECT count(*) AS n FROM task_events WHERE task_id = 't_int' "
            "AND kind = 'completion_opt_out_used'"
        ).fetchone()
        assert events["n"] == 0


class TestCompleteTaskIntegration:
    """End-to-end coverage through the public complete_task entrypoint.
    These tests would have caught wiring regressions in PR #11 / #12
    that the pure-function tests miss.
    """

    def test_friday_below_floor_blocks_and_emits_event(
        self, board_conn_with_task,
    ) -> None:
        # Re-claim as friday with a recent started_at to trip the floor.
        board_conn_with_task.execute(
            "UPDATE tasks SET assignee = 'friday', started_at = ? "
            "WHERE id = 't_int'",
            (int(__import__("time").time()) - 10,),  # 10s ago, well below 5min
        )
        board_conn_with_task.commit()
        with pytest.raises(kb.CompletionGateError):
            kb.complete_task(
                board_conn_with_task, "t_int",
                summary="implemented thing", result="done",
            )
        # Task state preserved
        row = board_conn_with_task.execute(
            "SELECT status FROM tasks WHERE id = 't_int'"
        ).fetchone()
        assert row["status"] == "running"
        events = board_conn_with_task.execute(
            "SELECT kind FROM task_events WHERE task_id = 't_int' "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert events["kind"] == "completion_blocked_v6_7_gates"

    def test_clean_completion_passes_all_gates(
        self, board_conn_with_task,
    ) -> None:
        """Default jarvis (no floor) + scratch workspace (no diff/stray
        gate) + no opt-outs = clean pass."""
        ok = kb.complete_task(
            board_conn_with_task, "t_int",
            summary="spawned chain", result="done",
        )
        assert ok
        row = board_conn_with_task.execute(
            "SELECT status FROM tasks WHERE id = 't_int'"
        ).fetchone()
        assert row["status"] == "done"


# =====================================================================
# verify_reviewer_fields — #29, #31 (with self-review tightening)
# =====================================================================

# Baseline valid verdict (test_quality only). Citations follow the
# required structure: bullet item with a tests path AND a :N reference.
_GOOD_BASELINE = """\
verdict: approve

test_quality:
  imports_match_deliverable_entrypoints: true
  evidence:
    - tests/integration.test.ts:42 calls app/api/metrics/route.ts:GET
    - tests/integration.test.ts:88 invokes app/[view]/page.tsx default export
"""

# Full verdict including adversarial_pass for HTTP/server tasks.
_GOOD_FULL = """\
verdict: approve

test_quality:
  imports_match_deliverable_entrypoints: true
  evidence:
    - tests/integration.test.ts:42 calls app/api/metrics/route.ts:GET
    - tests/integration.test.ts:88 invokes app/[view]/page.tsx default export

adversarial_pass:
  env_vars:
    - AGENT_DASHBOARD_DB: allowlisted to ~/.hermes/ (lib/ingest.ts:92)
    - HERMES_KANBAN_DB: same as above
  request_inputs: []
  file_paths:
    - DB open paths: allowlisted (lib/ingest.ts:88)
  external_io: []
"""


class TestReviewerFields:
    def test_tony_bare_approve_rejected(self) -> None:
        v = verify_reviewer_fields(
            assignee="tony",
            body="Review Block C and return verdict per kanban-worker convention.",
            result="verdict: approve\n",
        )
        assert isinstance(v, MissingReviewerFieldViolation)
        assert "test_quality.imports_match_deliverable_entrypoints" in v.missing_fields
        assert "test_quality.evidence" in v.missing_fields

    def test_baseline_fields_pass_for_pure_code_review(self) -> None:
        v = verify_reviewer_fields(
            assignee="tony",
            body="Review changes to hermes_cli/kanban_completion_gates.py",
            result=_GOOD_BASELINE,
        )
        assert v is None

    def test_app_api_body_requires_adversarial_pass(self) -> None:
        v = verify_reviewer_fields(
            assignee="tony",
            body="Review remediation: changes to app/api/metrics/route.ts and lib/ingest.ts",
            result=_GOOD_BASELINE,
        )
        assert v is not None
        assert all(f in v.missing_fields for f in [
            "adversarial_pass.env_vars",
            "adversarial_pass.request_inputs",
            "adversarial_pass.file_paths",
            "adversarial_pass.external_io",
        ])
        # test_quality already satisfied
        assert "test_quality.evidence" not in v.missing_fields

    def test_full_verdict_passes_with_adversarial_trigger(self) -> None:
        v = verify_reviewer_fields(
            assignee="tony",
            body="Review remediation: app/api/metrics/route.ts and lib/ingest.ts",
            result=_GOOD_FULL,
        )
        assert v is None

    # === self-review P0/P1 fix tests ===

    def test_field_leech_rejected(self) -> None:
        """The PR-#12 P0: unanchored captures let one section's content
        leech into earlier empty fields. After bounding captures with
        the next-field-key lookahead, empty `env_vars:`/`request_inputs:`/
        `file_paths:` are recognized as missing even if `external_io:`
        has lots of content."""
        leech_verdict = """\
verdict: approve

test_quality:
  imports_match_deliverable_entrypoints: true
  evidence:
    - tests/foo.test.ts:1 covers app/api/route.ts:GET

adversarial_pass:
  env_vars:
  request_inputs:
  file_paths:
  external_io:
    - lots of content past 20 chars but only this section has real text
"""
        v = verify_reviewer_fields(
            assignee="tony",
            body="Review changes to app/api/route.ts",
            result=leech_verdict,
        )
        assert v is not None
        assert "adversarial_pass.env_vars" in v.missing_fields
        assert "adversarial_pass.request_inputs" in v.missing_fields
        assert "adversarial_pass.file_paths" in v.missing_fields
        # external_io DOES have substantive content — that one passes
        assert "adversarial_pass.external_io" not in v.missing_fields

    def test_prose_evidence_rejected(self) -> None:
        """The PR-#12 P0: `test_quality.evidence` must be enumerated
        test→deliverable citations, not free prose. Tony writing a
        paragraph of justification under `evidence:` no longer
        satisfies the gate."""
        prose_verdict = """\
verdict: approve

test_quality:
  imports_match_deliverable_entrypoints: true
  evidence:
    this is just prose about why we approve, not an enumerated test list
    talking about coverage in general terms with no specific citation
"""
        v = verify_reviewer_fields(
            assignee="tony",
            body="Review code",
            result=prose_verdict,
        )
        assert v is not None
        assert "test_quality.evidence" in v.missing_fields

    def test_empty_not_applicable_rejected(self) -> None:
        """The PR-#12 P1: ``not_applicable:`` with no reason was
        previously accepted via greedy capture into the next field. Now
        the capture is anchored to the line terminator and the reason
        must be at least 8 chars."""
        empty_na_verdict = """\
verdict: approve

test_quality:
  imports_match_deliverable_entrypoints: not_applicable:
  evidence:
    - tests/foo.test.ts:1 covers app/foo.ts:exported function
"""
        v = verify_reviewer_fields(
            assignee="tony",
            body="Review docs change",
            result=empty_na_verdict,
        )
        assert v is not None
        assert "test_quality.imports_match_deliverable_entrypoints" in v.missing_fields

    def test_not_applicable_with_real_reason_passes(self) -> None:
        verdict = """\
verdict: approve

test_quality:
  imports_match_deliverable_entrypoints: not_applicable: pure docs reshuffle, no entrypoints touched
  evidence:
    - README.md updates only, no test changes needed
"""
        # Evidence here has no :N citation. To accept this we need the
        # honest-empty escape — let's use that.
        verdict = """\
verdict: approve

test_quality:
  imports_match_deliverable_entrypoints: not_applicable: pure docs reshuffle, no entrypoints touched
  evidence: none
"""
        v = verify_reviewer_fields(
            assignee="tony",
            body="Review docs reshuffle",
            result=verdict,
        )
        assert v is None

    def test_docs_only_openapi_mention_does_not_trigger_adversarial(self) -> None:
        """The PR-#12 P1: original `\\bopenapi\\b` matched prose. After
        tightening, an OpenAPI prose mention without a path doesn't
        force adversarial_pass."""
        v = verify_reviewer_fields(
            assignee="tony",
            body="Review the OpenAPI spec documentation update",
            result=_GOOD_BASELINE,
        )
        assert v is None

    def test_openapi_with_path_does_trigger_adversarial(self) -> None:
        """The legitimate trigger: openapi.yaml or openapi/ as part of
        the actual deliverable surface still triggers."""
        v = verify_reviewer_fields(
            assignee="tony",
            body="Review changes to openapi.yaml and the request handler in src/server/handlers/users.ts",
            result=_GOOD_BASELINE,
        )
        assert v is not None
        assert any("adversarial_pass" in m for m in v.missing_fields)

    def test_non_review_role_skipped(self) -> None:
        assert (
            verify_reviewer_fields(
                assignee="friday",
                body="Implement gates per spec",
                result="Implementation done.",
            )
            is None
        )

    def test_x_no_reviewer_fields_opt_out(self) -> None:
        """The reviewer-fields opt-out is itself just a flag at this
        layer (the opt-out audit happens in
        `_v6_7_run_completion_gates`, not in the pure function)."""
        v = verify_reviewer_fields(
            assignee="tony",
            body="Review",
            result="verdict: approve",
            allow_no_reviewer_fields=True,
        )
        assert v is None

    def test_tchalla_and_vision_get_same_discipline(self) -> None:
        for role in ("tchalla", "vision"):
            v = verify_reviewer_fields(
                assignee=role,
                body="Review",
                result="verdict: approve",
            )
            assert v is not None

    def test_case_insensitive_role_match(self) -> None:
        v = verify_reviewer_fields(
            assignee="Tony",
            body="Review",
            result="verdict: approve",
        )
        assert v is not None


class TestReviewerFieldsIntegration:
    """Through-the-public-API tests for the reviewer-fields gate.

    Mirrors `TestCompleteTaskIntegration` but targets the reviewer-
    fields wiring specifically — these would catch a regression where
    we forget to pass `body` or `result` into `verify_reviewer_fields`.
    """

    @pytest.fixture
    def tony_running_task(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "k.db"))
        conn = kb.connect(board="default")
        import time
        now = int(time.time())
        # started_at well before now so floor doesn't fire
        conn.execute(
            "INSERT INTO tasks (id, title, body, status, assignee, started_at, "
            "  created_at, workspace_kind, workspace_path) "
            "VALUES ('t_rev', 'Tony review of remediation', "
            "        'Review remediation: changes to app/api/metrics/route.ts and lib/ingest.ts', "
            "        'running', 'tony', ?, ?, 'scratch', NULL)",
            (now - 1000, now),
        )
        conn.commit()
        yield conn
        conn.close()

    def test_tony_bare_approve_blocks_and_emits_event(
        self, tony_running_task,
    ) -> None:
        with pytest.raises(kb.CompletionGateError):
            kb.complete_task(
                tony_running_task, "t_rev",
                summary="approved", result="verdict: approve",
            )
        events = tony_running_task.execute(
            "SELECT kind FROM task_events WHERE task_id = 't_rev' "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert events["kind"] == "completion_blocked_v6_7_gates"
        row = tony_running_task.execute(
            "SELECT status FROM tasks WHERE id = 't_rev'"
        ).fetchone()
        assert row["status"] == "running"

    def test_tony_full_verdict_passes(self, tony_running_task) -> None:
        ok = kb.complete_task(
            tony_running_task, "t_rev",
            summary="approved", result=_GOOD_FULL,
        )
        assert ok


# =====================================================================
# Self-review fixes for PR #12 — N1 (empty body), N2 (inline-prose
# bypass on adversarial_pass), N6 (evidence:none in code-change context)
# =====================================================================


class TestReviewerFieldsSelfReviewFixes:
    def test_empty_body_does_not_crash(self) -> None:
        """Regression for N1: empty body used to crash splitlines()[0]
        before the gate could surface the violation."""
        v = verify_reviewer_fields(
            assignee="tony",
            body="",
            result="verdict: approve\n",
        )
        assert isinstance(v, MissingReviewerFieldViolation)

    def test_none_body_does_not_crash(self) -> None:
        v = verify_reviewer_fields(
            assignee="tony",
            body=None,
            result="verdict: approve\n",
        )
        assert isinstance(v, MissingReviewerFieldViolation)

    def test_adversarial_inline_prose_bypass_rejected(self) -> None:
        """The N2 self-review finding: an inline value of 51 chars of
        prose used to satisfy adversarial_pass.<field>. Now requires
        structural markers (bullet, ENV_VAR:, path, file extension)."""
        prose_verdict = """\
verdict: approve

test_quality:
  imports_match_deliverable_entrypoints: true
  evidence:
    - tests/foo.test.ts:1 covers app/api/route.ts:GET

adversarial_pass:
  env_vars: see above explanation about general env safety
  request_inputs: handled with care, full coverage everywhere
  file_paths: standard set of paths, all are looked after
  external_io: same as above, addressed in prior commit
"""
        v = verify_reviewer_fields(
            assignee="tony",
            body="Review changes to app/api/route.ts",
            result=prose_verdict,
        )
        assert v is not None
        for f in (
            "adversarial_pass.env_vars",
            "adversarial_pass.request_inputs",
            "adversarial_pass.file_paths",
            "adversarial_pass.external_io",
        ):
            assert f in v.missing_fields, f"expected {f} in {v.missing_fields}"

    def test_adversarial_structured_inline_passes(self) -> None:
        """Inline values WITH path/env-var/extension cues pass — these
        are honest one-line enumerations."""
        verdict = """\
verdict: approve

test_quality:
  imports_match_deliverable_entrypoints: true
  evidence:
    - tests/foo.test.ts:1 covers app/api/route.ts:GET

adversarial_pass:
  env_vars: DASH_DB: allowlist enforced in lib/ingest.ts:88
  request_inputs: route.ts:42 parses with zod schema
  file_paths: lib/ingest.ts only opens paths under ~/.hermes/
  external_io: []
"""
        v = verify_reviewer_fields(
            assignee="tony",
            body="Review changes to app/api/route.ts",
            result=verdict,
        )
        assert v is None

    def test_evidence_none_blocked_when_body_triggers_adversarial(self) -> None:
        """N6 self-review: ``evidence: none`` is normally an honest-
        empty escape, but when the body indicates code-touching review
        the reviewer MUST produce real test citations."""
        verdict = """\
verdict: approve

test_quality:
  imports_match_deliverable_entrypoints: true
  evidence: none

adversarial_pass:
  env_vars: []
  request_inputs: []
  file_paths: []
  external_io: []
"""
        v = verify_reviewer_fields(
            assignee="tony",
            body="Review changes to app/api/metrics/route.ts",
            result=verdict,
        )
        assert v is not None
        assert "test_quality.evidence" in v.missing_fields

    def test_evidence_none_allowed_for_pure_docs_review(self) -> None:
        """The escape stays valid for non-code reviews."""
        verdict = """\
verdict: approve

test_quality:
  imports_match_deliverable_entrypoints: not_applicable: docs reshuffle, no entrypoints
  evidence: none
"""
        v = verify_reviewer_fields(
            assignee="tony",
            body="Review the README rewrite",
            result=verdict,
        )
        assert v is None

    def test_prose_padded_with_readme_md_rejected(self) -> None:
        """Second self-review found that the structure check accepted
        prose that name-dropped README.md / config.yaml. After dropping
        docs extensions from the source-file alternation, this bypass
        is closed."""
        prose_padding = """\
verdict: approve

test_quality:
  imports_match_deliverable_entrypoints: true
  evidence:
    - tests/foo.test.ts:1 covers app/api/route.ts:GET

adversarial_pass:
  env_vars: see README.md elsewhere for general environment safety guidance
  request_inputs: discussed in config.yaml above; nothing concrete here either
  file_paths: notes.md handles the overview; we're broadly compliant
  external_io: handled per docs.json conventions across the codebase
"""
        v = verify_reviewer_fields(
            assignee="tony",
            body="Review changes to app/api/route.ts",
            result=prose_padding,
        )
        assert v is not None
        for f in (
            "adversarial_pass.env_vars",
            "adversarial_pass.request_inputs",
            "adversarial_pass.file_paths",
            "adversarial_pass.external_io",
        ):
            assert f in v.missing_fields, (
                f"expected {f} in {v.missing_fields} — README.md / config.yaml "
                "prose name-drop should not satisfy the gate"
            )

    def test_real_yaml_path_with_slash_still_passes(self) -> None:
        """Honest yaml/json/md mentions inside a real path (e.g.
        ``configs/app.yaml``) still satisfy the path-with-slash regex."""
        verdict = """\
verdict: approve

test_quality:
  imports_match_deliverable_entrypoints: true
  evidence:
    - tests/foo.test.ts:1 covers app/api/route.ts:GET

adversarial_pass:
  env_vars:
    - configs/env.example: bounds in lib/ingest.ts
  request_inputs:
    - schemas/inputs.yaml validated at app/api/route.ts:42
  file_paths:
    - lib/ingest.ts only opens paths under ~/.hermes/
  external_io: []
"""
        v = verify_reviewer_fields(
            assignee="tony",
            body="Review changes to app/api/route.ts",
            result=verdict,
        )
        assert v is None

    def test_adversarial_bullet_list_passes(self) -> None:
        """The most common honest shape: bullet list under each section."""
        verdict = """\
verdict: approve

test_quality:
  imports_match_deliverable_entrypoints: true
  evidence:
    - tests/integration.test.ts:42 invokes route.ts:GET

adversarial_pass:
  env_vars:
    - DASH_DB
    - HERMES_KANBAN_DB
  request_inputs:
    - none
  file_paths:
    - lib/ingest.ts paths only
  external_io: []
"""
        v = verify_reviewer_fields(
            assignee="tony",
            body="Review app/api/metrics changes",
            result=verdict,
        )
        assert v is None


# =====================================================================
# verify_pr_urls_exist — #63
# =====================================================================


def _gh_pr_real(url: str):
    """Stub: every URL exists."""
    return True


def _gh_pr_phantom(url: str):
    """Stub: every URL is 404."""
    return False


def _gh_pr_indeterminate(url: str):
    """Stub: gh not installed / network error."""
    return None


def _gh_pr_only_42_real(url: str):
    """Stub: only PR #42 exists; others are phantom."""
    if "/pull/42" in url:
        return True
    return False


class TestPRExistence:
    def test_no_pr_url_in_text_skips(self) -> None:
        assert verify_pr_urls_exist(
            result="verdict: approve\ntest_quality: ...",
            summary="reviewed",
        ) is None

    def test_real_pr_passes(self) -> None:
        assert verify_pr_urls_exist(
            result="Approved at https://github.com/o/r/pull/1",
            summary=None,
            gh_pr_exists=_gh_pr_real,
        ) is None

    def test_phantom_pr_rejected(self) -> None:
        """The 2026-06-09 Tchalla case: 'cannot run gh pr diff 42'
        with PR #42 not existing on the remote."""
        v = verify_pr_urls_exist(
            result="Approved at https://github.com/1Team-Engineering/hermes-agent/pull/42",
            summary=None,
            gh_pr_exists=_gh_pr_phantom,
        )
        assert isinstance(v, PhantomPRViolation)
        assert "pull/42" in v.phantom_urls[0]

    def test_mixed_real_and_phantom(self) -> None:
        v = verify_pr_urls_exist(
            result=(
                "Approved against https://github.com/o/r/pull/42 (real) and "
                "also against https://github.com/o/r/pull/99 (phantom)"
            ),
            summary=None,
            gh_pr_exists=_gh_pr_only_42_real,
        )
        assert v is not None
        assert any("pull/99" in u for u in v.phantom_urls)
        assert not any("pull/42" in u for u in v.phantom_urls)

    def test_indeterminate_falls_open(self) -> None:
        """When gh is missing or auth is broken, the gate doesn't reject
        — workers can still complete in offline / broken-gh envs."""
        assert verify_pr_urls_exist(
            result="See https://github.com/o/r/pull/42",
            summary=None,
            gh_pr_exists=_gh_pr_indeterminate,
        ) is None

    def test_summary_text_also_scanned(self) -> None:
        v = verify_pr_urls_exist(
            result="approve",
            summary="Closing the loop at https://github.com/o/r/pull/99",
            gh_pr_exists=_gh_pr_phantom,
        )
        assert v is not None

    def test_dedup_same_url_twice(self) -> None:
        v = verify_pr_urls_exist(
            result=(
                "first mention https://github.com/o/r/pull/9 "
                "second mention https://github.com/o/r/pull/9"
            ),
            summary=None,
            gh_pr_exists=_gh_pr_phantom,
        )
        assert v is not None
        assert len(v.phantom_urls) == 1

    def test_allow_phantom_pr_opt_out(self) -> None:
        assert verify_pr_urls_exist(
            result="https://github.com/o/r/pull/42",
            summary=None,
            gh_pr_exists=_gh_pr_phantom,
            allow_phantom_pr=True,
        ) is None


# =====================================================================
# verify_doc_drift — #32
# =====================================================================


def _make_repo_with_doc(tmp_path: Path, filename: str, content: str) -> Path:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / filename).write_text(content)
    return tmp_path


class TestDocDrift:
    def test_non_versioned_tenant_skips(self, tmp_path: Path) -> None:
        _make_repo_with_doc(tmp_path, "README.md", "# Project v5.0 docs\n")
        assert verify_doc_drift(
            tenant="hermes-self-improvement",
            workspace_kind="dir",
            workspace_path=str(tmp_path),
        ) is None

    def test_no_readme_skips(self, tmp_path: Path) -> None:
        _make_repo_with_doc(tmp_path, "src.py", "print('hi')\n")
        assert verify_doc_drift(
            tenant="marvel-swarm-v6-6-test",
            workspace_kind="dir",
            workspace_path=str(tmp_path),
        ) is None

    def test_stale_version_in_readme_rejected(self, tmp_path: Path) -> None:
        """The exact 2026-06-09 agent-dashboard PR #1 case: README still
        said 'v6.2 Marvel swarm test target' while the chain was v6.6."""
        _make_repo_with_doc(
            tmp_path, "README.md",
            "# Hermes Operator Dashboard\n\n"
            "v6.2 Marvel swarm test target with 8 metric views.\n",
        )
        v = verify_doc_drift(
            tenant="marvel-swarm-v6-6-test",
            workspace_kind="dir",
            workspace_path=str(tmp_path),
        )
        assert isinstance(v, DocDriftViolation)
        assert v.active_version == "v6.6"

    def test_current_version_in_readme_passes(self, tmp_path: Path) -> None:
        _make_repo_with_doc(
            tmp_path, "README.md",
            "# Project\n\n"
            "Current target: v6.6 Marvel swarm chain.\n",
        )
        assert verify_doc_drift(
            tenant="marvel-swarm-v6-6-test",
            workspace_kind="dir",
            workspace_path=str(tmp_path),
        ) is None

    def test_history_section_excused(self, tmp_path: Path) -> None:
        """Mentions of older versions in a # History or # Older
        versions section don't count — they're historical context."""
        _make_repo_with_doc(
            tmp_path, "README.md",
            "# Project (v6.6)\n\n"
            "Current target is the v6.6 chain.\n\n"
            "## Previous versions\n\n"
            "- v6.2 was the original swarm test target\n"
            "- v6.5 added the dispatcher heartbeat\n",
        )
        assert verify_doc_drift(
            tenant="marvel-swarm-v6-6-test",
            workspace_kind="dir",
            workspace_path=str(tmp_path),
        ) is None

    def test_changelog_section_excused(self, tmp_path: Path) -> None:
        _make_repo_with_doc(
            tmp_path, "CHANGELOG.md",
            "# Changelog\n\n"
            "## v6.6\n- new gates\n\n"
            "## v6.2\n- original release\n",
        )
        assert verify_doc_drift(
            tenant="marvel-swarm-v6-6-test",
            workspace_kind="dir",
            workspace_path=str(tmp_path),
        ) is None

    def test_higher_version_in_doc_is_not_stale(self, tmp_path: Path) -> None:
        """A future v6.7 mention in a v6.6 chain isn't 'stale' — only
        OLDER mentions trip the gate."""
        _make_repo_with_doc(
            tmp_path, "README.md",
            "# Project (v6.6)\n\nLooking ahead to v6.7\n",
        )
        assert verify_doc_drift(
            tenant="marvel-swarm-v6-6-test",
            workspace_kind="dir",
            workspace_path=str(tmp_path),
        ) is None

    def test_scratch_workspace_skipped(self) -> None:
        assert verify_doc_drift(
            tenant="marvel-swarm-v6-6-test",
            workspace_kind="scratch",
            workspace_path=None,
        ) is None

    def test_allow_doc_drift_opt_out(self, tmp_path: Path) -> None:
        _make_repo_with_doc(
            tmp_path, "README.md",
            "# Project (v6.2)\n",
        )
        assert verify_doc_drift(
            tenant="marvel-swarm-v6-6-test",
            workspace_kind="dir",
            workspace_path=str(tmp_path),
            allow_doc_drift=True,
        ) is None

    # === self-review fixes ===

    def test_nested_history_heading_does_not_exit_history(
        self, tmp_path: Path,
    ) -> None:
        """Self-review #5: a `### v6.2 details` subsection under
        `## History` used to exit history mode (the new heading reset
        in_history to False). Now depth-aware: subsection only exits
        history when level <= history_depth AND it's not itself a
        history heading."""
        _make_repo_with_doc(
            tmp_path, "README.md",
            "# Project (v6.6)\n\n"
            "Current target: v6.6\n\n"
            "## History\n\n"
            "### v6.2 details\n\n"
            "We did v6.2 things here.\n\n"
            "### v6.5 details\n\n"
            "Added the dispatcher heartbeat.\n",
        )
        assert verify_doc_drift(
            tenant="marvel-swarm-v6-6-test",
            workspace_kind="dir",
            workspace_path=str(tmp_path),
        ) is None

    def test_nonhistory_heading_at_history_level_exits_history(
        self, tmp_path: Path,
    ) -> None:
        """The complement of the previous test: a sibling heading at
        the same level (here `## Roadmap`) DOES exit the history
        section, so a stale v6.2 there gets flagged."""
        _make_repo_with_doc(
            tmp_path, "README.md",
            "# Project (v6.6)\n\n"
            "## History\n\n"
            "- v6.2 was the first cut.\n\n"
            "## Roadmap\n\n"
            "Considering features carried over from v6.2.\n",
        )
        v = verify_doc_drift(
            tenant="marvel-swarm-v6-6-test",
            workspace_kind="dir",
            workspace_path=str(tmp_path),
        )
        assert v is not None
        # The v6.2 inside ## History is excused; only the one under
        # ## Roadmap should count.
        assert v.stale_refs == (("README.md", "v6.2"),)


class TestPRExistenceErrorClassification:
    """Self-review #2: the substring matcher for `_gh_pr_exists`
    confused DNS / network errors with 404. Now those fall open as
    indeterminate, not phantom."""

    def _make_run(self, returncode: int, stderr: str):
        class _R:
            def __init__(self, rc, se):
                self.returncode = rc
                self.stdout = ""
                self.stderr = se
        return _R(returncode, stderr)

    def test_dns_error_falls_open(self, monkeypatch) -> None:
        """`could not resolve host: api.github.com` no longer counts
        as a 404."""
        from hermes_cli import kanban_completion_gates as gates
        monkeypatch.setattr("shutil.which", lambda *_: "/usr/local/bin/gh")
        monkeypatch.setattr(
            "subprocess.run",
            lambda *a, **k: self._make_run(
                1, "could not resolve host: api.github.com"
            ),
        )
        assert gates._gh_pr_exists("https://github.com/o/r/pull/1") is None

    def test_tls_error_falls_open(self, monkeypatch) -> None:
        from hermes_cli import kanban_completion_gates as gates
        monkeypatch.setattr("shutil.which", lambda *_: "/usr/local/bin/gh")
        monkeypatch.setattr(
            "subprocess.run",
            lambda *a, **k: self._make_run(
                1, "tls handshake error: connection reset"
            ),
        )
        assert gates._gh_pr_exists("https://github.com/o/r/pull/1") is None

    def test_auth_error_falls_open(self, monkeypatch) -> None:
        from hermes_cli import kanban_completion_gates as gates
        monkeypatch.setattr("shutil.which", lambda *_: "/usr/local/bin/gh")
        monkeypatch.setattr(
            "subprocess.run",
            lambda *a, **k: self._make_run(
                1, "error: gh auth login required to access this resource"
            ),
        )
        assert gates._gh_pr_exists("https://github.com/o/r/pull/1") is None

    def test_graphql_404_classified_phantom(self, monkeypatch) -> None:
        from hermes_cli import kanban_completion_gates as gates
        monkeypatch.setattr("shutil.which", lambda *_: "/usr/local/bin/gh")
        monkeypatch.setattr(
            "subprocess.run",
            lambda *a, **k: self._make_run(
                1, "GraphQL: Could not resolve to a PullRequest with the number of 42",
            ),
        )
        assert gates._gh_pr_exists("https://github.com/o/r/pull/42") is False

    def test_markdown_link_url_extracted(self) -> None:
        """Confirm extraction works inside a markdown link."""
        from hermes_cli.kanban_completion_gates import _extract_pr_urls
        urls = _extract_pr_urls(
            "See [PR #42](https://github.com/o/r/pull/42) for details"
        )
        assert urls == ["https://github.com/o/r/pull/42"]


# =====================================================================
# hermes-jarvis#75 — bullet `not_applicable: <reason>` for evidence
# =====================================================================


class TestBulletNotApplicableEvidence:
    """The exact 2026-06-10 validation case: Tony wrote
    `evidence:\\n    - not_applicable: no tests directory...` declaring
    honestly that no tests existed. The regex only accepted inline
    `evidence: none` so verbose-but-correct form was rejected. Closes
    hermes-jarvis#75."""

    def test_bullet_not_applicable_accepted_on_docs_review(self) -> None:
        """The Tony-2026-06-10 case: bullet `not_applicable: <reason>`
        passes when body doesn't trigger code_change_context."""
        verdict = """\
verdict: reject

test_quality:
  imports_match_deliverable_entrypoints: false
  evidence:
    - not_applicable: no tests directory or *.test.* files reference swarm status
"""
        v = verify_reviewer_fields(
            assignee="tony",
            body="Review the Friday widget deliverable",
            result=verdict,
        )
        assert v is None

    def test_bullet_not_applicable_blocked_on_code_review(self) -> None:
        """For code-touching reviews (body triggers adversarial),
        evidence honest-empty escapes are NOT accepted — same rule
        already applied to inline `evidence: none`."""
        verdict = """\
verdict: approve

test_quality:
  imports_match_deliverable_entrypoints: true
  evidence:
    - not_applicable: no tests for this route handler

adversarial_pass:
  env_vars: []
  request_inputs: []
  file_paths: []
  external_io: []
"""
        v = verify_reviewer_fields(
            assignee="tony",
            body="Review changes to app/api/metrics/route.ts",
            result=verdict,
        )
        assert v is not None
        assert "test_quality.evidence" in v.missing_fields

    def test_bullet_not_applicable_with_short_reason_rejected(self) -> None:
        """Reason must be ≥8 chars (matching the existing imports_match
        not_applicable rule). Short reasons should be rejected so
        workers don't bypass with `not_applicable: ok`."""
        verdict = """\
verdict: reject

test_quality:
  imports_match_deliverable_entrypoints: false
  evidence:
    - not_applicable: ok
"""
        v = verify_reviewer_fields(
            assignee="tony",
            body="Review the deliverable",
            result=verdict,
        )
        assert v is not None
        assert "test_quality.evidence" in v.missing_fields

    def test_bullet_not_applicable_with_empty_reason_rejected(self) -> None:
        verdict = """\
verdict: reject

test_quality:
  imports_match_deliverable_entrypoints: false
  evidence:
    - not_applicable:
"""
        v = verify_reviewer_fields(
            assignee="tony",
            body="Review the deliverable",
            result=verdict,
        )
        assert v is not None
        assert "test_quality.evidence" in v.missing_fields

    def test_real_citations_still_pass(self) -> None:
        """Backwards-compat: a real bullet-list of citations still
        passes (the canonical happy path)."""
        verdict = """\
verdict: approve

test_quality:
  imports_match_deliverable_entrypoints: true
  evidence:
    - tests/integration.test.ts:42 calls app/api/metrics/route.ts:GET
"""
        v = verify_reviewer_fields(
            assignee="tony",
            body="Review changes to lib/foo.ts",
            result=verdict,
        )
        assert v is None

    def test_inline_evidence_none_still_works(self) -> None:
        """Backwards-compat: inline `evidence: none` (the original
        honest-empty form) continues to work on non-code reviews."""
        verdict = """\
verdict: reject

test_quality:
  imports_match_deliverable_entrypoints: not_applicable: pure docs reshuffle
  evidence: none
"""
        v = verify_reviewer_fields(
            assignee="tony",
            body="Review the README rewrite",
            result=verdict,
        )
        assert v is None

    def test_bullet_with_other_bullets_still_accepted(self) -> None:
        """A reviewer who lists 2 real citations AND one
        not_applicable bullet should still pass — the not_applicable
        is the escape hatch, not a requirement."""
        verdict = """\
verdict: approve

test_quality:
  imports_match_deliverable_entrypoints: true
  evidence:
    - tests/integration.test.ts:42 calls lib/foo.ts:bar
    - tests/unit.test.ts:88 covers lib/foo.ts:baz
"""
        v = verify_reviewer_fields(
            assignee="tony",
            body="Review the deliverable",
            result=verdict,
        )
        assert v is None


# =====================================================================
# hermes-jarvis#74 — honest-reject floor bypass (integration)
# =====================================================================


class TestHonestRejectIntegration:
    """End-to-end through complete_task: a reviewer who completes fast
    with `verdict: reject` should not trip the runtime-floor gate."""

    @pytest.fixture
    def tony_running_task(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "k.db"))
        conn = kb.connect(board="default")
        import time
        now = int(time.time())
        # started_at = now-20s, way under the 90s reviewer floor
        conn.execute(
            "INSERT INTO tasks (id, title, body, status, assignee, started_at, "
            "  created_at, workspace_kind, workspace_path) "
            "VALUES ('t_rev', 'Tony fast review', 'Review the docs reshuffle', "
            "        'running', 'tony', ?, ?, 'scratch', NULL)",
            (now - 20, now),
        )
        conn.commit()
        yield conn
        conn.close()

    def test_tony_fast_reject_passes_floor(self, tony_running_task) -> None:
        """The 2026-06-10 case end-to-end: Tony submits a structured
        reject in 20s. Old behavior: rejected by floor. New: accepted."""
        verdict = """\
verdict: reject

test_quality:
  imports_match_deliverable_entrypoints: false
  evidence:
    - not_applicable: docs-only review, no tests cover the deliverable
"""
        ok = kb.complete_task(
            tony_running_task, "t_rev",
            summary="reject — docs review", result=verdict,
        )
        assert ok

    def test_tony_fast_approve_still_blocked_by_floor(
        self, tony_running_task,
    ) -> None:
        """The other half of the bypass: approves still need the floor
        (rubber-stamp protection unchanged)."""
        good_verdict = """\
verdict: approve

test_quality:
  imports_match_deliverable_entrypoints: not_applicable: pure docs
  evidence: none
"""
        with pytest.raises(kb.CompletionGateError):
            kb.complete_task(
                tony_running_task, "t_rev",
                summary="approve docs", result=good_verdict,
            )

    def test_tony_fast_bare_reject_still_blocks_on_reviewer_fields(
        self, tony_running_task,
    ) -> None:
        """The floor bypass for honest-reject doesn't accidentally
        green-light no-evidence rejects. A reviewer who submits
        `verdict: reject` in 20s with NO structured test_quality
        fields should still trip the reviewer-fields gate. The floor
        gate passes (because verdict is reject) but reviewer-fields
        catches the missing discipline."""
        with pytest.raises(kb.CompletionGateError) as excinfo:
            kb.complete_task(
                tony_running_task, "t_rev",
                summary="bare reject", result="verdict: reject\n",
            )
        # Verify it was the REVIEWER-FIELDS gate that caught it, not
        # the runtime-floor gate.
        violations = excinfo.value.violations
        kinds = {type(v).__name__ for v in violations}
        assert "MissingReviewerFieldViolation" in kinds
        assert "RuntimeFloorViolation" not in kinds

    def test_tony_no_verdict_still_subject_to_floor(
        self, tony_running_task,
    ) -> None:
        """A null verdict (no canonical `verdict: ...` line) must NOT
        be treated as honest reject — that would let any reviewer
        bypass the floor by simply omitting a verdict. parsed_verdict
        is None, is_honest_reject is False, floor fires normally."""
        with pytest.raises(kb.CompletionGateError) as excinfo:
            kb.complete_task(
                tony_running_task, "t_rev",
                summary="no verdict", result="Just my thoughts, no verdict line.",
            )
        kinds = {type(v).__name__ for v in excinfo.value.violations}
        assert "RuntimeFloorViolation" in kinds


# =====================================================================
# hermes-jarvis#77 — progressive runtime-floor error escalation
# =====================================================================


class TestProgressiveFloorEscalation:
    """When a worker keeps retrying the same below-floor completion,
    the second attempt's error message escalates from option-style
    ('Either... or...') to directive ('You must choose ONE of...').
    This breaks the retry-loop pattern that burned Friday's 250-turn
    iteration budget on 2026-06-10. Closes hermes-jarvis#77."""

    def test_first_rejection_uses_default_message(self) -> None:
        v = verify_runtime_floor(
            "tony", 1000, 1020,  # 20s
            prior_floor_rejections=0,
        )
        assert v is not None
        msg = v.message()
        assert "Either keep working" in msg
        assert "kanban_complete" in msg
        # Default message should NOT include the escalation language
        assert "REJECTED FOR THE" not in msg

    def test_second_rejection_escalates(self) -> None:
        v = verify_runtime_floor(
            "tony", 1000, 1020,
            prior_floor_rejections=1,
        )
        assert v is not None
        msg = v.message()
        assert "REJECTED FOR THE 2-th TIME" in msg
        assert "You must choose ONE of" in msg
        assert "Wait" in msg
        assert "Opt out NOW" in msg
        assert "Do NOT repeat" in msg

    def test_third_rejection_counter_advances(self) -> None:
        v = verify_runtime_floor(
            "tony", 1000, 1020,
            prior_floor_rejections=2,
        )
        assert v is not None
        msg = v.message()
        assert "REJECTED FOR THE 3-th TIME" in msg

    def test_floor_elapses_at_in_default_message(self) -> None:
        """The default message includes the wait time."""
        v = verify_runtime_floor(
            "tony", 1000, 1030,  # 30s, 60s remaining
            prior_floor_rejections=0,
        )
        assert v is not None
        msg = v.message()
        assert "Wait" in msg or "wait" in msg
        # 90 - 30 = 60 seconds remaining
        assert "60s" in msg

    def test_floor_elapses_at_encoded_correctly(self) -> None:
        v = verify_runtime_floor(
            "tony", started_at=1000, completed_at=1020,
        )
        assert v is not None
        assert v.floor_elapses_at == 1000 + 90  # tony's floor

    def test_friday_progressive_for_build_role(self) -> None:
        """Build roles also get escalation, with their 5min floor."""
        v = verify_runtime_floor(
            "friday", 1000, 1060,  # 60s
            prior_floor_rejections=1,
        )
        assert v is not None
        msg = v.message()
        assert "REJECTED FOR THE 2-th TIME" in msg
        # 300 - 60 = 240 seconds remaining
        assert "240s" in msg


class TestProgressiveFloorIntegration:
    """End-to-end: a worker that calls complete twice in a row at
    20s should see escalation on the SECOND CompletionGateError."""

    @pytest.fixture
    def tony_running_task(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "k.db"))
        conn = kb.connect(board="default")
        import time
        now = int(time.time())
        conn.execute(
            "INSERT INTO tasks (id, title, body, status, assignee, started_at, "
            "  created_at, workspace_kind, workspace_path) "
            "VALUES ('t_esc', 'Tony fast review', 'Review the docs', "
            "        'running', 'tony', ?, ?, 'scratch', NULL)",
            (now - 20, now),
        )
        conn.commit()
        yield conn
        conn.close()

    def test_second_attempt_sees_escalated_message(
        self, tony_running_task,
    ) -> None:
        # First attempt: bare approve, hits floor, gets rejected.
        with pytest.raises(kb.CompletionGateError) as excinfo1:
            kb.complete_task(
                tony_running_task, "t_esc",
                summary="approve", result="verdict: approve",
            )
        msg1 = str(excinfo1.value)
        assert "Either keep working" in msg1
        assert "REJECTED FOR THE" not in msg1

        # Second attempt: same input. Should now see escalation.
        with pytest.raises(kb.CompletionGateError) as excinfo2:
            kb.complete_task(
                tony_running_task, "t_esc",
                summary="approve", result="verdict: approve",
            )
        msg2 = str(excinfo2.value)
        assert "REJECTED FOR THE 2-th TIME" in msg2

    def test_count_helper_returns_correct_number(
        self, tony_running_task,
    ) -> None:
        from hermes_cli.kanban_db import _v6_7_count_prior_floor_rejections
        assert _v6_7_count_prior_floor_rejections(tony_running_task, "t_esc") == 0
        with pytest.raises(kb.CompletionGateError):
            kb.complete_task(
                tony_running_task, "t_esc",
                summary="x", result="verdict: approve",
            )
        assert _v6_7_count_prior_floor_rejections(tony_running_task, "t_esc") == 1
        with pytest.raises(kb.CompletionGateError):
            kb.complete_task(
                tony_running_task, "t_esc",
                summary="x", result="verdict: approve",
            )
        assert _v6_7_count_prior_floor_rejections(tony_running_task, "t_esc") == 2


# =====================================================================
# hermes-jarvis#78 — heartbeat surfaces floor-elapsed status
# =====================================================================


class TestHeartbeatFloorStatus:
    """When a worker that was rejected by the runtime floor heartbeats,
    the response includes how long until the floor elapses and a
    retry_complete_now hint. Closes hermes-jarvis#78."""

    @pytest.fixture
    def tony_task_with_rejection(self, tmp_path, monkeypatch):
        """A tony task that has one prior runtime-floor rejection
        recorded in task_events."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "k.db"))
        conn = kb.connect(board="default")
        import time
        now = int(time.time())
        # Tony started 30s ago, so 60s remaining on the 90s floor.
        conn.execute(
            "INSERT INTO tasks (id, title, status, assignee, started_at, "
            "  created_at, workspace_kind, workspace_path) "
            "VALUES ('t_hb', 'Tony review', 'running', 'tony', ?, ?, "
            "        'scratch', NULL)",
            (now - 30, now),
        )
        with pytest.raises(kb.CompletionGateError):
            kb.complete_task(
                conn, "t_hb",
                summary="approve", result="verdict: approve",
            )
        yield conn
        conn.close()

    def test_heartbeat_floor_status_includes_elapses_at(
        self, tony_task_with_rejection,
    ) -> None:
        status = kb.v6_7_heartbeat_floor_status(
            tony_task_with_rejection, "t_hb",
        )
        assert status is not None
        assert "elapses_at" in status
        assert "seconds_remaining" in status
        assert "retry_complete_now" in status
        assert "floor_seconds" in status
        assert status["floor_seconds"] == 90

    def test_heartbeat_floor_status_before_elapsed(
        self, tony_task_with_rejection,
    ) -> None:
        """Tony started 30s ago, floor is 90s, so ~60s remaining."""
        status = kb.v6_7_heartbeat_floor_status(
            tony_task_with_rejection, "t_hb",
        )
        assert status["seconds_remaining"] > 0
        assert status["retry_complete_now"] is False

    def test_heartbeat_floor_status_returns_none_when_no_rejection(
        self, tmp_path, monkeypatch,
    ) -> None:
        """A task that hasn't hit the runtime floor returns None
        (no nudge needed)."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "k.db"))
        conn = kb.connect(board="default")
        import time
        now = int(time.time())
        conn.execute(
            "INSERT INTO tasks (id, title, status, assignee, started_at, "
            "  created_at, workspace_kind, workspace_path) "
            "VALUES ('t_clean', 'no rejection', 'running', 'tony', ?, ?, "
            "        'scratch', NULL)",
            (now - 100, now),
        )
        conn.commit()
        assert kb.v6_7_heartbeat_floor_status(conn, "t_clean") is None
        conn.close()

    def test_heartbeat_floor_status_signals_retry_when_elapsed(
        self, tmp_path, monkeypatch,
    ) -> None:
        """When the floor has elapsed (started_at long enough ago),
        retry_complete_now flips to True so the worker knows to
        stop heartbeating and call complete."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "k.db"))
        conn = kb.connect(board="default")
        import time
        now = int(time.time())
        # Started 200s ago (past 90s floor). Tony hit the gate at
        # 5s in — well below floor at the time. Now elapsed.
        conn.execute(
            "INSERT INTO tasks (id, title, status, assignee, started_at, "
            "  created_at, workspace_kind, workspace_path) "
            "VALUES ('t_done', 'past floor', 'running', 'tony', ?, ?, "
            "        'scratch', NULL)",
            (now - 200, now - 200),
        )
        # Forge a rejection that happened at started_at + 5
        from hermes_cli.kanban_db import _append_event
        with kb.write_txn(conn):
            _append_event(
                conn, "t_done", "completion_blocked_v6_7_gates",
                {
                    "violations": [{
                        "kind": "RuntimeFloorViolation",
                        "message": "below the 90s floor",
                    }],
                    "summary_preview": "approve",
                },
            )
        conn.commit()
        status = kb.v6_7_heartbeat_floor_status(conn, "t_done")
        assert status is not None
        assert status["seconds_remaining"] <= 0
        assert status["retry_complete_now"] is True
        conn.close()
