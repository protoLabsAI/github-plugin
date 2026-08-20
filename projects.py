"""The host's managed-projects registry (ADR 0095), read defensively.

protoAgent gained a top-level ``projects:`` registry in v0.115.0 — one place to
declare a project, with consumers projecting from it instead of re-declaring it.
This module is the GitHub half of that: a registered project's ``github`` field
feeds the repo picker and ``/issue``, so registering a project once is enough
instead of also re-typing ``owner/name`` into ``github.repos``.

Two properties are deliberate:

**Explicit config is ADDED to, never replaced — and never hides the registry.**
``github.repos`` set ⇒ those repos come FIRST, in the operator's order, and the
registry's repos follow (deduped). v0.4.0 made an explicit list WIN outright, on the
non-regression argument — but that turned "I typed a repos list once" into "the
registry is dead for me forever": every project the agent onboarded afterwards
(`onboard_project` registers into ``projects:``) stayed invisible to ``/issue`` and
the board, silently, with no config knob to opt back in (2026-08-20, protoEngineer:
three self-onboarded repos never reached the picker). A union is still
non-regressing for everything the explicit list names (same entries, same order,
same default) — it only adds the registry's entries after them.

**Every read degrades to ``[]``.** The plugin's ``min_protoagent_version`` stays
0.27.0 — the projection is additive, and bumping the floor would cut off older
hosts that don't want it anyway. So the host import is lazy and broadly guarded:
no host (the host-free test suite), a pre-0.115.0 host (no ``projects``
attribute), or config not yet loaded all yield ``[]`` rather than raising. Same
posture as ``__init__.py``'s ``hasattr(registry, "live_config")`` fallback.
"""

from __future__ import annotations


def registry_repos() -> list[str]:
    """``owner/name`` for every registered project that declares one — config
    order, deduped, blanks dropped. ``[]`` on any host without the registry.

    Reads the ADR 0095 ``projects:`` registry first, then the legacy explicit
    ``filesystem.projects`` override: an instance that predates the registry (or
    one written by the pre-#2925 ``onboard_project``) carries its ``github``
    bindings THERE, and those repos are just as real. Registry entries lead."""
    try:
        from graph.sdk import config

        cfg = config()
        entries = list(getattr(cfg, "projects", None) or []) + list(getattr(cfg, "filesystem_projects", None) or [])
    except Exception:  # noqa: BLE001 — no host / older host / config unloaded; never fatal
        return []
    return _dedupe(str(e.get("github") or "") for e in entries if isinstance(e, dict))


def effective_repos(cfg_repos: list | None) -> list[str]:
    """The repo picker list: the explicit ``github.repos`` entries (operator order,
    first) UNION the host's managed-projects registry. The single place the two
    layers meet — and the reason an onboarded project needs no second declaration."""
    explicit = [str(r).strip() for r in (cfg_repos or []) if str(r).strip()]
    return _dedupe([*explicit, *registry_repos()])


def _dedupe(repos) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for repo in repos:
        repo = repo.strip()
        if repo and repo not in seen:
            seen.add(repo)
            out.append(repo)
    return out
