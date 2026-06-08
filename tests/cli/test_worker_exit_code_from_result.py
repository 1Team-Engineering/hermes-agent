"""Tests for cli._worker_exit_code_from_result.

Closes hermes-agent#5. The non-quiet `chat -q "..."` path (the one Marvel
team workers use) previously didn't apply the kanban exit-code mapping at
all — a rate-limited worker exited 0 by virtue of `cli.chat()` returning
cleanly. The dispatcher then classified the 0-exit as a "protocol violation"
and auto-blocked the task.

This helper centralizes the mapping and is now called from BOTH the quiet
and non-quiet single-query paths so the contract is consistent.
"""
from __future__ import annotations

import importlib
import os
import sys
from typing import Any

import pytest


@pytest.fixture
def helper(monkeypatch: pytest.MonkeyPatch):
    """Import cli._worker_exit_code_from_result without triggering CLI side effects."""
    # cli.py is heavyweight; import once via the package.
    import cli
    importlib.reload(cli)
    return cli._worker_exit_code_from_result


def _result(failed: bool = True, failure_reason: str | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"failed": failed}
    if failure_reason is not None:
        out["failure_reason"] = failure_reason
    return out


def test_none_result_returns_zero(helper, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    assert helper(None) == 0


def test_non_dict_result_returns_zero(helper, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    assert helper("just a string") == 0


def test_success_result_returns_zero(helper, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    assert helper({"completed": True, "failed": False}) == 0


def test_failure_outside_kanban_returns_one(helper, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    assert helper(_result(failure_reason="rate_limit")) == 1
    assert helper(_result(failure_reason="billing")) == 1
    assert helper(_result(failure_reason="other")) == 1
    assert helper(_result()) == 1


def test_rate_limit_inside_kanban_returns_75(helper, monkeypatch: pytest.MonkeyPatch) -> None:
    """The bug v6.6 hit: worker rate-limited but exited 0 → dispatcher
    auto-blocked as protocol violation. Fix: exit 75 (EX_TEMPFAIL).
    """
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_abc123")
    code = helper(_result(failure_reason="rate_limit"))
    # Pull the canonical constant to ensure we match the dispatcher's classifier
    from hermes_cli.kanban_db import KANBAN_RATE_LIMIT_EXIT_CODE as RL_CODE
    assert code == RL_CODE == 75


def test_billing_inside_kanban_returns_75(helper, monkeypatch: pytest.MonkeyPatch) -> None:
    """Billing/quota wall is the same recovery story as rate-limit."""
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_abc123")
    from hermes_cli.kanban_db import KANBAN_RATE_LIMIT_EXIT_CODE as RL_CODE
    assert helper(_result(failure_reason="billing")) == RL_CODE


def test_other_failure_inside_kanban_returns_one(helper, monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-rate-limit failures inside a kanban worker still exit 1 — the
    dispatcher treats those as real task failures (worth retry-counting).
    """
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_abc123")
    assert helper(_result(failure_reason="other")) == 1
    assert helper(_result()) == 1  # no failure_reason field


def test_missing_failure_reason_field_inside_kanban_returns_one(
    helper, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Defensive: a result dict that's `failed=True` but doesn't carry a
    failure_reason (e.g. a thrown exception synthesized into a dict) should
    NOT be treated as rate-limited.
    """
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_abc123")
    assert helper({"failed": True}) == 1


def test_rate_limit_no_kanban_env_returns_one(helper, monkeypatch: pytest.MonkeyPatch) -> None:
    """The kanban exit-code mapping only fires for actual workers.
    A human-driven CLI run that hits 429 still gets the generic exit 1.
    """
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    assert helper(_result(failure_reason="rate_limit")) == 1
