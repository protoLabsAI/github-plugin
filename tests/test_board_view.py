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


def test_drawer_interactions_and_async_lifecycle():
    """Execute the shipped JS with a small DOM/fetch/timer harness, no host or network.

    Node is optional locally; the static contract and route tests always run in the
    Python-only gate. When available this also checks races and event behavior.
    """
    import shutil
    import subprocess

    import pytest
    from ghplugin.view import PAGE

    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required for the JavaScript interaction harness")
    script = PAGE.split('<script type="module">', 1)[1].split("</script>", 1)[0]
    script = script[script.index("  const $ =") :].replace("  kit.initPluginView(boot);", "")
    harness = r"""
const assert = require('node:assert/strict');
const elements = new Map(), events = {}, requests = [], timers = new Map();
let timerId = 0;
function element(id){
  if(!elements.has(id)) elements.set(id, {
    id, dataset: {}, handlers: {}, attrs: {}, writes: 0, scrollTop: 0, isConnected: true,
    classList: {add(){}, remove(){}},
    set innerHTML(v){this.html=v; this.writes++;}, get innerHTML(){return this.html||'';},
    setAttribute(k,v){this.attrs[k]=v;}, getAttribute(k){return this.attrs[k];},
    addEventListener(k,v){this.handlers[k]=v;}, focus(){document.activeElement=this;},
    querySelectorAll(){return Object.keys(DRAWER_TABS).map(t => {
      const el=element('dtab-'+t); el.attrs['data-drawer-tab']=t; return el;
    });}
  });
  return elements.get(id);
}
const document = {hidden: false, getElementById: element, addEventListener(k,v){events[k]=v;}};
const kit = {apiFetch(path){return new Promise((resolve,reject)=>requests.push({path,resolve,reject}));}};
const setInterval = (fn,ms) => {assert.equal(ms,15000); timers.set(++timerId,fn); return timerId;};
const clearInterval = id => timers.delete(id);
const flush = async()=>{await Promise.resolve(); await Promise.resolve(); await Promise.resolve();};
const reply = async(index,data,ok=true)=>{requests[index].resolve({ok,json:async()=>data}); await flush();};
"""
    checks = r"""
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
  assert.equal(timers.size,1);
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
  // Add a future tab to verify automatic activation beyond today's single Overview.
  DRAWER_TABS.checks = {label:'Checks',path:'/checks',render:()=>'<p>Checks</p>'};
  renderDrawerTabs();
  for(const [key,want] of [['ArrowRight','checks'],['Home','overview'],['End','checks'],['ArrowRight','overview'],['ArrowLeft','checks']]){
    let prevented=false;
    element('drawer-tabs').handlers.keydown({key,preventDefault(){prevented=true;}});
    assert.equal(drawer.tab,want); assert.equal(prevented,true);
    assert.equal(document.activeElement.id,'dtab-'+want);
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
    result = subprocess.run([node, "-"], input=harness + script + checks, text=True, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr
