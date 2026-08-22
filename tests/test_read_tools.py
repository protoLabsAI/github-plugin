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


@pytest.mark.asyncio
async def test_status_tool_reports_to_the_setup_gap_seam():
    """The model's own check is a recovery observation too — it must clear the banner."""
    calls = []

    class _Seam:
        def report_setup_gap(self, key, message):
            calls.append((key, message))

    tools = {t.name: t for t in get_read_tools("o/n", ["o/n"], registry=_Seam())}
    with patch("ghplugin.status.resolve_gh", return_value=None):
        await tools["github_status"].ainvoke({})
    assert dict(calls)["gh"] and dict(calls)["auth"] is None


@pytest.mark.asyncio
async def test_path_exists_classifies_before_the_missing_verdict():
    """An auth / rate-limit / missing-binary failure is UNVERIFIED (the classified error),
    never MISSING — only a real 404 is a verdict, and it names the inaccessible-repo case."""
    with patch("ghplugin.read_tools.run_gh", new=AsyncMock(return_value=(4, "", "please run: gh auth login"))):
        out = await _path_exists_tool().ainvoke({"repo": "owner/name", "path": "x"})
    assert out.startswith("Error: GitHub CLI is not authenticated") and "MISSING" not in out
    with patch(
        "ghplugin.read_tools.run_gh", new=AsyncMock(return_value=(127, "", "gh CLI is not installed or not on PATH."))
    ):
        out = await _path_exists_tool().ainvoke({"repo": "owner/name", "path": "x"})
    assert out.startswith("Error: gh CLI is not installed") and "MISSING" not in out
    with patch("ghplugin.read_tools.run_gh", new=AsyncMock(return_value=(1, "", "HTTP 403: API rate limit exceeded"))):
        out = await _path_exists_tool().ainvoke({"repo": "owner/name", "path": "x"})
    assert out.startswith("Error: GitHub API rate limit hit")
    with patch("ghplugin.read_tools.run_gh", new=AsyncMock(return_value=(1, "", "gh: Not Found (HTTP 404)"))):
        out = await _path_exists_tool().ainvoke({"repo": "owner/name", "path": "x"})
    assert out.startswith("MISSING: owner/name/x") and "repo is inaccessible" in out


# ── v0.7.0: the PM verbs ─────────────────────────────────────────────────────────


def _tool(name, default_repo=""):
    for t in get_read_tools(default_repo):
        if t.name == name:
            return t
    raise AssertionError(f"{name} not found")


_RICH_PR_JSON = json.dumps(
    {
        **json.loads(_PR_JSON),
        "isDraft": True,
        "reviewDecision": "CHANGES_REQUESTED",
        "mergeable": "MERGEABLE",
        "mergeStateStatus": "BLOCKED",
        "statusCheckRollup": [
            {"__typename": "CheckRun", "name": "test", "status": "COMPLETED", "conclusion": "SUCCESS"},
            {"__typename": "CheckRun", "name": "lint", "status": "COMPLETED", "conclusion": "FAILURE"},
            {"__typename": "CheckRun", "name": "e2e", "status": "IN_PROGRESS", "conclusion": ""},
            {"__typename": "CheckRun", "name": "docs", "status": "COMPLETED", "conclusion": "SKIPPED"},
            {"__typename": "StatusContext", "context": "CodeRabbit", "state": "SUCCESS"},
            {"__typename": "StatusContext", "context": "deploy/preview", "state": "PENDING"},
            {"__typename": "StatusContext", "context": "security", "state": "ERROR"},
        ],
        "reviews": [
            {"author": {"login": "quinn"}, "state": "CHANGES_REQUESTED", "body": "Blocker: " + "x" * 400},
            {"author": {"login": "kj"}, "state": "APPROVED", "body": ""},
            {"author": None, "state": "COMMENTED", "body": "drive-by"},
        ],
    }
)


@pytest.mark.asyncio
async def test_get_pr_requests_and_renders_the_merge_readiness_fields():
    mock = AsyncMock(return_value=(0, _RICH_PR_JSON, ""))
    with patch("ghplugin.read_tools.run_gh", mock):
        out = await _get_pr_tool().ainvoke({"repo": "owner/name", "number": 38})
    json_arg = mock.call_args.args[0][mock.call_args.args[0].index("--json") + 1]
    for f in ("isDraft", "reviewDecision", "mergeable", "mergeStateStatus", "statusCheckRollup", "reviews"):
        assert f in json_arg
    assert "PR #38 [OPEN] (DRAFT) feat: fix the ephemeral label" in out
    assert "review decision: CHANGES_REQUESTED | mergeable: MERGEABLE | merge state: BLOCKED" in out
    assert "checks: 2 pass / 2 fail / 2 pending / 1 skipped — failing: lint, security" in out
    assert "reviews (3):" in out
    assert "  - quinn [CHANGES_REQUESTED]: Blocker: " in out and "x" * 300 not in out  # body capped at 300
    assert "  - kj [APPROVED]" in out and "  - ? [COMMENTED]: drive-by" in out  # null author tolerated
    assert "files: view.py" in out and out.rstrip().endswith("fixed it")


@pytest.mark.asyncio
async def test_get_pr_with_no_checks_or_reviews_says_so():
    d = {**json.loads(_PR_JSON), "statusCheckRollup": [], "reviews": [], "reviewDecision": ""}
    with patch("ghplugin.read_tools.run_gh", new=AsyncMock(return_value=(0, json.dumps(d), ""))):
        out = await _get_pr_tool().ainvoke({"repo": "owner/name", "number": 38})
    assert "checks: none" in out and "reviews (0): none" in out and "review decision: none yet" in out


@pytest.mark.asyncio
async def test_get_pr_output_is_bounded():
    from ghplugin.read_tools import _MAX_PR_CHARS

    d = {
        **json.loads(_RICH_PR_JSON),
        "body": "b" * 50000,
        "reviews": [{"author": {"login": "r"}, "state": "COMMENTED", "body": "y" * 2000}] * 40,
    }
    with patch("ghplugin.read_tools.run_gh", new=AsyncMock(return_value=(0, json.dumps(d), ""))):
        out = await _get_pr_tool().ainvoke({"repo": "owner/name", "number": 1})
    assert len(out) <= _MAX_PR_CHARS + 60 and "… 30 more review(s)" in out


def test_summarize_checks_shapes():
    from ghplugin.read_tools import _summarize_checks

    assert _summarize_checks(None) == "none" and _summarize_checks([]) == "none"
    assert _summarize_checks("garbage") == "none"
    assert (
        _summarize_checks([{"name": "a", "status": "COMPLETED", "conclusion": "SUCCESS"}])
        == "1 pass / 0 fail / 0 pending"
    )
    assert _summarize_checks([{"context": "ci", "state": "FAILURE"}]) == "0 pass / 1 fail / 0 pending — failing: ci"
    assert _summarize_checks([{"name": "q", "status": "QUEUED", "conclusion": None}]) == "0 pass / 0 fail / 1 pending"


# github_list_prs — reuses api.fetch_prs (so patch run_gh where api binds it)


@pytest.mark.asyncio
async def test_list_prs_reuses_the_board_fetch_and_renders_flags():
    rows = json.dumps(
        [
            {
                "number": 5,
                "title": "PR",
                "state": "OPEN",
                "author": {"login": "kj"},
                "isDraft": True,
                "headRefName": "f",
                "baseRefName": "main",
                "reviewDecision": "",
                "mergeStateStatus": "BLOCKED",
                "url": "u5",
            },
            {
                "number": 6,
                "title": "Q",
                "state": "OPEN",
                "author": None,
                "isDraft": False,
                "headRefName": "g",
                "baseRefName": "main",
                "reviewDecision": "APPROVED",
                "mergeStateStatus": "CLEAN",
                "url": "u6",
            },
        ]
    )
    mock = AsyncMock(return_value=(0, rows, ""))
    with patch("ghplugin.api.run_gh", mock):
        out = await _tool("github_list_prs").ainvoke({"repo": "owner/name", "state": "all", "limit": 500})
    argv = mock.call_args.args[0]
    assert (
        argv[:4] == ["pr", "list", "--repo", "owner/name"]
        and "--state" in argv
        and argv[argv.index("--limit") + 1] == "100"
    )
    json_arg = argv[argv.index("--json") + 1]
    for f in ("isDraft", "headRefName", "baseRefName", "reviewDecision", "mergeStateStatus"):
        assert f in json_arg
    assert out.startswith("2 all pull request(s) in owner/name:")
    assert "  #5 [OPEN] PR — kj | f -> main | draft, BLOCKED | u5" in out
    assert "  #6 [OPEN] Q — ? | g -> main | APPROVED, CLEAN | u6" in out


@pytest.mark.asyncio
async def test_list_prs_empty_bad_state_and_errors():
    with patch("ghplugin.api.run_gh", new=AsyncMock(return_value=(0, "[]", ""))):
        assert (
            await _tool("github_list_prs").ainvoke({"repo": "owner/name"})
        ) == "No open pull requests in owner/name."
    with patch("ghplugin.api.run_gh", new=AsyncMock(return_value=(4, "", "gh auth login"))):
        out = await _tool("github_list_prs").ainvoke({"repo": "owner/name"})
    assert out.startswith("Error: GitHub CLI is not authenticated")
    assert "state must be" in (await _tool("github_list_prs").ainvoke({"repo": "owner/name", "state": "weird"}))
    assert (await _tool("github_list_prs").ainvoke({"repo": "bad"})).startswith("Error: no usable repo")


# github_issue_comments


@pytest.mark.asyncio
async def test_issue_comments_renders_newest_limit_in_order_and_caps_bodies():
    comments = [
        {"author": {"login": f"u{i}"}, "createdAt": f"2026-08-{i:02d}", "body": f"c{i} " + "z" * 1500}
        for i in range(1, 6)
    ]
    mock = AsyncMock(return_value=(0, json.dumps({"comments": comments}), ""))
    with patch("ghplugin.read_tools.run_gh", mock):
        out = await _tool("github_issue_comments").ainvoke({"repo": "owner/name", "number": 7, "limit": 2})
    assert mock.call_args.args[0] == ["issue", "view", "7", "--repo", "owner/name", "--json", "comments"]
    assert out.startswith("5 comment(s) on owner/name#7 — showing the last 2:")
    assert "--- u4 · 2026-08-04" in out and "--- u5 · 2026-08-05" in out and "u3" not in out
    assert out.index("u4") < out.index("u5")  # chronological
    assert "z" * 1000 not in out and "…" in out  # each body capped at 1000


@pytest.mark.asyncio
async def test_issue_comments_empty_and_odd_rows():
    with patch("ghplugin.read_tools.run_gh", new=AsyncMock(return_value=(0, '{"comments": []}', ""))):
        assert (
            await _tool("github_issue_comments").ainvoke({"repo": "owner/name", "number": 1})
        ) == "No comments on owner/name#1."
    odd = json.dumps({"comments": [None, {"author": None, "createdAt": None, "body": None}]})
    with patch("ghplugin.read_tools.run_gh", new=AsyncMock(return_value=(0, odd, ""))):
        out = await _tool("github_issue_comments").ainvoke({"repo": "owner/name", "number": 1})
    assert "1 comment(s)" in out and "--- ? · ?\n(empty)" in out


# github_search_issues


@pytest.mark.asyncio
async def test_search_issues_argv_and_render():
    hits = json.dumps([{"number": 23, "title": "Validate default_repo", "state": "closed", "url": "u23"}])
    mock = AsyncMock(return_value=(0, hits, ""))
    with patch("ghplugin.read_tools.run_gh", mock):
        out = await _tool("github_search_issues").ainvoke(
            {"repo": "owner/name", "query": " default_repo typo ", "state": "closed", "limit": 5}
        )
    argv = mock.call_args.args[0]
    assert argv[:4] == ["search", "issues", "--repo", "owner/name"] and argv[4] == "default_repo typo"
    assert argv[argv.index("--limit") + 1] == "5" and argv[argv.index("--state") + 1] == "closed"
    assert (
        out
        == "1 closed issue(s) in owner/name matching 'default_repo typo':\n  #23 [closed] Validate default_repo — u23"
    )


@pytest.mark.asyncio
async def test_search_issues_all_state_omits_the_flag_and_validates():
    """gh's --state is open|closed only — `all` means no filter (verified against gh 2.92)."""
    mock = AsyncMock(return_value=(0, "[]", ""))
    with patch("ghplugin.read_tools.run_gh", mock):
        out = await _tool("github_search_issues").ainvoke({"repo": "owner/name", "query": "x", "state": "all"})
    assert "--state" not in mock.call_args.args[0]
    assert out == "No all issues in owner/name match 'x' — nothing to dedupe against."
    assert "query` is empty" in (await _tool("github_search_issues").ainvoke({"repo": "owner/name", "query": "  "}))
    assert "state must be" in (
        await _tool("github_search_issues").ainvoke({"repo": "owner/name", "query": "x", "state": "merged"})
    )
    mock.assert_called_once()


def test_search_issues_description_says_dedupe_before_filing():
    assert "DEDUPE BEFORE FILING" in _tool("github_search_issues").description
    assert (
        "duplicate"
        in {t.name: t for t in __import__("ghplugin.write_tools", fromlist=["get_write_tools"]).get_write_tools()}[
            "github_create_issue"
        ].description
    )
