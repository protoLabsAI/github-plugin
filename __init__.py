"""github plugin — read/write GitHub tools over `gh`, with per-agent write gating.

`register(registry)` is the ONLY place plugin code runs. The READ tools are always
registered; the WRITE tools are registered ONLY when the agent's config sets
`github.write: true` — that's the per-agent gate (each instance has its own config,
ADR 0019), so the same plugin serves a read-only research agent and a write-capable
coding/PM agent.

It also owns the user-only `/issue` chat control command (the write the model must
NOT do autonomously): registered via the host's `register_chat_command` seam when the
host provides it, reading the configured `default_repo`/`repos` for routing. On an
older host without that seam, `/issue` is simply skipped — the tools still load.

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

    # /issue — the user-only chat control command (not an agent tool). Registered via
    # the host's chat-command seam; guarded so an older host (no seam) still loads the
    # tools above. Reads the plugin's own configured default repo (no host coupling).
    issue_cmd = False
    if hasattr(registry, "register_chat_command"):
        try:
            from .gh_issue import effective_default_repo, run_issue_command

            default_repo = effective_default_repo(cfg.get("default_repo", ""), cfg.get("repos", []))

            async def _issue(rest: str, session_id: str) -> str:
                """File a GitHub issue (user-only). Usage: /issue <title> [--bug|--feature] [--repo owner/name]."""
                return await run_issue_command(rest, default_repo=default_repo)

            registry.register_chat_command("issue", _issue)
            issue_cmd = True
        except Exception:  # noqa: BLE001
            log.exception("[github] registering the /issue command failed")

    log.info(
        "[github] registered %d read tool(s)%s%s",
        n_read,
        f" + {n_write} write tool(s) (write enabled)" if write_enabled else " (read-only — github.write is false)",
        " + /issue command" if issue_cmd else "",
    )
