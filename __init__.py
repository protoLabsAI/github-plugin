"""github plugin — read/write GitHub tools over `gh`, with per-agent write gating.

`register(registry)` is the ONLY place plugin code runs. The READ tools are always
registered; the WRITE tools are registered ONLY when the agent's config sets
`github.write: true` — that's the per-agent gate (each instance has its own config,
ADR 0019), so the same plugin serves a read-only research agent and a write-capable
coding/PM agent.

It also owns the user-only `/issue` chat control command — the path a PERSON files
from, on any agent, without the model: registered via the host's
`register_chat_command` seam when the host provides it. (The `github_create_issue`
AGENT tool exists too, behind the write gate.) On an older host without that seam,
`/issue` is simply skipped — the tools still load.

And it serves its own console board view (two tabs: Issues / PRs) via two routers
(public PAGE + gated DATA, the notes pattern) when the host exposes `register_router`.

Config is read LIVE (v0.6.0): the tools' default repo, the `/issue` command's
routing, the views' picker, and the `github.token` secret all go through a getter
(`registry.live_config` when the host has it, else the register-time snapshot), so
a Settings edit — or a project the agent onboards mid-session — is seen by the very
next call, not the next register().

Every host-coupled registration (chat command, routers, setup-gap reporting) is
`hasattr`-guarded so the plugin degrades gracefully on an older host: the tools
always load.

Host-only imports stay LAZY (none here) so the test suite imports the modules with no
protoAgent host present.
"""

from __future__ import annotations

import logging

log = logging.getLogger("protoagent.plugins.github")


def register(registry) -> None:
    cfg = registry.config or {}
    write_enabled = bool(cfg.get("write", False))

    # LIVE config: the host's `live_config` re-reads the resolved section (manifest
    # defaults ⊕ YAML ⊕ secrets) per call; without it (older host, tests) the
    # register-time snapshot is the fixed answer. Everything below closes over THIS,
    # never over a value computed once here.
    live = getattr(registry, "live_config", None)
    get_cfg = live if callable(live) else (lambda: cfg)

    def current_cfg() -> dict:
        try:
            return get_cfg() or {}
        except Exception:  # noqa: BLE001 — a failing host read ⇒ the snapshot
            return cfg

    from .gh_cli import set_token_getter
    from .gh_issue import effective_default_repo
    from .projects import effective_repos

    def current_repos() -> list[str]:
        """The picker list, live: explicit `repos` ∪ the host's project registry ∪
        the registered checkouts' origin remotes (projects.py)."""
        return effective_repos(current_cfg().get("repos"))

    def current_default_repo() -> str:
        """The default the tools / `/issue` fall back to when no repo is passed —
        the configured `default_repo`, else the first of `repos` (same resolution
        as the board), evaluated per call."""
        return effective_default_repo(str(current_cfg().get("default_repo") or ""), current_repos())

    # The `github.token` secret (Settings ▸ GitHub) — injected into every `gh` run as
    # GH_TOKEN, winning over an ambient env token. Read live, so a pasted token works
    # without a restart. Process-wide by design: one `gh` runner per process.
    set_token_getter(lambda: str(current_cfg().get("token") or ""))

    # READ tools — always available (they return an error string if `gh`/auth is missing).
    n_read = 0
    try:
        from .read_tools import get_read_tools

        read = get_read_tools(current_default_repo, current_repos)
        for t in read:
            registry.register_tool(t)
        n_read = len(read)
    except Exception:  # noqa: BLE001 — never let one group sink the rest
        log.exception("[github] registering read tools failed")

    # WRITE tools — GATED: registered only when github.write is true.
    n_write = 0
    if write_enabled:
        try:
            from .review_tools import get_review_tools
            from .write_tools import get_write_tools

            # Pass the host's event-bus seam (ADR 0039) when it exists so PR lifecycle
            # events broadcast as `github.pr.opened` / `github.pr.merged`. hasattr-guarded
            # like the other host couplings — an older host just gets no events.
            write = get_write_tools(current_default_repo, emit=getattr(registry, "emit", None)) + get_review_tools(
                current_default_repo
            )
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
            from .gh_issue import run_issue_command

            async def _issue(rest: str, session_id: str) -> str:
                """File a GitHub issue (user-only). Usage: /issue <title> [--bug|--feature] [--repo owner/name]."""
                return await run_issue_command(rest, default_repo=current_default_repo())

            registry.register_chat_command("issue", _issue)
            issue_cmd = True
        except Exception:  # noqa: BLE001
            log.exception("[github] registering the /issue command failed")

    # Console board view (ADR 0026/0038) — two routers, the notes pattern: the PAGE on
    # the PUBLIC /plugins/github prefix (iframe-loadable), the DATA routes on the GATED
    # /api/plugins/github prefix. Guarded so a host without register_router still loads
    # the tools + /issue. The manifest's `views` entry points the console at /view.
    view = False
    if hasattr(registry, "register_router"):
        try:
            from .api import build_data_router, build_view_router

            registry.register_router(build_view_router(), prefix="/plugins/github")
            # The data router reads config per request through the same live getter,
            # so a repo/default_repo edit shows in the board with no server restart — a
            # hot-reload can't re-mount this router, but reading config per request does.
            registry.register_router(build_data_router(current_cfg), prefix="/api/plugins/github")
            view = True
        except Exception:  # noqa: BLE001
            log.exception("[github] registering the board view failed")

    # First-run setup gaps — `gh` missing / not authenticated — reported to the host's
    # operator-warning seam (`report_setup_gap`, newer than this plugin's floor, so
    # guarded: no seam ⇒ no thread). Off the hot path: a daemon thread probes `gh`
    # and reports (or clears) the gaps; register() never waits on it.
    probe = False
    try:
        from .status import probe_in_background

        probe = probe_in_background(registry, current_default_repo, current_repos) is not None
    except Exception:  # noqa: BLE001
        log.debug("[github] status probe not started", exc_info=True)

    log.info(
        "[github] registered %d read tool(s)%s%s%s%s",
        n_read,
        f" + {n_write} write tool(s) (write enabled)" if write_enabled else " (read-only — github.write is false)",
        " + /issue command" if issue_cmd else "",
        " + board view" if view else "",
        " + setup probe" if probe else "",
    )
