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


_CHECKS = [
    {
        "__typename": "CheckRun",
        "name": "unit",
        "workflowName": "CI",
        "status": "COMPLETED",
        "conclusion": "SUCCESS",
        "detailsUrl": "https://github.com/o/n/actions/runs/123/job/7",
        "startedAt": "start",
        "completedAt": "end",
    },
    {
        "__typename": "StatusContext",
        "context": "external",
        "state": "ERROR",
        "targetUrl": "https://ci.test/1",
        "createdAt": "created",
    },
    {"context": "waiting", "state": "EXPECTED"},
    {"name": "skip", "status": "COMPLETED", "conclusion": "NEUTRAL"},
    {"name": "running", "status": "IN_PROGRESS", "conclusion": "SUCCESS"},
]


def test_checks_route_normalizes_both_shapes():
    fake = AsyncMock(return_value=(0, json.dumps({"statusCheckRollup": _CHECKS}), ""))
    with patch("ghplugin.detail.run_gh", fake):
        data = (
            TestClient(_app()).get("/api/plugins/github/detail/pr/checks", params={"repo": "o/n", "number": 5}).json()
        )
    assert data["summary"] == {"pass": 1, "fail": 1, "pending": 2, "skipped": 1}
    assert data["checks"][0] == {
        "name": "unit",
        "workflow": "CI",
        "status": "COMPLETED",
        "conclusion": "SUCCESS",
        "url": _CHECKS[0]["detailsUrl"],
        "started_at": "start",
        "completed_at": "end",
        "run_id": 123,
    }
    assert data["checks"][1] == {
        "name": "external",
        "workflow": None,
        "status": "COMPLETED",
        "conclusion": "ERROR",
        "url": "https://ci.test/1",
        "started_at": "created",
        "completed_at": None,
        "run_id": None,
    }
    fake.assert_awaited_once_with(["pr", "view", "5", "--repo", "o/n", "--json", "statusCheckRollup"])


def test_failure_log_route_uses_shared_fetcher():
    fake = AsyncMock(return_value=(0, "\n".join(f"line {i}" for i in range(250)), ""))
    with patch("ghplugin.read_tools.run_gh", fake):
        client = TestClient(_app())
        data = client.get(
            "/api/plugins/github/detail/pr/checks", params={"repo": "o/n", "number": 5, "run_id": 99}
        ).json()
    assert data == {"log_tail": "\n".join(f"line {i}" for i in range(50, 250))}
    fake.assert_awaited_once_with(["run", "view", "99", "--repo", "o/n", "--log-failed"], timeout=60)


_DIFF = """diff --git a/new.txt b/new.txt
new file mode 100644
--- /dev/null
+++ b/new.txt
@@ -0,0 +1 @@
+new
 diff --git is context, not a file header
 diff --git is context too
diff --git a/edit.txt b/edit.txt
index 123..456 100644
--- a/edit.txt
+++ b/edit.txt
@@ -1 +1 @@
-old
+new
+++literal pluses
---literal minuses
diff --git a/gone.txt b/gone.txt
deleted file mode 100644
--- a/gone.txt
+++ /dev/null
@@ -1 +0,0 @@
-old
diff --git a/old name.txt b/new name.txt
similarity index 100%
rename from old name.txt
rename to new name.txt
diff --git a/image.png b/image.png
index 123..456 100644
Binary files a/image.png and b/image.png differ
"""


def test_diff_route_file_shapes_and_argv():
    fake = AsyncMock(return_value=(0, _DIFF, ""))
    with patch("ghplugin.detail.run_gh", fake):
        data = TestClient(_app()).get("/api/plugins/github/detail/pr/diff", params={"repo": "o/n", "number": 5}).json()
    files = data["files"]
    assert [(f["path"], f["status"], f["additions"], f["deletions"]) for f in files] == [
        ("new.txt", "added", 1, 0),
        ("edit.txt", "modified", 2, 2),
        ("gone.txt", "deleted", 0, 1),
        ("new name.txt", "renamed", 0, 0),
        ("image.png", "modified", 0, 0),
    ]
    assert files[3]["old_path"] == "old name.txt"
    assert files[0]["old_path"] is None
    assert files[4]["binary"] and files[4]["patch"] is None
    assert not data["total_truncated"]
    assert files[0]["patch"].startswith("diff --git")
    fake.assert_awaited_once_with(["pr", "diff", "5", "--repo", "o/n"])


async def test_diff_caps_include_json_escaping_and_preserve_counts():
    from ghplugin.detail import fetch_pr_diff

    diff = "".join(f"diff --git a/f{i} b/f{i}\n@@ -0,0 +1 @@\n+" + '"' * 45000 + "\n" for i in range(12))
    with patch("ghplugin.detail.run_gh", AsyncMock(return_value=(0, diff, ""))):
        data = await fetch_pr_diff("o/n", 5)
    assert len(json.dumps(data)) <= 400000
    assert len(data["files"]) == 12
    assert data["total_truncated"]
    assert len(data["files"][0]["patch"]) == 40000
    assert data["files"][-1]["patch"] is None
    assert all(f["truncated"] and f["additions"] == 1 for f in data["files"])


@pytest.mark.parametrize("route,extra", [("checks", {}), ("checks", {"run_id": 2}), ("diff", {})])
@pytest.mark.parametrize("invalid", [{"repo": "bad"}, {"number": 0}, {"number": "bad"}, {"number": -2}])
def test_new_route_validation(route, extra, invalid):
    with (
        patch("ghplugin.detail.run_gh", AsyncMock()) as diff_gh,
        patch("ghplugin.read_tools.run_gh", AsyncMock()) as log_gh,
    ):
        data = (
            TestClient(_app())
            .get(f"/api/plugins/github/detail/pr/{route}", params={"repo": "o/n", "number": 5, **extra, **invalid})
            .json()
        )
    assert "error" in data
    diff_gh.assert_not_called()
    log_gh.assert_not_called()


@pytest.mark.parametrize("run_id", [0, -1, "bad", "", "1.5"])
def test_bad_run_id(run_id):
    with patch("ghplugin.read_tools.run_gh", AsyncMock()) as fake:
        data = (
            TestClient(_app())
            .get("/api/plugins/github/detail/pr/checks", params={"repo": "o/n", "number": 5, "run_id": run_id})
            .json()
        )
    assert "error" in data
    fake.assert_not_called()


@pytest.mark.parametrize(
    "route,extra,output,target",
    [
        ("checks", {}, '{"statusCheckRollup": []}', "ghplugin.detail.run_gh"),
        ("checks", {"run_id": 2}, "log", "ghplugin.read_tools.run_gh"),
        ("diff", {}, _DIFF, "ghplugin.detail.run_gh"),
    ],
)
def test_new_routes_cache_fresh_expiry_and_errors(route, extra, output, target):
    fake = AsyncMock(return_value=(0, output, ""))
    clock = [100.0]
    params = {"repo": "o/n", "number": 5, **extra}
    with patch(target, fake), patch("ghplugin.detail._now", lambda: clock[0]):
        client = TestClient(_app())
        url = f"/api/plugins/github/detail/pr/{route}"
        assert client.get(url, params=params).json() == client.get(url, params=params).json()
        assert fake.call_count == 1
        client.get(url, params={**params, "fresh": 1})
        assert fake.call_count == 2
        clock[0] += 16
        client.get(url, params=params)
        assert fake.call_count == 3
        clear_cache()
        fake.return_value = (1, "", "HTTP 404: Not Found")
        for _ in range(2):
            assert "404" in client.get(url, params=params).json()["error"]
        assert fake.call_count == 5


@pytest.mark.parametrize("rollup", [None, [], [None, 1, "bad"], {}])
async def test_malformed_checks_do_not_raise(rollup):
    from ghplugin.detail import fetch_pr_checks

    with patch("ghplugin.detail.run_gh", AsyncMock(return_value=(0, json.dumps({"statusCheckRollup": rollup}), ""))):
        assert (await fetch_pr_checks("o/n", 5))["checks"] == []


@pytest.mark.parametrize("size,truncated", [(39999, False), (40000, False), (40001, True)])
async def test_patch_cap_boundary(size, truncated):
    from ghplugin.detail import fetch_pr_diff

    header = "diff --git a/f b/f\n@@ -0,0 +1 @@\n+"
    diff = header + "x" * (size - len(header))
    with patch("ghplugin.detail.run_gh", AsyncMock(return_value=(0, diff, ""))):
        data = await fetch_pr_diff("o/n", 5)
    assert len(data["files"][0]["patch"]) == min(size, 40000)
    assert data["files"][0]["truncated"] is truncated
    assert not data["total_truncated"]


@pytest.mark.parametrize("out", ["[]", "null", "invalid"])
async def test_checks_invalid_json_is_error(out):
    from ghplugin.detail import fetch_pr_checks

    with patch("ghplugin.detail.run_gh", AsyncMock(return_value=(0, out, ""))):
        assert "error" in await fetch_pr_checks("o/n", 5)


async def test_failure_tool_keeps_error_filtering_with_shared_fetcher():
    from ghplugin.read_tools import get_read_tools

    tool = next(t for t in get_read_tools() if t.name == "github_run_failure")
    log = "step\tsetup\nstep\terror: bad\nstep\terror: bad\nstep\tdone\n"
    with patch("ghplugin.read_tools.run_gh", AsyncMock(return_value=(0, log, ""))):
        result = await tool.ainvoke({"repo": "o/n", "run_id": 9})
    assert result == "o/n run 9 — failure log (1 line(s)):\nerror: bad"


async def test_quoted_diff_paths():
    from ghplugin.detail import fetch_pr_diff

    diff = 'diff --git "a/caf\\303\\251.txt" "b/caf\\303\\251.txt"\n@@ -1 +1 @@\n-old\n+new\n'
    with patch("ghplugin.detail.run_gh", AsyncMock(return_value=(0, diff, ""))):
        data = await fetch_pr_diff("o/n", 5)
    assert data["files"][0]["path"] == "café.txt"
