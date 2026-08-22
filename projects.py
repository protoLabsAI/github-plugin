"""The host's managed-projects registry (ADR 0095), read defensively — plus the
last-resort fallback: a local checkout's ``origin`` remote.

protoAgent gained a top-level ``projects:`` registry in v0.115.0 — one place to
declare a project, with consumers projecting from it instead of re-declaring it.
This module is the GitHub half of that: a registered project's ``github`` field
feeds the repo picker and ``/issue``, so registering a project once is enough
instead of also re-typing ``owner/name`` into ``github.repos``.

Three properties are deliberate:

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

**A local checkout becomes ``owner/name`` by itself (v0.6.0).** A registry project
declared with only a ``path`` (no ``github:``), and the board's ``project_board.repo``
path, are git checkouts — their ``origin`` remote already says which GitHub repo
they are. ``remote_repos`` parses ``git -C <path> remote get-url origin`` for each
(``github.com[:/]owner/name(.git)?``, HTTPS or SSH) so a fresh install that
onboarded a project, or pointed the board at a checkout, gets a working default
repo with nothing typed twice. These come LAST (after explicit + registry
``github:`` entries), are cached per path (10 min — the register-time probe thread
primes the cache, the async routes call this via ``asyncio.to_thread``, and a
configured ``default_repo`` short-circuits it entirely), and a non-git or
remote-less path contributes nothing.

**Every read degrades to ``[]``.** The plugin's ``min_protoagent_version`` stays
0.27.0 — the projection is additive, and bumping the floor would cut off older
hosts that don't want it anyway. So the host import is lazy and broadly guarded:
no host (the host-free test suite), a pre-0.115.0 host (no ``projects``
attribute), or config not yet loaded all yield ``[]`` rather than raising. Same
posture as ``__init__.py``'s ``hasattr(registry, "live_config")`` fallback.
"""

from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path

# `origin` URL shapes: https://github.com/o/n(.git), git@github.com:o/n(.git),
# ssh://git@github.com/o/n(.git), github.com/o/n — host-anchored so a mirror on
# another forge never masquerades as a GitHub repo.
GITHUB_REMOTE_RE = re.compile(r"github\.com[:/]([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?$", re.I)

_GIT_TIMEOUT = 3  # seconds — `git remote get-url` is local and instant; never let it hang a tool call
_REMOTE_TTL = 600.0  # seconds — a checkout's origin rarely changes; the per-call getters must not fork git
_remote_cache: dict[str, tuple[float, str | None]] = {}


def repo_from_remote_url(url: str) -> str | None:
    """``owner/name`` from a GitHub remote URL (HTTPS / SSH / scp-like), else None."""
    m = GITHUB_REMOTE_RE.search((url or "").strip())
    return f"{m.group(1)}/{m.group(2)}" if m else None


def repo_from_checkout(path: str) -> str | None:
    """``owner/name`` from the ``origin`` remote of the git checkout at ``path``
    (None when the path isn't a git repo, has no GitHub origin, or git isn't
    available). Cached per path for a minute."""
    key = str(path or "").strip()
    if not key:
        return None
    now = time.monotonic()
    hit = _remote_cache.get(key)
    if hit and now - hit[0] < _REMOTE_TTL:
        return hit[1]
    result: str | None = None
    try:
        p = Path(key).expanduser()
        if p.is_dir():
            proc = subprocess.run(
                ["git", "-C", str(p), "remote", "get-url", "origin"],
                capture_output=True,
                text=True,
                timeout=_GIT_TIMEOUT,
            )
            if proc.returncode == 0:
                result = repo_from_remote_url(proc.stdout)
    except Exception:  # noqa: BLE001 — no git / timeout / odd path: contributes nothing
        result = None
    _remote_cache[key] = (now, result)
    return result


def reset_remote_cache() -> None:
    """Forget the per-path origin cache (tests; a config save)."""
    _remote_cache.clear()


def _host_entries() -> list[dict]:
    """The registry + legacy-fence project entries (dicts only), or ``[]``."""
    try:
        from graph.sdk import config

        cfg = config()
        entries = list(getattr(cfg, "projects", None) or []) + list(getattr(cfg, "filesystem_projects", None) or [])
    except Exception:  # noqa: BLE001 — no host / older host / config unloaded; never fatal
        return []
    return [e for e in entries if isinstance(e, dict)]


def registry_repos() -> list[str]:
    """``owner/name`` for every registered project that declares one — config
    order, deduped, blanks dropped. ``[]`` on any host without the registry.

    Reads the ADR 0095 ``projects:`` registry first, then the legacy explicit
    ``filesystem.projects`` override: an instance that predates the registry (or
    one written by the pre-#2925 ``onboard_project``) carries its ``github``
    bindings THERE, and those repos are just as real. Registry entries lead."""
    return _dedupe(str(e.get("github") or "") for e in _host_entries())


def board_repo_path() -> str:
    """The project-board plugin's ``project_board.repo`` checkout path when the host
    config carries one and it's a real path — ``""`` for the board's ``"."`` /
    blank unconfigured sentinel (the board itself refuses to build there), or on a
    host with no plugin config."""
    try:
        from graph.sdk import config

        pconf = getattr(config(), "plugin_config", None) or {}
        repo = str((pconf.get("project_board") or {}).get("repo") or "").strip()
    except Exception:  # noqa: BLE001
        return ""
    return "" if repo in ("", ".") else repo


def checkout_paths() -> list[str]:
    """The local checkouts whose ``origin`` can name a repo: every registry /
    fence project's ``path`` (those WITHOUT a ``github:`` binding first — a bound
    one already contributed via ``registry_repos``), then ``project_board.repo``."""
    entries = _host_entries()
    unbound = [str(e.get("path") or "") for e in entries if not str(e.get("github") or "").strip()]
    bound = [str(e.get("path") or "") for e in entries if str(e.get("github") or "").strip()]
    board = board_repo_path()
    return _dedupe([*unbound, *bound, board])


def remote_repos() -> list[str]:
    """``owner/name`` parsed from the ``origin`` remote of each checkout path the
    host knows about — the last-resort source. ``[]`` with no host."""
    return _dedupe(repo_from_checkout(p) or "" for p in checkout_paths())


def effective_repos(cfg_repos: list | None, *, include_remotes: bool = True) -> list[str]:
    """The repo picker list: the explicit ``github.repos`` entries (operator order,
    first) UNION the host's managed-projects registry UNION (last) the repos parsed
    from the checkouts' ``origin`` remotes. The single place the layers meet — and
    the reason an onboarded project needs no second declaration."""
    explicit = [str(r).strip() for r in (cfg_repos or []) if str(r).strip()]
    remotes = remote_repos() if include_remotes else []
    return _dedupe([*explicit, *registry_repos(), *remotes])


def _dedupe(repos) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for repo in repos:
        repo = (repo or "").strip()
        if repo and repo not in seen:
            seen.add(repo)
            out.append(repo)
    return out
