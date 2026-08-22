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


# ── v0.6.0: the descriptions the model sees are TRUE ─────────────────────────────


def test_no_tool_description_carries_a_todo_or_stub_note():
    """`github_read_file`'s docstring (= the tool description the model reads) shipped a
    `TODO(team): implement via …` for five releases while the tool worked fine."""
    for t in get_read_tools():
        desc = (t.description or "").lower()
        assert "todo" not in desc and "stub" not in desc, f"{t.name}: {t.description!r}"


@pytest.mark.asyncio
async def test_repo_contents_on_a_file_path_returns_an_error_string():
    """The contents API returns a dict for a FILE — iterating it raised through the tool layer."""
    body = json.dumps({"type": "file", "name": "README.md", "path": "README.md", "size": 42})
    with patch("ghplugin.read_tools.run_gh", new=AsyncMock(return_value=(0, body, ""))):
        result = await _repo_contents_tool().ainvoke({"repo": "owner/name", "path": "README.md"})
    assert result == "Error: 'README.md' is a file, not a directory — use github_read_file to read it."


@pytest.mark.asyncio
async def test_repo_contents_skips_non_dict_rows():
    body = json.dumps([{"name": "a", "path": "a", "type": "file", "size": 1}, None, "x"])
    with patch("ghplugin.read_tools.run_gh", new=AsyncMock(return_value=(0, body, ""))):
        result = await _repo_contents_tool().ainvoke({"repo": "owner/name", "path": ""})
    assert "1 item(s)" in result and "  a  (a)" in result


@pytest.mark.asyncio
async def test_get_pr_wrong_json_type_is_an_error_string():
    with patch("ghplugin.read_tools.run_gh", new=AsyncMock(return_value=(0, "[1, 2]", ""))):
        result = await _get_pr_tool().ainvoke({"repo": "owner/name", "number": 1})
    assert result.startswith("Error: unexpected gh output (expected a JSON dict, got list)")


@pytest.mark.asyncio
async def test_unauthenticated_gh_is_a_classified_error(monkeypatch):
    monkeypatch.delenv("GITHUB_DEFAULT_REPO", raising=False)
    monkeypatch.delenv("GH_REPO", raising=False)
    with patch(
        "ghplugin.read_tools.run_gh",
        new=AsyncMock(return_value=(4, "", "To get started with GitHub CLI, please run:  gh auth login")),
    ):
        result = await _list_issues_tool("owner/name").ainvoke({})
    assert result.startswith("Error: GitHub CLI is not authenticated — run `gh auth login`")


@pytest.mark.asyncio
async def test_repo_not_found_is_a_classified_error():
    serr = "GraphQL: Could not resolve to a Repository with the name 'owner/gone'. (repository)"
    with patch("ghplugin.read_tools.run_gh", new=AsyncMock(return_value=(1, "", serr))):
        result = await _list_issues_tool().ainvoke({"repo": "owner/gone"})
    assert result.startswith("Error: repo 'owner/gone' not found or not accessible")


# ── github_status ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_status_tool_summarises_and_names_a_bad_default(monkeypatch):
    tools = {t.name: t for t in get_read_tools("just-owner", ["o/a"])}
    with patch("ghplugin.status.resolve_gh", return_value=None):
        out = await tools["github_status"].ainvoke({})
    assert out.startswith("GitHub CLI is NOT installed")
    assert "NOTE: Error: github.default_repo must be 'owner/name' (got 'just-owner')" in out
    assert "1 repo(s) in the picker: o/a" in out


@pytest.mark.asyncio
async def test_status_tool_reads_default_and_repos_through_getters():
    calls = {"d": 0, "r": 0}

    def d():
        calls["d"] += 1
        return "o/live"

    def r():
        calls["r"] += 1
        return ["o/live", "o/x"]

    tools = {t.name: t for t in get_read_tools(d, r)}
    with patch("ghplugin.status.resolve_gh", return_value=None):
        out = await tools["github_status"].ainvoke({})
    assert "Default repo: o/live." in out and calls == {"d": 1, "r": 1}
