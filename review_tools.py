"""Formal PR-review verdict tools over `gh` — GATED behind `github.write: true`.

The verdict surface for the QA-review takeover (protoAgent ADR 0078 Phase B):
three tools posting formal reviews via the GitHub Review API, with the guards
that made Quinn's reviewer reliable enforced INSIDE the tools — below the model,
so a prompt can never bypass them:

  - github_review_comment          — non-blocking COMMENT review. No guards: a
    comment is always safe to post.
  - github_review_approve          — formal APPROVE. Guarded.
  - github_review_request_changes  — formal REQUEST_CHANGES. Guarded.

The guards (ported from protoWorkstacean's pr-inspector, her failure ledger):

  CI-TERMINAL (#863) — a blocking verdict locks in a judgement about the PR's
  settled state, so APPROVE / REQUEST_CHANGES are refused while any check run
  is still queued/in-progress, AND when CI cannot be read at all (a 403/error
  says nothing about whether checks pass — fail closed, never fail open). The
  refusal tells the model to post a review_comment instead. A repo with no
  checks at all is terminal by definition.

  SELF-REVIEW — the tool refuses a blocking verdict on a PR authored by the
  SAME identity the token authenticates as (approve-your-own-work loops).
  Commenting on your own PR stays allowed.

Keep tool docstrings PLAIN string literals (an f-string docstring → __doc__ is
None → the tool ships with no description).
"""

from __future__ import annotations

import json

from langchain_core.tools import tool

from .gh_cli import bad_repo, check_gh_error, run_gh
from .gh_issue import resolve_repo

# Check-run states that count as still-running. GitHub check runs carry
# `status` (queued | in_progress | completed | waiting | requested | pending)
# and only a completed run has a `conclusion`.
_NON_TERMINAL = {"queued", "in_progress", "waiting", "requested", "pending"}


async def _viewer_login() -> str:
    """The login the token authenticates as ('' on failure — guards fail closed
    on their own signal, not on this helper)."""
    rc, out, _err = await run_gh(["api", "user", "--jq", ".login"])
    return out.strip() if rc == 0 else ""


async def _pr_head_and_author(repo: str, number: int) -> tuple[str, str, str]:
    """(head_sha, author_login, error). error is '' on success."""
    rc, out, serr = await run_gh(["pr", "view", str(number), "--repo", repo, "--json", "headRefOid,author"])
    if gh_err := check_gh_error(rc, serr):
        return "", "", gh_err
    try:
        d = json.loads(out)
    except json.JSONDecodeError:
        return "", "", f"Error: could not parse gh output: {out[:200]}"
    return str(d.get("headRefOid") or ""), str((d.get("author") or {}).get("login") or ""), ""


async def _ci_state(repo: str, head_sha: str) -> tuple[str, str]:
    """('terminal' | 'pending' | 'unknown', detail).

    'terminal'  — every check run completed (or the commit has none at all).
    'pending'   — at least one run is queued/in-progress.
    'unknown'   — CI could not be read (403, network, parse) — treated exactly
                  like pending by the guards: you cannot verdict what you
                  cannot see (fail closed)."""
    rc, out, serr = await run_gh(
        ["api", f"repos/{repo}/commits/{head_sha}/check-runs", "--jq", "[.check_runs[].status]"]
    )
    if rc != 0:
        return "unknown", (serr or out).strip()[:200]
    try:
        statuses = json.loads(out or "[]")
    except json.JSONDecodeError:
        return "unknown", f"unparseable check-runs response: {out[:120]}"
    pending = [s for s in statuses if str(s).lower() in _NON_TERMINAL]
    if pending:
        return "pending", f"{len(pending)} of {len(statuses)} check run(s) still running"
    return "terminal", f"{len(statuses)} check run(s), all completed"


async def _post_review(repo: str, number: int, event: str, body: str) -> str:
    rc, out, serr = await run_gh(
        [
            "api",
            f"repos/{repo}/pulls/{number}/reviews",
            "-X",
            "POST",
            "-f",
            f"event={event}",
            "-f",
            f"body={body}",
        ]
    )
    if gh_err := check_gh_error(rc, serr):
        return gh_err
    try:
        d = json.loads(out)
        url = d.get("html_url") or ""
    except json.JSONDecodeError:
        url = ""
    return f"Posted {event} review on {repo}#{number}." + (f" {url}" if url else "")


async def _guard_blocking_verdict(repo: str, number: int, verdict: str) -> str:
    """The two below-the-model guards. Returns '' to allow, or the refusal text.

    Order matters: the cheap identity guard first, then CI. Both refusals
    instruct the model to use github_review_comment instead — the non-blocking
    outcome is always available."""
    head_sha, author, err = await _pr_head_and_author(repo, number)
    if err:
        return (
            f"{err}\nHeld: cannot verify the PR's head/author, so a formal {verdict} "
            "is refused (fail closed). Post your findings with github_review_comment instead."
        )
    me = await _viewer_login()
    if me and author and me == author:
        return (
            f"Held: this PR was authored by {author!r} — the same identity this agent "
            f"reviews as. A formal {verdict} on your own work is refused (self-approval "
            "loops). Post observations with github_review_comment and escalate to a "
            "human reviewer."
        )
    state, detail = await _ci_state(repo, head_sha)
    if state == "pending":
        return (
            f"Held: CI is not terminal on {repo}#{number} ({detail}). A formal {verdict} "
            "locks in a judgement about the PR's settled state, so it is refused while "
            "checks run. Post your findings NOW with github_review_comment (non-blocking) "
            "and finish — do not wait or poll; once checks settle, a later pass (or a "
            "deterministic approve-on-green policy) takes it from there."
        )
    if state == "unknown":
        return (
            f"Held: CI on {repo}#{number} cannot be read ({detail}) — which says nothing "
            f"about whether checks pass. A formal {verdict} on unverified CI is refused "
            "(fail closed). Record the CI-access gap with github_review_comment "
            "(non-blocking); do not treat unreadable CI as a failure."
        )
    return ""


def get_review_tools(default_repo: str = "") -> list:
    """Build the verdict tools. ``default_repo`` (``owner/name``) is used whenever a
    tool's ``repo`` arg is omitted."""

    @tool
    async def github_review_comment(number: int, body: str, repo: str = "") -> str:
        """Post a formal non-blocking COMMENT review on a pull request.

        The always-safe verdict: use it for findings while CI is still running, for
        observations that shouldn't block a merge, or when a blocking verdict was
        refused by its guards.

        Args:
            repo: Repository as ``owner/name``. Omit to use the agent's configured default repo.
            number: PR number.
            body: The review body (findings, observations — markdown).
        """
        repo = resolve_repo(repo, default_repo) or ""
        if err := bad_repo(repo):
            return err
        if not body.strip():
            return "Error: `body` is empty — a review needs content."
        return await _post_review(repo, number, "COMMENT", body)

    @tool
    async def github_review_approve(number: int, body: str, repo: str = "") -> str:
        """Post a formal APPROVE review on a pull request. GUARDED.

        Refused (with comment-instead guidance) while any check run is still
        running, when CI cannot be read at all, or on a PR this agent authored.
        An APPROVE clears the way to merge — it must reflect the PR's settled,
        verified state, never a guess.

        Args:
            repo: Repository as ``owner/name``. Omit to use the agent's configured default repo.
            number: PR number.
            body: The review body (what was verified, any non-blocking notes).
        """
        repo = resolve_repo(repo, default_repo) or ""
        if err := bad_repo(repo):
            return err
        if not body.strip():
            return "Error: `body` is empty — a review needs content."
        if held := await _guard_blocking_verdict(repo, number, "APPROVE"):
            return held
        return await _post_review(repo, number, "APPROVE", body)

    @tool
    async def github_review_request_changes(number: int, body: str, repo: str = "") -> str:
        """Post a formal REQUEST_CHANGES review on a pull request. GUARDED.

        Refused (with comment-instead guidance) while any check run is still
        running, when CI cannot be read at all, or on a PR this agent authored.
        A broken-CI block must rest on a check that COMPLETED with a failure you
        read — never on one still running or one you couldn't see.

        Args:
            repo: Repository as ``owner/name``. Omit to use the agent's configured default repo.
            number: PR number.
            body: The review body — every blocking finding with file:line evidence.
        """
        repo = resolve_repo(repo, default_repo) or ""
        if err := bad_repo(repo):
            return err
        if not body.strip():
            return "Error: `body` is empty — a review needs content."
        if held := await _guard_blocking_verdict(repo, number, "REQUEST_CHANGES"):
            return held
        return await _post_review(repo, number, "REQUEST_CHANGES", body)

    return [github_review_comment, github_review_approve, github_review_request_changes]
