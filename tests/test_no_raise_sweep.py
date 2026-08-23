"""The no-raise sweep — EVERY registered tool, against every shape `gh` can hand back.

A tool that raises kills the whole agent turn (the projectBoard v0.40.1 lesson — and
this plugin's own: `github_repo_contents` iterated the contents API's dict-for-a-file
and AttributeError'd through the tool layer). So: enumerate what `register()` actually
registers (read + write + review — nothing hand-listed that could drift), invoke each
with minimal valid args, and stub `run_gh` to return a dict / a list / garbage / an
error / a missing binary / a timeout. The ONLY acceptable outcome is a `str`.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest
from ghplugin import register

_GATE_OK_BODY = (
    "## Problem\nThe widget crashes on empty input and we should handle it gracefully "
    "throughout the pipeline instead of raising.\n## Acceptance\nNo crash on empty input."
)

# Minimal valid args per tool — enough to get PAST the arg validation and into the
# `run_gh` result handling, which is what the sweep is about. Any tool not listed
# here is invoked with `{}` (its defaults), and the sweep fails loudly if a NEW tool
# needs args it doesn't have (so adding a tool means adding a row here).
_ARGS: dict[str, dict] = {
    "github_get_pr": {"repo": "o/n", "number": 1},
    "github_get_issue": {"repo": "o/n", "number": 1},
    "github_list_issues": {"repo": "o/n"},
    "github_get_commit_diff": {"repo": "o/n", "ref": "abc"},
    "github_pr_diff": {"repo": "o/n", "number": 1},
    "github_path_exists": {"repo": "o/n", "path": "x"},
    "github_ci_runs": {"repo": "o/n"},
    "github_run_failure": {"repo": "o/n", "run_id": 1},
    "github_read_file": {"repo": "o/n", "path": "README.md"},
    "github_read_pr_file": {"repo": "o/n", "number": 1, "path": "x"},
    "github_repo_contents": {"repo": "o/n", "path": "src"},
    "github_list_prs": {"repo": "o/n"},
    "github_issue_comments": {"repo": "o/n", "number": 1},
    "github_search_issues": {"repo": "o/n", "query": "crash"},
    "github_status": {},
    # create_issue is body-GATED (v0.7.0) — a gate-passing body so the sweep reaches gh.
    "github_create_issue": {"repo": "o/n", "title": "t", "body": _GATE_OK_BODY},
    "github_comment": {"repo": "o/n", "number": 1, "body": "b"},
    "github_create_pr": {"repo": "o/n", "head": "h", "title": "t"},
    "github_edit_pr": {"repo": "o/n", "number": 1, "title": "t", "state": "ready"},
    "github_merge_pr": {"repo": "o/n", "number": 1, "confirm": True},
    "github_close": {"repo": "o/n", "number": 1},
    "github_set_labels": {"repo": "o/n", "number": 1, "add": "bug"},
    "github_set_assignees": {"repo": "o/n", "number": 1, "add": "kj"},
    "github_review_comment": {"repo": "o/n", "number": 1, "body": "b"},
    "github_review_approve": {"repo": "o/n", "number": 1, "body": "b"},
    "github_review_request_changes": {"repo": "o/n", "number": 1, "body": "b"},
}

# What a stubbed `gh` hands back: (rc, stdout, stderr). Every tool must turn each
# into a string — including shapes that are valid JSON but the WRONG type.
_SHAPES = {
    "dict": (
        0,
        json.dumps({"type": "file", "name": "x", "path": "x", "size": 3, "login": "u", "head": {"sha": "a" * 40}}),
        "",
    ),
    "list": (0, json.dumps([{"type": "file", "name": "x", "path": "x", "size": 1}, "not-a-dict", 7]), ""),
    "list-of-nulls": (0, "[null, null]", ""),
    # every nested field a tool reads is a SCALAR — dicts()/parse_json must be total
    "nested-scalars": (
        0,
        json.dumps(
            {
                "files": 7,
                "labels": 1,
                "statusCheckRollup": 42,
                "reviews": 42,
                "latestReviews": 0,
                "comments": 42,
                "author": 3,
                "head": 5,
                "hosts": 1,
                "check_runs": 2,
            }
        ),
        "",
    ),
    "empty": (0, "", ""),
    "garbage": (0, "<<<not json>>> \x00\xff", ""),
    "number": (0, "42", ""),
    "string-json": (0, '"just a string"', ""),
    "error": (1, "", "HTTP 500: Internal Server Error"),
    "auth": (4, "", "To get started with GitHub CLI, please run:  gh auth login"),
    "not-found": (1, "", "GraphQL: Could not resolve to a Repository with the name 'o/n'. (repository)"),
    "missing-binary": (127, "", "gh CLI is not installed or not on PATH."),
    "timeout": (1, "", "gh command timed out after 30s"),
}


def _all_tools(make_registry):
    reg = make_registry({"write": True})
    register(reg)
    return {t.name: t for t in reg.tools}


def _patch_all(fake):
    """Patch run_gh in EVERY module that binds it (each imports the name directly)."""
    targets = [
        "ghplugin.read_tools.run_gh",
        "ghplugin.write_tools.run_gh",
        "ghplugin.review_tools.run_gh",
        "ghplugin.status.run_gh",
        "ghplugin.gh_issue.run_gh",
        "ghplugin.api.run_gh",
    ]
    patches = [patch(t, fake) for t in targets]
    for p in patches:
        p.start()
    return patches


def test_sweep_covers_every_registered_tool(make_registry):
    """The arg table must name every tool register() produces — a new tool can't slip
    past the sweep by omission."""
    names = set(_all_tools(make_registry))
    assert names == set(_ARGS), f"sweep table out of date: missing {names - set(_ARGS)}, stale {set(_ARGS) - names}"


@pytest.mark.parametrize("shape", list(_SHAPES))
async def test_every_tool_returns_a_str_never_raises(make_registry, shape):
    tools = _all_tools(make_registry)
    rc, out, serr = _SHAPES[shape]
    fake = AsyncMock(return_value=(rc, out, serr))
    patches = _patch_all(fake)
    try:
        with patch("ghplugin.status.resolve_gh", return_value="/usr/bin/gh"):
            for name, tool in tools.items():
                try:
                    result = await tool.ainvoke(_ARGS.get(name, {}))
                except Exception as e:  # noqa: BLE001 — the assertion message is the point
                    raise AssertionError(f"{name} RAISED on gh shape {shape!r}: {type(e).__name__}: {e}") from e
                assert isinstance(result, str), f"{name} returned {type(result).__name__} on shape {shape!r}"
    finally:
        for p in patches:
            p.stop()


async def test_every_tool_survives_a_raising_runner(make_registry):
    """Even a runner that itself blows up (it shouldn't — but a transport error might)
    must not escape a tool. Today the tools let it propagate; this documents the
    CURRENT contract: run_gh is the no-raise boundary, so this exercises run_gh's
    own missing-binary path end-to-end instead."""
    from ghplugin import gh_cli

    tools = _all_tools(make_registry)
    gh_cli.reset_gh_cache()
    try:
        with patch("ghplugin.gh_cli.shutil.which", return_value=None), patch.object(gh_cli, "_FALLBACK_BIN_DIRS", ()):
            for name, tool in tools.items():
                result = await tool.ainvoke(_ARGS.get(name, {}))
                assert isinstance(result, str) and "not installed" in result.lower(), f"{name}: {result[:120]!r}"
    finally:
        gh_cli.reset_gh_cache()


@pytest.mark.parametrize("exc", ["enoexec", "emfile", "kill-after-exit"])
async def test_every_tool_survives_a_failing_spawn(make_registry, monkeypatch, exc):
    """OSError from the spawn (ENOEXEC on a foreign-arch ~/.local/bin/gh, EMFILE) and a
    ProcessLookupError from kill() after a timeout must all be Error strings (#4)."""
    import asyncio
    import errno

    from ghplugin import gh_cli

    tools = _all_tools(make_registry)
    monkeypatch.setattr(gh_cli, "resolve_gh", lambda: "/fake/gh")

    class _Proc:
        returncode = None

        async def communicate(self):
            await asyncio.sleep(10)

        def kill(self):
            raise ProcessLookupError()

    async def spawn(*a, **k):
        if exc == "enoexec":
            raise OSError(errno.ENOEXEC, "Exec format error")
        if exc == "emfile":
            raise OSError(errno.EMFILE, "Too many open files")
        return _Proc()

    monkeypatch.setattr(gh_cli.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(gh_cli, "_COMMAND_TIMEOUT", 0)
    if exc == "kill-after-exit":
        # the timeout path: every run_gh call must time out instantly, then kill raises
        real_wait_for = asyncio.wait_for

        async def instant(coro, timeout=None):
            return await real_wait_for(coro, timeout=0)

        monkeypatch.setattr(gh_cli.asyncio, "wait_for", instant)
    # run_gh is looked up at call time in each module; patch the underlying spawn only,
    # so the REAL run_gh (the no-raise boundary) is what's under test here.
    with patch("ghplugin.status.resolve_gh", return_value="/fake/gh"):
        for name, tool in tools.items():
            try:
                result = await tool.ainvoke(_ARGS.get(name, {}))
            except Exception as e:  # noqa: BLE001
                raise AssertionError(f"{name} RAISED on spawn failure {exc!r}: {type(e).__name__}: {e}") from e
            assert isinstance(result, str), f"{name} returned {type(result).__name__} on {exc!r}"


async def test_repo_contents_on_a_file_says_so(make_registry):
    """The bug that motivated the sweep: a FILE path → dict → was an AttributeError."""
    tools = _all_tools(make_registry)
    fake = AsyncMock(
        return_value=(0, json.dumps({"type": "file", "name": "README.md", "path": "README.md", "size": 9}), "")
    )
    with patch("ghplugin.read_tools.run_gh", fake):
        out = await tools["github_repo_contents"].ainvoke({"repo": "o/n", "path": "README.md"})
    assert out == "Error: 'README.md' is a file, not a directory — use github_read_file to read it."
