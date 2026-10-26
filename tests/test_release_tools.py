"""The release tools (v0.10.0) — dispatch, tag, notes, assets, and the pinned merge.

`run_gh` is patched where each tool looks it up, so these assert the exact `gh` argv and
the guards (confirm, tag never moved, version check, the asset-name trap) without
touching GitHub. asyncio_mode=auto runs the async tests directly.
"""

from __future__ import annotations

import base64
import json
import os
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from ghplugin import release_tools
from ghplugin.release_tools import get_release_read_tools, get_release_write_tools
from ghplugin.write_tools import get_write_tools

SHA = "0123456789abcdef0123456789abcdef01234567"
TAG_OBJ = "fedcba9876543210fedcba9876543210fedcba98"


def _tools():
    return {t.name: t for t in get_release_read_tools() + get_release_write_tools()}


def _seq(*results):
    """A run_gh fake answering each call in turn with (rc, stdout, stderr)."""
    return AsyncMock(side_effect=list(results))


def _not_found():
    return (1, "", "gh: Not Found (HTTP 404)")


def _pyproject(version: str):
    text = f'[project]\nname = "x"\nversion = "{version}"\n'
    return (0, json.dumps({"content": base64.b64encode(text.encode()).decode()}), "")


# ── github_create_tag ────────────────────────────────────────────────────────────────


async def test_create_tag_checks_the_version_then_makes_an_annotated_tag_and_ref():
    fake = _seq(_not_found(), _pyproject("1.4.0"), (0, json.dumps({"sha": TAG_OBJ}), ""), (0, "{}", ""))
    with patch("ghplugin.release_tools.run_gh", fake):
        out = await _tools()["github_create_tag"].ainvoke({"repo": "o/n", "tag": "v1.4.0", "sha": SHA, "confirm": True})
    assert out.startswith("Tagged v1.4.0 at 0123456789ab"), out
    calls = [c.args[0] for c in fake.call_args_list]
    assert calls[0] == ["api", "repos/o/n/git/ref/tags/v1.4.0"]
    assert calls[1] == ["api", f"repos/o/n/contents/pyproject.toml?ref={SHA}"]
    assert calls[2][:2] == ["api", "repos/o/n/git/tags"]
    assert "tag=v1.4.0" in calls[2] and f"object={SHA}" in calls[2] and "type=commit" in calls[2]
    assert "message=Release v1.4.0" in calls[2]
    assert calls[3] == ["api", "repos/o/n/git/refs", "-f", "ref=refs/tags/v1.4.0", "-f", f"sha={TAG_OBJ}"]


async def test_create_tag_never_moves_an_existing_tag():
    fake = _seq((0, json.dumps({"ref": "refs/tags/v1.4.0"}), ""))
    with patch("ghplugin.release_tools.run_gh", fake):
        out = await _tools()["github_create_tag"].ainvoke({"repo": "o/n", "tag": "v1.4.0", "sha": SHA, "confirm": True})
    assert "already exists" in out and "never moved" in out
    assert fake.call_count == 1


async def test_create_tag_refuses_a_commit_whose_version_is_not_the_tag():
    fake = _seq(_not_found(), _pyproject("1.3.9"))
    with patch("ghplugin.release_tools.run_gh", fake):
        out = await _tools()["github_create_tag"].ainvoke({"repo": "o/n", "tag": "v1.4.0", "sha": SHA, "confirm": True})
    assert "declares version 1.3.9, not 1.4.0" in out and "Not tagging" in out
    assert fake.call_count == 2  # nothing created


async def test_create_tag_without_confirm_reports_and_creates_nothing():
    fake = _seq(_not_found(), _pyproject("1.4.0"))
    with patch("ghplugin.release_tools.run_gh", fake):
        out = await _tools()["github_create_tag"].ainvoke({"repo": "o/n", "tag": "v1.4.0", "sha": SHA})
    assert out.startswith("Refusing to tag v1.4.0") and "pyproject.toml at that commit says 1.4.0" in out
    assert fake.call_count == 2


async def test_create_tag_wants_a_full_sha_and_a_semver_tag():
    t = _tools()["github_create_tag"]
    with patch("ghplugin.release_tools.run_gh", AsyncMock()) as fake:
        assert "full 40-character" in await t.ainvoke(
            {"repo": "o/n", "tag": "v1.4.0", "sha": "0123abc", "confirm": True}
        )
        assert "not a release tag" in await t.ainvoke({"repo": "o/n", "tag": "latest", "sha": SHA, "confirm": True})
    fake.assert_not_called()


async def test_create_tag_skips_the_version_check_when_there_is_no_pyproject():
    fake = _seq(_not_found(), _not_found(), (0, json.dumps({"sha": TAG_OBJ}), ""), (0, "{}", ""))
    with patch("ghplugin.release_tools.run_gh", fake):
        out = await _tools()["github_create_tag"].ainvoke({"repo": "o/n", "tag": "v2.0.0", "sha": SHA, "confirm": True})
    assert out.startswith("Tagged v2.0.0") and "version not checked" in out


# ── github_dispatch_workflow ─────────────────────────────────────────────────────────


async def test_dispatch_runs_the_workflow_with_inputs_and_finds_its_run():
    now = datetime.now(timezone.utc).isoformat()
    runs = {"workflow_runs": [{"id": 99, "html_url": "https://x/99", "created_at": now, "head_branch": "main"}]}
    fake = _seq((0, "", ""), (0, json.dumps(runs), ""))
    with patch("ghplugin.release_tools.run_gh", fake):
        out = await _tools()["github_dispatch_workflow"].ainvoke(
            {"repo": "o/n", "workflow": "prepare-release.yml", "inputs": "bump=minor, dry_run=true", "confirm": True}
        )
    assert "run 99" in out and "https://x/99" in out
    argv = fake.call_args_list[0].args[0]
    assert argv == [
        "workflow", "run", "prepare-release.yml", "--repo", "o/n", "--ref", "main",
        "-f", "bump=minor", "-f", "dry_run=true",
    ]  # fmt: skip


async def test_dispatch_ignores_an_older_run_of_the_same_workflow(monkeypatch):
    monkeypatch.setattr(release_tools, "_FIND_RUN_DELAY", 0)
    old = {"workflow_runs": [{"id": 1, "created_at": "2020-01-01T00:00:00Z", "head_branch": "main"}]}
    fake = AsyncMock(side_effect=[(0, "", "")] + [(0, json.dumps(old), "")] * release_tools._FIND_RUN_TRIES)
    with patch("ghplugin.release_tools.run_gh", fake):
        out = await _tools()["github_dispatch_workflow"].ainvoke(
            {"repo": "o/n", "workflow": "desktop-build.yml", "inputs": "tag=v1.4.0", "confirm": True}
        )
    assert "isn't listed yet" in out and "don't dispatch again" in out


async def test_dispatch_needs_confirm_and_well_formed_inputs():
    t = _tools()["github_dispatch_workflow"]
    with patch("ghplugin.release_tools.run_gh", AsyncMock()) as fake:
        assert (await t.ainvoke({"repo": "o/n", "workflow": "x.yml", "inputs": "bump=patch"})).startswith("Refusing")
        assert "key=value" in await t.ainvoke({"repo": "o/n", "workflow": "x.yml", "inputs": "patch", "confirm": True})
        assert "not a workflow" in await t.ainvoke({"repo": "o/n", "workflow": "x; rm", "confirm": True})
    fake.assert_not_called()


# ── notes and assets ─────────────────────────────────────────────────────────────────


async def test_upload_asset_publishes_under_exactly_the_given_name():
    seen = {}

    async def fake(args):
        path = args[3]
        seen["base"] = os.path.basename(path)
        seen["text"] = open(path, encoding="utf-8").read()
        seen["args"] = args
        return 0, "", ""

    with patch("ghplugin.release_tools.run_gh", fake):
        out = await _tools()["github_upload_release_asset"].ainvoke(
            {
                "repo": "o/n",
                "tag": "v1.4.0",
                "name": "release-notes.md",
                "content": "# v1.4.0\n",
                "clobber": True,
                "confirm": True,
            }
        )
    assert out.startswith("Uploaded release-notes.md")
    assert seen["base"] == "release-notes.md" and "#" not in seen["args"][3]  # never path#label
    assert seen["text"] == "# v1.4.0\n"
    assert seen["args"][:3] == ["release", "upload", "v1.4.0"] and seen["args"][-1] == "--clobber"


async def test_upload_asset_refuses_path_like_names_and_says_how_to_replace():
    t = _tools()["github_upload_release_asset"]
    base = {"repo": "o/n", "tag": "v1.4.0", "content": "x", "confirm": True}
    with patch("ghplugin.release_tools.run_gh", AsyncMock()) as fake:
        assert "plain file name" in await t.ainvoke({**base, "name": "../notes.md"})
        assert "plain file name" in await t.ainvoke({**base, "name": "a.md#label"})
    fake.assert_not_called()
    exists = AsyncMock(return_value=(1, "", "asset under the same name already exists: [release-notes.md]"))
    with patch("ghplugin.release_tools.run_gh", exists):
        assert "clobber=true" in await t.ainvoke({**base, "name": "release-notes.md"})


async def test_edit_release_writes_the_notes_file_and_refuses_blank_notes():
    seen = {}

    async def fake(args):
        seen["args"] = args
        seen["text"] = open(args[args.index("--notes-file") + 1], encoding="utf-8").read()
        return 0, "", ""

    t = _tools()["github_edit_release"]
    with patch("ghplugin.release_tools.run_gh", fake):
        out = await t.ainvoke({"repo": "o/n", "tag": "v1.4.0", "notes": "## Fixed\n- x", "confirm": True})
    assert out.startswith("Replaced the notes of v1.4.0")
    assert seen["args"][:5] == ["release", "edit", "v1.4.0", "--repo", "o/n"] and seen["text"] == "## Fixed\n- x"
    with patch("ghplugin.release_tools.run_gh", AsyncMock()) as never:
        assert "empty" in await t.ainvoke({"repo": "o/n", "tag": "v1.4.0", "notes": "  ", "confirm": True})
    never.assert_not_called()


async def test_release_shows_latest_assets_and_notes():
    rel = {
        "tag_name": "v1.4.0", "name": "v1.4.0", "draft": False, "prerelease": False, "body": "notes here",
        "assets": [{"name": "release-notes.md", "size": 10}, {"name": "latest.json", "size": 900}],
    }  # fmt: skip
    fake = _seq((0, json.dumps(rel), ""), (0, json.dumps({"tag_name": "v1.3.0"}), ""))
    with patch("ghplugin.release_tools.run_gh", fake):
        out = await _tools()["github_release"].ainvoke({"repo": "o/n", "tag": "v1.4.0"})
    assert "latest=no (Latest is v1.3.0)" in out
    assert "assets (2):" in out and "- latest.json (900 bytes)" in out and "notes here" in out


async def test_release_asset_reads_the_asset_text_by_id():
    rel = {"assets": [{"name": "release-notes.md", "size": 12, "id": 5}]}
    fake = _seq((0, json.dumps(rel), ""), (0, "# curated\n", ""))
    with patch("ghplugin.release_tools.run_gh", fake):
        out = await _tools()["github_release_asset"].ainvoke(
            {"repo": "o/n", "tag": "v1.4.0", "name": "release-notes.md"}
        )
    assert out == "# curated\n"
    assert fake.call_args_list[1].args[0] == [
        "api", "repos/o/n/releases/assets/5", "-H", "Accept: application/octet-stream",
    ]  # fmt: skip


async def test_run_jobs_lists_every_job():
    run = {
        "name": "Desktop Build",
        "event": "workflow_dispatch",
        "status": "completed",
        "conclusion": "success",
        "head_sha": SHA,
    }
    jobs = {"jobs": [{"name": "aarch64-apple-darwin", "status": "completed", "conclusion": "success"}]}
    fake = _seq((0, json.dumps(run), ""), (0, json.dumps(jobs), ""))
    with patch("ghplugin.release_tools.run_gh", fake):
        out = await _tools()["github_run_jobs"].ainvoke({"repo": "o/n", "run_id": 7})
    assert "status=completed conclusion=success" in out and "- aarch64-apple-darwin: completed / success" in out


# ── github_merge_pr pinned to a head ─────────────────────────────────────────────────


def _merge():
    return {t.name: t for t in get_write_tools()}["github_merge_pr"]


async def test_merge_with_sha_uses_the_pinned_rest_merge():
    fake = AsyncMock(return_value=(0, json.dumps({"merged": True, "sha": "m" * 40}), ""))
    with patch("ghplugin.write_tools.run_gh", fake):
        out = await _merge().ainvoke({"repo": "o/n", "number": 12, "sha": SHA, "confirm": True})
    assert "at head 0123456789ab" in out
    assert fake.call_args.args[0] == [
        "api", "-X", "PUT", "repos/o/n/pulls/12/merge", "-f", f"sha={SHA}", "-f", "merge_method=squash",
    ]  # fmt: skip


async def test_merge_with_sha_refuses_when_the_head_moved():
    fake = AsyncMock(return_value=(1, "", "gh: Head branch was modified. Review and try the merge again. (HTTP 409)"))
    with patch("ghplugin.write_tools.run_gh", fake):
        out = await _merge().ainvoke({"repo": "o/n", "number": 12, "sha": SHA, "confirm": True})
    assert out.startswith("Not merged") and "Re-verify the new head" in out


async def test_merge_sha_must_be_full_and_cannot_delete_the_branch():
    with patch("ghplugin.write_tools.run_gh", AsyncMock()) as fake:
        assert "full 40-character" in await _merge().ainvoke(
            {"repo": "o/n", "number": 1, "sha": "abc", "confirm": True}
        )
        assert "can't be combined" in await _merge().ainvoke(
            {"repo": "o/n", "number": 1, "sha": SHA, "delete_branch": True, "confirm": True}
        )
    fake.assert_not_called()
