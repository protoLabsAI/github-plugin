"""github plugin — read/write GitHub tools over `gh`, with per-agent write gating.

`register(registry)` is the ONLY place plugin code runs. The READ tools are always
registered; the WRITE tools are registered ONLY when the agent's config sets
`github.write: true` — that's the per-agent gate (each instance has its own config,
ADR 0019), so the same plugin serves a read-only research agent and a write-capable
coding/PM agent.

Host-only imports stay LAZY (none here) so the test suite imports the modules with no
protoAgent host present.
"""

from __future__ import annotations

import logging

log = logging.getLogger("protoagent.plugins.github")


def register(registry) -> None:
    cfg = registry.config or {}
    write_enabled = bool(cfg.get("write", False))

    # READ tools — always available (they return an error string if `gh`/auth is missing).
    n_read = 0
    try:
        from .read_tools import get_read_tools

        read = get_read_tools()
        for t in read:
            registry.register_tool(t)
        n_read = len(read)
    except Exception:  # noqa: BLE001 — never let one group sink the rest
        log.exception("[github] registering read tools failed")

    # WRITE tools — GATED: registered only when github.write is true.
    n_write = 0
    if write_enabled:
        try:
            from .write_tools import get_write_tools

            write = get_write_tools()
            for t in write:
                registry.register_tool(t)
            n_write = len(write)
        except Exception:  # noqa: BLE001
            log.exception("[github] registering write tools failed")

    log.info(
        "[github] registered %d read tool(s)%s",
        n_read,
        f" + {n_write} write tool(s) (write enabled)" if write_enabled else " (read-only — github.write is false)",
    )
