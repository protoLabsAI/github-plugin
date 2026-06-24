"""The /issue command logic — parsing, the gate-conformance check, and filing.

Host-free: `run_gh` is patched in the `ghplugin.gh_issue` namespace, so we assert the
gate behavior and the `gh issue create` path without touching GitHub.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from ghplugin.gh_issue import (
    effective_default_repo,
    labels_for,
    missing_sections,
    resolve_repo,
    run_issue_command,
)

# A body that clears the gate (>= 80 collapsed chars + a Problem + repro section).
_GOOD_BUG_BODY = (
    "## Problem\nThe parser crashes on empty input and we should handle it gracefully "
    "across the whole pipeline instead of raising.\n## Steps to reproduce\nRun it with an empty string."
)


# --- pure helpers ------------------------------------------------------------


def test_missing_sections_by_kind():
    assert missing_sections("", "generic")  # empty → missing description + problem
    assert missing_sections(_GOOD_BUG_BODY, "bug") == []  # passes for a bug
    # A feature needs a proposal OR acceptance section.
    feat = "## Motivation\nWe need this capability badly for the next release cycle to ship on time."
    assert "a Proposed-direction or Acceptance section" in missing_sections(feat, "feature")


def test_labels_for_prepends_type_label():
    assert labels_for("bug", ["p0"]) == ["bug", "p0"]
    assert labels_for("feature") == ["enhancement"]
    assert labels_for("generic", ["x", "x"]) == ["x"]  # de-duped, no type label


def test_resolve_repo_precedence(monkeypatch):
    monkeypatch.delenv("GITHUB_DEFAULT_REPO", raising=False)
    monkeypatch.delenv("GH_REPO", raising=False)
    assert resolve_repo("o/explicit", "o/default") == "o/explicit"  # explicit wins
    assert resolve_repo(None, "o/default") == "o/default"  # then configured default
    assert resolve_repo(None, "") is None  # then nothing
    monkeypatch.setenv("GH_REPO", "o/env")
    assert resolve_repo(None, "") == "o/env"  # then env


def test_effective_default_repo():
    assert effective_default_repo("o/explicit", ["o/a"]) == "o/explicit"
    assert effective_default_repo("", ["o/a", "o/b"]) == "o/a"  # first of the picker list
    assert effective_default_repo("", []) == ""


# --- run_issue_command -------------------------------------------------------


async def test_usage_when_no_title():
    out = await run_issue_command("--bug --repo o/n", default_repo="")
    assert out.startswith("Usage:") and "Scaffold" in out


async def test_no_repo_errors():
    out = await run_issue_command("A title with no repo anywhere", default_repo="")
    assert "No target repo" in out


async def test_bad_repo_errors():
    out = await run_issue_command("Title --repo not-a-repo", default_repo="")
    assert out.startswith("Error:") and "owner/name" in out


async def test_dry_run_previews_without_shelling_out():
    fake = AsyncMock()
    with patch("ghplugin.gh_issue.run_gh", fake):
        out = await run_issue_command(f"Crash on empty --bug --repo o/n --dry-run\n{_GOOD_BUG_BODY}", default_repo="")
    assert out.startswith("Dry run") and "**o/n**" in out and "labels: bug" in out
    fake.assert_not_called()


async def test_files_issue_and_returns_url():
    fake = AsyncMock(return_value=(0, "https://github.com/o/n/issues/42", ""))
    with patch("ghplugin.gh_issue.run_gh", fake):
        out = await run_issue_command(f"Crash on empty --bug --repo o/n\n{_GOOD_BUG_BODY}", default_repo="")
    assert out == "Filed in o/n · labels: bug: https://github.com/o/n/issues/42"
    args = fake.call_args.args[0]
    assert args[:4] == ["issue", "create", "--repo", "o/n"]
    assert "--label" in args and args[args.index("--label") + 1] == "bug"


async def test_uses_default_repo_when_no_flag():
    fake = AsyncMock(return_value=(0, "https://github.com/o/d/issues/1", ""))
    with patch("ghplugin.gh_issue.run_gh", fake):
        out = await run_issue_command(f"Crash on empty --bug\n{_GOOD_BUG_BODY}", default_repo="o/d")
    assert "Filed in o/d" in out
    assert (
        "--repo" in fake.call_args.args[0]
        and fake.call_args.args[0][fake.call_args.args[0].index("--repo") + 1] == "o/d"
    )


async def test_missing_sections_blocks_filing():
    fake = AsyncMock()
    with patch("ghplugin.gh_issue.run_gh", fake):
        out = await run_issue_command("Title --bug --repo o/n\ntoo short", default_repo="")
    assert out.startswith("Not filed") and "missing" in out
    fake.assert_not_called()  # never shells out when the body fails the gate


async def test_gh_failure_surfaces_error():
    fake = AsyncMock(return_value=(1, "", "HTTP 403: forbidden"))
    with patch("ghplugin.gh_issue.run_gh", fake):
        out = await run_issue_command(f"Crash on empty --bug --repo o/n\n{_GOOD_BUG_BODY}", default_repo="")
    assert out.startswith("Error (gh exit 1):") and "forbidden" in out
