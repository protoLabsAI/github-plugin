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
    assert result.startswith("Error: 'repo' must be")
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
