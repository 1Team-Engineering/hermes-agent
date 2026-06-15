"""claude-code-relay provider: routes chat-completions through interactive
`claude` CLI sessions in tmux. Bills against Max plan; the relay binary
handles its own OAuth — no API key needed at the Hermes layer.
"""

from providers import register_provider
from providers.base import ProviderProfile

claude_code_relay = ProviderProfile(
    name="claude-code-relay",
    aliases=("ccr",),
    api_mode="claude_code_relay",
    display_name="Claude Code Relay",
    description="Routes completions through tmux-hosted claude CLI sessions (Max plan)",
    auth_type="none",
    supports_health_check=False,   # no REST endpoint to probe
    fallback_models=("claude-sonnet-4-5", "claude-opus-4-5", "claude-haiku-4-5"),
)

register_provider(claude_code_relay)
