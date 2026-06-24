"""github_create_issue — the first built-out write tool.

These exercise the tool's command construction and error handling WITHOUT touching
GitHub: ``run_gh`` is patched in the ``ghplugin.write_tools`` namespace (where the tool
looks it up), so we assert the exact ``gh issue create`` argv and the readable error
strings. asyncio_mode=auto (see pyproject) runs the async tests directly.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from ghplugin.write_tools import get_write_tools


def _create_issue():
    return {t.name: t for t in get_write_tools()}["github_create_issue"]


def _labels_in(args: list[str]) -> list[str]:
    """The value following each ``--label`` flag, in order."""
    return [args[i + 1] for i, a in enumerate(args) if a == "--label"]


async def test_returns_issue_url():
    """On success the tool returns the new issue URL (gh's stdout, stripped)."""
    tool = _create_issue()
    fake = AsyncMock(return_value=(0, "https://github.com/o/n/issues/42\n", ""))
    with patch("ghplugin.write_tools.run_gh", fake):
        out = await tool.ainvoke({"repo": "o/n", "title": "Bug", "body": "It broke"})
    assert out == "https://github.com/o/n/issues/42"
    args = fake.call_args.args[0]
    assert args[:4] == ["issue", "create", "--repo", "o/n"]
    assert "--title" in args and args[args.index("--title") + 1] == "Bug"
    assert "--body" in args and args[args.index("--body") + 1] == "It broke"


async def test_no_labels_omits_label_flag():
    """No labels → no --label flags at all."""
    tool = _create_issue()
    fake = AsyncMock(return_value=(0, "https://github.com/o/n/issues/1", ""))
    with patch("ghplugin.write_tools.run_gh", fake):
        await tool.ainvoke({"repo": "o/n", "title": "t"})
    assert "--label" not in fake.call_args.args[0]


async def test_labels_split_into_separate_flags():
    """Comma-separated labels become one --label flag each, stripped; blanks dropped."""
    tool = _create_issue()
    fake = AsyncMock(return_value=(0, "https://github.com/o/n/issues/7", ""))
    with patch("ghplugin.write_tools.run_gh", fake):
        await tool.ainvoke({"repo": "o/n", "title": "t", "labels": "bug, enhancement ,"})
    assert _labels_in(fake.call_args.args[0]) == ["bug", "enhancement"]


async def test_bad_repo_short_circuits():
    """An invalid repo returns the bad_repo error and never shells out."""
    tool = _create_issue()
    fake = AsyncMock()
    with patch("ghplugin.write_tools.run_gh", fake):
        out = await tool.ainvoke({"repo": "not-a-repo", "title": "t"})
    assert out.startswith("Error:")
    assert "owner/name" in out
    fake.assert_not_called()


async def test_gh_failure_returns_check_gh_error():
    """A nonzero gh exit becomes a readable Error string (via check_gh_error)."""
    tool = _create_issue()
    fake = AsyncMock(return_value=(1, "", "could not create issue: forbidden"))
    with patch("ghplugin.write_tools.run_gh", fake):
        out = await tool.ainvoke({"repo": "o/n", "title": "t"})
    assert out == "Error (gh exit 1): could not create issue: forbidden"


def _comment():
    return {t.name: t for t in get_write_tools()}["github_comment"]


async def test_comment_returns_comment_url():
    """On success the tool returns the new comment URL (gh's stdout, stripped)."""
    tool = _comment()
    fake = AsyncMock(return_value=(0, "https://github.com/o/n/issues/42#issuecomment-1\n", ""))
    with patch("ghplugin.write_tools.run_gh", fake):
        out = await tool.ainvoke({"repo": "o/n", "number": 42, "body": "Thanks!"})
    assert out == "https://github.com/o/n/issues/42#issuecomment-1"
    args = fake.call_args.args[0]
    assert args[:5] == ["issue", "comment", "42", "--repo", "o/n"]
    assert "--body" in args and args[args.index("--body") + 1] == "Thanks!"


async def test_comment_number_is_stringified():
    """The integer number is passed to gh as a string argument."""
    tool = _comment()
    fake = AsyncMock(return_value=(0, "https://github.com/o/n/issues/7#issuecomment-9", ""))
    with patch("ghplugin.write_tools.run_gh", fake):
        await tool.ainvoke({"repo": "o/n", "number": 7, "body": "hi"})
    args = fake.call_args.args[0]
    assert args[2] == "7"


async def test_comment_bad_repo_short_circuits():
    """An invalid repo returns the bad_repo error and never shells out."""
    tool = _comment()
    fake = AsyncMock()
    with patch("ghplugin.write_tools.run_gh", fake):
        out = await tool.ainvoke({"repo": "not-a-repo", "number": 1, "body": "hi"})
    assert out.startswith("Error:")
    assert "owner/name" in out
    fake.assert_not_called()


async def test_comment_gh_failure_returns_check_gh_error():
    """A nonzero gh exit becomes a readable Error string (via check_gh_error)."""
    tool = _comment()
    fake = AsyncMock(return_value=(1, "", "could not comment: forbidden"))
    with patch("ghplugin.write_tools.run_gh", fake):
        out = await tool.ainvoke({"repo": "o/n", "number": 1, "body": "hi"})
    assert out == "Error (gh exit 1): could not comment: forbidden"


def _create_pr():
    return {t.name: t for t in get_write_tools()}["github_create_pr"]


async def test_create_pr_returns_pr_url():
    """On success the tool returns the new PR URL (gh's stdout, stripped)."""
    tool = _create_pr()
    fake = AsyncMock(return_value=(0, "https://github.com/o/n/pull/5\n", ""))
    with patch("ghplugin.write_tools.run_gh", fake):
        out = await tool.ainvoke({"repo": "o/n", "head": "feature", "title": "Add thing", "body": "Does the thing"})
    assert out == "https://github.com/o/n/pull/5"
    args = fake.call_args.args[0]
    assert args[:4] == ["pr", "create", "--repo", "o/n"]
    assert "--head" in args and args[args.index("--head") + 1] == "feature"
    assert "--title" in args and args[args.index("--title") + 1] == "Add thing"
    assert "--body" in args and args[args.index("--body") + 1] == "Does the thing"


async def test_create_pr_base_defaults_to_main():
    """The base flag is always passed and defaults to ``main`` when omitted."""
    tool = _create_pr()
    fake = AsyncMock(return_value=(0, "https://github.com/o/n/pull/6", ""))
    with patch("ghplugin.write_tools.run_gh", fake):
        await tool.ainvoke({"repo": "o/n", "head": "feature", "title": "t"})
    args = fake.call_args.args[0]
    assert "--base" in args and args[args.index("--base") + 1] == "main"


async def test_create_pr_base_override_is_passed():
    """An explicit base branch is passed through to gh."""
    tool = _create_pr()
    fake = AsyncMock(return_value=(0, "https://github.com/o/n/pull/7", ""))
    with patch("ghplugin.write_tools.run_gh", fake):
        await tool.ainvoke({"repo": "o/n", "head": "feature", "title": "t", "base": "develop"})
    args = fake.call_args.args[0]
    assert "--base" in args and args[args.index("--base") + 1] == "develop"


async def test_create_pr_bad_repo_short_circuits():
    """An invalid repo returns the bad_repo error and never shells out."""
    tool = _create_pr()
    fake = AsyncMock()
    with patch("ghplugin.write_tools.run_gh", fake):
        out = await tool.ainvoke({"repo": "not-a-repo", "head": "feature", "title": "t"})
    assert out.startswith("Error:")
    assert "owner/name" in out
    fake.assert_not_called()


async def test_create_pr_gh_failure_returns_check_gh_error():
    """A nonzero gh exit becomes a readable Error string (via check_gh_error)."""
    tool = _create_pr()
    fake = AsyncMock(return_value=(1, "", "could not create pr: forbidden"))
    with patch("ghplugin.write_tools.run_gh", fake):
        out = await tool.ainvoke({"repo": "o/n", "head": "feature", "title": "t"})
    assert out == "Error (gh exit 1): could not create pr: forbidden"


def _by_name(name: str):
    return {t.name: t for t in get_write_tools()}[name]


# --- github_edit_pr ----------------------------------------------------------


async def test_edit_pr_title_and_body_via_pr_edit():
    """Title/body changes go through `gh pr edit` and return the PR URL."""
    tool = _by_name("github_edit_pr")
    fake = AsyncMock(return_value=(0, "https://github.com/o/n/pull/3\n", ""))
    with patch("ghplugin.write_tools.run_gh", fake):
        out = await tool.ainvoke({"repo": "o/n", "number": 3, "title": "New", "body": "Updated"})
    assert out == "https://github.com/o/n/pull/3"
    args = fake.call_args.args[0]
    assert args[:5] == ["pr", "edit", "3", "--repo", "o/n"]
    assert args[args.index("--title") + 1] == "New"
    assert args[args.index("--body") + 1] == "Updated"


async def test_edit_pr_state_ready_and_draft():
    """state=ready → `gh pr ready`; state=draft → `gh pr ready --undo`."""
    tool = _by_name("github_edit_pr")
    fake = AsyncMock(return_value=(0, "", ""))
    with patch("ghplugin.write_tools.run_gh", fake):
        await tool.ainvoke({"repo": "o/n", "number": 4, "state": "ready"})
    assert fake.call_args.args[0] == ["pr", "ready", "4", "--repo", "o/n"]
    with patch("ghplugin.write_tools.run_gh", fake):
        await tool.ainvoke({"repo": "o/n", "number": 4, "state": "draft"})
    assert fake.call_args.args[0] == ["pr", "ready", "4", "--repo", "o/n", "--undo"]


async def test_edit_pr_requires_a_change_and_validates_state():
    tool = _by_name("github_edit_pr")
    fake = AsyncMock()
    with patch("ghplugin.write_tools.run_gh", fake):
        assert (await tool.ainvoke({"repo": "o/n", "number": 1})).startswith("Error: nothing to change")
        assert "state must be" in await tool.ainvoke({"repo": "o/n", "number": 1, "state": "merged"})
    fake.assert_not_called()


# --- github_merge_pr (guarded) ----------------------------------------------


async def test_merge_pr_refuses_without_confirm():
    """The default call refuses and never shells out — merging is irreversible."""
    tool = _by_name("github_merge_pr")
    fake = AsyncMock()
    with patch("ghplugin.write_tools.run_gh", fake):
        out = await tool.ainvoke({"repo": "o/n", "number": 9})
    assert "confirm=true" in out and "Refusing" in out
    fake.assert_not_called()


async def test_merge_pr_dry_run_previews_without_merging():
    tool = _by_name("github_merge_pr")
    fake = AsyncMock()
    with patch("ghplugin.write_tools.run_gh", fake):
        out = await tool.ainvoke({"repo": "o/n", "number": 9, "dry_run": True})
    assert out.startswith("DRY RUN") and "via squash" in out
    fake.assert_not_called()


async def test_merge_pr_confirm_runs_gh_merge():
    tool = _by_name("github_merge_pr")
    fake = AsyncMock(return_value=(0, "Merged", ""))
    with patch("ghplugin.write_tools.run_gh", fake):
        await tool.ainvoke({"repo": "o/n", "number": 9, "method": "rebase", "delete_branch": True, "confirm": True})
    args = fake.call_args.args[0]
    assert args == ["pr", "merge", "9", "--repo", "o/n", "--rebase", "--delete-branch"]


async def test_merge_pr_validates_method():
    tool = _by_name("github_merge_pr")
    fake = AsyncMock()
    with patch("ghplugin.write_tools.run_gh", fake):
        out = await tool.ainvoke({"repo": "o/n", "number": 9, "method": "fast-forward", "confirm": True})
    assert "method must be" in out
    fake.assert_not_called()


# --- github_close / reopen ---------------------------------------------------


async def test_close_issue_with_comment():
    tool = _by_name("github_close")
    fake = AsyncMock(return_value=(0, "", ""))
    with patch("ghplugin.write_tools.run_gh", fake):
        await tool.ainvoke({"repo": "o/n", "number": 5, "comment": "dupe"})
    args = fake.call_args.args[0]
    assert args[:5] == ["issue", "close", "5", "--repo", "o/n"]
    assert args[args.index("--comment") + 1] == "dupe"


async def test_close_pr_uses_pr_subcommand():
    tool = _by_name("github_close")
    fake = AsyncMock(return_value=(0, "", ""))
    with patch("ghplugin.write_tools.run_gh", fake):
        await tool.ainvoke({"repo": "o/n", "number": 5, "kind": "pr"})
    assert fake.call_args.args[0][:2] == ["pr", "close"]


async def test_reopen_ignores_comment_and_uses_reopen():
    tool = _by_name("github_close")
    fake = AsyncMock(return_value=(0, "", ""))
    with patch("ghplugin.write_tools.run_gh", fake):
        await tool.ainvoke({"repo": "o/n", "number": 5, "reopen": True, "comment": "ignored"})
    args = fake.call_args.args[0]
    assert args[:2] == ["issue", "reopen"]
    assert "--comment" not in args


async def test_close_validates_kind():
    tool = _by_name("github_close")
    fake = AsyncMock()
    with patch("ghplugin.write_tools.run_gh", fake):
        out = await tool.ainvoke({"repo": "o/n", "number": 5, "kind": "discussion"})
    assert "kind must be" in out
    fake.assert_not_called()


# --- github_set_labels / github_set_assignees -------------------------------


async def test_set_labels_add_and_remove():
    tool = _by_name("github_set_labels")
    fake = AsyncMock(return_value=(0, "", ""))
    with patch("ghplugin.write_tools.run_gh", fake):
        await tool.ainvoke({"repo": "o/n", "number": 2, "add": "bug, p0", "remove": "stale"})
    args = fake.call_args.args[0]
    assert args[:5] == ["issue", "edit", "2", "--repo", "o/n"]
    assert [args[i + 1] for i, a in enumerate(args) if a == "--add-label"] == ["bug", "p0"]
    assert [args[i + 1] for i, a in enumerate(args) if a == "--remove-label"] == ["stale"]


async def test_set_labels_requires_at_least_one():
    tool = _by_name("github_set_labels")
    fake = AsyncMock()
    with patch("ghplugin.write_tools.run_gh", fake):
        out = await tool.ainvoke({"repo": "o/n", "number": 2})
    assert out.startswith("Error:") and "label" in out
    fake.assert_not_called()


async def test_set_assignees_pr_kind_add_and_remove():
    tool = _by_name("github_set_assignees")
    fake = AsyncMock(return_value=(0, "", ""))
    with patch("ghplugin.write_tools.run_gh", fake):
        await tool.ainvoke({"repo": "o/n", "number": 8, "add": "alice,bob", "remove": "carol", "kind": "pr"})
    args = fake.call_args.args[0]
    assert args[:5] == ["pr", "edit", "8", "--repo", "o/n"]
    assert [args[i + 1] for i, a in enumerate(args) if a == "--add-assignee"] == ["alice", "bob"]
    assert [args[i + 1] for i, a in enumerate(args) if a == "--remove-assignee"] == ["carol"]


async def test_new_write_tools_bad_repo_short_circuits():
    """Every new write tool validates the repo before shelling out."""
    fake = AsyncMock()
    cases = [
        ("github_edit_pr", {"repo": "x", "number": 1, "title": "t"}),
        ("github_merge_pr", {"repo": "x", "number": 1, "confirm": True}),
        ("github_close", {"repo": "x", "number": 1}),
        ("github_set_labels", {"repo": "x", "number": 1, "add": "bug"}),
        ("github_set_assignees", {"repo": "x", "number": 1, "add": "alice"}),
    ]
    for name, kwargs in cases:
        with patch("ghplugin.write_tools.run_gh", fake):
            out = await _by_name(name).ainvoke(kwargs)
        assert out.startswith("Error:") and "owner/name" in out, name
    fake.assert_not_called()
