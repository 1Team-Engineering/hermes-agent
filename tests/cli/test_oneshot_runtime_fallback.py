"""Tests for hermes_cli.oneshot._resolve_runtime_with_fallback.

Closes hermes-agent#6: when the worker's primary provider auth fails during
startup (e.g. xAI OAuth token expired, Anthropic logged out, Codex revoked),
the configured fallback_providers chain should be tried before the worker
gives up. The fallback chain was previously only honored AFTER successful
credential resolution, not during initial startup.

Three safety bounds preserved:
1. Explicit CLI pin (--model/--provider) → no silent downgrade, raise.
2. Rate-limit AuthError on primary → don't burn the chain, let rate-limit retry.
3. Successful fallback → AIAgent only sees the REMAINING chain entries (no
   re-attempt of dead primary or already-used entry).
"""
from __future__ import annotations

import logging

import pytest

from hermes_cli.auth import AuthError
from hermes_cli.oneshot import _resolve_runtime_with_fallback


def _runtime(provider: str, model: str = "x", api_key: str = "k") -> dict:
    return {
        "provider": provider,
        "model": model,
        "api_key": api_key,
        "base_url": "https://example.test",
        "api_mode": "openai_chat",
    }


def test_primary_succeeds_no_fallback_attempted() -> None:
    calls: list[tuple] = []

    def fake_resolve(*, requested, target_model, explicit_base_url):
        calls.append((requested, target_model, explicit_base_url))
        return _runtime(provider=requested or "auto", model=target_model or "x")

    runtime, model, landed_idx, remaining = _resolve_runtime_with_fallback(
        resolve_runtime_provider=fake_resolve,
        effective_provider="anthropic",
        effective_model="claude-opus-4-7",
        explicit_base_url=None,
        fallback_chain=[{"provider": "xai-oauth", "model": "grok-4.3"}],
        logger=logging.getLogger("test"),
    )
    assert runtime["provider"] == "anthropic"
    assert model == "claude-opus-4-7"
    assert landed_idx == -1, "primary succeeded → landed_idx must be -1"
    assert remaining == [{"provider": "xai-oauth", "model": "grok-4.3"}], (
        "primary succeeded → remaining chain unchanged for AIAgent's runtime loop"
    )
    assert len(calls) == 1, "fallback should not have been touched"


def test_primary_auth_fails_first_fallback_succeeds() -> None:
    calls: list[tuple] = []

    def fake_resolve(*, requested, target_model, explicit_base_url):
        calls.append((requested, target_model, explicit_base_url))
        if requested == "xai-oauth":
            raise AuthError(
                "xAI OAuth state is missing access_token",
                provider="xai-oauth",
                code="xai_auth_missing_access_token",
            )
        return _runtime(provider=requested or "auto", model=target_model or "x")

    runtime, model, landed_idx, remaining = _resolve_runtime_with_fallback(
        resolve_runtime_provider=fake_resolve,
        effective_provider="xai-oauth",
        effective_model="grok-4.3",
        explicit_base_url=None,
        fallback_chain=[
            {"provider": "openai-codex", "model": "gpt-5.5"},
            {"provider": "anthropic", "model": "claude-opus-4-7"},
        ],
        logger=logging.getLogger("test"),
    )
    assert runtime["provider"] == "openai-codex"
    assert model == "gpt-5.5", "effective_model should advance to fallback's model"
    assert landed_idx == 0
    assert remaining == [{"provider": "anthropic", "model": "claude-opus-4-7"}], (
        "AIAgent should only see fallbacks AFTER the one we just used"
    )
    assert len(calls) == 2, "primary tried, then first fallback succeeded"


def test_primary_and_first_fallback_fail_second_fallback_succeeds() -> None:
    calls: list[str] = []

    def fake_resolve(*, requested, target_model, explicit_base_url):
        calls.append(requested or "auto")
        if requested in ("xai-oauth", "openai-codex"):
            raise AuthError(f"{requested} auth dead", provider=requested)
        return _runtime(provider=requested or "auto", model=target_model or "x")

    runtime, model, landed_idx, remaining = _resolve_runtime_with_fallback(
        resolve_runtime_provider=fake_resolve,
        effective_provider="xai-oauth",
        effective_model="grok-4.3",
        explicit_base_url=None,
        fallback_chain=[
            {"provider": "openai-codex", "model": "gpt-5.5"},
            {"provider": "anthropic", "model": "claude-opus-4-7"},
        ],
        logger=logging.getLogger("test"),
    )
    assert runtime["provider"] == "anthropic"
    assert model == "claude-opus-4-7"
    assert calls == ["xai-oauth", "openai-codex", "anthropic"]
    assert landed_idx == 1
    assert remaining == [], "landed on last entry → nothing left for AIAgent"


def test_all_providers_fail_last_auth_error_propagates() -> None:
    last_seen = {"provider": None}

    def fake_resolve(*, requested, target_model, explicit_base_url):
        last_seen["provider"] = requested
        raise AuthError(f"{requested} auth dead", provider=requested or "auto", code="xx")

    with pytest.raises(AuthError) as excinfo:
        _resolve_runtime_with_fallback(
            resolve_runtime_provider=fake_resolve,
            effective_provider="xai-oauth",
            effective_model="grok-4.3",
            explicit_base_url=None,
            fallback_chain=[
                {"provider": "openai-codex", "model": "gpt-5.5"},
                {"provider": "anthropic", "model": "claude-opus-4-7"},
            ],
            logger=logging.getLogger("test"),
        )
    # The raised exception should be from the LAST attempted provider so
    # downstream consumers see the final failure mode (not the original primary).
    assert last_seen["provider"] == "anthropic"
    assert "anthropic" in str(excinfo.value)


def test_empty_fallback_chain_propagates_primary_error_verbatim() -> None:
    primary_err = AuthError("primary dead", provider="xai-oauth", code="dead")

    def fake_resolve(*, requested, target_model, explicit_base_url):
        raise primary_err

    with pytest.raises(AuthError) as excinfo:
        _resolve_runtime_with_fallback(
            resolve_runtime_provider=fake_resolve,
            effective_provider="xai-oauth",
            effective_model="grok-4.3",
            explicit_base_url=None,
            fallback_chain=[],
            logger=logging.getLogger("test"),
        )
    assert excinfo.value is primary_err, "no fallback configured → original error must surface unchanged"


def test_fallback_without_explicit_model_keeps_primary_model() -> None:
    """A fallback entry that omits 'model' should leave effective_model alone."""
    def fake_resolve(*, requested, target_model, explicit_base_url):
        if requested == "xai-oauth":
            raise AuthError("dead", provider="xai-oauth")
        return _runtime(provider=requested or "auto", model=target_model or "x")

    runtime, model, _, _ = _resolve_runtime_with_fallback(
        resolve_runtime_provider=fake_resolve,
        effective_provider="xai-oauth",
        effective_model="grok-4.3",
        explicit_base_url=None,
        fallback_chain=[{"provider": "openai-codex"}],  # no model key
        logger=logging.getLogger("test"),
    )
    assert runtime["provider"] == "openai-codex"
    assert model == "grok-4.3", "effective_model unchanged when fallback has no model"


# === Safety-bound tests (closes P0 review findings) ===


def test_explicit_pin_does_not_fall_back_even_with_chain() -> None:
    """User pinned --model/--provider on CLI → silent downgrade would surprise.
    Re-raise primary AuthError verbatim with no fallback attempt.
    """
    primary_err = AuthError(
        "xAI OAuth missing access_token", provider="xai-oauth",
        code="xai_auth_missing_access_token",
    )
    calls: list[str] = []

    def fake_resolve(*, requested, target_model, explicit_base_url):
        calls.append(requested or "auto")
        raise primary_err

    with pytest.raises(AuthError) as excinfo:
        _resolve_runtime_with_fallback(
            resolve_runtime_provider=fake_resolve,
            effective_provider="xai-oauth",
            effective_model="grok-4.3",
            explicit_base_url=None,
            fallback_chain=[
                {"provider": "openai-codex", "model": "gpt-5.5"},
            ],
            logger=logging.getLogger("test"),
            explicit_pin=True,
        )
    assert excinfo.value is primary_err
    assert calls == ["xai-oauth"], "explicit_pin must skip fallback chain entirely"


def test_rate_limit_auth_error_on_primary_does_not_burn_chain() -> None:
    """Rate-limit on primary should re-raise immediately — falling through
    would burn the quota of every other configured provider in milliseconds.
    Existing rate-limit handling (cli.py exit code 75) gets the task requeued.
    """
    # Construct a real rate-limit AuthError that is_rate_limited_auth_error
    # will recognize — needs `code=CODEX_RATE_LIMITED_CODE` and
    # `relogin_required=False` per auth.py:746-750.
    from hermes_cli.auth import CODEX_RATE_LIMITED_CODE
    primary_err = AuthError(
        "rate limit exceeded",
        provider="openai-codex",
        code=CODEX_RATE_LIMITED_CODE,
        relogin_required=False,
    )
    calls: list[str] = []

    def fake_resolve(*, requested, target_model, explicit_base_url):
        calls.append(requested or "auto")
        raise primary_err

    with pytest.raises(AuthError) as excinfo:
        _resolve_runtime_with_fallback(
            resolve_runtime_provider=fake_resolve,
            effective_provider="openai-codex",
            effective_model="gpt-5.5",
            explicit_base_url=None,
            fallback_chain=[
                {"provider": "xai-oauth", "model": "grok-4.3"},
                {"provider": "anthropic", "model": "claude-opus-4-7"},
            ],
            logger=logging.getLogger("test"),
        )
    assert excinfo.value is primary_err
    assert calls == ["openai-codex"], (
        "rate-limit on primary must skip fallback chain entirely — let rate-limit retry handle it"
    )


def test_same_provider_in_chain_no_infinite_loop() -> None:
    """If the fallback chain (perhaps misconfigured) contains the same
    provider that failed primary, the loop must not retry it forever. Each
    chain entry is attempted at most once; a second AuthError on the same
    provider just advances to the next entry.
    """
    call_count: dict[str, int] = {}

    def fake_resolve(*, requested, target_model, explicit_base_url):
        call_count[requested or "auto"] = call_count.get(requested or "auto", 0) + 1
        if requested == "xai-oauth":
            raise AuthError("xai dead", provider="xai-oauth")
        return _runtime(provider=requested or "auto", model=target_model or "x")

    runtime, _, landed_idx, _ = _resolve_runtime_with_fallback(
        resolve_runtime_provider=fake_resolve,
        effective_provider="xai-oauth",
        effective_model="grok-4.3",
        explicit_base_url=None,
        fallback_chain=[
            {"provider": "xai-oauth", "model": "grok-4.3"},  # same as primary
            {"provider": "openai-codex", "model": "gpt-5.5"},
        ],
        logger=logging.getLogger("test"),
    )
    assert runtime["provider"] == "openai-codex"
    assert landed_idx == 1, "should have skipped misconfigured same-provider entry"
    assert call_count.get("xai-oauth") == 2, "primary + chain[0] both tried, then stopped"
    assert call_count.get("openai-codex") == 1
