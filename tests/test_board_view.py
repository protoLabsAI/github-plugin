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
        # "merged" is a PR state; `gh issue list --state merged` would fail with a bare gh error
        assert "state must be" in (await fetch_issues("o/n", state="merged"))["error"]
    fake.assert_not_called()


async def test_fetch_prs_still_accepts_merged():
    fake = AsyncMock(return_value=(0, "[]", ""))
    with patch("ghplugin.api.run_gh", fake):
        assert await fetch_prs("o/n", state="merged") == {"items": []}
    args = fake.call_args.args[0]
    assert args[args.index("--state") + 1] == "merged"


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


# --- bd-nm78.8: search, limit, and the opt-in CI rollup ----------------------


async def test_fetch_issues_and_prs_pass_search_as_one_argv_element():
    """r1 — the query reaches `gh … list --search <q>` as ONE argv element (never shell-joined),
    so a multi-word query with a leading qualifier stays a single argument."""
    fake = AsyncMock(return_value=(0, "[]", ""))
    with patch("ghplugin.api.run_gh", fake):
        await fetch_issues("o/n", search="is:open label:bug crash")
        iargs = fake.call_args.args[0]
        await fetch_prs("o/n", search="review:required status:success")
        pargs = fake.call_args.args[0]
    assert iargs.count("--search") == 1 and iargs[iargs.index("--search") + 1] == "is:open label:bug crash"
    assert pargs.count("--search") == 1 and pargs[pargs.index("--search") + 1] == "review:required status:success"


async def test_search_is_stripped_capped_and_omitted_when_blank():
    """r1 — the query is trimmed and capped at 256 chars; a blank/whitespace query adds no flag."""
    fake = AsyncMock(return_value=(0, "[]", ""))
    with patch("ghplugin.api.run_gh", fake):
        await fetch_prs("o/n", search="   " + "x" * 300 + "   ")
        capped_args = fake.call_args.args[0]
        await fetch_issues("o/n", search="   ")
        blank_args = fake.call_args.args[0]
    assert capped_args[capped_args.index("--search") + 1] == "x" * 256  # stripped then capped
    assert "--search" not in blank_args  # a blank query is no search at all


async def test_limit_is_clamped_1_to_100():
    fake = AsyncMock(return_value=(0, "[]", ""))
    with patch("ghplugin.api.run_gh", fake):
        await fetch_issues("o/n", limit=9999)
        assert fake.call_args.args[0][fake.call_args.args[0].index("--limit") + 1] == "100"
        await fetch_prs("o/n", limit=0)
        assert fake.call_args.args[0][fake.call_args.args[0].index("--limit") + 1] == "1"


async def test_fetch_prs_with_checks_adds_field_and_compacts_the_rollup():
    """r3 — with_checks=True asks for statusCheckRollup and replaces each raw rollup with a
    compact {state, fail, total}, classifying failing/pending/passing/none like the tool."""
    rows = [
        {"number": 1, "statusCheckRollup": [{"status": "COMPLETED", "conclusion": "SUCCESS"}]},
        {
            "number": 2,
            "statusCheckRollup": [
                {"status": "COMPLETED", "conclusion": "SUCCESS"},
                {"status": "COMPLETED", "conclusion": "FAILURE"},
                {"state": "PENDING"},  # StatusContext shape
            ],
        },
        {"number": 3, "statusCheckRollup": [{"status": "IN_PROGRESS", "conclusion": ""}]},
        {"number": 4, "statusCheckRollup": []},
    ]
    import json

    fake = AsyncMock(return_value=(0, json.dumps(rows), ""))
    with patch("ghplugin.api.run_gh", fake):
        out = await fetch_prs("o/n", with_checks=True)
    assert "statusCheckRollup" in fake.call_args.args[0][fake.call_args.args[0].index("--json") + 1]
    items = {it["number"]: it["checks"] for it in out["items"]}
    assert "statusCheckRollup" not in out["items"][0]  # raw rollup replaced, not kept
    assert items[1] == {"state": "passing", "fail": 0, "total": 1}
    assert items[2] == {"state": "failing", "fail": 1, "total": 3}  # any fail ⇒ failing
    assert items[3] == {"state": "pending", "fail": 0, "total": 1}  # running, nothing failed
    assert items[4] == {"state": "none", "fail": 0, "total": 0}  # no checks at all


async def test_fetch_prs_without_checks_keeps_argv_and_output_unchanged():
    """r3 — the default (github_list_prs) path never asks for statusCheckRollup and never adds a
    `checks` key, so the tool's argv and output are byte-for-byte what they were."""
    fake = AsyncMock(return_value=(0, '[{"number":5,"title":"PR","isDraft":true}]', ""))
    with patch("ghplugin.api.run_gh", fake):
        out = await fetch_prs("o/n")
    json_fields = fake.call_args.args[0][fake.call_args.args[0].index("--json") + 1]
    assert "statusCheckRollup" not in json_fields
    assert "--search" not in fake.call_args.args[0]
    assert out == {"items": [{"number": 5, "title": "PR", "isDraft": True}]}  # no `checks` key


def test_issues_route_passes_search_and_limit():
    """r1/r2 — the /issues route forwards search (single --search element) and the clamped limit."""
    fake = AsyncMock(return_value=(0, "[]", ""))
    with patch("ghplugin.api.run_gh", fake):
        TestClient(_app()).get(
            "/api/plugins/github/issues", params={"repo": "o/n", "search": "needle here", "limit": 200}
        )
    args = fake.call_args.args[0]
    assert args[args.index("--search") + 1] == "needle here"  # one argv element
    assert args[args.index("--limit") + 1] == "100"  # clamped


def test_prs_route_turns_on_checks_and_passes_search_and_limit():
    """r2/r3 — the /prs route asks for statusCheckRollup (the chip), forwards search + limit."""
    fake = AsyncMock(return_value=(0, "[]", ""))
    with patch("ghplugin.api.run_gh", fake):
        TestClient(_app()).get("/api/plugins/github/prs", params={"repo": "o/n", "search": "wip", "limit": 60})
    args = fake.call_args.args[0]
    assert "statusCheckRollup" in args[args.index("--json") + 1]
    assert args[args.index("--search") + 1] == "wip"
    assert args[args.index("--limit") + 1] == "60"


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


def test_pages_boot_once():
    """#13 regression — each page boots via the kit ONCE. A second direct ``boot();`` ran two
    overlapping config+load sequences → the list flicker/thrash on mount."""
    from ghplugin.view import NEW_ISSUE_PAGE, PAGE

    for page in (PAGE, NEW_ISSUE_PAGE):
        assert "kit.initPluginView(boot)" in page  # booted via the handshake callback
        assert "boot();" not in page  # …and NOT also called directly


def test_pages_boot_is_idempotent():
    """#15 — the kit's ``initPluginView`` callback fires on the initial init AND on every
    re-theme (plus the handshake re-send), so ``boot`` must guard itself to run exactly once.
    'Boot once' at the call site (#13) wasn't enough — the single callback fires repeatedly,
    each run rebuilding the picker + re-loading → the list flicker/thrash returned."""
    from ghplugin.view import NEW_ISSUE_PAGE, PAGE

    for page in (PAGE, NEW_ISSUE_PAGE):
        assert "if (booted) return;" in page  # the callback is guarded, not just called once


def test_board_renders_comment_count_not_the_array():
    """#16 — ``gh issue list --json comments`` returns an ARRAY of comment objects; the board
    must render its COUNT (``.length``), not ``String(array)`` which shows '[object Object]'."""
    from ghplugin.view import PAGE

    assert "it.comments.length" in PAGE  # the count, not the raw array
    assert "esc(it.comments)" not in PAGE  # the old object-stringifying render is gone
    assert "💬" not in PAGE  # emoji swapped for a Lucide icon
    assert '<svg class="ico"' in PAGE  # inline Lucide icon markup present


def test_board_heading_uses_icon_actions():
    """#16 follow-up + heading tightening — the refresh action is a Lucide icon (not the raw
    glyph) and the pull-request tab is the compact 'PRs'."""
    from ghplugin.view import PAGE

    assert "↻" not in PAGE  # raw refresh glyph replaced by the Lucide refresh-cw icon
    assert "ICON.refresh" in PAGE  # …set from the shared icon set
    assert ">PRs<" in PAGE  # tightened tab label


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


def test_data_router_reads_config_live_from_a_getter():
    """Given a config GETTER (callable), /config reflects edits per request — so a saved
    repo shows in the board without a server restart (the mounted router can't re-mount)."""
    from fastapi import FastAPI

    live = {"repos": [], "default_repo": ""}
    app = FastAPI()
    app.include_router(build_data_router(lambda: live), prefix="/api/plugins/github")
    c = TestClient(app)

    assert c.get("/api/plugins/github/config").json()["repos"] == []  # nothing yet
    live["repos"] = ["o/added"]  # operator saves a repo (config reloaded under us)
    assert c.get("/api/plugins/github/config").json()["repos"] == ["o/added"]  # no restart


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
    """The existing dock/palette board and file-an-issue widget stay unchanged."""
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


# --- v0.6.0: /status, the named default_repo error (#23), the setup card -------


def _status_gh(*, auth=(0, '{"hosts":{}}', "You are not logged into any GitHub hosts.")):
    async def run_gh(args, timeout=None, **kw):
        if args == ["--version"]:
            return (0, "gh version 2.92.0 (2026-04-28)", "")
        if args[:2] == ["auth", "status"]:
            return auth
        raise AssertionError(f"unexpected gh call: {args}")

    return run_gh


def test_status_route_shape_when_unauthenticated():
    with (
        patch("ghplugin.status.resolve_gh", return_value="/usr/local/bin/gh"),
        patch("ghplugin.status.run_gh", new=AsyncMock(side_effect=_status_gh())),
    ):
        body = TestClient(_app()).get("/api/plugins/github/status").json()
    assert body["gh_path"] == "/usr/local/bin/gh" and body["gh_version"] == "2.92.0"
    assert body["authenticated"] is False and body["login"] is None
    assert body["error"] == "not logged in"
    assert body["default_repo"] == "o/n" and body["repos"] == ["o/n", "o/m"]
    assert body["default_repo_error"] is None
    for k in ("host", "token_source"):
        assert k in body


def test_status_route_when_gh_is_missing_never_errors():
    with patch("ghplugin.status.resolve_gh", return_value=None):
        r = TestClient(_app()).get("/api/plugins/github/status")
    assert r.status_code == 200
    body = r.json()
    assert body["gh_path"] is None and body["authenticated"] is False and "not installed" in body["error"]


def test_status_route_authenticated():
    ok = '{"hosts":{"github.com":[{"state":"success","active":true,"host":"github.com","login":"kj"}]}}'
    with (
        patch("ghplugin.status.resolve_gh", return_value="/usr/bin/gh"),
        patch("ghplugin.status.run_gh", new=AsyncMock(side_effect=_status_gh(auth=(0, ok, "")))),
    ):
        body = TestClient(_app()).get("/api/plugins/github/status").json()
    assert body["authenticated"] is True and body["login"] == "kj" and body["host"] == "github.com"


def test_config_names_a_malformed_default_repo_and_keeps_it_out_of_the_picker():
    """#23 — `default_repo: protoLabsAI` (no slash) used to be fed straight to `gh`. Now it's
    the named error, the picker carries only well-formed entries, and the default falls
    through to the first good one."""
    body = (
        TestClient(_app({"repos": ["o/good"], "default_repo": "protoLabsAI"})).get("/api/plugins/github/config").json()
    )
    assert body["default_repo_error"].startswith("Error: github.default_repo must be 'owner/name' (got 'protoLabsAI')")
    assert body["repos"] == ["o/good"] and body["default_repo"] == "o/good"


def test_config_default_repo_error_is_none_when_well_formed_or_blank():
    assert TestClient(_app()).get("/api/plugins/github/config").json()["default_repo_error"] is None
    assert (
        TestClient(_app({"repos": [], "default_repo": ""}))
        .get("/api/plugins/github/config")
        .json()["default_repo_error"]
        is None
    )


def test_status_route_carries_the_default_repo_error_too():
    with patch("ghplugin.status.resolve_gh", return_value=None):
        body = TestClient(_app({"repos": [], "default_repo": "nope"})).get("/api/plugins/github/status").json()
    assert "must be 'owner/name'" in body["default_repo_error"]


def test_create_issue_route_names_a_malformed_default_when_nothing_else_resolves():
    fake = AsyncMock()
    with patch("ghplugin.gh_issue.run_gh", fake):
        body = (
            TestClient(_app({"repos": [], "default_repo": "just-owner"}))
            .post("/api/plugins/github/issue", json={"title": "T", "body": "x"})
            .json()
        )
    assert body["ok"] is False and body["error"].startswith(
        "Error: github.default_repo must be 'owner/name' (got 'just-owner')"
    )
    fake.assert_not_called()


def test_pages_render_the_setup_card_from_status_inside_the_kit_boot():
    """Both views fetch /status ONLY from inside the kit's boot (post-handshake, #2926) and
    carry the setup card with the three remedies: install gh / gh auth login / the token."""
    from ghplugin.view import NEW_ISSUE_PAGE, PAGE

    for page in (PAGE, NEW_ISSUE_PAGE):
        assert "/api/plugins/github/status" in page
        assert 'id="setup"' in page and "function setupCard" in page
        # the fetch is reached from boot(), which the kit invokes — not at top level
        boot_body = page.split("async function boot(){", 1)[1].split("\n  }", 1)[0]
        assert "checkStatus();" in boot_body
        assert "cli.github.com" in page and "gh auth login" in page and "github.token" in page
        assert "Default repo is malformed" in page  # #23 surfaces in the views too
        assert "No repositories configured" in page  # the existing empty state stays


def test_register_wires_the_data_router_to_the_live_config(make_registry):
    """register() passes a getter (not the snapshot) so the routes see config edits."""
    reg = make_registry({"repos": ["o/n"], "default_repo": "o/n"})
    register(reg)
    data = next(r["router"] for r in reg.routers if r["prefix"] == "/api/plugins/github")
    app = FastAPI()
    app.include_router(data, prefix="/api/plugins/github")
    c = TestClient(app)
    assert c.get("/api/plugins/github/config").json()["repos"] == ["o/n"]
    reg.config["repos"] = ["o/n", "o/added"]  # the (shared) config dict is edited after register()
    assert c.get("/api/plugins/github/config").json()["repos"] == ["o/n", "o/added"]


class _SeamRegistry:
    """A host with the setup-gap seam — records every (key, message) it's handed."""

    def __init__(self):
        self.calls: list[tuple[str, str | None]] = []

    def report_setup_gap(self, key, message):
        self.calls.append((key, message))


def test_status_route_reports_gaps_and_clears_them_on_recovery():
    """The banner must clear on the very Re-check that sees gh installed / signed in —
    not only at register time (#2 of the adversarial review)."""
    seam = _SeamRegistry()
    app = FastAPI()
    app.include_router(build_data_router(_CFG, registry=seam), prefix="/api/plugins/github")
    c = TestClient(app)

    with patch("ghplugin.status.resolve_gh", return_value=None):
        assert c.get("/api/plugins/github/status").json()["gh_path"] is None
    assert dict(seam.calls)["gh"] and "not installed" in dict(seam.calls)["gh"]
    assert dict(seam.calls)["auth"] is None

    seam.calls.clear()
    ok = '{"hosts":{"github.com":[{"state":"success","active":true,"host":"github.com","login":"kj"}]}}'
    with (
        patch("ghplugin.status.resolve_gh", return_value="/usr/bin/gh"),
        patch("ghplugin.status.run_gh", new=AsyncMock(side_effect=_status_gh(auth=(0, ok, "")))),
    ):
        assert c.get("/api/plugins/github/status").json()["authenticated"] is True
    assert dict(seam.calls) == {"gh": None, "auth": None}  # CLEARED, live


def test_status_route_without_a_registry_still_works():
    with patch("ghplugin.status.resolve_gh", return_value=None):
        assert TestClient(_app()).get("/api/plugins/github/status").status_code == 200


def test_register_hands_the_registry_to_the_data_router(make_registry):
    """register() passes the registry so /status can report; a host without the seam is fine."""
    seen = {}
    real = __import__("ghplugin.api", fromlist=["build_data_router"]).build_data_router

    def spy(cfg, registry=None):
        seen["registry"] = registry
        return real(cfg, registry=registry)

    reg = make_registry(_CFG)
    with patch("ghplugin.api.build_data_router", spy):
        register(reg)
    assert seen["registry"] is reg


def test_routes_resolve_the_picker_off_the_event_loop():
    """resolve_config may fork git (checkout remotes) — the routes must run it in a worker
    thread, never on the loop (#5 of the adversarial review)."""
    import threading

    from ghplugin import api

    threads = []
    real = api.resolve_config

    def spy(cfg):
        threads.append(threading.current_thread())
        return real(cfg)

    with patch("ghplugin.api.resolve_config", spy), patch("ghplugin.status.resolve_gh", return_value=None):
        c = TestClient(_app())
        c.get("/api/plugins/github/config")
        c.get("/api/plugins/github/status")
        c.post("/api/plugins/github/issue", json={"title": ""})
    assert len(threads) == 3
    # TestClient runs the app loop on its own portal thread; to_thread hands off to a
    # default-executor worker, whose name is the giveaway.
    assert all(t.name.startswith("asyncio_") for t in threads), [t.name for t in threads]


def test_workspace_serves_same_page_and_is_public():
    from pathlib import Path

    import yaml
    from ghplugin.view import PAGE

    client = TestClient(_app())
    response = client.get("/plugins/github/workspace")
    assert response.status_code == 200
    assert response.text == client.get("/plugins/github/view").text == PAGE
    manifest = yaml.safe_load((Path(__file__).resolve().parent.parent / "protoagent.plugin.yaml").read_text())
    assert "/plugins/github/workspace" in manifest["public_paths"]
    workspace = next(v for v in manifest["views"] if v["id"] == "github-workspace")
    assert workspace == {
        "id": "github-workspace",
        "label": "GitHub",
        "icon": "Github",
        "path": "/plugins/github/workspace",
        "placement": "rail",
    }


def test_drawer_shell_and_plain_text_security_contract():
    from ghplugin.view import PAGE

    assert 'id="scrim"' in PAGE
    assert 'role="dialog" aria-modal="true" aria-labelledby="drawer-title"' in PAGE
    assert 'id="drawer-tabs" role="tablist"' in PAGE
    assert 'id="drawer-body" role="tabpanel"' in PAGE
    assert "width:min(460px,92vw)" in PAGE
    assert "@media(min-width:1000px){#drawer.wide{width:min(780px,96vw)}}" in PAGE
    assert ".pr-body{white-space:pre-wrap" in PAGE
    for field in ("headRefName", "baseRefName", "body"):
        assert f"esc(it.{field}" in PAGE
    assert "esc(data.title)" in PAGE
    assert "esc(data.error)" in PAGE
    for forbidden in ("marked", "markdown-it", "renderMarkdown", "innerHTML = data.body", "innerHTML = it.body"):
        assert forbidden not in PAGE


# A tiny DOM / fetch / timer harness so the shipped drawer JS runs with no host or network.
# `lastIntervalMs` records the most recent poll cadence (15s default, 30s for Files); `latest()`
# points at the newest fetch; `bodyClick` fires a delegated #drawer-body click whose target
# matches one attribute selector (the log / file toggles).
_DRAWER_HARNESS = r"""
const assert = require('node:assert/strict');
const elements = new Map(), events = {}, requests = [], timers = new Map();
let timerId = 0, lastIntervalMs = 0;
function element(id){
  if(!elements.has(id)) elements.set(id, {
    id, dataset: {}, handlers: {}, attrs: {}, writes: 0, scrollTop: 0, isConnected: true,
    classList: {add(){}, remove(){}},
    set innerHTML(v){this.html=v; this.writes++;}, get innerHTML(){return this.html||'';},
    setAttribute(k,v){this.attrs[k]=v;}, getAttribute(k){return this.attrs[k];},
    addEventListener(k,v){this.handlers[k]=v;}, focus(){document.activeElement=this;},
    querySelectorAll(){return Object.keys((drawer && drawer.tabs) ? drawer.tabs : DRAWER_TABS).map(t => {
      const el=element('dtab-'+t); el.attrs['data-drawer-tab']=t; return el;
    });}
  });
  return elements.get(id);
}
const document = {hidden: false, getElementById: element, addEventListener(k,v){events[k]=v;}};
const kit = {apiFetch(path){return new Promise((resolve,reject)=>requests.push({path,resolve,reject}));}};
const setInterval = (fn,ms) => {lastIntervalMs = ms; timers.set(++timerId,fn); return timerId;};
const clearInterval = id => timers.delete(id);
// Controllable debounce timers (search box): setTimeout never auto-fires — runTimeouts() does.
let timeoutId = 0; const timeouts = new Map();
const setTimeout = (fn) => {timeouts.set(++timeoutId,fn); return timeoutId;};
const clearTimeout = id => timeouts.delete(id);
const runTimeouts = () => {const fns = [...timeouts.values()]; timeouts.clear(); fns.forEach(fn => fn());};
const flush = async()=>{await Promise.resolve(); await Promise.resolve(); await Promise.resolve();};
const reply = async(index,data,ok=true)=>{requests[index].resolve({ok,json:async()=>data}); await flush();};
const latest = () => requests.length - 1;
const bodyClick = (attrs) => element('drawer-body').handlers.click({target:{closest:(sel)=>{
  for(const k in attrs){ if(sel.includes(k)) return {getAttribute:(n)=>attrs[n]}; }
  return null;
}}});
"""


def _run_drawer_js(checks):
    """Execute the shipped drawer JS (``_DRAWER_HARNESS`` + the page module + ``checks``).

    Node is optional locally; the static contract and route tests always run in the
    Python-only gate. When available this also checks races, event behavior, and the
    Checks/Files tab rendering."""
    import shutil
    import subprocess

    import pytest
    from ghplugin.view import PAGE

    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required for the JavaScript interaction harness")
    script = PAGE.split('<script type="module">', 1)[1].split("</script>", 1)[0]
    script = script[script.index("  const $ =") :].replace("  kit.initPluginView(boot);", "")
    result = subprocess.run(
        [node, "-"], input=_DRAWER_HARNESS + script + checks, text=True, capture_output=True, timeout=15
    )
    assert result.returncode == 0, result.stderr


def test_drawer_interactions_and_async_lifecycle():
    """Open/close, escaping, snapshot suppression, races, visibility, and arrow-key tab nav
    across all four tabs — Files polls slower (30s), the other tabs at 15s."""
    _run_drawer_js(
        r"""
(async()=>{
  const hostile = '<img src=x onerror="steal()">&';
  const data = {number:7,title:hostile,state:'OPEN',author:{login:hostile},
    headRefName:hostile,baseRefName:hostile,body:hostile,createdAt:'2026-01-01',updatedAt:'2026-01-02'};
  const row = element('pr-row'); row.dataset={repo:'o/n',pr:'7'};
  const click = (opts={})=>{
    let prevented=false;
    element('list').handlers.click({button:0,target:{closest:()=>row},preventDefault(){prevented=true;},...opts});
    return prevented;
  };
  for(const opts of [{ctrlKey:true},{metaKey:true},{shiftKey:true},{altKey:true},{button:1},{defaultPrevented:true}]){
    assert.equal(click(opts),false);
  }
  assert.equal(requests.length,0);
  element('list').handlers.click({button:0,target:{closest:()=>null},preventDefault(){assert.fail('issue intercepted');}});
  assert.equal(click(),true);
  assert.equal(requests[0].path,'/api/plugins/github/detail/pr?repo=o%2Fn&number=7');
  assert.equal(timers.size,1); assert.equal(lastIntervalMs,15000);
  assert.equal(element('drawer').hidden,false);
  await reply(0,data);
  assert.ok(element('drawer-title').innerHTML.includes('&lt;img'));
  assert.ok(element('drawer-body').innerHTML.includes('&lt;img'));
  assert.ok(!element('drawer-body').innerHTML.includes('<img'));
  assert.ok(element('drawer-meta').innerHTML.includes('https://github.com/o/n/pull/7'));
  assert.ok(element('drawer-meta').innerHTML.includes('open'));
  const writes = element('drawer-body').writes;
  pollDrawer(); await reply(1,data);
  assert.equal(element('drawer-body').writes,writes);
  pollDrawer(); pollDrawer();
  await reply(3,{...data,title:'New'}); await reply(2,{...data,title:'Old'});
  assert.equal(element('drawer-title').innerHTML,'#7 New');
  pollDrawer(); closeDrawer(); openDrawer('o/m','7',row);
  await reply(4,{...data,title:'Wrong repo'});
  assert.ok(!element('drawer-title').innerHTML.includes('Wrong repo'));
  await reply(5,{...data,state:'MERGED',isDraft:true});
  assert.ok(element('drawer-meta').innerHTML.includes('merged'));
  pollDrawer(); document.hidden=true; events.visibilitychange();
  assert.equal(timers.size,0);
  const count=requests.length; pollDrawer(); assert.equal(requests.length,count);
  await reply(6,{...data,title:'Hidden'});
  assert.ok(!element('drawer-title').innerHTML.includes('Hidden'));
  document.hidden=false; events.visibilitychange(); assert.equal(timers.size,1);
  await reply(7,{error:hostile}); assert.ok(element('drawer-body').innerHTML.includes('&lt;img'));
  pollDrawer(); await reply(8,data); assert.ok(element('drawer-body').innerHTML.includes('Author'));
  // All four tabs are keyboard-reachable; Files restarts the poller at 30s.
  assert.deepEqual(Object.keys(DRAWER_TABS),['overview','checks','files','conversation']);
  for(const [key,want,ms] of [['ArrowRight','checks',15000],['Home','overview',15000],
      ['End','conversation',15000],['ArrowRight','overview',15000],['ArrowLeft','conversation',15000],['ArrowLeft','files',30000]]){
    let prevented=false;
    element('drawer-tabs').handlers.keydown({key,preventDefault(){prevented=true;}});
    assert.equal(drawer.tab,want); assert.equal(prevented,true);
    assert.equal(document.activeElement.id,'dtab-'+want);
    assert.equal(lastIntervalMs,ms);
  }
  const pending=requests.length-1;
  events.keydown({key:'Escape',preventDefault(){}});
  assert.equal(drawer,null); assert.equal(timers.size,0); assert.equal(document.activeElement,row);
  await reply(pending,{title:'Late'});
  assert.equal(element('drawer').hidden,true);
  for(const id of ['drawer-close','scrim']){
    openDrawer('o/n','7',row); element(id).onclick();
    assert.equal(drawer,null); assert.equal(timers.size,0);
  }
  assert.ok(prRow({...data,url:'https://github.com/o/n/pull/7'},'o/n').includes('data-repo="o/n"'));
  assert.ok(prRow({...data,url:'https://github.com/o/n/pull/7'},'o/n').includes('target="_blank"'));
  assert.ok(!issueRow(data).includes('data-pr='));
})().catch(e=>{console.error(e);process.exitCode=1;});
"""
    )


def test_drawer_checks_tab_renders_summary_logs_and_badge():
    """Checks tab: summary, failing-first order, escaped names, a fail-count badge, and a
    lazily-loaded failure log in an escaped <pre> whose expanded state survives re-renders."""
    _run_drawer_js(
        r"""
(async()=>{
  const row = element('pr-row'); row.dataset={repo:'o/n',pr:'7'};
  openDrawer('o/n','7',row);
  await reply(latest(),{number:7,title:'PR',state:'OPEN',author:{login:'kj'},
    headRefName:'f',baseRefName:'main',body:'b',createdAt:'2026-01-01',updatedAt:'2026-01-02'});
  selectDrawerTab('checks',false);
  assert.equal(DRAWER_TABS.checks.path,'/api/plugins/github/detail/pr/checks');
  assert.ok(requests[latest()].path.startsWith('/api/plugins/github/detail/pr/checks?repo=o%2Fn&number=7'));
  const evil = '<b>evil</b>';
  const checks = {summary:{pass:1,fail:2,pending:0,skipped:1}, checks:[
    {name:'lint',workflow:'CI',status:'COMPLETED',conclusion:'SUCCESS',url:'https://x/1',
      started_at:'2026-01-01T00:00:00Z',completed_at:'2026-01-01T00:00:30Z',run_id:null},
    {name:evil,workflow:'CI',status:'COMPLETED',conclusion:'FAILURE',url:'https://x/2',
      started_at:'2026-01-01T00:00:00Z',completed_at:'2026-01-01T00:01:05Z',run_id:42},
    {name:'skip-step',workflow:null,status:'COMPLETED',conclusion:'SKIPPED',url:null,
      started_at:null,completed_at:null,run_id:null},
    {name:'deploy',workflow:'CD',status:'COMPLETED',conclusion:'TIMED_OUT',url:'https://x/3',
      started_at:null,completed_at:null,run_id:99},
  ]};
  await reply(latest(),checks);
  let html = element('drawer-body').innerHTML;
  assert.ok(html.includes('1</b> passing') && html.includes('2</b> failing'));
  assert.ok(html.includes('0</b> pending') && html.includes('1</b> skipped'));
  assert.ok(html.indexOf('evil') < html.indexOf('lint'));   // failing first
  assert.ok(html.indexOf('deploy') < html.indexOf('lint'));
  assert.ok(html.includes('&lt;b&gt;evil') && !html.includes('<b>evil'));  // check name escaped
  assert.ok(html.includes('dot fail') && html.includes('dot pass') && html.includes('dot skipped'));
  assert.ok(html.includes('1m 5s'));                         // duration
  assert.ok(element('drawer-tabs').innerHTML.includes('chk-badge">2<'));  // fail-count badge
  // Lazily fetch the failure log for run 42.
  const before = requests.length;
  bodyClick({'data-run-id':'42'});
  assert.equal(requests.length,before+1);
  assert.ok(requests[latest()].path.includes('run_id=42'));
  assert.ok(element('drawer-body').innerHTML.includes('Loading'));
  await reply(latest(),{log_tail:'ok\n<script>bad()</script>'});
  html = element('drawer-body').innerHTML;
  assert.ok(html.includes('<pre class="log">'));
  assert.ok(html.includes('&lt;script&gt;bad') && !html.includes('<script>bad'));  // log escaped
  // The expanded log survives a polling re-render (a changed snapshot).
  pollDrawer();
  await reply(latest(),{...checks,summary:{pass:1,fail:2,pending:1,skipped:1}});
  html = element('drawer-body').innerHTML;
  assert.ok(html.includes('<pre class="log">') && html.includes('&lt;script&gt;bad'));
  // Toggle off hides it; toggle on reuses the cached log — neither refetches.
  const reqs = requests.length;
  bodyClick({'data-run-id':'42'});
  assert.ok(!element('drawer-body').innerHTML.includes('<pre class="log">'));
  bodyClick({'data-run-id':'42'});
  assert.ok(element('drawer-body').innerHTML.includes('&lt;script&gt;bad'));
  assert.equal(requests.length,reqs);
})().catch(e=>{console.error(e);process.exitCode=1;});
"""
    )


def test_drawer_files_tab_renders_diffs_notes_and_polls_slowly():
    """Files tab: header, per-file collapsible diffs with +/- tints + muted hunks, escaped
    patch lines, binary/truncated notes, the >20 collapse default, and the 30s poll — all
    with expand state surviving re-renders."""
    _run_drawer_js(
        r"""
(async()=>{
  const row = element('pr-row'); row.dataset={repo:'o/n',pr:'7'};
  openDrawer('o/n','7',row);
  await reply(latest(),{number:7,title:'PR',state:'OPEN',author:{login:'kj'},
    headRefName:'f',baseRefName:'main',body:'b',createdAt:'2026-01-01',updatedAt:'2026-01-02'});
  selectDrawerTab('files',false);
  assert.equal(DRAWER_TABS.files.path,'/api/plugins/github/detail/pr/diff');
  assert.ok(requests[latest()].path.startsWith('/api/plugins/github/detail/pr/diff?repo=o%2Fn&number=7'));
  assert.equal(lastIntervalMs,30000);   // Files polls no faster than 30s
  const diff = {total_truncated:false, files:[
    {path:'src/a.py',old_path:null,status:'modified',additions:2,deletions:1,
      patch:'@@ -1,2 +1,3 @@\n ctx\n+<script>x</script>\n-gone',truncated:false,binary:false},
    {path:'img.png',old_path:null,status:'added',additions:0,deletions:0,patch:null,truncated:false,binary:true},
    {path:'new.txt',old_path:'old.txt',status:'renamed',additions:1,deletions:1,
      patch:'@@ -1 +1 @@\n-a\n+b',truncated:false,binary:false},
    {path:'big.txt',old_path:null,status:'modified',additions:9,deletions:0,patch:null,truncated:true,binary:false},
  ]};
  await reply(latest(),diff);
  let html = element('drawer-body').innerHTML;
  assert.ok(html.includes('4</b> files changed'));
  assert.ok(html.includes('>M<') && html.includes('>A<') && html.includes('>R<'));  // status letters
  assert.ok(html.includes('old.txt → new.txt'));             // rename
  // <=20 files: expanded by default — tinted add/del lines, muted hunk, escaped patch.
  assert.ok(html.includes('dl add') && html.includes('dl del') && html.includes('dl hunk'));
  assert.ok(html.includes('&lt;script&gt;x') && !html.includes('<script>x'));
  assert.ok(html.includes('Binary file'));                   // binary note
  assert.ok(html.includes('Diff too large'));                // truncated note
  assert.ok(html.includes('/pull/7/files'));                 // link to the PR files page
  // Collapse src/a.py (index 0); the collapse survives a polling re-render.
  bodyClick({'data-file':'0'});
  assert.ok(!element('drawer-body').innerHTML.includes('&lt;script&gt;x'));
  pollDrawer();
  await reply(latest(),{...diff,total_truncated:true});
  html = element('drawer-body').innerHTML;
  assert.ok(!html.includes('&lt;script&gt;x'));              // still collapsed
  assert.ok(html.includes('some diffs truncated'));          // header reflects total_truncated
  // A fresh PR with >20 files collapses everything by default; expanding one survives a poll.
  closeDrawer(); openDrawer('o/n','8',row);
  await reply(latest(),{number:8,title:'PR',state:'OPEN',author:{login:'kj'},
    headRefName:'f',baseRefName:'main',body:'b',createdAt:'2026-01-01',updatedAt:'2026-01-02'});
  selectDrawerTab('files',false);
  const many = {total_truncated:false, files: Array.from({length:21},(_,i)=>({
    path:'f'+i+'.txt',old_path:null,status:'modified',additions:1,deletions:0,
    patch:'@@ -1 +1 @@\n+x',truncated:false,binary:false}))};
  await reply(latest(),many);
  html = element('drawer-body').innerHTML;
  assert.ok(html.includes('21</b> files changed'));
  assert.ok(!html.includes('class="diff"'));                 // all collapsed
  bodyClick({'data-file':'0'});
  assert.ok(element('drawer-body').innerHTML.includes('class="diff"'));
  pollDrawer();
  await reply(latest(),{...many,total_truncated:true});
  assert.ok(element('drawer-body').innerHTML.includes('class="diff"'));  // expansion survived the poll
})().catch(e=>{console.error(e);process.exitCode=1;});
"""
    )


def test_drawer_fetches_checks_and_diff_routes_and_is_read_only():
    """r5 (static) — the drawer fetches the gated Checks + Files data routes and references
    no write route or mutating fetch anywhere on the board page."""
    from ghplugin.view import PAGE

    assert 'const DET = "/api/plugins/github/detail/pr"' in PAGE  # the detail route base
    assert 'checks: {label: "Checks", path: DET+"/checks"' in PAGE  # Checks tab data route
    assert 'files: {label: "Files", path: DET+"/diff"' in PAGE  # Files tab data route
    assert "POST" not in PAGE  # the board is strictly read-only — no writes
    for forbidden in ('"PUT"', '"DELETE"', '"PATCH"', "method:"):
        assert forbidden not in PAGE
    assert '/api/plugins/github/issue"' not in PAGE  # filing still lives in the widget/palette


def test_drawer_escapes_checks_and_files_strings_and_tints_diffs():
    """r5 (static) — every GitHub-sourced string in the new tabs passes through esc(), diff
    lines carry the +/- tint classes from --pl-* tokens, and the Files tab declares a 30s poll."""
    from ghplugin.view import PAGE

    for fragment in ("esc(c.name", "esc(st.log)", "esc(f.path)", "esc(line)", "esc(c.workflow)"):
        assert fragment in PAGE
    assert "chk-badge" in PAGE  # the fail-count badge markup
    assert ".dl.add{background:var(--pl-color-success-muted" in PAGE
    assert ".dl.del{background:var(--pl-color-danger-muted" in PAGE
    assert ".dl.hunk{color:var(--pl-color-fg-muted)" in PAGE
    assert 'files: {label: "Files"' in PAGE and "poll: 30000" in PAGE  # Files polls no faster than 30s
    assert "setInterval(pollDrawer, (spec && spec.poll) || 15000)" in PAGE


def test_conversation_plain_text_and_read_only_contract():
    from ghplugin.view import PAGE

    assert 'conversation: {label: "Conversation", path: DET+"/conversation"' in PAGE
    for escaped in ('esc(c.body||"")', "esc(author)", "esc(t.path)", "esc(comments[0].diffHunk)"):
        assert escaped in PAGE
    assert ".pr-body{white-space:pre-wrap" in PAGE
    for forbidden in (
        "marked",
        "markdown-it",
        "renderMarkdown",
        "innerHTML = c.body",
        "POST",
        "/reply",
        "/resolve",
        "/comment",
        "method:",
    ):
        assert forbidden not in PAGE


def test_conversation_timeline_threads_summary_and_polling():
    _run_drawer_js(
        r"""
(async()=>{
  const hostile = '<img src=x onerror=alert(1)>';
  openDrawer('o/n',7,element('row'));
  await reply(latest(),{latestReviews:[{author:{login:'alice'},state:'APPROVED'},
    {author:{login:'bob'},state:'CHANGES_REQUESTED'}],reviewRequests:[{login:'carol'},{name:hostile}]});
  assert.ok(element('drawer-body').innerHTML.includes('alice ✓ approved · bob ✗ changes requested · carol requested'));
  assert.ok(element('drawer-body').innerHTML.includes('&lt;img'));
  selectDrawerTab('conversation',false);
  assert.equal(requests[latest()].path,'/api/plugins/github/detail/pr/conversation?repo=o%2Fn&number=7');
  assert.equal(timers.size,1);
  assert.equal(lastIntervalMs,15000);
  const c = (body,date='2026-01-01') => ({body,author:{login:hostile},createdAt:date,url:'https://github.com/o/n/pull/7#comment'});
  const data = {comments:[c('late','2026-01-04'),c('early')],reviews:[
    {author:{login:'reviewer'},state:'APPROVED',body:'',submittedAt:'2026-01-02'},
    {state:'CHANGES_REQUESTED',body:hostile,submittedAt:'2026-01-03'},
    {state:'COMMENTED',body:'review-body',submittedAt:'2026-01-05'},
    {author:{login:'omit-me'},state:'COMMENTED',body:'',submittedAt:'2026-01-06'}],
    threads:[{id:'resolved',isResolved:true,isOutdated:true,path:'done.py',line:9,
      comments:[{...c('resolved-first'),diffHunk:hostile},c('resolved-second')]},
      {id:'open',isResolved:false,path:hostile,line:3,comments:[{...c(hostile),diffHunk:hostile},c('thread-last')]}],
    counts:{unresolved:1}};
  await reply(latest(),data);
  let html = element('drawer-body').innerHTML;
  for(const [a,b] of [['early','approved'],['approved','changes requested'],['changes requested','late'],['late','review-body'],['review-body','Review threads'],['data-thread="open"','data-thread="resolved"']]){
    assert.ok(html.indexOf(a)<html.indexOf(b),a+' before '+b);
  }
  assert.ok(html.includes('>commented</span>'));
  assert.ok(!html.includes('omit-me'));
  assert.ok(!html.includes('<img'));
  assert.ok(html.includes('&lt;img src=x onerror=alert(1)&gt;:3'));
  assert.ok(html.includes('<pre class="thread-hunk">&lt;img'));
  assert.ok(html.includes('thread-last'));
  assert.ok(html.includes('data-thread="resolved" aria-expanded="false"'));
  assert.ok(html.includes('>resolved</span>') && html.includes('>outdated</span>'));
  assert.ok(!html.includes('resolved-second'));
  assert.ok(html.includes('href="https://github.com/o/n/pull/7#comment"'));
  assert.ok(html.includes('d ago'));
  assert.ok(element('drawer-tabs').innerHTML.includes('Conversation<span class="chk-badge">1</span>'));
  bodyClick({'data-thread':'resolved'});
  bodyClick({'data-thread':'open'});
  assert.ok(element('drawer-body').innerHTML.includes('resolved-first'));
  assert.ok(element('drawer-body').innerHTML.includes('resolved-second'));
  element('drawer-body').scrollTop=80;
  pollDrawer();
  await reply(latest(),{...data,threads:[...data.threads].reverse(),counts:{unresolved:0}});
  html=element('drawer-body').innerHTML;
  assert.ok(html.includes('resolved-second'));
  assert.ok(!html.includes('thread-last'));
  assert.equal(element('drawer-body').scrollTop,80);
  assert.ok(!element('drawer-tabs').innerHTML.includes('chk-badge'));
  assert.ok(!conversationItem({...c('x'),url:'javascript:alert(1)'}).includes('javascript:'));
  pollDrawer(); const stale=latest();
  selectDrawerTab('overview',false);
  await reply(stale,data);
  assert.ok(!element('drawer-body').innerHTML.includes('Review threads'));
  selectDrawerTab('conversation',false); await reply(latest(),data);
  assert.ok(element('drawer-body').innerHTML.includes('resolved-second'));
  closeDrawer(); openDrawer('o/n',8,element('row'));
  selectDrawerTab('conversation',false); await reply(latest(),data);
  assert.ok(!element('drawer-body').innerHTML.includes('resolved-second'));
  pollDrawer(); await reply(latest(),{});
  assert.ok(element('drawer-body').innerHTML.includes('No review threads.'));
  closeDrawer();
})().catch(e=>{console.error(e);process.exitCode=1;});
"""
    )


# --- bd-nm78.7: the issue drawer (issue mode reuses the PR drawer machinery) ----


def test_issue_drawer_reuses_one_drawer_and_poller_and_is_read_only():
    """r1/r2/r5 (static) — issue rows open the SAME drawer in issue mode via /detail/issue,
    reusing the single drawer element + one fenced poller, and the drawer stays read-only."""
    from ghplugin.view import PAGE

    assert 'const ISS = "/api/plugins/github/detail/issue"' in PAGE  # the issue data route
    assert "const ISSUE_TABS = {" in PAGE  # issue tab set reuses the shared tablist
    assert 'overview: {label: "Overview", path: ISS' in PAGE
    assert 'comments: {label: "Comments", path: ISS' in PAGE
    # a plain click on an issue row opens the issue drawer (modified clicks keep the href)
    assert "openIssueDrawer(issueRowEl.dataset.repo, issueRowEl.dataset.issue, issueRowEl)" in PAGE
    assert 'data-issue="' in PAGE
    # ONE drawer element and ONE poller serve both modes — not a copied second drawer/poller
    assert PAGE.count('<div id="drawer"') == 1
    assert PAGE.count("setInterval(pollDrawer") == 1
    # read-only everywhere, issue mode included
    assert "POST" not in PAGE
    assert '/api/plugins/github/issue"' not in PAGE  # no create-issue route from the board
    for forbidden in ('"PUT"', '"DELETE"', '"PATCH"', "method:"):
        assert forbidden not in PAGE


def test_issue_drawer_escapes_sources_and_renders_linked_pr_chips():
    """r3/r5 (static) — every GitHub-sourced issue string passes through esc(), the Comments tab
    carries a count badge, and linked PRs are chips."""
    from ghplugin.view import PAGE

    for fragment in (
        'esc(it.body||"")',  # body as escaped pre-wrap text
        "esc(it.milestone.title)",
        "esc((a && a.login)",  # assignees
        "esc(data.title)",  # issue header title
        "esc(data.number)",
        "esc(p.number)",  # linked-PR chip
    ):
        assert fragment in PAGE
    assert 'data-linked-pr="' in PAGE  # the linked-PR chip
    assert 'class="d-count"' in PAGE  # the Comments count badge
    assert "Open on GitHub ↗" in PAGE
    assert ".pr-body{white-space:pre-wrap" in PAGE  # body is plain pre-wrap, never markdown
    for forbidden in ("marked", "markdown-it", "renderMarkdown", "innerHTML = it.body"):
        assert forbidden not in PAGE


def test_linked_pr_uses_its_repository_and_falls_back_when_missing():
    _run_drawer_js(
        r"""
(async()=>{
  for(const [repository, expectedRepo] of [
    [{owner:{login:'other-owner'},name:'other-repo'}, 'other-owner/other-repo'],
    [{owner:{login:'o'},name:'n'}, 'o/n'],
    [undefined, 'o/n'], [null, 'o/n'], [{}, 'o/n'],
    [{owner:{login:'other-owner'}}, 'o/n'],
  ]){
    openIssueDrawer('o/n','12',element('issue-row'));
    await reply(latest(),{number:12,closedByPullRequestsReferences:[{number:34,repository}]});
    // Read the rendered chip's attributes so both rendering and delegation are exercised.
    const html = element('drawer-body').innerHTML;
    const attrs = Object.fromEntries(Array.from(html.matchAll(/(data-linked-[\w-]+)="([^"]*)"/g),
      match => [match[1],match[2]]));
    assert.equal(attrs['data-linked-pr'],'34');
    bodyClick(attrs);
    assert.equal(drawer.repo,expectedRepo);
    assert.equal(drawer.mode,'pr'); assert.equal(drawer.tab,'overview');
    assert.equal(timers.size,1);
    assert.equal(requests[latest()].path,
      '/api/plugins/github/detail/pr?repo='+encodeURIComponent(expectedRepo)+'&number=34');
    closeDrawer();
  }
  const html = issueOverview({closedByPullRequestsReferences:[{number:34,
    repository:{owner:{login:'evil"<owner'},name:'repo">'}}]});
  assert.ok(html.includes('data-linked-repo="evil&quot;&lt;owner/repo&quot;&gt;"'));
})().catch(e=>{console.error(e);process.exitCode=1;});
"""
    )


def test_issue_drawer_interactions_open_tabs_linked_prs_and_missing_fields():
    """r1-r4 (dynamic) — plain vs modified click, Overview + Comments on one fenced poller, the
    header state/stateReason pill, the comment count, a linked-PR chip switching to PR mode, and
    missing optional fields rendering without error."""
    _run_drawer_js(
        r"""
(async()=>{
  const hostile = '<img src=x onerror=alert(1)>';
  const issue = {number:12,title:hostile,state:'CLOSED',stateReason:'NOT_PLANNED',author:{login:hostile},
    body:hostile,createdAt:'2026-01-01',updatedAt:'2026-01-02',labels:[{name:hostile}],
    assignees:[{login:hostile}],milestone:{title:hostile},
    closedByPullRequestsReferences:[{number:34,repository:{owner:{login:'o'},name:'n'},url:'https://github.com/o/n/pull/34'}],
    comments:[{author:{login:hostile},body:'c-late',createdAt:'2026-01-05',url:'https://github.com/o/n/issues/12#c2'},
              {author:{login:hostile},body:'c-early',createdAt:'2026-01-03',url:'https://github.com/o/n/issues/12#c1'}],
    body_truncated:true};
  // Only data-issue closes match (the handler probes data-pr first, then data-issue).
  const issueEl = element('issue-row'); issueEl.dataset={repo:'o/n',issue:'12'};
  const click = (opts={})=>{
    let prevented=false;
    element('list').handlers.click({button:0,target:{closest:(sel)=>sel.includes('data-issue')?issueEl:null},
      preventDefault(){prevented=true;},...opts});
    return prevented;
  };
  // A modified / middle / already-handled click never opens the drawer — the href is followed.
  for(const opts of [{ctrlKey:true},{metaKey:true},{shiftKey:true},{altKey:true},{button:1},{defaultPrevented:true}]){
    assert.equal(click(opts),false);
  }
  assert.equal(requests.length,0);
  // A plain click opens the drawer in ISSUE mode and fetches /detail/issue through the one poller.
  assert.equal(click(),true);
  assert.equal(drawer.mode,'issue');
  assert.deepEqual(Object.keys(drawer.tabs),['overview','comments']);
  assert.equal(requests[0].path,'/api/plugins/github/detail/issue?repo=o%2Fn&number=12');
  assert.equal(timers.size,1); assert.equal(lastIntervalMs,15000);
  assert.equal(element('drawer').hidden,false);
  const did = drawer.id;
  await reply(0,issue);
  // Header: #N title (escaped), closed + stateReason pill, Open-on-GitHub to the issues URL.
  assert.ok(element('drawer-title').innerHTML.includes('#12'));
  assert.ok(element('drawer-title').innerHTML.includes('&lt;img') && !element('drawer-title').innerHTML.includes('<img'));
  assert.ok(element('drawer-meta').innerHTML.includes('closed · not planned'));
  assert.ok(element('drawer-meta').innerHTML.includes('https://github.com/o/n/issues/12'));
  assert.ok(element('drawer-meta').innerHTML.includes('Open on GitHub'));
  // Overview: author/dates/assignees/milestone/labels, escaped body + truncated note, linked-PR chip.
  let html = element('drawer-body').innerHTML;
  for(const dt of ['Author','Created','Updated','Assignees','Milestone','Labels'])
    assert.ok(html.includes('>'+dt+'</dt>'),dt);
  assert.ok(html.includes('&lt;img') && !html.includes('<img'));
  assert.ok(html.includes('Body truncated'));
  assert.ok(html.includes('data-linked-pr="34"'));
  // The Comments tab label carries a live count.
  assert.ok(element('drawer-tabs').innerHTML.includes('Comments<span class="d-count">2</span>'));
  // Arrow keys move across the issue tablist (same keyboard machinery as PR mode).
  for(const [key,want] of [['ArrowRight','comments'],['ArrowRight','overview'],['End','comments'],['Home','overview']]){
    let prevented=false;
    element('drawer-tabs').handlers.keydown({key,preventDefault(){prevented=true;}});
    assert.equal(drawer.tab,want); assert.equal(prevented,true);
    assert.equal(document.activeElement.id,'dtab-'+want);
  }
  // Switch to Comments — SAME endpoint, SAME one poller/drawer; chronological, escaped, each ↗ linked.
  selectDrawerTab('comments',false);
  assert.equal(drawer.tab,'comments'); assert.equal(drawer.id,did); assert.equal(timers.size,1);
  assert.ok(requests[latest()].path.startsWith('/api/plugins/github/detail/issue?repo=o%2Fn&number=12'));
  await reply(latest(),issue);
  html = element('drawer-body').innerHTML;
  assert.ok(html.indexOf('c-early') < html.indexOf('c-late'));  // chronological
  assert.ok(html.includes('href="https://github.com/o/n/issues/12#c1"'));
  assert.ok(html.includes('&lt;img') && !html.includes('<img'));
  // A linked-PR chip switches THIS drawer to that PR (same repo), opening on Overview, one poller.
  selectDrawerTab('overview',false); await reply(latest(),issue);
  bodyClick({'data-linked-pr':'34'});
  assert.equal(drawer.mode,'pr'); assert.equal(drawer.number,'34'); assert.equal(drawer.tab,'overview');
  assert.deepEqual(Object.keys(drawer.tabs),['overview','checks','files','conversation']);
  assert.equal(timers.size,1);
  assert.ok(requests[latest()].path.startsWith('/api/plugins/github/detail/pr?repo=o%2Fn&number=34'));
  await reply(latest(),{number:34,title:'PR!',state:'OPEN',author:{login:'kj'},
    headRefName:'f',baseRefName:'main',body:'b',createdAt:'2026-01-01',updatedAt:'2026-01-02'});
  assert.ok(element('drawer-title').innerHTML.includes('#34 PR!'));
  assert.ok(element('drawer-meta').innerHTML.includes('/pull/34'));
  // Missing optional fields (stateReason, milestone, assignees, labels, linked PRs) render cleanly.
  closeDrawer(); openIssueDrawer('o/n','99',issueEl);
  await reply(latest(),{number:99,title:'Bare',state:'OPEN',author:{login:'kj'},body:'',
    createdAt:'2026-01-01',updatedAt:'2026-01-02',comments:[]});
  html = element('drawer-body').innerHTML;
  assert.ok(html.includes('>Author</dt>'));
  for(const absent of ['Assignees','Milestone','Labels'])
    assert.ok(!html.includes('>'+absent+'</dt>'),absent);
  assert.ok(!html.includes('Linked pull requests'));
  assert.ok(element('drawer-meta').innerHTML.includes('>open<'));  // plain state pill, no reason
  assert.ok(element('drawer-tabs').innerHTML.includes('Comments<span class="d-count">0</span>'));
  selectDrawerTab('comments',false);
  await reply(latest(),{number:99,title:'Bare',state:'OPEN',comments:[]});
  assert.ok(element('drawer-body').innerHTML.includes('No comments.'));
  // Escape closes the issue drawer and returns focus to the opener row.
  openIssueDrawer('o/n','12',issueEl);
  events.keydown({key:'Escape',preventDefault(){}});
  assert.equal(drawer,null); assert.equal(timers.size,0); assert.equal(document.activeElement,issueEl);
  // issueRow carries data-issue/data-repo + keeps its href (modified-click fallback), never data-pr.
  assert.ok(issueRow(issue,'o/n').includes('data-issue="12"'));
  assert.ok(issueRow(issue,'o/n').includes('data-repo="o/n"'));
  assert.ok(issueRow(issue,'o/n').includes('target="_blank"'));
  assert.ok(!issueRow(issue,'o/n').includes('data-pr='));
})().catch(e=>{console.error(e);process.exitCode=1;});
"""
    )


# --- bd-nm78.8: search box, load-more, and the CI chip (view) ----------------


def test_board_has_debounced_search_box_with_enter_and_esc():
    """r1 (static) — the bar carries a search input, debounced 300ms, Enter runs it now, Esc
    clears it; the result still rides the existing loadSeq fence (drop-stale)."""
    from ghplugin.view import PAGE

    assert 'id="search"' in PAGE and 'type="search"' in PAGE
    assert "setTimeout(reload, 300)" in PAGE  # 300ms debounce
    assert 'if(e.key === "Enter")' in PAGE and 'e.key === "Escape"' in PAGE
    assert '$("search").value = ""' in PAGE  # Esc clears the box
    assert 'encodeURIComponent($("search").value' not in PAGE  # trimmed before it's sent
    assert "&search=" in PAGE and "let loadSeq = 0;" in PAGE  # the stale-drop fence is unchanged


def test_board_has_load_more_up_to_100_with_refine_hint():
    """r2 (static) — the page starts at 30, grows by 30 to a 100 ceiling, and swaps Load more
    for a refine hint at the cap; the limit rides the query string."""
    from ghplugin.view import PAGE

    assert "const PAGE_STEP = 30, MAX_PAGE = 100;" in PAGE
    assert "pageLimit = PAGE_STEP" in PAGE  # starts/resets at 30
    assert "Load more" in PAGE
    assert "Showing first 100 — refine with search" in PAGE
    assert "&limit=" in PAGE
    assert "list.scrollTop = keepTop" in PAGE  # scroll kept across a Load more


def test_pr_rows_render_a_ci_chip_passed_through_esc():
    """r4/r5 (static) — PR rows render a ✓/✗/● chip from the compact `checks` summary, with
    an escaped tooltip; a `none` rollup renders nothing."""
    from ghplugin.view import PAGE

    assert "function checksChip(checks)" in PAGE
    assert "checksChip(it.checks)" in PAGE  # rendered on PR rows
    assert 'CI_SYM = {passing:"✓", failing:"✗", pending:"●"}' in PAGE
    assert 'of "+total+" "+noun+" failing' in PAGE  # the "2 of 9 checks failing" tooltip
    assert "esc(tip)" in PAGE and "esc(checks.state)" in PAGE  # chip strings are escaped
    assert ".ci-passing{color:#3fb950}" in PAGE and ".ci-failing{color:#f85149}" in PAGE


def test_load_more_grows_to_100_keeps_scroll_and_renders_ci_chip():
    """r2/r4 (dynamic) — a full page shows Load more; each click bumps the limit by 30, keeps the
    scroll, and the list stops at 100 with the refine hint. PR rows carry the CI chip."""
    _run_drawer_js(
        r"""
(async()=>{
  element('repo').value='o/n'; element('state').value='open'; element('search').value='';
  const mk = n => Array.from({length:n},(_,i)=>({number:i+1,title:'PR'+i,state:'OPEN',
    author:{login:'kj'},url:'https://github.com/o/n/pull/'+(i+1),
    checks:{state: i===0?'failing':'passing', total:9, fail: i===0?2:0}}));
  setTab('prs');  // reload(): pageLimit resets to 30 and fires one request
  assert.ok(requests[latest()].path.startsWith('/api/plugins/github/prs?repo=o%2Fn&state=open&limit=30'));
  await reply(latest(), {items: mk(30)});
  let html = element('list').innerHTML;
  assert.ok(html.includes('Load more'));               // a full page ⇒ there may be more
  assert.ok(html.includes('ci-failing') && html.includes('2 of 9 checks failing'));  // CI chip + tooltip
  assert.ok(html.includes('ci-passing'));
  // Click Load more — limit → 60, scroll preserved, re-fetched through the fence.
  element('list').scrollTop = 140;
  element('more').onclick();
  assert.ok(requests[latest()].path.includes('limit=60'));
  await reply(latest(), {items: mk(60)});
  assert.equal(element('list').scrollTop, 140);         // scroll kept
  assert.ok(element('list').innerHTML.includes('Load more'));
  element('more').onclick();                            // 60 → 90
  assert.ok(requests[latest()].path.includes('limit=90'));
  await reply(latest(), {items: mk(90)});
  element('more').onclick();                            // 90 → min(120,100)=100
  assert.ok(requests[latest()].path.includes('limit=100'));
  await reply(latest(), {items: mk(100)});
  html = element('list').innerHTML;
  assert.ok(html.includes('Showing first 100 — refine with search'));
  assert.ok(!html.includes('Load more'));              // no more growth past the ceiling
  // A short page (fewer than the limit) shows no Load more at all.
  reload();  // pageLimit back to 30
  await reply(latest(), {items: mk(10)});
  assert.ok(!element('list').innerHTML.includes('Load more'));
})().catch(e=>{console.error(e);process.exitCode=1;});
"""
    )


def test_search_box_debounces_runs_on_enter_and_clears_on_esc():
    """r1 (dynamic) — typing debounces (one request after the timer), Enter runs immediately and
    cancels the pending debounce, Esc clears a non-empty box and reloads without the search param."""
    _run_drawer_js(
        r"""
(async()=>{
  element('repo').value='o/n'; element('state').value='open'; element('search').value='';
  setTab('issues');
  await reply(latest(), {items: []});
  // Typing schedules a debounced reload — nothing fires until the 300ms timer runs.
  const base = requests.length;
  element('search').value = 'crash bug';
  element('search').handlers.input();
  assert.equal(requests.length, base);   // debounced, not yet sent
  runTimeouts();                         // the 300ms timer fires reload()
  assert.equal(requests.length, base+1);
  assert.ok(requests[latest()].path.includes('&search=crash%20bug'));  // one encoded param
  assert.ok(requests[latest()].path.includes('limit=30'));             // search resets to page 1
  await reply(latest(), {items: []});
  // Enter runs immediately AND cancels any pending debounce (no double fetch).
  element('search').value = 'needle';
  element('search').handlers.input();    // schedules a timer…
  element('search').handlers.keydown({key:'Enter',preventDefault(){}});  // …Enter supersedes it
  assert.ok(requests[latest()].path.includes('&search=needle'));
  const afterEnter = requests.length;
  runTimeouts();                         // the cancelled debounce must NOT fire again
  assert.equal(requests.length, afterEnter);
  await reply(latest(), {items: []});
  // Esc clears a non-empty box and reloads with no search param.
  element('search').value = 'needle';
  element('search').handlers.keydown({key:'Escape',preventDefault(){}});
  assert.equal(element('search').value, '');
  assert.ok(!requests[latest()].path.includes('search='));
  await reply(latest(), {items: []});
  // Esc on an already-empty box is a no-op (no reload).
  const quiet = requests.length;
  element('search').handlers.keydown({key:'Escape',preventDefault(){}});
  assert.equal(requests.length, quiet);
})().catch(e=>{console.error(e);process.exitCode=1;});
"""
    )
