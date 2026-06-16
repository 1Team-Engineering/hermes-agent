"""Tests for derive_project, derive_project_root, build_task_header."""
import os
import subprocess
import pytest

from agent.claude_code_relay_helpers import (
    derive_project,
    derive_project_root,
    build_task_header,
    ProviderError,
    SCRATCH_WORKSPACES_ROOT,
)


def test_derive_project_git_repo(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    assert derive_project(str(tmp_path)) == tmp_path.name


def test_derive_project_scratch():
    assert derive_project(f"{SCRATCH_WORKSPACES_ROOT}/t_abc12345") == "scratch"


def test_derive_project_raises_for_non_git_non_scratch(tmp_path):
    with pytest.raises(ProviderError, match="Cannot derive project"):
        derive_project(str(tmp_path))


def test_derive_project_root_scratch():
    assert derive_project_root(f"{SCRATCH_WORKSPACES_ROOT}/t_x") == SCRATCH_WORKSPACES_ROOT


def test_derive_project_root_git(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    root = derive_project_root(str(tmp_path))
    # On macOS /tmp resolves to /private/tmp; compare via realpath
    assert os.path.realpath(root) == os.path.realpath(str(tmp_path))


def test_build_task_header_simple():
    h = build_task_header(task_id="t_2d77d8b1", workspace="/path/to/wt")
    assert h.startswith("[task_id=t_2d77d8b1 workspace=/path/to/wt]")
    assert h.endswith("\n")


def test_build_task_header_preserves_spaces():
    h = build_task_header(task_id="t_x", workspace="/path with space/x")
    assert "/path with space/x" in h


def test_ensure_scope_spawns_if_missing(monkeypatch, tmp_path):
    from agent.claude_code_relay_transport import ensure_scope, ScopeContext
    import sqlite3
    from hermes_cli.kanban_db import init_db, get_session

    spawned = []
    def fake_run(args, **kw):
        spawned.append(args)
        class R:
            returncode = 0
            stdout = "spawned: claude-test-x\n"
            stderr = ""
        return R()
    monkeypatch.setattr("subprocess.run", fake_run)
    monkeypatch.setattr(
        "agent.claude_code_relay_transport.tmux_session_alive",
        lambda name: False,
    )
    monkeypatch.setattr(
        "agent.claude_code_relay_transport._check_prereqs",
        lambda: None,
    )
    # derive_project_root calls subprocess.check_output (git); stub it out
    monkeypatch.setattr(
        "agent.claude_code_relay_transport.derive_project_root",
        lambda workspace: workspace,
    )

    conn = sqlite3.connect(":memory:")
    init_db(conn)

    ctx = ScopeContext(profile="test", project="x", workspace=str(tmp_path))
    ensure_scope(conn, ctx)

    assert any("relay-spawn-scope.sh" in str(a) for a in spawned[0])
    assert get_session(conn, "test-x") is not None


def test_ensure_scope_noops_if_alive(monkeypatch, tmp_path):
    from agent.claude_code_relay_transport import ensure_scope, ScopeContext
    import sqlite3
    from hermes_cli.kanban_db import init_db, upsert_session

    called = []
    def fake_run(*a, **kw):
        called.append(a)
        class R:
            returncode = 0
            stdout = ""
            stderr = ""
        return R()
    monkeypatch.setattr("subprocess.run", fake_run)
    monkeypatch.setattr(
        "agent.claude_code_relay_transport.tmux_session_alive",
        lambda name: True,
    )
    monkeypatch.setattr(
        "agent.claude_code_relay_transport._check_prereqs",
        lambda: None,
    )

    conn = sqlite3.connect(":memory:")
    init_db(conn)
    upsert_session(conn, scope_slug="test-x", profile="test", project="x",
                   tmux_session="claude-test-x", fifo_path="/tmp/f",
                   mcp_config_path="/tmp/m", project_root=str(tmp_path),
                   scope_cwd=str(tmp_path))
    ctx = ScopeContext(profile="test", project="x", workspace=str(tmp_path))
    ensure_scope(conn, ctx)  # should NOT spawn
    spawn_calls = [a for a in called if a and len(a) > 0 and isinstance(a[0], list)
                   and any("relay-spawn-scope" in str(x) for x in a[0])]
    assert not spawn_calls


def test_missing_binary_raises_provider_error(monkeypatch):
    from agent.claude_code_relay_transport import _check_prereqs
    from agent.claude_code_relay_helpers import ProviderError
    monkeypatch.setattr("shutil.which", lambda b: None)
    with pytest.raises(ProviderError, match="required binary"):
        _check_prereqs()


def test_chat_completion_returns_openai_shape(monkeypatch, tmp_path):
    from agent.claude_code_relay_transport import chat_completion
    import sqlite3
    from hermes_cli.kanban_db import init_db

    monkeypatch.setattr(
        "agent.claude_code_relay_transport.send_turn",
        lambda conn, ctx, *, task_id, user_text, timeout=300: "answer text",
    )

    conn = sqlite3.connect(":memory:")
    init_db(conn)

    resp = chat_completion(
        conn,
        messages=[{"role": "user", "content": "hi"}],
        model="sonnet",
        profile="test", project="x",
        workspace=str(tmp_path), task_id="t_test",
    )
    assert resp["choices"][0]["message"]["role"] == "assistant"
    assert resp["choices"][0]["message"]["content"] == "answer text"
    assert resp["choices"][0]["finish_reason"] == "stop"
    assert resp["model"] == "sonnet"
    assert resp["object"] == "chat.completion"


def test_stop_hook_missing_raises_provider_error(monkeypatch, tmp_path):
    from agent.claude_code_relay_transport import _check_stop_hook
    from agent.claude_code_relay_helpers import ProviderError
    settings = tmp_path / "settings.json"
    settings.write_text('{"hooks":{}}')
    with pytest.raises(ProviderError, match="Stop hook"):
        _check_stop_hook(str(settings))


def test_stop_hook_present_passes(tmp_path):
    from agent.claude_code_relay_transport import _check_stop_hook
    settings = tmp_path / "settings.json"
    settings.write_text(
        '{"hooks":{"Stop":[{"hooks":[{"type":"command",'
        '"command":"echo done > $HERMES_RELAY_FIFO"}]}]}}'
    )
    _check_stop_hook(str(settings))  # no raise


def test_stop_hook_settings_missing_raises(tmp_path):
    from agent.claude_code_relay_transport import _check_stop_hook
    from agent.claude_code_relay_helpers import ProviderError
    nonexistent = tmp_path / "does-not-exist.json"
    with pytest.raises(ProviderError, match="settings.json"):
        _check_stop_hook(str(nonexistent))


def test_mcp_config_is_chmod_600(tmp_path):
    from agent.claude_code_relay_transport import _write_mcp_config, ScopeContext
    ctx = ScopeContext(profile="friday", project="cv", workspace=str(tmp_path))
    p = _write_mcp_config(ctx)
    try:
        mode = oct(p.stat().st_mode & 0o777)
        assert mode == "0o600", f"expected 0o600, got {mode}"
    finally:
        if p.exists():
            p.unlink()


def test_scope_context_rejects_path_traversal_in_profile():
    from agent.claude_code_relay_transport import ScopeContext
    from agent.claude_code_relay_helpers import ProviderError
    with pytest.raises(ProviderError, match="invalid profile"):
        ScopeContext(profile="../evil", project="x", workspace="/tmp")


def test_scope_context_rejects_slash_in_project():
    from agent.claude_code_relay_transport import ScopeContext
    from agent.claude_code_relay_helpers import ProviderError
    with pytest.raises(ProviderError, match="invalid project"):
        ScopeContext(profile="friday", project="a/b", workspace="/tmp")


def test_scope_context_accepts_valid_names():
    from agent.claude_code_relay_transport import ScopeContext
    ctx = ScopeContext(profile="friday", project="hermes-agent_v2", workspace="/tmp")
    assert ctx.slug == "friday-hermes-agent_v2"


def test_provider_registered():
    import importlib
    import providers as _pmod
    # Reset discovery so importing the new plugin module takes effect
    _pmod._discovered = False
    _pmod._REGISTRY.pop("claude-code-relay", None)
    _pmod._ALIASES.pop("ccr", None)

    # Force-import the plugin module via importlib (mirrors _import_plugin_dir)
    from pathlib import Path
    plugin_init = (
        Path(__file__).resolve().parent.parent
        / "plugins" / "model-providers" / "claude-code-relay" / "__init__.py"
    )
    spec = importlib.util.spec_from_file_location(
        "plugins.model_providers.claude_code_relay",
        plugin_init,
        submodule_search_locations=[str(plugin_init.parent)],
    )
    mod = importlib.util.module_from_spec(spec)
    import sys
    sys.modules.pop("plugins.model_providers.claude_code_relay", None)
    spec.loader.exec_module(mod)

    from providers import get_provider_profile
    profile = get_provider_profile("claude-code-relay")
    assert profile is not None
    assert profile.api_mode == "claude_code_relay"
