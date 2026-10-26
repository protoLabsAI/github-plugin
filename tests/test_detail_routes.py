"""The detail-drawer data foundation — `detail.fetch_pr_detail` + its gated route.

The data function is tested directly with `run_gh` mocked (argv + bounding + the TTL
cache); the route is tested through FastAPI's TestClient under its real gated prefix,
the same host-free pattern as test_board_view.py.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from ghplugin.api import build_data_router
from ghplugin.detail import cached, clear_cache, fetch_pr_detail

_CFG = {"repos": ["o/n"], "default_repo": "o/n"}

# A representative `gh pr view --json` body — enough to assert the merge-readiness
# picture and metadata survive the round-trip unchanged.
_PR = {
    "number": 5,
    "title": "Add the drawer",
    "state": "OPEN",
    "isDraft": False,
    "author": {"login": "kj"},
    "body": "## What\nA PR body.",
    "url": "https://github.com/o/n/pull/5",
    "reviewDecision": "APPROVED",
    "mergeable": "MERGEABLE",
    "mergeStateStatus": "CLEAN",
    "latestReviews": [{"author": {"login": "rev"}, "state": "APPROVED"}],
    "files": [{"path": "detail.py", "additions": 100, "deletions": 0}],
    "labels": [{"name": "enhancement"}],
    "assignees": [{"login": "kj"}],
}


@pytest.fixture(autouse=True)
def _clear_detail_cache():
    """The TTL cache is process-wide — isolate every test from its neighbours."""
    clear_cache()
    yield
    clear_cache()


def _app(cfg=None):
    app = FastAPI()
    app.include_router(build_data_router(cfg or _CFG), prefix="/api/plugins/github")
    return app


# --- fetch_pr_detail: argv + parse (r1) --------------------------------------


async def test_fetch_pr_detail_one_call_and_full_object():
    fake = AsyncMock(return_value=(0, json.dumps(_PR), ""))
    with patch("ghplugin.detail.run_gh", fake):
        out = await fetch_pr_detail("o/n", 5)
    # The full object comes back — the merge-readiness picture + metadata, not a lean row.
    assert out["mergeable"] == "MERGEABLE"
    assert out["mergeStateStatus"] == "CLEAN"
    assert out["reviewDecision"] == "APPROVED"
    assert out["latestReviews"][0]["state"] == "APPROVED"
    assert out["files"][0]["path"] == "detail.py"
    # Exactly one `gh pr view` call, argv asserted.
    fake.assert_called_once()
    args = fake.call_args.args[0]
    assert args[:6] == ["pr", "view", "5", "--repo", "o/n", "--json"]
    fields = args[6]
    for f in (
        "mergeable",
        "mergeStateStatus",
        "reviewDecision",
        "latestReviews",
        "files",
        "closingIssuesReferences",
        "milestone",
        "reviewRequests",
    ):
        assert f in fields


def test_detail_pr_route_returns_parsed_dict():
    fake = AsyncMock(return_value=(0, json.dumps(_PR), ""))
    with patch("ghplugin.detail.run_gh", fake):
        body = TestClient(_app()).get("/api/plugins/github/detail/pr", params={"repo": "o/n", "number": 5}).json()
    assert body["number"] == 5 and body["mergeStateStatus"] == "CLEAN"
    assert body["files"][0]["path"] == "detail.py"
    assert fake.call_args.args[0][:3] == ["pr", "view", "5"]


# --- validation: no gh call for a bad repo / bad number (r2) -----------------


async def test_bad_repo_and_bad_number_never_call_gh():
    fake = AsyncMock()
    with patch("ghplugin.detail.run_gh", fake):
        assert "error" in await fetch_pr_detail("nope", 5)  # malformed repo
        for n in (0, -1, "5", 1.5, True, None):
            out = await fetch_pr_detail("o/n", n)
            assert "error" in out, f"expected error for number={n!r}"
    fake.assert_not_called()


def test_route_bad_number_returns_error_and_no_gh_call():
    fake = AsyncMock()
    with patch("ghplugin.detail.run_gh", fake):
        body = TestClient(_app()).get("/api/plugins/github/detail/pr", params={"repo": "o/n", "number": 0}).json()
    assert "error" in body and "positive integer" in body["error"]
    fake.assert_not_called()


def test_route_bad_repo_returns_error_and_no_gh_call():
    fake = AsyncMock()
    with patch("ghplugin.detail.run_gh", fake):
        body = TestClient(_app()).get("/api/plugins/github/detail/pr", params={"repo": "bad", "number": 5}).json()
    assert "error" in body
    fake.assert_not_called()


# --- gh failure + a non-dict body are classified, never raise (r3) -----------


async def test_gh_failure_is_classified():
    fake = AsyncMock(return_value=(1, "", "boom"))
    with patch("ghplugin.detail.run_gh", fake):
        out = await fetch_pr_detail("o/n", 5)
    assert out["error"].startswith("Error (gh exit 1)")


async def test_non_dict_body_is_an_error_not_a_crash():
    fake = AsyncMock(return_value=(0, "[1, 2, 3]", ""))  # gh handed back a list
    with patch("ghplugin.detail.run_gh", fake):
        out = await fetch_pr_detail("o/n", 5)
    assert "error" in out and "expected a JSON dict" in out["error"]


async def test_not_found_is_classified():
    fake = AsyncMock(return_value=(1, "", "HTTP 404: Not Found"))
    with patch("ghplugin.detail.run_gh", fake):
        out = await fetch_pr_detail("o/n", 404)
    assert "error" in out and "404" in out["error"]


# --- the TTL cache (r4) ------------------------------------------------------


async def test_cache_coalesces_repeated_reads_within_the_window():
    fake = AsyncMock(return_value=(0, json.dumps(_PR), ""))
    with patch("ghplugin.detail.run_gh", fake):
        a = await fetch_pr_detail("o/n", 5)
        b = await fetch_pr_detail("o/n", 5)  # within 15s — served from cache
    assert a == b
    fake.assert_called_once()


async def test_error_results_are_never_cached():
    fake = AsyncMock(return_value=(1, "", "transient"))
    with patch("ghplugin.detail.run_gh", fake):
        await fetch_pr_detail("o/n", 5)
        await fetch_pr_detail("o/n", 5)  # a cached error would pin the drawer stale
    assert fake.call_count == 2


async def test_fresh_bypasses_the_cache():
    fake = AsyncMock(return_value=(0, json.dumps(_PR), ""))
    with patch("ghplugin.detail.run_gh", fake):
        await fetch_pr_detail("o/n", 5)  # warms the cache
        await fetch_pr_detail("o/n", 5, fresh=True)  # bypasses it
    assert fake.call_count == 2


def test_route_fresh_param_bypasses_the_cache():
    fake = AsyncMock(return_value=(0, json.dumps(_PR), ""))
    with patch("ghplugin.detail.run_gh", fake):
        c = TestClient(_app())
        c.get("/api/plugins/github/detail/pr", params={"repo": "o/n", "number": 5})
        c.get("/api/plugins/github/detail/pr", params={"repo": "o/n", "number": 5})  # cached
        c.get("/api/plugins/github/detail/pr", params={"repo": "o/n", "number": 5, "fresh": 1})  # bypass
    assert fake.call_count == 2


async def test_expired_entry_is_recomputed():
    """Past the TTL the entry is dropped and `gh` runs again (clock patched forward)."""
    fake = AsyncMock(return_value=(0, json.dumps(_PR), ""))
    t = [1000.0]
    with patch("ghplugin.detail.run_gh", fake), patch("ghplugin.detail._now", lambda: t[0]):
        await fetch_pr_detail("o/n", 5)
        t[0] += 16  # 16s later — the 15s entry has expired
        await fetch_pr_detail("o/n", 5)
    assert fake.call_count == 2


async def test_cache_helper_keys_are_independent():
    calls = []

    async def _one():
        calls.append("one")
        return {"v": 1}

    async def _two():
        calls.append("two")
        return {"v": 2}

    assert (await cached(("pr", "o/n", 1), _one))["v"] == 1
    assert (await cached(("pr", "o/n", 2), _two))["v"] == 2
    assert (await cached(("pr", "o/n", 1), _one))["v"] == 1  # second (1) is cached
    assert calls == ["one", "two"]  # _one ran once


# --- body cap (r5) -----------------------------------------------------------


async def test_body_over_cap_is_truncated_and_flagged():
    big = {**_PR, "body": "x" * 60001}
    fake = AsyncMock(return_value=(0, json.dumps(big), ""))
    with patch("ghplugin.detail.run_gh", fake):
        out = await fetch_pr_detail("o/n", 5)
    assert len(out["body"]) == 60000
    assert out["body_truncated"] is True


async def test_body_at_or_under_cap_is_untouched():
    fake = AsyncMock(return_value=(0, json.dumps(_PR), ""))
    with patch("ghplugin.detail.run_gh", fake):
        out = await fetch_pr_detail("o/n", 5)
    assert out["body"] == "## What\nA PR body."
    assert "body_truncated" not in out
