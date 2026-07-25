"""The host's managed-projects registry (ADR 0095), read defensively.

protoAgent gained a top-level ``projects:`` registry in v0.115.0 — one place to
declare a project, with consumers projecting from it instead of re-declaring it.
This module is the GitHub half of that: a registered project's ``github`` field
feeds the repo picker and ``/issue``, so registering a project once is enough
instead of also re-typing ``owner/name`` into ``github.repos``.

Two properties are deliberate:

**Explicit config always wins.** ``github.repos`` set ⇒ the registry is not
consulted at all. Same non-regression property the core work has: configuring
nothing new keeps today's behavior byte for byte.

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
    order, deduped, blanks dropped. ``[]`` on any host without the registry."""
    try:
        from graph.sdk import config

        entries = getattr(config(), "projects", None) or []
    except Exception:  # noqa: BLE001 — no host / older host / config unloaded; never fatal
        return []
    out: list[str] = []
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        repo = str(entry.get("github") or "").strip()
        if repo and repo not in seen:
            seen.add(repo)
            out.append(repo)
    return out


def effective_repos(cfg_repos: list | None) -> list[str]:
    """The repo picker list: explicit ``github.repos`` when set, else the host's
    managed-projects registry. The single place the two layers meet."""
    explicit = [str(r).strip() for r in (cfg_repos or []) if str(r).strip()]
    return explicit or registry_repos()
