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
