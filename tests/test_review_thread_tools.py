"""Review-thread tools: list (read), reply + resolve (write, gated). Unresolved review
threads keep a PR's QA check from clearing, and a PR-level comment can't answer one."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

from ghplugin.read_tools import get_read_tools
from ghplugin.write_tools import get_write_tools

THREADS = {
    "data": {
        "repository": {
            "pullRequest": {
                "reviewThreads": {
                    "nodes": [
                        {
                            "id": "PRRT_open123456",
                            "isResolved": False,
                            "isOutdated": False,
                            "path": "plugins/artifact/_store.py",
                            "line": 75,
                            "comments": {
                                "totalCount": 1,
                                "nodes": [
                                    {
                                        "author": {"login": "coderabbitai"},
                                        "body": "Restore blobs/ if the history move fails.",
                                        "url": "u",
                                    }
                                ],
                            },
                        },
                        {
                            "id": "PRRT_done123456",
                            "isResolved": True,
                            "isOutdated": False,
                            "path": "README.md",
                            "line": 3,
                            "comments": {
                                "totalCount": 2,
                                "nodes": [{"author": {"login": "protoreview"}, "body": "nit", "url": "u2"}],
                            },
                        },
                    ]
                }
            }
        }
    }
}


def _tool(tools, name):
    return next(t for t in tools if t.name == name)


async def test_lists_unresolved_threads_with_their_ids():
    gh = AsyncMock(return_value=(0, json.dumps(THREADS), ""))
    with patch("ghplugin.read_tools.run_gh", gh):
        out = await _tool(get_read_tools("o/r"), "github_review_threads").ainvoke({"number": 3745})
    assert "1 unresolved of 2" in out and "PRRT_open123456" in out and "_store.py:75" in out
    assert "PRRT_done123456" not in out  # resolved hidden by default
    args = gh.await_args.args[0]
    assert args[:2] == ["api", "graphql"] and "owner=o" in args and "name=r" in args and "number=3745" in args


async def test_include_resolved_shows_everything():
    gh = AsyncMock(return_value=(0, json.dumps(THREADS), ""))
    with patch("ghplugin.read_tools.run_gh", gh):
        out = await _tool(get_read_tools("o/r"), "github_review_threads").ainvoke(
            {"number": 1, "include_resolved": True}
        )
    assert "PRRT_done123456 [resolved]" in out and "1 reply" in out


async def test_reply_then_resolve_runs_both_mutations():
    replies = [
        (0, json.dumps({"data": {"addPullRequestReviewThreadReply": {"comment": {"url": "https://x/c1"}}}}), ""),
        (
            0,
            json.dumps({"data": {"resolveReviewThread": {"thread": {"id": "PRRT_open123456", "isResolved": True}}}}),
            "",
        ),
    ]
    gh = AsyncMock(side_effect=replies)
    with patch("ghplugin.write_tools.run_gh", gh):
        out = await _tool(get_write_tools("o/r"), "github_reply_thread").ainvoke(
            {"thread_id": "PRRT_open123456", "body": "Fixed: rolls blobs/ back (_store.py:88).", "resolve": True}
        )
    assert out == "Replied (https://x/c1) and resolved the thread."
    first, second = (c.args[0] for c in gh.await_args_list)
    assert (
        "addPullRequestReviewThreadReply" in " ".join(first)
        and "body=Fixed: rolls blobs/ back (_store.py:88)." in first
    )
    assert "resolveReviewThread" in " ".join(second)


async def test_resolve_and_unresolve():
    gh = AsyncMock(
        return_value=(0, json.dumps({"data": {"unresolveReviewThread": {"thread": {"isResolved": False}}}}), "")
    )
    with patch("ghplugin.write_tools.run_gh", gh):
        out = await _tool(get_write_tools("o/r"), "github_resolve_thread").ainvoke(
            {"thread_id": "PRRT_open123456", "unresolve": True}
        )
    assert out == "Thread PRRT_open123456 is now unresolved." and "unresolveReviewThread" in " ".join(
        gh.await_args.args[0]
    )


async def test_bad_thread_id_and_empty_body_refused_before_gh():
    gh = AsyncMock()
    with patch("ghplugin.write_tools.run_gh", gh):
        tools = get_write_tools("o/r")
        assert "not a review-thread id" in await _tool(tools, "github_resolve_thread").ainvoke({"thread_id": "12345"})
        assert "body is empty" in await _tool(tools, "github_reply_thread").ainvoke(
            {"thread_id": "PRRT_open123456", "body": " "}
        )
    gh.assert_not_awaited()


async def test_graphql_errors_are_surfaced():
    gh = AsyncMock(
        return_value=(0, json.dumps({"errors": [{"message": "Resource not accessible by integration"}]}), "")
    )
    with patch("ghplugin.write_tools.run_gh", gh):
        out = await _tool(get_write_tools("o/r"), "github_resolve_thread").ainvoke({"thread_id": "PRRT_open123456"})
    assert out.startswith("Error:") and "not accessible" in out
