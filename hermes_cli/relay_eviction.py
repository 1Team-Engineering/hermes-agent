"""Evict idle relay scopes."""
import logging
import os
import subprocess
from pathlib import Path

from hermes_cli.kanban_db import list_idle_sessions, evict_session

log = logging.getLogger(__name__)
_RELAY_BIN = Path.home() / ".hermes" / "scripts" / "tmux-relay" / "bin"

DEFAULT_IDLE_SECS = int(os.environ.get("HERMES_RELAY_IDLE_EVICT_SECS", 86400))


def evict_idle_scopes(conn, threshold_secs: int = DEFAULT_IDLE_SECS) -> list:
    """Evict relay scopes idle longer than *threshold_secs*.

    For each idle scope:
      1. Tear down the tmux session via relay-kill-scope.sh.
      2. Delete the task_sessions row via evict_session.

    Returns a list of scope_slugs that were evicted. Errors from
    relay-kill-scope.sh are logged but do not block row deletion —
    the session may already be gone.

    Pass threshold_secs=0 to disable eviction entirely (returns []).
    The env var HERMES_RELAY_IDLE_EVICT_SECS overrides the default
    (86400 = 24 h).
    """
    if threshold_secs <= 0:
        log.info("idle eviction disabled (threshold=0)")
        return []
    evicted = []
    for row in list_idle_sessions(conn, threshold_secs):
        slug = row["scope_slug"]
        try:
            subprocess.run(
                [str(_RELAY_BIN / "relay-kill-scope.sh"), row["profile"], row["project"]],
                capture_output=True, text=True, timeout=30,
            )
        except Exception as e:
            log.warning("relay-kill-scope failed for %s: %s", slug, e)
        evict_session(conn, slug)
        evicted.append(slug)
    log.info("evicted %d idle scopes: %s", len(evicted), evicted)
    return evicted
