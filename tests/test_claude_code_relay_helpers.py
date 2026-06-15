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
