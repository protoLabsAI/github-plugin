"""The GitHub board view — data functions, the gated data routes, and register wiring.

The data logic (`fetch_issues`/`fetch_prs`) is tested directly with `run_gh` mocked;
the routes are tested through FastAPI's TestClient (mounted under their real prefixes,
the host-free terminal-plugin pattern). register() wires two routers when the host
exposes register_router, and the page references the right gated endpoints.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from ghplugin import register
from ghplugin.api import build_data_router, build_view_router, fetch_issues, fetch_prs, gh_available

_CFG = {"repos": ["o/n", "o/m"], "default_repo": "o/n"}


def _app(cfg=None):
    app = FastAPI()
    app.include_router(build_view_router(), prefix="/plugins/github")
    app.include_router(build_data_router(cfg or _CFG), prefix="/api/plugins/github")
    return app


# --- data functions ----------------------------------------------------------


async def test_fetch_issues_builds_argv_and_parses():
    fake = AsyncMock(return_value=(0, '[{"number":1,"title":"Bug","state":"OPEN"}]', ""))
    with patch("ghplugin.api.run_gh", fake):
        out = await fetch_issues("o/n", state="open")
    assert out == {"items": [{"number": 1, "title": "Bug", "state": "OPEN"}]}
    args = fake.call_args.args[0]
    assert args[:5] == ["issue", "list", "--repo", "o/n", "--state"]
    assert "--json" in args  # asks for the structured row


async def test_fetch_issues_bad_repo_and_bad_state():
    fake = AsyncMock()
    with patch("ghplugin.api.run_gh", fake):
        assert "owner/name" in (await fetch_issues("nope"))["error"]
        assert "state must be" in (await fetch_issues("o/n", state="weird"))["error"]
    fake.assert_not_called()


async def test_fetch_issues_gh_failure():
    fake = AsyncMock(return_value=(1, "", "not found"))
    with patch("ghplugin.api.run_gh", fake):
        out = await fetch_issues("o/n")
    assert out["error"].startswith("Error (gh exit 1)")


async def test_fetch_prs_builds_argv_and_parses():
    fake = AsyncMock(return_value=(0, '[{"number":5,"title":"PR","isDraft":true}]', ""))
    with patch("ghplugin.api.run_gh", fake):
        out = await fetch_prs("o/n", state="open")
    assert out["items"][0]["number"] == 5
    assert fake.call_args.args[0][:2] == ["pr", "list"]


def test_gh_available_is_bool():
    assert isinstance(gh_available(), bool)


# --- routes (TestClient) -----------------------------------------------------


def test_view_page_served():
    c = TestClient(_app())
    r = c.get("/plugins/github/view")
    assert r.status_code == 200 and "GitHub" in r.text


def test_new_issue_page_served():
    r = TestClient(_app()).get("/plugins/github/new-issue")
    assert r.status_code == 200 and "New issue" in r.text


def test_board_page_is_read_only():
    """The board is a viewer — it must NOT POST to /issue (filing lives in the widget/palette)."""
    from ghplugin.view import PAGE

    # The board must not POST to the create-issue endpoint. Match it exactly with its
    # closing quote, since "/api/plugins/github/issue" is a substring of ".../issues".
    assert '/api/plugins/github/issue"' not in PAGE  # no create-issue from the board
    assert "/api/plugins/github/issues" in PAGE and "/api/plugins/github/prs" in PAGE  # reads only


def test_config_route_returns_repos_and_default():
    c = TestClient(_app())
    body = c.get("/api/plugins/github/config").json()
    assert body["repos"] == ["o/n", "o/m"]
    assert body["default_repo"] == "o/n"
    assert isinstance(body["gh_available"], bool)


def test_config_folds_default_repo_into_picker_when_repos_empty():
    """default_repo set + repos empty (a common config) must still give the picker a
    selectable option — otherwise the board shows "nothing to select" though a repo is
    configured."""
    c = TestClient(_app({"repos": [], "default_repo": "o/solo"}))
    body = c.get("/api/plugins/github/config").json()
    assert body["repos"] == ["o/solo"]  # default surfaced as the selectable option
    assert body["default_repo"] == "o/solo"


def test_config_does_not_duplicate_default_already_in_repos():
    c = TestClient(_app({"repos": ["o/a", "o/b"], "default_repo": "o/b"}))
    body = c.get("/api/plugins/github/config").json()
    assert body["repos"] == ["o/a", "o/b"]  # no dup; order preserved
    assert body["default_repo"] == "o/b"


def test_issues_route_proxies_fetch():
    fake = AsyncMock(return_value=(0, '[{"number":3,"title":"X"}]', ""))
    with patch("ghplugin.api.run_gh", fake):
        body = TestClient(_app()).get("/api/plugins/github/issues", params={"repo": "o/n"}).json()
    assert body["items"][0]["number"] == 3


def test_prs_route_proxies_fetch():
    fake = AsyncMock(return_value=(0, "[]", ""))
    with patch("ghplugin.api.run_gh", fake):
        body = TestClient(_app()).get("/api/plugins/github/prs", params={"repo": "o/n", "state": "all"}).json()
    assert body["items"] == []


def test_create_issue_route_uses_gate_and_default_repo():
    """POST /issue filing goes through file_issue (gate + gh) and the configured repo."""
    fake = AsyncMock(return_value=(0, "https://github.com/o/n/issues/7", ""))
    # A bug must clear the gate: a substantive body + a Problem AND a repro/steps section.
    good_body = (
        "## Problem\nThe widget crashes on empty input and we should handle it gracefully "
        "throughout the pipeline.\n## Steps to reproduce\nCall it with an empty string."
    )
    with patch("ghplugin.gh_issue.run_gh", fake):
        body = (
            TestClient(_app())
            .post("/api/plugins/github/issue", json={"title": "Crash on empty", "kind": "bug", "body": good_body})
            .json()
        )
    assert body["ok"] and body["url"].endswith("/issues/7")
    assert fake.call_args.args[0][:4] == ["issue", "create", "--repo", "o/n"]  # default repo used


def test_create_issue_route_requires_title():
    body = TestClient(_app()).post("/api/plugins/github/issue", json={"title": ""}).json()
    assert body["ok"] is False and "Title" in body["error"]


# --- register wiring ---------------------------------------------------------


def test_register_wires_both_routers(make_registry):
    reg = make_registry(_CFG)
    register(reg)
    assert "/plugins/github" in reg.router_prefixes  # public PAGE
    assert "/api/plugins/github" in reg.router_prefixes  # gated DATA
    assert "github_get_pr" in reg.tool_names  # tools still load


def test_legacy_host_without_register_router_still_loads_tools(make_legacy_registry):
    reg = make_legacy_registry({})
    register(reg)  # must not raise — no register_router on this host
    assert "github_get_pr" in reg.tool_names
    assert not hasattr(reg, "routers")


def test_page_references_gated_endpoints():
    """The pages fetch their DATA from the gated /api/plugins/github routes."""
    from ghplugin.view import NEW_ISSUE_PAGE, PAGE

    assert "/api/plugins/github/config" in PAGE
    assert "/api/plugins/github/issues" in PAGE
    assert "/api/plugins/github/prs" in PAGE
    assert "/_ds/plugin-kit" in PAGE  # themed via the DS kit
    # The new-issue page posts to the gated create route + is kit-themed.
    assert "/api/plugins/github/issue" in NEW_ISSUE_PAGE
    assert "/_ds/plugin-kit" in NEW_ISSUE_PAGE


def test_manifest_declares_board_and_widget_views():
    """Two views: the read-only board (right dock + ⌘K) and the file-an-issue widget
    (util-bar pill + ⌘K palette page)."""
    from pathlib import Path

    import yaml

    root = Path(__file__).resolve().parent.parent
    views = {v["id"]: v for v in yaml.safe_load((root / "protoagent.plugin.yaml").read_text())["views"]}
    board, new_issue = views["github"], views["github-new-issue"]
    assert board["placement"] == "right" and board["palette"] == "inline"
    assert board["path"] == "/plugins/github/view"
    assert new_issue["utility"]["info"]  # a util-bar pill with hover info
    assert new_issue["palette"]["path"] == "/plugins/github/new-issue"  # distinct ⌘K page
    assert new_issue["path"] == "/plugins/github/new-issue"
