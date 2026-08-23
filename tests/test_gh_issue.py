"""The /issue command logic — parsing, the gate-conformance check, and filing.

Host-free: `run_gh` is patched in the `ghplugin.gh_issue` namespace, so we assert the
gate behavior and the `gh issue create` path without touching GitHub.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from ghplugin.gh_issue import (
    current_default,
    default_repo_error,
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


@pytest.mark.parametrize(
    "key,heading",
    [
        ("problem", "Observed"),
        ("problem", "Symptom"),
        ("problem", "Idea"),
        ("problem", "Current behavior"),
        ("problem", "What happens"),
        ("repro", "Root cause"),
        ("repro", "Symptom"),
        ("proposal", "Proposed work"),
        ("proposal", "Plan"),
    ],
)
def test_section_regexes_match_protoagents_ci_gate(key, heading):
    """Lockstep with .github/workflows/issue-gate.yml — a heading CI accepts must be
    accepted here (else the tool refuses a body CI would pass)."""
    from ghplugin.gh_issue import _has_section

    assert _has_section(f"## {heading}\nbody", key)
    assert _has_section(f"**{heading}**\nbody", key)  # the bold-line form too


def test_whatever_does_not_match_the_word_boundary_what():
    from ghplugin.gh_issue import _has_section

    assert not _has_section("## Whatever\nbody", "problem")
    assert not _has_section("## Network\nbody", "proposal")  # \bwork\b, not "network"


def test_infer_kind_promotes_generic_by_label():
    from ghplugin.gh_issue import infer_kind

    assert infer_kind("generic", ["bug"]) == "bug"
    assert infer_kind("generic", ["Enhancement", "p1"]) == "feature"
    assert infer_kind("generic", ["p1"]) == "generic"
    assert infer_kind("feature", ["bug"]) == "feature"  # explicit wins
    assert infer_kind("", None) == "generic"


async def test_issue_command_label_bug_demands_repro(monkeypatch):
    """`/issue t --label bug` without --bug is still a bug for the gate (CI keys on the label)."""
    fake = AsyncMock()
    body = "## Problem\nThe parser crashes on empty input and we should handle it gracefully across the whole pipeline."
    with patch("ghplugin.gh_issue.run_gh", fake):
        out = await run_issue_command(f"Crash --label bug --repo o/n\n{body}", default_repo="")
    assert out.startswith("Not filed") and "Steps to reproduce" in out
    fake.assert_not_called()


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


def test_resolve_repo_env_fallback_is_logged_not_silent(monkeypatch, caplog):
    """The env step is invisible in Settings — when it decides the repo, say so at INFO."""
    import logging

    monkeypatch.delenv("GITHUB_DEFAULT_REPO", raising=False)
    monkeypatch.setenv("GH_REPO", "o/env")
    with caplog.at_level(logging.INFO, logger="protoagent.plugins.github"):
        assert resolve_repo(None, "") == "o/env"
    assert any("GH_REPO=o/env" in r.getMessage() for r in caplog.records)
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="protoagent.plugins.github"):
        assert resolve_repo("o/explicit", "") == "o/explicit"  # env never consulted
    assert not caplog.records


def test_resolve_repo_accepts_a_getter_and_calls_it_lazily(monkeypatch):
    monkeypatch.delenv("GITHUB_DEFAULT_REPO", raising=False)
    monkeypatch.delenv("GH_REPO", raising=False)
    calls = []

    def getter():
        calls.append(1)
        return "o/live"

    assert resolve_repo("o/explicit", getter) == "o/explicit" and calls == []  # explicit ⇒ getter untouched
    assert resolve_repo("", getter) == "o/live" and len(calls) == 1

    def boom():
        raise RuntimeError("host not ready")

    assert resolve_repo("", boom) is None  # a broken getter reads as "no default", never raises
    assert current_default(boom) == "" and current_default(" o/s ") == "o/s"


def test_default_repo_error_names_only_malformed_values():
    assert default_repo_error("") is None and default_repo_error("  ") is None
    assert default_repo_error("o/n") is None
    err = default_repo_error("protoLabsAI")
    assert err.startswith("Error: github.default_repo must be 'owner/name' (got 'protoLabsAI')")
    assert "Settings ▸ GitHub" in err


async def test_issue_command_names_a_malformed_configured_default(monkeypatch):
    monkeypatch.delenv("GITHUB_DEFAULT_REPO", raising=False)
    monkeypatch.delenv("GH_REPO", raising=False)
    out = await run_issue_command("A title with no repo flag", default_repo="just-owner")
    assert out.startswith("Error: github.default_repo must be 'owner/name' (got 'just-owner')")
    # an explicit --repo that's malformed is still the --repo error
    out = await run_issue_command("Title --repo nope", default_repo="o/fine")
    assert out.startswith("Error: --repo must be 'owner/name'")


def test_effective_default_repo():
    assert effective_default_repo("o/explicit", ["o/a"]) == "o/explicit"
    assert effective_default_repo("", ["o/a", "o/b"]) == "o/a"  # first of the picker list
    assert effective_default_repo("", []) == ""


def test_effective_default_repo_only_calls_the_picker_getter_when_needed():
    """The picker getter may fork git (checkout remotes) — a configured default_repo
    must short-circuit it (#5 of the adversarial review)."""
    calls = []

    def picker():
        calls.append(1)
        return ["o/a"]

    assert effective_default_repo("o/explicit", picker) == "o/explicit" and calls == []
    assert effective_default_repo("", picker) == "o/a" and len(calls) == 1

    def boom():
        raise RuntimeError("host not ready")

    assert effective_default_repo("", boom) == ""  # never raises


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
