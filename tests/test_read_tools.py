"""github_read_file + github_repo_contents over a mocked `gh`.

Host-free: we mock ``ghplugin.read_tools.run_gh`` (the name bound in read_tools) so no
real `gh`/network is touched. The tools are langchain ``@tool``; invoke via ``ainvoke``.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest
from ghplugin.read_tools import get_read_tools


def _read_file_tool():
    for t in get_read_tools():
        if t.name == "github_read_file":
            return t
    raise AssertionError("github_read_file tool not found")


def _repo_contents_tool():
    for t in get_read_tools():
        if t.name == "github_repo_contents":
            return t
    raise AssertionError("github_repo_contents tool not found")


_CONTENTS_JSON = json.dumps(
    [
        {"name": "README.md", "path": "src/README.md", "type": "file", "size": 1234},
        {"name": "lib", "path": "src/lib", "type": "dir", "size": 0},
    ]
)


@pytest.mark.asyncio
async def test_success_returns_raw_content():
    with patch("ghplugin.read_tools.run_gh", new=AsyncMock(return_value=(0, "file content", ""))):
        result = await _read_file_tool().ainvoke({"repo": "owner/name", "path": "README.md"})
    assert result == "file content"


@pytest.mark.asyncio
async def test_ref_is_passed_to_gh_args():
    mock = AsyncMock(return_value=(0, "ok", ""))
    with patch("ghplugin.read_tools.run_gh", mock):
        await _read_file_tool().ainvoke({"repo": "owner/name", "path": "docs/guide.md", "ref": "main"})
    args = mock.call_args.args[0]
    assert "-f" in args
    assert "ref=main" in args


@pytest.mark.asyncio
async def test_invalid_repo_returns_bad_repo_error():
    # run_gh must NOT be called for an invalid repo.
    mock = AsyncMock(return_value=(0, "nope", ""))
    with patch("ghplugin.read_tools.run_gh", mock):
        result = await _read_file_tool().ainvoke({"repo": "bad", "path": "README.md"})
    assert result.startswith("Error: no usable repo")
    mock.assert_not_called()


@pytest.mark.asyncio
async def test_gh_error_is_surfaced():
    with patch("ghplugin.read_tools.run_gh", new=AsyncMock(return_value=(1, "", "not found"))):
        result = await _read_file_tool().ainvoke({"repo": "owner/name", "path": "missing.md"})
    assert result.startswith("Error (gh exit 1)")


@pytest.mark.asyncio
async def test_long_content_is_truncated():
    with patch("ghplugin.read_tools.run_gh", new=AsyncMock(return_value=(0, "x" * 25000, ""))):
        result = await _read_file_tool().ainvoke({"repo": "owner/name", "path": "big.txt"})
    assert result.endswith("… (truncated at 20000 chars)")
    assert len(result) == 20000 + len("\n… (truncated at 20000 chars)")


@pytest.mark.asyncio
async def test_empty_content_returns_empty_string():
    with patch("ghplugin.read_tools.run_gh", new=AsyncMock(return_value=(0, "", ""))):
        result = await _read_file_tool().ainvoke({"repo": "owner/name", "path": "empty.txt"})
    assert result == ""


# ── github_repo_contents ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_repo_contents_lists_file_and_dir():
    with patch("ghplugin.read_tools.run_gh", new=AsyncMock(return_value=(0, _CONTENTS_JSON, ""))):
        result = await _repo_contents_tool().ainvoke({"repo": "owner/name", "path": "src"})
    assert "owner/name/src — 2 item(s):" in result
    assert "FILE" in result
    assert "DIR " in result
    assert "README.md" in result
    assert "(src/README.md)" in result
    assert "lib" in result
    assert "1234" in result


@pytest.mark.asyncio
async def test_repo_contents_root_passes_no_trailing_slash():
    mock = AsyncMock(return_value=(0, _CONTENTS_JSON, ""))
    with patch("ghplugin.read_tools.run_gh", mock):
        await _repo_contents_tool().ainvoke({"repo": "owner/name"})
    assert mock.call_args.args[0] == ["api", "repos/owner/name/contents"]


@pytest.mark.asyncio
async def test_repo_contents_ref_is_passed_to_gh_args():
    mock = AsyncMock(return_value=(0, _CONTENTS_JSON, ""))
    with patch("ghplugin.read_tools.run_gh", mock):
        await _repo_contents_tool().ainvoke({"repo": "owner/name", "path": "src", "ref": "main"})
    args = mock.call_args.args[0]
    assert "-f" in args
    assert "ref=main" in args


@pytest.mark.asyncio
async def test_repo_contents_empty_dir_returns_note():
    with patch("ghplugin.read_tools.run_gh", new=AsyncMock(return_value=(0, "[]", ""))):
        result = await _repo_contents_tool().ainvoke({"repo": "owner/name", "path": "empty"})
    assert "No contents" in result


@pytest.mark.asyncio
async def test_repo_contents_invalid_repo_returns_bad_repo_error():
    mock = AsyncMock(return_value=(0, "[]", ""))
    with patch("ghplugin.read_tools.run_gh", mock):
        result = await _repo_contents_tool().ainvoke({"repo": "bad", "path": ""})
    assert result.startswith("Error: no usable repo")
    mock.assert_not_called()


@pytest.mark.asyncio
async def test_repo_contents_gh_error_is_surfaced():
    with patch("ghplugin.read_tools.run_gh", new=AsyncMock(return_value=(1, "", "not found"))):
        result = await _repo_contents_tool().ainvoke({"repo": "owner/name", "path": "missing"})
    assert result.startswith("Error (gh exit 1)")


@pytest.mark.asyncio
async def test_repo_contents_bad_json_returns_parse_error():
    with patch("ghplugin.read_tools.run_gh", new=AsyncMock(return_value=(0, "not json", ""))):
        result = await _repo_contents_tool().ainvoke({"repo": "owner/name", "path": ""})
    assert result.startswith("Error: could not parse gh output")


# ── github_get_pr: the head branch must be readable, not guessed ─────────────────
# A consumer that wants a file FROM the PR (github_read_file/github_repo_contents,
# both `ref`-keyed) has no other way to learn the real branch name — omitting it
# here means the caller has to guess one from the PR title, which is exactly how
# a real PR lookup once 404'd (guessed "proto/feature/<slug-of-title>" instead of
# the actual "feat/bd-3a4").
def _get_pr_tool():
    for t in get_read_tools():
        if t.name == "github_get_pr":
            return t
    raise AssertionError("github_get_pr tool not found")


_PR_JSON = json.dumps(
    {
        "number": 38,
        "title": "feat: fix the ephemeral label",
        "state": "OPEN",
        "author": {"login": "roxy"},
        "body": "fixed it",
        "additions": 10,
        "deletions": 2,
        "files": [{"path": "view.py"}],
        "url": "https://github.com/owner/name/pull/38",
        "headRefName": "feat/bd-3a4",
        "baseRefName": "main",
    }
)


@pytest.mark.asyncio
async def test_get_pr_requests_head_and_base_ref():
    mock = AsyncMock(return_value=(0, _PR_JSON, ""))
    with patch("ghplugin.read_tools.run_gh", mock):
        await _get_pr_tool().ainvoke({"repo": "owner/name", "number": 38})
    args = mock.call_args.args[0]
    json_arg = args[args.index("--json") + 1]
    assert "headRefName" in json_arg
    assert "baseRefName" in json_arg


@pytest.mark.asyncio
async def test_get_pr_surfaces_the_branch_in_its_output():
    with patch("ghplugin.read_tools.run_gh", new=AsyncMock(return_value=(0, _PR_JSON, ""))):
        result = await _get_pr_tool().ainvoke({"repo": "owner/name", "number": 38})
    assert "feat/bd-3a4 -> main" in result


# ── github_pr_diff (the review workflow's diff source) ───────────────────────────
def _pr_diff_tool(default_repo=""):
    for t in get_read_tools(default_repo):
        if t.name == "github_pr_diff":
            return t
    raise AssertionError("github_pr_diff tool not found")


@pytest.mark.asyncio
async def test_pr_diff_returns_diff_via_gh_pr_diff():
    mock = AsyncMock(return_value=(0, "diff --git a/x b/x\n+added", ""))
    with patch("ghplugin.read_tools.run_gh", mock):
        result = await _pr_diff_tool().ainvoke({"repo": "owner/name", "number": 42})
    argv = mock.call_args.args[0]
    assert argv[:3] == ["pr", "diff", "42"]
    assert "--repo" in argv and "owner/name" in argv
    assert "diff --git a/x b/x" in result
    assert result.startswith("PR owner/name#42 diff:")


@pytest.mark.asyncio
async def test_pr_diff_truncates_at_max_chars():
    mock = AsyncMock(return_value=(0, "x" * 500, ""))
    with patch("ghplugin.read_tools.run_gh", mock):
        result = await _pr_diff_tool().ainvoke({"repo": "owner/name", "number": 1, "max_chars": 100})
    assert "truncated at 100 chars" in result


@pytest.mark.asyncio
async def test_pr_diff_empty_returns_note():
    with patch("ghplugin.read_tools.run_gh", new=AsyncMock(return_value=(0, "  ", ""))):
        result = await _pr_diff_tool().ainvoke({"repo": "owner/name", "number": 7})
    assert "No diff for owner/name#7" in result


@pytest.mark.asyncio
async def test_pr_diff_invalid_repo_returns_bad_repo_error():
    mock = AsyncMock(return_value=(0, "nope", ""))
    with patch("ghplugin.read_tools.run_gh", mock):
        result = await _pr_diff_tool().ainvoke({"repo": "bad", "number": 1})
    assert result.startswith("Error:")
    mock.assert_not_called()


@pytest.mark.asyncio
async def test_pr_diff_gh_error_is_surfaced():
    with patch("ghplugin.read_tools.run_gh", new=AsyncMock(return_value=(1, "", "gh: not found"))):
        result = await _pr_diff_tool().ainvoke({"repo": "owner/name", "number": 1})
    assert result.startswith("Error")


# ── default_repo fallback (omit repo → use the configured default) ───────────────
def _list_issues_tool(default_repo=""):
    for t in get_read_tools(default_repo):
        if t.name == "github_list_issues":
            return t
    raise AssertionError("github_list_issues tool not found")


@pytest.mark.asyncio
async def test_omitted_repo_uses_configured_default(monkeypatch):
    """A tool called WITHOUT repo falls back to the factory's default_repo (#issue)."""
    monkeypatch.delenv("GITHUB_DEFAULT_REPO", raising=False)
    monkeypatch.delenv("GH_REPO", raising=False)
    mock = AsyncMock(return_value=(0, "[]", ""))
    with patch("ghplugin.read_tools.run_gh", mock):
        await _list_issues_tool("owner/default").ainvoke({})  # no repo passed
    argv = mock.call_args.args[0]
    assert "--repo" in argv and "owner/default" in argv


@pytest.mark.asyncio
async def test_explicit_repo_overrides_default(monkeypatch):
    monkeypatch.delenv("GITHUB_DEFAULT_REPO", raising=False)
    monkeypatch.delenv("GH_REPO", raising=False)
    mock = AsyncMock(return_value=(0, "[]", ""))
    with patch("ghplugin.read_tools.run_gh", mock):
        await _list_issues_tool("owner/default").ainvoke({"repo": "owner/explicit"})
    argv = mock.call_args.args[0]
    assert "owner/explicit" in argv and "owner/default" not in argv


@pytest.mark.asyncio
async def test_omitted_repo_no_default_errors(monkeypatch):
    monkeypatch.delenv("GITHUB_DEFAULT_REPO", raising=False)
    monkeypatch.delenv("GH_REPO", raising=False)
    result = await _list_issues_tool("").ainvoke({})  # no repo, no default, no env
    assert result.startswith("Error: no usable repo")


# ── github_path_exists: the EXISTS/MISSING grounding probe (ADR 0078) ───────────
def _path_exists_tool():
    for t in get_read_tools():
        if t.name == "github_path_exists":
            return t
    raise AssertionError("github_path_exists tool not found")


@pytest.mark.asyncio
async def test_path_exists_reports_exists():
    with patch("ghplugin.read_tools.run_gh", new=AsyncMock(return_value=(0, "{}", ""))):
        out = await _path_exists_tool().ainvoke({"repo": "owner/name", "path": "packages/x"})
    assert out.startswith("EXISTS: owner/name/packages/x")


@pytest.mark.asyncio
async def test_path_exists_reports_missing_on_404():
    with patch("ghplugin.read_tools.run_gh", new=AsyncMock(return_value=(1, "", "HTTP 404: Not Found"))):
        out = await _path_exists_tool().ainvoke({"repo": "owner/name", "path": "gone.txt", "ref": "main"})
    assert out.startswith("MISSING: owner/name/gone.txt @ main")


@pytest.mark.asyncio
async def test_path_exists_other_error_is_unverified_not_a_verdict():
    with patch("ghplugin.read_tools.run_gh", new=AsyncMock(return_value=(1, "", "HTTP 500"))):
        out = await _path_exists_tool().ainvoke({"repo": "owner/name", "path": "x"})
    assert "EXISTS" not in out and "MISSING" not in out.split(":")[0]
    assert out.startswith("Error")


# ── github_read_pr_file: the ref is resolved server-side, never by the caller ────
# Regression for pr-reviewer-plugin#20: a review that read a PR's files at the
# DEFAULT branch "confirmed" that symbols the PR ADDS did not exist, producing
# blocker findings on correct PRs. This tool removes the ref from the caller's
# hands entirely.


def _read_pr_file_tool():
    for t in get_read_tools():
        if t.name == "github_read_pr_file":
            return t
    raise AssertionError("github_read_pr_file tool not found")


@pytest.mark.asyncio
async def test_read_pr_file_reads_at_the_prs_head_sha():
    head = "19c37f7a7db6f0c1a2b3c4d5e6f708192a3b4c5d"
    calls = []

    async def fake_gh(args, **kw):
        calls.append(args)
        if "/pulls/" in args[1]:
            return 0, head + "\n", ""
        return 0, "export const goalDetailQuery = () => {}\n", ""

    with patch("ghplugin.read_tools.run_gh", new=AsyncMock(side_effect=fake_gh)):
        out = await _read_pr_file_tool().ainvoke({"repo": "o/r", "number": 2088, "path": "src/lib/queries.ts"})

    # the content read is pinned to the head SHA the PR reported
    content_call = " ".join(calls[-1])
    assert f"ref={head}" in content_call
    assert "goalDetailQuery" in out and head[:12] in out


@pytest.mark.asyncio
async def test_read_pr_file_never_falls_back_to_the_default_branch():
    """A failed pinned read must ERROR, not silently serve the pre-PR file."""

    async def fake_gh(args, **kw):
        if "/pulls/" in args[1]:
            return 0, "a" * 40 + "\n", ""
        return 1, "", "Not Found (HTTP 404)"

    with patch("ghplugin.read_tools.run_gh", new=AsyncMock(side_effect=fake_gh)):
        out = await _read_pr_file_tool().ainvoke({"repo": "o/r", "number": 1, "path": "nope.ts"})
    assert out.startswith("Error reading nope.ts")


@pytest.mark.asyncio
async def test_read_pr_file_errors_when_the_head_sha_is_unresolvable():
    async def fake_gh(args, **kw):
        return 0, "\n", ""  # empty head — a PR we cannot pin

    with patch("ghplugin.read_tools.run_gh", new=AsyncMock(side_effect=fake_gh)):
        out = await _read_pr_file_tool().ainvoke({"repo": "o/r", "number": 1, "path": "x.ts"})
    assert "could not resolve the head SHA" in out
