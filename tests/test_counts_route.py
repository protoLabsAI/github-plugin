"""Host-free coverage for the board's totals data route."""

import asyncio
import json
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from ghplugin import api

QUERY = (
    "query($issues: String!, $prs: String!) { "
    "issues: search(query: $issues, type: ISSUE, first: 0) { issueCount } "
    "prs: search(query: $prs, type: ISSUE, first: 0) { issueCount } }"
)
RESPONSE = {"data": {"issues": {"issueCount": 412}, "prs": {"issueCount": 107}}}


def client():
    app = FastAPI()
    app.include_router(api.build_data_router({}), prefix="/api/plugins/github")
    return TestClient(app)


def argv(issues, prs):
    return ["api", "graphql", "-f", f"query={QUERY}", "-f", f"issues={issues}", "-f", f"prs={prs}"]


def test_counts_route_exact_argv():
    with patch.object(api, "run_gh", AsyncMock(return_value=(0, json.dumps(RESPONSE), ""))) as gh:
        response = client().get("/api/plugins/github/counts", params={"repo": "o/n", "state": "open"})
    assert response.status_code == 200
    assert response.json() == {"issues": 412, "prs": 107}
    gh.assert_awaited_once_with(argv("repo:o/n is:issue is:open", "repo:o/n is:pr is:open"))
    assert "/counts" not in {route.path for route in api.build_view_router().routes}


@pytest.mark.parametrize(
    "state,issue_state,pr_state,issues",
    [("closed", " is:closed", " is:closed", 412), ("merged", "", " is:merged", 0), ("all", "", "", 412)],
)
def test_states_and_search(state, issue_state, pr_state, issues):
    search = '-label:bug "two words"'
    with patch.object(api, "run_gh", AsyncMock(return_value=(0, json.dumps(RESPONSE), ""))) as gh:
        result = client().get("/api/plugins/github/counts", params={"repo": "o/n", "state": state, "search": search})
    assert result.json() == {"issues": issues, "prs": 107}
    gh.assert_awaited_once_with(argv(f"repo:o/n is:issue{issue_state} {search}", f"repo:o/n is:pr{pr_state} {search}"))


def test_normalization_and_search_cap():
    with patch.object(api, "run_gh", AsyncMock(return_value=(0, json.dumps(RESPONSE), ""))) as gh:
        result = asyncio.run(api.fetch_counts("o/n", " OPEN ", "  " + "x" * 300 + "  "))
    assert result == {"issues": 412, "prs": 107}
    gh.assert_awaited_once_with(argv("repo:o/n is:issue is:open " + "x" * 256, "repo:o/n is:pr is:open " + "x" * 256))


@pytest.mark.parametrize("repo,state", [("bad", "open"), ("", "open"), ("o/n", "invalid")])
def test_invalid_input_skips_gh(repo, state):
    with patch.object(api, "run_gh", AsyncMock()) as gh:
        result = asyncio.run(api.fetch_counts(repo, state))
    assert result["error"]
    gh.assert_not_awaited()


def test_gh_error():
    with patch.object(api, "run_gh", AsyncMock(return_value=(1, "", "failed"))):
        assert "failed" in asyncio.run(api.fetch_counts("o/n"))["error"]


def test_non_json():
    with patch.object(api, "run_gh", AsyncMock(return_value=(0, "not JSON", ""))):
        assert "parse" in asyncio.run(api.fetch_counts("o/n"))["error"]


def test_missing_issue_count():
    body = {"data": {"issues": {}, "prs": {"issueCount": 1}}}
    with patch.object(api, "run_gh", AsyncMock(return_value=(0, json.dumps(body), ""))):
        assert "data.issues.issueCount" in asyncio.run(api.fetch_counts("o/n"))["error"]


@pytest.mark.parametrize("value", [None, [], "bad", 1, True])
@pytest.mark.parametrize("level", ["root", "data", "issues", "prs"])
def test_malformed_response_levels(level, value):
    body = json.loads(json.dumps(RESPONSE))
    if level == "root":
        body = value
    elif level == "data":
        body["data"] = value
    else:
        body["data"][level] = value
    with patch.object(api, "run_gh", AsyncMock(return_value=(0, json.dumps(body), ""))):
        assert asyncio.run(api.fetch_counts("o/n"))["error"]


@pytest.mark.parametrize("value", [None, [], {}, "12", 1.5, True, -1])
@pytest.mark.parametrize("name", ["issues", "prs"])
def test_invalid_count(name, value):
    body = json.loads(json.dumps(RESPONSE))
    body["data"][name]["issueCount"] = value
    with patch.object(api, "run_gh", AsyncMock(return_value=(0, json.dumps(body), ""))):
        assert asyncio.run(api.fetch_counts("o/n", "merged"))["error"]


def test_graphql_partial_error():
    body = {**RESPONSE, "errors": [{"message": "query failed"}]}
    with patch.object(api, "run_gh", AsyncMock(return_value=(0, json.dumps(body), ""))):
        assert asyncio.run(api.fetch_counts("o/n"))["error"]


def test_zero_counts_and_defaults():
    body = {"data": {"issues": {"issueCount": 0}, "prs": {"issueCount": 0}}}
    with patch.object(api, "run_gh", AsyncMock(return_value=(0, json.dumps(body), ""))) as gh:
        assert client().get("/api/plugins/github/counts", params={"repo": "o/n"}).json() == {"issues": 0, "prs": 0}
    gh.assert_awaited_once_with(argv("repo:o/n is:issue is:open", "repo:o/n is:pr is:open"))
