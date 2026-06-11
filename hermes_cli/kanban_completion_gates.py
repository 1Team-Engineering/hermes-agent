"""Verification gates run before `complete_task` writes `status=done`.

Each gate is a pure function that takes structured inputs and returns either
``None`` (pass) or a violation dataclass describing why the completion should
be rejected. The caller (``complete_task`` in ``kanban_db.py``) collects any
violation, emits an auditable event, and raises so the worker layer surfaces a
structured retry message.

Pattern mirrors the existing ``_verify_created_cards`` /
``HallucinatedCardsError`` flow — gates fire BEFORE the write transaction so
state is unchanged on rejection and the worker can simply retry with corrected
output.

Gates so far (v6.7 + v6.8, closing #28, #62, #64, #73, #74, #77, #78, #79):

1. :func:`verify_runtime_floor` — per-role floor on
   ``completed_at - started_at``. Catches Tony's 20-second "approve" verdicts
   and Friday's 59-second "implemented 7 dispatcher gates" claims. v6.8
   adds an honest-reject bypass (#74) and progressive message escalation
   (#77/#78) so workers stop bare-retrying the same payload.

2. :func:`verify_workspace_diff` — when a non-review worker on a
   ``dir`` / ``worktree`` workspace claims to have produced code, the workspace
   must show a real diff against its tracking base. Catches Friday's "Wave A
   gates implemented" with zero changes on the branch.

3. :func:`verify_no_stray_artifacts` — reject untracked artifacts matching
   patterns the swarm has historically committed by accident
   (``*evidence*``, ``commit-hash*``, ``triage/*``, ``tmp-*``, and tracked
   files with no extension and no shebang — the "all prior block evidence
   files" failure mode).

4. :func:`verify_reviewer_fields` — reviewers must produce a structured
   ``verdict:`` line plus the supporting evidence / test_quality bullets
   before completion writes. v6.8 accepts bullet-form ``not_applicable``
   with a ≥20-char reason.

5. :func:`verify_umbrella_review_coverage` (#79) — a goal-mode task
   assigned to an orchestration role (jarvis / pepper / banner) cannot
   complete until at least one review-role descendant exists somewhere
   in its transitive subtree. Forces the umbrella to keep_running until
   reviewers spawn — prevents the 2026-06-10 "JARVIS spawns Pepper +
   Friday then approves itself before any reviewer queues" case.

See hermes-jarvis#61 for the bootstrap-paradox case study that motivates
these gates.
"""
from __future__ import annotations

import os
import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


# =====================================================================
# Per-role runtime floors (#64)
# =====================================================================

# Empirically derived from the 2026-06-09 build-chain failure: any number
# below the floor on a non-orchestration task is more likely fabrication
# than fast work. Workers may opt out per-call via the
# ``x_fast_justified`` metadata field, surfaced through ``allow_below_floor``.
ROLE_RUNTIME_FLOORS_SECONDS: dict[str, int] = {
    # Build / implementation roles — real code changes don't ship in <5 min
    "friday": 5 * 60,
    "shuri": 5 * 60,
    "build-engineer": 5 * 60,
    # Review roles — even a tiny review needs to read the diff
    "tony": 90,
    "tchalla": 90,
    "vision": 90,
    "reviewer": 90,
    # Orchestration roles — JARVIS umbrella spawn can be fast and correct
    "jarvis": 0,
    "pepper": 0,
    "banner": 0,
}

# Role sets — referenced by the floor's honest-reject bypass (#74) and
# the workspace-diff and reviewer-fields gates below. Hoisted here so
# verify_runtime_floor can reference REVIEW_ROLES at call time without
# forward-ref gymnastics.
REVIEW_ROLES = {"tony", "tchalla", "vision", "reviewer"}
ORCHESTRATION_ROLES = {"jarvis", "pepper", "banner"}


@dataclass(frozen=True)
class RuntimeFloorViolation:
    role: str
    started_at: int
    completed_at: int
    floor_seconds: int
    actual_seconds: int
    # hermes-jarvis#77/#78: how many times THIS task has already been
    # rejected by the runtime floor. Used to escalate the error
    # message — bare retries past N=2 become more directive so the
    # worker stops looping and chooses an actual exit (wait OR opt
    # out). The caller in _v6_7_run_completion_gates queries the
    # task's prior completion_blocked_v6_7_gates events to seed this.
    prior_floor_rejections: int = 0
    floor_elapses_at: int = 0  # unix ts when the floor will pass

    def message(self) -> str:
        seconds_remaining = max(0, self.floor_elapses_at - self.completed_at)
        if self.prior_floor_rejections == 0:
            return (
                f"runtime-floor: {self.role} completed in "
                f"{self.actual_seconds}s, below the {self.floor_seconds}s "
                f"floor for this role. Either keep working (wait "
                f"{seconds_remaining}s and re-call kanban_complete) or, if "
                f"the work was genuinely trivial, set "
                f"metadata={{\"x_fast_justified\": \"<≥20-char reason>\"}} "
                f"on the completion call."
            )
        # Progressive escalation: after the first rejection, retrying
        # with the same inputs will only be rejected again. Force the
        # worker to actually choose: wait or opt out.
        return (
            f"runtime-floor: {self.role} REJECTED #"
            f"{self.prior_floor_rejections + 1} — same inputs, "
            f"same result. Bare retries will keep being rejected.\n"
            f"You must choose ONE of:\n"
            f"  (A) Wait {seconds_remaining}s (until the floor elapses at "
            f"epoch {self.floor_elapses_at}), THEN call kanban_complete "
            f"with your current verdict — heartbeat in the meantime, "
            f"don't call complete again until the floor has passed.\n"
            f"  (B) Opt out NOW: call kanban_complete with "
            f"metadata={{\"x_fast_justified\": \"<≥20-char reason "
            f"explaining why the work is genuinely done in "
            f"{self.actual_seconds}s\"}}. This is audited.\n"
            f"Do NOT repeat the previous call verbatim — that's how "
            f"workers burn iteration budgets."
        )


def verify_runtime_floor(
    assignee: Optional[str],
    started_at: Optional[int],
    completed_at: int,
    *,
    allow_below_floor: bool = False,
    is_honest_reject: bool = False,
    prior_floor_rejections: int = 0,
) -> Optional[RuntimeFloorViolation]:
    """Return a violation if the worker's runtime is below its role floor.

    ``started_at`` is the timestamp the dispatcher recorded when the worker
    FIRST claimed the task (set once via ``COALESCE`` in ``claim_task``;
    NOT updated on reclaim). ``completed_at`` is "now" from the
    dispatcher's perspective when ``complete_task`` runs.

    **Reclaim semantics**: the floor is anchored to task LIFETIME, not
    to the current attempt's runtime. After a reclaim, ``actual`` is
    measured from the original ``started_at``, so a second-attempt
    worker that completes 5s after claiming may pass the floor if
    enough lifetime has elapsed since the first attempt. The floor
    was designed to catch fabrication in the FIRST attempt
    specifically; a reclaim usually means the chain has been at it
    for a while, and the discipline already applied to the first
    attempt. If this becomes a real problem, switch to
    ``task_runs.started_at`` for the current run.

    A floor of 0 (or an unknown assignee, or a missing ``started_at``) is a
    pass — we never invent floors for roles we don't know.

    ``is_honest_reject=True`` is a bypass for review-role tasks that
    are completing with ``verdict: reject``. Closes hermes-jarvis#74:
    the floor was designed against rubber-stamp APPROVES; penalizing
    accurate fast rejects creates pressure to either fake-pad time or
    rubber-stamp-approve, neither of which is good. Build/orchestration
    roles get no bypass — a 60-second "implementation" that ends in
    reject still warrants the floor.

    ``prior_floor_rejections`` (hermes-jarvis#77) seeds the
    progressive escalation in ``RuntimeFloorViolation.message()`` so
    bare retries past the first rejection get a more directive error.
    """
    if allow_below_floor:
        return None
    if not assignee or started_at is None:
        return None
    role = assignee.lower()
    if is_honest_reject and role in REVIEW_ROLES:
        return None
    floor = ROLE_RUNTIME_FLOORS_SECONDS.get(role)
    if not floor:
        return None
    actual = max(0, completed_at - int(started_at))
    if actual >= floor:
        return None
    return RuntimeFloorViolation(
        role=assignee, started_at=int(started_at), completed_at=completed_at,
        floor_seconds=floor, actual_seconds=actual,
        prior_floor_rejections=max(0, int(prior_floor_rejections)),
        floor_elapses_at=int(started_at) + floor,
    )


# =====================================================================
# Workspace-diff gate (#62)
# =====================================================================


@dataclass(frozen=True)
class WorkspaceDiffViolation:
    assignee: str
    workspace_path: str
    summary_excerpt: str
    diff_stat: str  # may be empty string if no changes

    def message(self) -> str:
        diff_preview = self.diff_stat.strip() or "(no changes against tracking base)"
        return (
            f"workspace-diff: {self.assignee} summary claims implementation "
            f"work ({self.summary_excerpt!r}) but `git diff` in "
            f"{self.workspace_path} shows: {diff_preview}. "
            f"Either produce the changes the summary describes, or block "
            f"with an honest reason. To skip this check on a doc-only or "
            f"genuinely-no-code task, set metadata={{\"x_no_code\": true}}."
        )


def _git_diff_stat_against_base(workspace_path: str) -> str:
    """Return `git diff --stat` against the workspace's tracking base.

    Tracking base is, in order: ``@{upstream}`` if it exists, else
    ``origin/main`` if it exists, else ``main``. If git rejects all three,
    returns the empty string (gate treats as "no diff").

    Subprocess calls use a hard 10s wallclock so a hung git can't stall the
    dispatcher.
    """
    if not workspace_path or not os.path.isdir(workspace_path):
        return ""
    if not os.path.isdir(os.path.join(workspace_path, ".git")):
        # Worktree-backed dirs have .git as a file pointer; that's fine.
        if not os.path.isfile(os.path.join(workspace_path, ".git")):
            return ""

    def _run(args: list[str]) -> Optional[str]:
        try:
            out = subprocess.run(
                args, cwd=workspace_path, capture_output=True,
                text=True, timeout=10, check=False,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            return None
        if out.returncode != 0:
            return None
        return out.stdout

    for base_spec in ("@{upstream}", "origin/main", "main"):
        # First check the base exists (cheap), then diff against it.
        if _run(["git", "rev-parse", "--verify", base_spec]) is None:
            continue
        stat = _run(["git", "diff", "--stat", base_spec, "HEAD"])
        if stat is not None:
            return stat
    return ""


def verify_workspace_diff(
    assignee: Optional[str],
    workspace_kind: Optional[str],
    workspace_path: Optional[str],
    summary: Optional[str],
    *,
    allow_no_code: bool = False,
) -> Optional[WorkspaceDiffViolation]:
    """Reject completions that claim code work but show no diff.

    Skipped (returns None) when:
    - assignee is a review or orchestration role
    - workspace is scratch (no diff target)
    - workspace_path is missing or not a directory
    - summary doesn't claim implementation
    - caller opted out via ``allow_no_code=True``
    """
    if allow_no_code:
        return None
    if not assignee:
        return None
    role = assignee.lower()
    if role in REVIEW_ROLES or role in ORCHESTRATION_ROLES:
        return None
    if (workspace_kind or "scratch") not in {"dir", "worktree"}:
        return None
    if not workspace_path or not os.path.isdir(workspace_path):
        # Wrong / typo'd path is the dispatcher's problem to surface
        # elsewhere — we don't punish the worker for it.
        return None
    # Build-role workers on a real workspace ALWAYS need a non-empty
    # diff. The earlier implementation only fired when the summary used
    # a trigger verb ("implemented X"), which made the gate trivially
    # bypassable: a worker that wrote "Per spec, the changes land in
    # hermes_cli and tests pass" had no trigger verb and passed even
    # with an empty branch. Build-role + workspace=dir/worktree implies
    # code work; if the worker honestly produced no code, they should
    # call ``kanban_block`` with a reason or opt out via
    # ``x_no_code`` with a string justification. See PR-#11 self-
    # review notes in hermes-jarvis#61.
    diff_stat = _git_diff_stat_against_base(workspace_path)
    # A real implementation produces SOME change line. We only reject when
    # the diff is empty / whitespace.
    if diff_stat and diff_stat.strip():
        return None
    # Defensive — empty/None summary used to crash here on
    # splitlines()[0] before the gate could surface its violation.
    # Fall back to an empty excerpt so the violation is still
    # constructed cleanly (the gate's CompletionGateError message still
    # tells the worker what to do).
    summary_lines = (summary or "").strip().splitlines()
    summary_excerpt = summary_lines[0][:200] if summary_lines else ""
    return WorkspaceDiffViolation(
        assignee=assignee, workspace_path=workspace_path,
        summary_excerpt=summary_excerpt, diff_stat=diff_stat,
    )


# =====================================================================
# Repo-hygiene gate (#28)
# =====================================================================

# Patterns that mark a path as "stray orchestration artifact" rather
# than real source. Matched against the path relative to the repo
# root, case insensitive. Tightened after a self-review false-positive
# audit: matching `evidence-types.md` or `LICENSE` or `Dockerfile`
# would block every Friday completion. Patterns now match only
# segments that are exactly the stray token (not legitimate filenames
# that contain the token as a substring).
#
# Each pattern matches a PATH SEGMENT or a FULL BASENAME and only the
# specific shapes we've seen as accidental commits in prior chains:
# agent-dashboard PR #1's "all prior block evidence files", `commit-
# hash.txt`, the `evidence/` artifact dirs under `changes/`, and
# `triage/` report drops.
_STRAY_PATH_PATTERNS = [
    # `evidence` as a directory segment, or basename `block-*-evidence.*`
    # or `*-evidence.json/png/log/txt`. Excludes `evidence-types.md` (a
    # legitimate source doc) by requiring the segment END at `evidence`.
    re.compile(r"(^|/)evidence(/|$)", re.IGNORECASE),
    re.compile(
        r"(^|/)[^/]*-evidence\.(json|png|log|txt|md|yaml|yml)$",
        re.IGNORECASE,
    ),
    re.compile(r"(^|/)commit-hash(\.[a-z]+)?$", re.IGNORECASE),
    re.compile(r"(^|/)triage(/|$)", re.IGNORECASE),
    re.compile(r"(^|/)tmp-[^/]+$", re.IGNORECASE),
    re.compile(r"(^|/)all prior block evidence files$", re.IGNORECASE),
]

# Tracked basenames that look like they MIGHT be stray (no extension)
# but are universally legitimate source files in many repos. The
# untracked-only scoping below already protects these in practice, but
# we keep the allowlist as defense-in-depth for repos whose history
# includes these as tracked files long before any swarm activity.
_LEGITIMATE_NO_EXT_BASENAMES = {
    "LICENSE", "LICENCE", "COPYING", "NOTICE", "AUTHORS",
    "CHANGELOG", "CONTRIBUTORS", "MAINTAINERS", "OWNERS", "CODEOWNERS",
    "Dockerfile", "Makefile", "Vagrantfile", "Procfile", "Brewfile",
    "Rakefile", "Gemfile", "Guardfile", "Capfile", "Jenkinsfile",
    "Containerfile", "Earthfile", "README",
}


@dataclass(frozen=True)
class StrayArtifactViolation:
    workspace_path: str
    stray_paths: tuple[str, ...]

    def message(self) -> str:
        listing = "\n  ".join(self.stray_paths)
        return (
            f"repo-hygiene: workspace {self.workspace_path} contains files "
            f"that look like leftover orchestration artifacts:\n  {listing}\n"
            f"Delete (or .gitignore) them before calling kanban_complete. "
            f"If a stray-looking path is intentional, prefix it with a real "
            f"file extension and add a one-line comment explaining why it's "
            f"in the repo."
        )


def _has_shebang(path: str) -> bool:
    try:
        with open(path, "rb") as f:
            head = f.read(2)
        return head == b"#!"
    except (OSError, IOError):
        return False


def _stray_path_score(repo_root: str, rel_path: str, *, is_tracked: bool) -> bool:
    """True if ``rel_path`` looks like a stray artifact.

    The specific stray patterns apply to BOTH tracked and untracked
    files (a worker who actually committed ``commit-hash.txt`` is just
    as wrong as one who left it untracked). The fuzzy
    no-extension/no-shebang heuristic only applies to UNTRACKED files —
    repos legitimately track LICENSE / Dockerfile / Makefile, and
    blaming a worker for files that were in main before they started is
    a false positive that teaches the swarm to opt out reflexively.
    """
    norm = rel_path.replace("\\", "/")
    if any(p.search(norm) for p in _STRAY_PATH_PATTERNS):
        return True
    if is_tracked:
        return False
    base = os.path.basename(norm)
    if base in _LEGITIMATE_NO_EXT_BASENAMES:
        return False
    if "." not in base and not _has_shebang(os.path.join(repo_root, rel_path)):
        return True
    return False


def _list_workspace_files_split(
    workspace_path: str,
) -> tuple[list[str], list[str]]:
    """Return ``(tracked, untracked)`` lists of paths in ``workspace_path``.

    Untracked respects .gitignore. Empty lists on any git error.
    """
    if not workspace_path or not os.path.isdir(workspace_path):
        return [], []

    def _run(args: list[str]) -> Optional[str]:
        try:
            out = subprocess.run(
                args, cwd=workspace_path, capture_output=True,
                text=True, timeout=10, check=False,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            return None
        if out.returncode != 0:
            return None
        return out.stdout

    def _split(blob: Optional[str]) -> list[str]:
        if not blob:
            return []
        return [ln.strip() for ln in blob.splitlines() if ln.strip()]

    tracked = _split(_run(["git", "ls-files"]))
    untracked = _split(_run(["git", "ls-files", "--others", "--exclude-standard"]))
    return tracked, untracked


def verify_no_stray_artifacts(
    workspace_kind: Optional[str],
    workspace_path: Optional[str],
    *,
    allow_stray: bool = False,
) -> Optional[StrayArtifactViolation]:
    """Reject completions where the workspace tree contains stray files.

    Skipped (returns None) when:
    - workspace is scratch
    - workspace_path is missing or not a directory
    - caller opted out via ``allow_stray=True``
    """
    if allow_stray:
        return None
    if (workspace_kind or "scratch") not in {"dir", "worktree"}:
        return None
    if not workspace_path or not os.path.isdir(workspace_path):
        return None
    tracked, untracked = _list_workspace_files_split(workspace_path)
    stray: list[str] = []
    for p in tracked:
        if _stray_path_score(workspace_path, p, is_tracked=True):
            stray.append(p)
    for p in untracked:
        if _stray_path_score(workspace_path, p, is_tracked=False):
            stray.append(p)
    stray = sorted(set(stray))
    if not stray:
        return None
    return StrayArtifactViolation(
        workspace_path=workspace_path, stray_paths=tuple(stray),
    )


# =====================================================================
# Reviewer-field gate (#29, #31)
# =====================================================================

# Surfaces in the task body that mean the deliverable touches HTTP /
# server / public-API code. Tightened after self-review: the original
# `\bopenapi\b` and `\bpublic-api\b` patterns false-positived on docs
# bodies (e.g., "Review the OpenAPI spec docs"). New rule: each pattern
# requires path-like context (`app/api/`, `/server/`, `.ts`, etc.) so
# prose mentions don't trigger.
_ADVERSARIAL_TRIGGER_PATTERNS = [
    re.compile(r"\bapp/api/[A-Za-z_\-]", re.IGNORECASE),
    re.compile(r"(^|[\s/])server/[A-Za-z_\-]", re.IGNORECASE),
    re.compile(r"\broute\.ts\b", re.IGNORECASE),
    re.compile(r"\bopenapi[\.-/]", re.IGNORECASE),  # openapi.yaml/openapi-spec/openapi/...
    re.compile(r"\brequest handler.*\.(ts|tsx|js|py|go|rs)\b", re.IGNORECASE),
    re.compile(r"\bhttp endpoint[\s:].*[/\.]", re.IGNORECASE),
]

# Reviewer fields are parsed by walking the verdict text line-by-line
# rather than with regex. YAML-ish indentation makes the boundary rules
# clean: a "field" is a line of the form ``<indent>key:[ value]``, and
# its body is the consecutive following lines indented STRICTLY DEEPER
# than the field line. The next sibling field (same or shallower
# indent) ends the body. This stops the regex-leech bug where one
# section's content was captured as if it belonged to an earlier
# section.
MIN_FIELD_VALUE_LEN = 20

_FIELD_HEADER_RE = re.compile(
    r"^([ \t]*)([A-Za-z_][\w-]*)[ \t]*:[ \t]*(.*?)[ \t]*$"
)
_IMPORTS_VALUE_RE = re.compile(
    r"^(?:(true|false)\b"
    r"|not_applicable[ \t]*:[ \t]*(.+?))[ \t]*$",
    re.IGNORECASE,
)

# Real citations the evidence field must contain — at least one bullet
# item line that names a test path AND a line:column reference. This
# stops a Tony from writing prose ("this is just prose about why we
# approve") into evidence:.
_EVIDENCE_CITATION_RE = re.compile(
    r"(?m)^\s*[-*]\s+.*(?:tests?[/_.][\w/.\-]+|\.test\.[\w.]+|"
    r"_test\.[\w.]+|spec\.[\w.]+)[^\n]*?:\d+",
)


@dataclass(frozen=True)
class MissingReviewerFieldViolation:
    assignee: str
    missing_fields: tuple[str, ...]
    body_excerpt: str

    def message(self) -> str:
        listing = "\n  - ".join(self.missing_fields)
        return (
            f"reviewer-fields: {self.assignee} verdict is missing required "
            f"discipline fields:\n  - {listing}\n"
            f"Each missing field must appear in the verdict text with a real "
            f"value (not a placeholder). Format example:\n"
            f"  test_quality:\n"
            f"    imports_match_deliverable_entrypoints: true\n"
            f"    evidence:\n"
            f"      - tests/integration.test.ts:42 calls app/api/metrics/route.ts:GET\n"
            f"      - tests/integration.test.ts:88 invokes app/[view]/page.tsx default export\n"
            f"  adversarial_pass:\n"
            f"    env_vars:\n"
            f"      - AGENT_DASHBOARD_DB: allowlisted to ~/.hermes/ (lib/ingest.ts:92)\n"
            f"    request_inputs: []\n"
            f"    file_paths:\n"
            f"      - db open paths: allowlisted (lib/ingest.ts:88)\n"
            f"    external_io: []\n"
            f"The evidence field MUST be a bullet list with each item naming "
            f"both a test path (tests/..., *.test.*, or *_test.*) AND a "
            f"line:column reference. Prose evidence is rejected. If a section "
            f"is genuinely not applicable, write it as "
            f"`not_applicable: <reason at least 8 chars>` instead of omitting it."
        )


def _body_triggers_adversarial(body: str) -> bool:
    return any(p.search(body or "") for p in _ADVERSARIAL_TRIGGER_PATTERNS)


def _captured_value_substantive(captured: str) -> bool:
    """True if ``captured`` looks like a real value rather than a
    leech or placeholder."""
    stripped = captured.strip()
    if not stripped:
        return False
    # Explicit empty list / none — honest enumerations are valid.
    head = stripped.split("\n", 1)[0].strip()
    if head in {"[]", "{}", "none", "None", "NONE", "n/a", "N/A"}:
        return True
    return len(stripped) >= MIN_FIELD_VALUE_LEN


def _parse_field(text: str, *path: str) -> Optional[tuple[str, str]]:
    """Walk ``text`` line-by-line and return ``(inline_value, body)`` for
    the field located by ``path`` (e.g., ``("test_quality", "evidence")``),
    or ``None`` if the path doesn't resolve.

    ``inline_value`` is the same-line content after the colon (stripped).
    ``body`` is the concatenation of subsequent lines indented strictly
    deeper than the field's line (newlines preserved). A sibling at
    equal or shallower indent ends the body.

    For nested paths (parent.child), the child's body must be searched
    within the parent's body. The parent body is found first; the child
    is looked up inside it as if the body were a standalone document.
    """
    if not text or not path:
        return None
    lines = text.splitlines()
    # Find the top-level key first.
    head_key = path[0]
    head_idx = None
    head_indent = None
    head_inline = ""
    for i, line in enumerate(lines):
        m = _FIELD_HEADER_RE.match(line)
        if not m:
            continue
        if m.group(2).lower() != head_key.lower():
            continue
        head_idx = i
        head_indent = m.group(1)
        head_inline = m.group(3)
        break
    if head_idx is None:
        return None
    head_body_lines: list[str] = []
    deeper = head_indent + " "  # any string strictly longer than head_indent
    for line in lines[head_idx + 1:]:
        if not line.strip():
            head_body_lines.append(line)
            continue
        leading = len(line) - len(line.lstrip(" \t"))
        if leading <= len(head_indent):
            break
        head_body_lines.append(line)
    if len(path) == 1:
        return head_inline, "\n".join(head_body_lines).strip("\n")
    # Recurse into the body for child path.
    body_text = "\n".join(head_body_lines)
    return _parse_field(body_text, *path[1:])


# For adversarial_pass.* fields, a substantive value must either be
# an explicit empty marker or contain a structural cue that the
# reviewer actually enumerated something: a bullet item, an env-var
# token (UPPER_SNAKE), a path-like substring (a/b), a file extension
# of source code shape, or a colon-separated "<name>: <bound>" on the
# same line. Pure prose ≥20 chars no longer satisfies the gate — that
# was the same shape as the prose-evidence bypass we fixed for
# test_quality.evidence (hermes-jarvis#61 N2 self-review).
_ADVERSARIAL_BULLET_RE = re.compile(r"(?m)^\s*[-*]\s+\S+")
_ADVERSARIAL_ENV_VAR_RE = re.compile(r"\b[A-Z][A-Z0-9_]{2,}\b\s*:")
_ADVERSARIAL_PATH_RE = re.compile(r"\b[\w-]+/[\w./-]+")
# Code-only extensions. Docs/config extensions (md/yaml/yml/json/txt)
# are intentionally excluded — workers commonly mention "README.md" or
# "config.yaml" in prose, which gave a marker-padding bypass through
# the adversarial structure check. If a reviewer's evidence genuinely
# touches a yaml/json/md path, they can still satisfy the gate via the
# path-with-slash regex above (e.g. "configs/app.yaml" matches
# _ADVERSARIAL_PATH_RE).
_ADVERSARIAL_SOURCE_FILE_RE = re.compile(
    r"\b[\w-]+\.(?:ts|tsx|js|jsx|py|go|rs|java|rb|kt|swift|c|cpp|cc|h|hpp|cs|scala|clj|ex|exs|erl|lua|sh)\b",
    re.IGNORECASE,
)


def _has_adversarial_structure(text: str) -> bool:
    return bool(
        _ADVERSARIAL_BULLET_RE.search(text)
        or _ADVERSARIAL_ENV_VAR_RE.search(text)
        or _ADVERSARIAL_PATH_RE.search(text)
        or _ADVERSARIAL_SOURCE_FILE_RE.search(text)
    )

# Empty-marker tokens accepted as honest declarations.
_HONEST_EMPTY_MARKERS = {"none", "[]", "{}", "n/a"}

# hermes-jarvis#75: bullet form of honest-empty for test_quality.evidence.
# The reason must be ≥8 chars (1 non-whitespace + at least 7 more
# characters), mirroring the ``not_applicable`` shape already accepted
# for imports_match.
_BULLET_NOT_APPLICABLE_RE = re.compile(
    r"(?mi)^\s*-\s*not_applicable\s*:\s*(\S.{7,})\s*$",
)


def _adversarial_value_substantive(captured: str) -> bool:
    """True if an adversarial_pass.* value's content shows the reviewer
    enumerated structure rather than padded with prose. Honest empty
    markers are accepted."""
    stripped = captured.strip()
    if not stripped:
        return False
    head = stripped.split("\n", 1)[0].strip().lower()
    if head in _HONEST_EMPTY_MARKERS:
        return True
    return _has_adversarial_structure(stripped)


def _field_present(field_key: str, text: str, *, code_change_context: bool = False) -> bool:
    """True if the verdict text shows the field with a real value.

    ``code_change_context`` (when True) forbids the honest-empty escape
    on ``test_quality.evidence`` — a reviewer of code touching HTTP /
    server surfaces must produce real test citations, not a bare
    ``none``. Closes hermes-jarvis#61 N6 self-review note.
    """
    if field_key == "test_quality.imports_match_deliverable_entrypoints":
        parsed = _parse_field(text or "", "test_quality", "imports_match_deliverable_entrypoints")
        if parsed is None:
            return False
        inline, body = parsed
        candidate = inline.strip() if inline else ""
        if not candidate and body.strip():
            candidate = body.strip().splitlines()[0].strip()
        if not candidate:
            return False
        m = _IMPORTS_VALUE_RE.match(candidate)
        if not m:
            return False
        if m.group(1):
            return True
        reason = (m.group(2) or "").strip()
        return len(reason) >= 8

    # Multi-key field
    path = tuple(field_key.split("."))
    parsed = _parse_field(text or "", *path)
    if parsed is None:
        return False
    inline, body = parsed
    inline = inline.strip()
    body = body.strip("\n")

    # adversarial_pass.* — structure-required.
    if field_key.startswith("adversarial_pass."):
        if inline:
            return _adversarial_value_substantive(inline)
        return _adversarial_value_substantive(body)

    # test_quality.evidence — citation-required, or honest-empty
    # (unless code_change_context forbids the empty escape).
    if field_key == "test_quality.evidence":
        if inline:
            inline_low = inline.lower()
            if inline_low in _HONEST_EMPTY_MARKERS:
                return not code_change_context
            return False  # inline prose like "evidence: see above" never qualifies
        if not _captured_value_substantive(body):
            return False
        head = body.strip().split("\n", 1)[0].strip().lower()
        if head in _HONEST_EMPTY_MARKERS:
            return not code_change_context
        # hermes-jarvis#75: accept ``- not_applicable: <≥8 char reason>``
        # as a bullet equivalent of the inline honest-empty markers.
        # Same code_change_context rule applies — code-touching reviews
        # must produce real citations, not honest-empty escapes.
        if _BULLET_NOT_APPLICABLE_RE.search(body):
            return not code_change_context
        return bool(_EVIDENCE_CITATION_RE.search(body))

    # Other multi-key fields (currently none) fall through.
    if inline:
        return inline.lower() in _HONEST_EMPTY_MARKERS or len(inline) >= MIN_FIELD_VALUE_LEN
    return _captured_value_substantive(body)


def verify_reviewer_fields(
    assignee: Optional[str],
    body: Optional[str],
    result: Optional[str],
    *,
    allow_no_reviewer_fields: bool = False,
) -> Optional[MissingReviewerFieldViolation]:
    """Reject reviewer completions missing structured discipline fields.

    Skipped (returns None) when:
    - assignee isn't a review role
    - caller opted out via ``allow_no_reviewer_fields=True``

    Always required for reviewers:
    - ``test_quality.imports_match_deliverable_entrypoints`` (true|false|not_applicable)
    - ``test_quality.evidence`` — at least one bullet-list line citing a
      test path AND a ``:N`` line reference, OR an explicit empty marker

    Additionally required when the task body mentions HTTP/server
    surfaces in a path-like context (``app/api/<file>``, ``server/<file>``,
    ``route.ts``, ``openapi.<ext>``, etc.):
    - ``adversarial_pass.env_vars`` / ``request_inputs`` / ``file_paths`` /
      ``external_io`` — each substantive or explicit empty marker
    """
    if allow_no_reviewer_fields:
        return None
    if not assignee or assignee.lower() not in REVIEW_ROLES:
        return None
    text = result or ""
    required = [
        "test_quality.imports_match_deliverable_entrypoints",
        "test_quality.evidence",
    ]
    code_change = _body_triggers_adversarial(body or "")
    if code_change:
        required.extend([
            "adversarial_pass.env_vars",
            "adversarial_pass.request_inputs",
            "adversarial_pass.file_paths",
            "adversarial_pass.external_io",
        ])
    missing = [
        f for f in required
        if not _field_present(f, text, code_change_context=code_change)
    ]
    if not missing:
        return None
    # Defensive — empty/None body used to crash on splitlines()[0]
    # before the gate could surface its violation.
    body_lines = (body or "").strip().splitlines()
    body_excerpt = body_lines[0][:200] if body_lines else ""
    return MissingReviewerFieldViolation(
        assignee=assignee, missing_fields=tuple(missing),
        body_excerpt=body_excerpt,
    )


# =====================================================================
# PR-existence gate (#63)
# =====================================================================

# GitHub PR URLs in verdict text or summary. Captures org, repo, and
# pull number for `gh pr view <url>` verification.
_GITHUB_PR_URL_RE = re.compile(
    r"https?://(?:www\.)?github\.com/"
    r"(?P<org>[\w-]+)/(?P<repo>[\w.-]+)/pull/(?P<num>\d+)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class PhantomPRViolation:
    phantom_urls: tuple[str, ...]

    def message(self) -> str:
        listing = "\n  - ".join(self.phantom_urls)
        return (
            f"pr-existence: the verdict references GitHub PR URLs that "
            f"don't resolve via `gh pr view`:\n  - {listing}\n"
            f"Either remove the phantom URL from the verdict, or open the "
            f"PR first (gh pr create) and then re-call kanban_complete. "
            f"This catches the 2026-06-09 Tchalla case where a release-"
            f"gate reviewer 'approved' PR #42 that didn't exist."
        )


def _extract_pr_urls(text: str) -> list[str]:
    if not text:
        return []
    seen: dict[str, None] = {}
    for m in _GITHUB_PR_URL_RE.finditer(text):
        url = m.group(0)
        seen.setdefault(url, None)
    return list(seen.keys())


def _gh_pr_exists(url: str) -> Optional[bool]:
    """Return True if the URL resolves via `gh pr view`, False if it
    explicitly doesn't exist, None on indeterminate (gh missing /
    network error / unauthenticated). Indeterminate falls open (the
    gate doesn't reject) so transient gh problems don't trap workers.

    404 detection is conservative: we look for the GraphQL not-found
    phrase ("could not resolve to a") and the legacy "not found"
    pattern but explicitly exclude network-error variants like "could
    not resolve host" (DNS) and "not found" that co-occurs with
    network/host tokens. Self-review note: a DNS failure used to be
    misclassified as a phantom PR, which then trapped workers behind
    a flaky network.
    """
    import shutil
    if not shutil.which("gh"):
        return None
    try:
        out = subprocess.run(
            ["gh", "pr", "view", url, "--json", "number"],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None
    if out.returncode == 0:
        return True
    stderr = (out.stderr or "").lower()
    network_tokens = ("host", "dial", "timeout", "network", "tls", "i/o timeout")
    is_network = any(tok in stderr for tok in network_tokens)
    if is_network:
        return None
    # The GraphQL 404 wording is exact: "could not resolve to a <Type>".
    if "could not resolve to a" in stderr:
        return False
    if "no pull request" in stderr:
        return False
    # Legacy "not found" only when there's no network token in stderr.
    if "not found" in stderr:
        return False
    return None


def verify_pr_urls_exist(
    result: Optional[str],
    summary: Optional[str],
    *,
    allow_phantom_pr: bool = False,
    gh_pr_exists: callable = _gh_pr_exists,
) -> Optional[PhantomPRViolation]:
    """Reject completions whose verdict/summary references PR URLs
    that don't exist.

    Skipped (returns None) when:
    - no GitHub PR URLs found in either text
    - every URL resolves to True or indeterminate (None)
    - caller opted out via ``allow_phantom_pr=True``

    Indeterminate URLs (gh missing / network error / unauthenticated)
    fall open — workers can still complete when gh is broken.
    """
    if allow_phantom_pr:
        return None
    urls = _extract_pr_urls((result or "") + "\n" + (summary or ""))
    if not urls:
        return None
    phantom = []
    for url in urls:
        verdict = gh_pr_exists(url)
        if verdict is False:
            phantom.append(url)
    if not phantom:
        return None
    return PhantomPRViolation(phantom_urls=tuple(phantom))


# =====================================================================
# Doc-drift gate (#32)
# =====================================================================

# Tenant slugs encode a version in the shape ``marvel-swarm-vN-N-test``
# (or similar). The gate only fires for tasks whose tenant matches —
# generic tenants (e.g. ``hermes-self-improvement``) are unaffected.
_VERSIONED_TENANT_RE = re.compile(
    r"(?P<chain>[a-z]+(?:-[a-z]+)+)-v(?P<major>\d+)-(?P<minor>\d+)(?:-[a-z]+)*",
    re.IGNORECASE,
)

# Files in the workspace to scan for stale version stamps. README and
# CHANGELOG are the canonical drift surfaces; we deliberately don't
# walk the whole tree (too noisy + slow).
_DOC_DRIFT_FILES = ("README.md", "README", "CHANGELOG.md", "CHANGELOG")


@dataclass(frozen=True)
class DocDriftViolation:
    active_version: str
    stale_refs: tuple[tuple[str, str], ...]  # (filename, version_text)

    def message(self) -> str:
        listing = "\n  - ".join(
            f"{fn}: mentions {v} (active is {self.active_version})"
            for fn, v in self.stale_refs
        )
        return (
            f"doc-drift: the workspace's README/CHANGELOG mentions versions "
            f"older than the active chain version ({self.active_version}) "
            f"outside a history/changelog section:\n  - {listing}\n"
            f"Update the doc to reference the current version, or move "
            f"the older mention into a history section (a heading like "
            f"## Previous versions / ## Older versions / a CHANGELOG "
            f"entry under the older heading)."
        )


def _parse_active_version(tenant: str) -> Optional[tuple[int, int]]:
    if not tenant:
        return None
    m = _VERSIONED_TENANT_RE.match(tenant)
    if not m:
        return None
    return (int(m.group("major")), int(m.group("minor")))


def _scan_doc_for_stale(
    path: str, active: tuple[int, int],
) -> list[str]:
    """Return list of stale version strings found in ``path`` outside
    a history/older-versions heading. Empty list = no drift.

    Depth-aware: a ``## History`` block stays in history mode until a
    non-history heading of equal-or-shallower depth appears. So
    ``## History\\n### v6.2 details`` correctly treats the subsection
    as historical content (self-review fix).
    """
    try:
        text = open(path, encoding="utf-8", errors="replace").read()
    except (OSError, IOError):
        return []
    stale: list[str] = []
    version_re = re.compile(r"\bv(\d+)[\.\-](\d+)\b", re.IGNORECASE)
    history_heading_re = re.compile(
        r"^#+\s*.*(?:history|older|previous|changelog|archive)",
        re.IGNORECASE,
    )
    history_depth: Optional[int] = None
    for line in text.splitlines():
        if line.startswith("#"):
            level = len(line) - len(line.lstrip("#"))
            is_hist = bool(history_heading_re.match(line))
            if is_hist:
                history_depth = level
            elif history_depth is not None and level <= history_depth:
                history_depth = None
        if history_depth is not None:
            continue
        for m in version_re.finditer(line):
            v_major, v_minor = int(m.group(1)), int(m.group(2))
            if (v_major, v_minor) < active:
                stale.append(m.group(0))
    return stale


def verify_doc_drift(
    tenant: Optional[str],
    workspace_kind: Optional[str],
    workspace_path: Optional[str],
    *,
    allow_doc_drift: bool = False,
) -> Optional[DocDriftViolation]:
    """Reject completions where README/CHANGELOG mention versions older
    than the active chain version outside a history section.

    Skipped (returns None) when:
    - tenant doesn't encode a version (e.g. plain ``hermes-self-improvement``)
    - workspace is scratch or path doesn't exist
    - none of the canonical doc files exist in the workspace
    - all stale mentions are inside history headings
    - caller opted out via ``allow_doc_drift=True``
    """
    if allow_doc_drift:
        return None
    if not tenant or not workspace_path:
        return None
    if (workspace_kind or "scratch") not in {"dir", "worktree"}:
        return None
    if not os.path.isdir(workspace_path):
        return None
    active = _parse_active_version(tenant)
    if active is None:
        return None
    active_str = f"v{active[0]}.{active[1]}"
    stale: list[tuple[str, str]] = []
    for fn in _DOC_DRIFT_FILES:
        path = os.path.join(workspace_path, fn)
        if not os.path.isfile(path):
            continue
        # CHANGELOG files are historical by nature — older versions
        # there are expected, not stale. Only scan READMEs.
        if "changelog" in fn.lower():
            continue
        for v in _scan_doc_for_stale(path, active):
            stale.append((fn, v))
    if not stale:
        return None
    return DocDriftViolation(
        active_version=active_str,
        stale_refs=tuple(stale),
    )


# =====================================================================
# Umbrella review-coverage gate (#79)
# =====================================================================


@dataclass(frozen=True)
class MissingUmbrellaReviewViolation:
    umbrella_id: str
    has_non_review_descendant: bool

    def message(self) -> str:
        if not self.has_non_review_descendant:
            # Pathological: a goal-mode umbrella with no descendants
            # at all. The orchestrator never decomposed.
            return (
                f"umbrella-review-coverage: this goal-mode umbrella "
                f"({self.umbrella_id}) has no descendants at all. "
                f"A `--goal` umbrella that completes without ever "
                f"spawning child tasks is almost always a misfire — "
                f"either decompose the work into children first, or, "
                f"if this umbrella legitimately has no actionable "
                f"sub-tasks, set "
                f"metadata={{\"x_umbrella_no_review\": \"<≥20-char "
                f"reason — e.g. 'pure status-only ack of upstream "
                f"completion'>\"}}."
            )
        return (
            f"umbrella-review-coverage: this goal-mode umbrella "
            f"({self.umbrella_id}) has build-role descendants but NO "
            f"review-role descendants (tony / tchalla / vision / "
            f"reviewer) anywhere in its task_links subtree. "
            f"Marvel-swarm chains call for at least one per-block "
            f"review before the umbrella archives. Either spawn the "
            f"review task(s) now via kanban_create (--parent <build-"
            f"task-id> --assignee tony/tchalla/vision), THEN re-call "
            f"kanban_complete, or — if this work genuinely doesn't "
            f"need review (rare; usually a misfire) — set "
            f"metadata={{\"x_umbrella_no_review\": \"<≥20-char "
            f"reason>\"}}.\n"
            f"This catches the 2026-06-10 validation case where JARVIS "
            f"spawned Pepper+Friday and exited done without queuing "
            f"any reviewer, leaving Kaipo to hand-spawn the rest of "
            f"the chain. See hermes-jarvis#79."
        )


def verify_umbrella_review_coverage(
    is_goal_mode: bool,
    umbrella_assignee: Optional[str],
    umbrella_id: str,
    descendants: list,  # rows from _v6_7_walk_descendants
    *,
    allow_no_review_needed: bool = False,
) -> Optional[MissingUmbrellaReviewViolation]:
    """Reject an orchestration umbrella's kanban_complete if its
    descendant subtree contains no review-role tasks.

    Closes hermes-jarvis#79. The 2026-06-10 v6.7 validation chain on
    hermes-dashboard had JARVIS spawn Pepper + Friday from the
    umbrella, then call kanban_complete on itself before any reviewer
    was queued. The umbrella showed as done with build-only
    descendants; Kaipo had to manually spawn Tony / Tchalla / Vision
    to continue the chain. This gate forces orchestration umbrellas
    to keep_running until at least one review task exists in their
    subtree.

    Skipped (returns None) when:
    - The task is not goal_mode (avoids per-call CTE on the 99% case).
    - The task's assignee is NOT in ORCHESTRATION_ROLES (i.e., a
      goal-loop Friday/Tony/etc. shouldn't be subject to this
      umbrella discipline — only JARVIS/Pepper/Banner).
    - At least one descendant has a review-role assignee. **Status
      doesn't matter** — a queued / blocked / running review still
      proves the orchestrator decomposed correctly. The integrative-
      review-at-archive gate (#30) handles terminal-status discipline
      at the other end of the lifecycle.
    - Opt-out via ``allow_no_review_needed=True``.

    Differs from the v6.7 #30 integrative-review-at-archive gate:
    - #30 fires at ARCHIVE time and spawns Tchalla after the chain
      settles. The integrative review is intentionally NOT linked
      into ``task_links`` (see ``_v6_7_spawn_integrative_review`` —
      "Intentionally NO parents" to avoid the parents-not-done
      deadlock), so it does NOT satisfy this #79 gate. #79 requires
      a PER-BLOCK review (tony/tchalla/vision in the build subtree).
    - #79 fires at COMPLETE time and forces JARVIS to spawn
      reviewers BEFORE marking itself done — catching the empty-
      chain misfire one step earlier in the orchestration lifecycle.
    """
    if allow_no_review_needed:
        return None
    if not is_goal_mode:
        return None
    # Self-review fix: tighten to orchestration roles only so a
    # goal-loop Friday/Tony doesn't get caught by an umbrella gate.
    # The 2026-06-10 case was specifically a JARVIS misfire.
    if not umbrella_assignee or umbrella_assignee.lower() not in ORCHESTRATION_ROLES:
        return None
    if not descendants:
        return MissingUmbrellaReviewViolation(
            umbrella_id=umbrella_id, has_non_review_descendant=False,
        )
    has_review = False
    for ch in descendants:
        assignee = (ch["assignee"] or "").lower()
        if assignee in REVIEW_ROLES:
            has_review = True
            break  # one is enough to satisfy the gate
    if has_review:
        return None
    # No review descendants — fail. The umbrella does have other
    # children; the message specializes between "no descendants at
    # all" and "build-only descendants" based on count, not the
    # has_non_review flag (which would lie if descendants were
    # all empty-assignee entries).
    return MissingUmbrellaReviewViolation(
        umbrella_id=umbrella_id,
        has_non_review_descendant=bool(descendants),
    )


# =====================================================================
# Exception class for the integration in `complete_task`
# =====================================================================

class CompletionGateError(ValueError):
    """Raised by ``complete_task`` when one or more v6.7 gates reject.

    ``violations`` is a list of dataclasses (one per failed gate). Each has
    a ``.message()`` returning a worker-actionable string. Subclass of
    ``ValueError`` so existing tool-error handlers treat this as a
    recoverable user error (same convention as
    :class:`HallucinatedCardsError`).
    """

    def __init__(self, violations: list, completing_task_id: str):
        self.violations = list(violations)
        self.completing_task_id = completing_task_id
        lines = [v.message() for v in self.violations]
        super().__init__(
            "kanban_complete blocked by v6.7 gates:\n- "
            + "\n- ".join(lines)
        )
