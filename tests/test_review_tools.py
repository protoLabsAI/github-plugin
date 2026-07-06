"""Verdict tools (ADR 0078 Phase B) over a mocked `gh`.

The point of these tests is the GUARDS: a blocking verdict must be impossible
against pending CI, unreadable CI, or the agent's own PR — refusals the model
cannot prompt its way around because they live below it, in the tool.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from ghplugin.review_tools import get_review_tools

PR_JSON = json.dumps({"headRefOid": "a" * 40, "author": {"login": "someone-else"}})
OWN_PR_JSON = json.dumps({"headRefOid": "a" * 40, "author": {"login": "proto-bot"}})


def _tool(name: str):
    for t in get_review_tools("owner/name"):
        if t.name == name:
            return t
    raise AssertionError(f"{name} not found")


def _gh(*, pr_json=PR_JSON, viewer="proto-bot", statuses='["completed","completed"]', review=None, checks_rc=0):
    """A run_gh stub routing by argv shape."""

    async def run_gh(args, timeout=None, **kw):
        if args[:2] == ["pr", "view"]:
            return (0, pr_json, "")
        if args[:2] == ["api", "user"]:
            return (0, viewer, "")
        if args[0] == "api" and "/check-runs" in args[1]:
            return (checks_rc, statuses if checks_rc == 0 else "", "HTTP 403" if checks_rc else "")
        if args[0] == "api" and "/reviews" in args[1]:
            if review is not None:
                review.append(args)
            return (0, json.dumps({"html_url": "https://example/review/1"}), "")
        raise AssertionError(f"unexpected gh call: {args}")

    return run_gh


# ── COMMENT: always allowed, no guards ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_comment_posts_without_any_guard_calls():
    posted = []
    with patch("ghplugin.review_tools.run_gh", new=_gh(review=posted)):
        out = await _tool("github_review_comment").ainvoke({"number": 7, "body": "findings…"})
    assert "Posted COMMENT review" in out
    assert any("event=COMMENT" in a for a in posted[0])


# ── APPROVE / REQUEST_CHANGES: the CI-terminal guard (#863) ──────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool_name,verdict", [("github_review_approve", "APPROVE"), ("github_review_request_changes", "REQUEST_CHANGES")]
)
async def test_blocking_verdict_refused_while_ci_pending(tool_name, verdict):
    posted = []
    with patch(
        "ghplugin.review_tools.run_gh",
        new=_gh(statuses='["completed","in_progress"]', review=posted),
    ):
        out = await _tool(tool_name).ainvoke({"number": 7, "body": "verdict"})
    assert "Held" in out and "github_review_comment" in out
    assert "do not wait or poll" in out or "do not wait" in out
    assert not posted  # nothing reached the Review API


@pytest.mark.asyncio
async def test_blocking_verdict_refused_when_ci_unreadable_fails_closed():
    posted = []
    with patch("ghplugin.review_tools.run_gh", new=_gh(checks_rc=1, review=posted)):
        out = await _tool("github_review_approve").ainvoke({"number": 7, "body": "lgtm"})
    assert "Held" in out and "cannot be read" in out
    assert not posted


@pytest.mark.asyncio
async def test_approve_posts_when_ci_terminal_and_author_differs():
    posted = []
    with patch("ghplugin.review_tools.run_gh", new=_gh(review=posted)):
        out = await _tool("github_review_approve").ainvoke({"number": 7, "body": "verified"})
    assert "Posted APPROVE review" in out
    assert any("event=APPROVE" in a for a in posted[0])


@pytest.mark.asyncio
async def test_no_checks_at_all_is_terminal_by_definition():
    posted = []
    with patch("ghplugin.review_tools.run_gh", new=_gh(statuses="[]", review=posted)):
        out = await _tool("github_review_approve").ainvoke({"number": 7, "body": "verified"})
    assert "Posted APPROVE review" in out


# ── the self-review guard ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_blocking_verdict_refused_on_own_pr():
    posted = []
    with patch("ghplugin.review_tools.run_gh", new=_gh(pr_json=OWN_PR_JSON, review=posted)):
        out = await _tool("github_review_approve").ainvoke({"number": 7, "body": "ship it"})
    assert "Held" in out and "own work" in out
    assert not posted


@pytest.mark.asyncio
async def test_comment_on_own_pr_stays_allowed():
    posted = []
    with patch("ghplugin.review_tools.run_gh", new=_gh(pr_json=OWN_PR_JSON, review=posted)):
        out = await _tool("github_review_comment").ainvoke({"number": 7, "body": "notes"})
    assert "Posted COMMENT review" in out


# ── hygiene ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_empty_body_and_bad_repo_are_rejected():
    with patch("ghplugin.review_tools.run_gh", new=_gh()):
        assert "empty" in await _tool("github_review_approve").ainvoke({"number": 7, "body": "  "})
        out = await _tool("github_review_comment").ainvoke({"number": 7, "body": "x", "repo": "bad"})
        assert out.startswith("Error")
