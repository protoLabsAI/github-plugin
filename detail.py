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

import json
import re
import time
from collections.abc import Awaitable, Callable

from .gh_cli import bad_repo, check_gh_error, dicts, parse_json, run_gh

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


async def fetch_pr_checks(repo: str, number: int, fresh: bool = False, *, run_id: int | None = None) -> dict:
    """Normalised rollup, or the last 200 failed-step log lines for an Actions run."""
    from .read_tools import classify_check, fetch_failure_log, is_status_context

    if err := bad_repo(repo):
        return {"error": err}
    if not _positive_int(number):
        return {"error": "Error: PR number must be a positive integer."}
    if run_id is not None and not _positive_int(run_id):
        return {"error": "Error: run_id must be a positive integer."}

    async def _run():
        if run_id is not None:
            result = await fetch_failure_log(repo, run_id)
            if "error" in result:
                return result
            return {"log_tail": "\n".join(result["log"].splitlines()[-200:])}
        rc, out, serr = await run_gh(["pr", "view", str(number), "--repo", repo, "--json", "statusCheckRollup"])
        if err := check_gh_error(rc, serr, repo=repo):
            return {"error": err}
        data, err = parse_json(out, dict)
        if err:
            return {"error": err}
        checks = []
        summary = dict.fromkeys(("pass", "fail", "pending", "skipped"), 0)
        for c in dicts(data.get("statusCheckRollup")):
            category = classify_check(c)
            summary[category] += 1
            context = is_status_context(c)
            url = c.get("targetUrl" if context else "detailsUrl") or None
            match = re.search(r"/actions/runs/([1-9][0-9]*)(?:[/?#]|$)", url) if isinstance(url, str) else None
            checks.append(
                {
                    "name": c.get("context" if context else "name") or "?",
                    "workflow": c.get("workflowName") or None,
                    "status": ("PENDING" if category == "pending" else "COMPLETED") if context else c.get("status"),
                    "conclusion": (None if category == "pending" else c.get("state"))
                    if context
                    else c.get("conclusion"),
                    "url": url,
                    "started_at": c.get("createdAt" if context else "startedAt"),
                    "completed_at": None if context else c.get("completedAt"),
                    "run_id": int(match[1]) if match and len(match[1]) < 100 else None,
                }
            )
        return {"checks": checks, "summary": summary}

    return await cached(("pr_checks", repo, number, run_id), _run, fresh=fresh)


def _diff_path(value: str) -> str:
    """Decode Git's quoted paths (including UTF-8 octal escapes)."""
    if value.startswith('"') and value.endswith('"'):
        import ast

        try:
            return ast.literal_eval("b" + value).decode("utf-8", errors="replace")
        except (ValueError, SyntaxError):
            return value[1:-1]
    return value


def _split_diff(diff: str) -> dict:
    files = []
    for block in re.split(r"(?m)^(?=diff --git )", diff):
        if not block.startswith("diff --git "):
            continue
        lines = block.splitlines()
        # Git leaves spaces unquoted; split at the b/ prefix, not whitespace.
        match = re.match(r'diff --git ("a/.*?"|a/.*?) ("b/.*"|b/.*)$', lines[0])
        if not match:
            return {"error": "Error: could not parse diff file header."}
        old, path = (_diff_path(v)[2:] for v in match.groups())
        status = "modified"
        additions = deletions = 0
        in_hunk = False
        binary = False
        for line in lines[1:]:
            if line.startswith("@@ "):
                in_hunk = True
            elif in_hunk:
                additions += line.startswith("+")
                deletions += line.startswith("-")
            elif line.startswith("new file mode "):
                status = "added"
            elif line.startswith("deleted file mode "):
                status = "deleted"
            elif line.startswith("rename from "):
                status, old = "renamed", _diff_path(line[len("rename from ") :])
            elif line.startswith("rename to "):
                path = _diff_path(line[len("rename to ") :])
            elif line.startswith(("Binary files ", "GIT binary patch")):
                binary = True
        files.append(
            {
                "path": path,
                "old_path": old if status == "renamed" else None,
                "status": status,
                "additions": additions,
                "deletions": deletions,
                "patch": None,
                "truncated": not binary and len(block) > 40000,
                "binary": binary,
                "_patch": None if binary else block[:40000],
            }
        )
    patches = [f.pop("_patch") for f in files]
    result = {"files": files, "total_truncated": False}
    # Include JSON escaping and metadata in the total response budget.
    remaining = 400000 - len(json.dumps(result))
    if remaining < 0:
        return {"error": "Error: diff file metadata exceeds the 400000-character response limit."}
    exhausted = False
    for f, patch in zip(files, patches, strict=True):
        if patch is None:
            continue
        cost = len(json.dumps(patch)) - len("null")
        if exhausted or cost > remaining:
            exhausted = True
            f["truncated"] = True
            result["total_truncated"] = True
        else:
            f["patch"] = patch
            remaining -= cost
    return result


async def fetch_pr_diff(repo: str, number: int, fresh: bool = False) -> dict:
    """A bounded per-file unified diff, cached alongside other drawer reads."""
    if err := bad_repo(repo):
        return {"error": err}
    if not _positive_int(number):
        return {"error": "Error: PR number must be a positive integer."}

    async def _run():
        rc, out, serr = await run_gh(["pr", "diff", str(number), "--repo", repo])
        if err := check_gh_error(rc, serr, repo=repo):
            return {"error": err}
        return _split_diff(out)

    return await cached(("pr_diff", repo, number), _run, fresh=fresh)
