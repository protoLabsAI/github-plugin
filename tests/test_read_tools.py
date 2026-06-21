"""github_read_file over a mocked `gh` — formats raw content, errors readably, caps size.

Host-free: we mock ``ghplugin.read_tools.run_gh`` (the name bound in read_tools) so no
real `gh`/network is touched. The tool is a langchain ``@tool``; invoke via ``ainvoke``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from ghplugin.read_tools import get_read_tools


def _read_file_tool():
    for t in get_read_tools():
        if t.name == "github_read_file":
            return t
    raise AssertionError("github_read_file tool not found")


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
    assert result.startswith("Error: 'repo' must be")
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
