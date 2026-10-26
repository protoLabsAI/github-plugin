"""Detail-drawer data — host-free async functions behind the gated data routes.

The in-console PR/issue drawer (epic bd-nm78) needs the FULL object for one PR or
issue, not the lean board row ``api.fetch_prs`` returns. These functions are the data
foundation the drawer cards build on; this card adds the PR detail + its route only
(no UI, no write routes).

Same contract as ``api.fetch_prs``: every `gh` call goes through ``run_gh``, the repo
is validated with ``bad_repo``, failures are classified with ``check_gh_error``, the
body is parsed with ``parse_json``, and each function returns a ``dict`` (the parsed
object) or ``{"error": "..."}`` — never raises. ``number`` must be a positive int;
anything else is an ``{"error"}`` returned WITHOUT calling `gh`.

A small in-process TTL cache (``cached``) memoises a detail read for a few seconds, so
the drawer's open + its immediate refreshes don't each fork `gh`. Error results are
never cached, and a caller passes ``fresh=True`` to bypass a live entry.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable

from .gh_cli import bad_repo, check_gh_error, parse_json, run_gh

# The JSON fields the drawer needs for one PR — the full merge-readiness picture plus
# the metadata the UI renders (labels / assignees / milestone / review requests /
# linked issues). All verified against `gh pr view --json` on this gh (see the PR body).
_PR_DETAIL_FIELDS = (
    "number,title,state,isDraft,author,body,url,createdAt,updatedAt,"
    "headRefName,baseRefName,labels,assignees,milestone,reviewRequests,"
    "reviewDecision,mergeable,mergeStateStatus,latestReviews,additions,"
    "deletions,changedFiles,files,closingIssuesReferences"
)

# A PR body can be large; cap what we hand the drawer (and flag the cut) so one giant
# description can't blow the response budget. Matches read_tools._MAX_BODY_CHARS.
_MAX_BODY_CHARS = 60000

_DEFAULT_TTL = 15  # seconds — a drawer open + its reflexive refreshes share one `gh` call.


# ── the TTL cache ──────────────────────────────────────────────────────────────────

# key (e.g. ("pr", repo, number)) -> (expiry_monotonic, value). Process-wide and tiny;
# detail reads are bursty (open the drawer, refresh it) and cheap to recompute on miss.
_CACHE: dict[tuple, tuple[float, dict]] = {}


def _now() -> float:
    """Monotonic clock — never goes backwards on a wall-clock adjustment (patched in tests)."""
    return time.monotonic()


def clear_cache() -> None:
    """Drop every cached entry (tests; a config change that should invalidate reads)."""
    _CACHE.clear()


async def cached(
    key: tuple,
    coro_fn: Callable[[], Awaitable[dict]],
    ttl: int = _DEFAULT_TTL,
    *,
    fresh: bool = False,
) -> dict:
    """Memoise ``coro_fn()``'s dict result under ``key`` for ``ttl`` seconds.

    - a live entry is returned without awaiting ``coro_fn`` (ONE `gh` call per window);
    - an ERROR result (a dict carrying ``"error"``) is never cached — a transient
      failure must not pin a drawer to a stale error for the whole TTL;
    - ``fresh=True`` bypasses a live entry (and refreshes it from ``coro_fn``).
    """
    if not fresh:
        hit = _CACHE.get(key)
        if hit is not None:
            expiry, value = hit
            if expiry > _now():
                return value
            del _CACHE[key]  # expired — fall through and recompute
    value = await coro_fn()
    if isinstance(value, dict) and "error" not in value:
        _CACHE[key] = (_now() + ttl, value)
    return value


# ── PR detail ───────────────────────────────────────────────────────────────────────


def _positive_int(value) -> bool:
    """True only for a real positive int — ``bool`` (an int subclass) and 0/negatives are out."""
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


async def fetch_pr_detail(repo: str, number: int, fresh: bool = False) -> dict:
    """The full detail object for ONE pull request as a dict (or ``{"error": "..."}``).

    One ``gh pr view N --repo o/n --json <fields>`` call; the parsed dict carries the
    merge-readiness picture (``mergeable`` / ``mergeStateStatus`` / ``reviewDecision`` /
    ``latestReviews``) and the metadata the drawer renders (labels / assignees /
    milestone / review requests / linked issues / files). The body is capped at
    60000 chars with ``body_truncated: true`` set when it's cut.

    Validated and bounded like ``api.fetch_prs``: a bad repo or a non-positive-int
    ``number`` is an ``{"error"}`` returned WITHOUT calling `gh`. Memoised through
    ``cached`` (``("pr", repo, number)``); ``fresh=True`` bypasses the cache.
    """
    if err := bad_repo(repo):
        return {"error": err}
    if not _positive_int(number):
        return {"error": f"Error: PR number must be a positive integer (got {number!r})."}

    async def _run() -> dict:
        rc, out, serr = await run_gh(["pr", "view", str(number), "--repo", repo, "--json", _PR_DETAIL_FIELDS])
        if gh_err := check_gh_error(rc, serr, repo=repo):
            return {"error": gh_err}
        d, perr = parse_json(out, dict)  # a non-dict body is an error, never a crash
        if perr:
            return {"error": perr}
        body = d.get("body")
        if isinstance(body, str) and len(body) > _MAX_BODY_CHARS:
            d["body"] = body[:_MAX_BODY_CHARS]
            d["body_truncated"] = True
        return d

    return await cached(("pr", repo, number), _run, fresh=fresh)
