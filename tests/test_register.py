"""The per-agent write gate is the load-bearing behavior — test it directly.

Read tools register unconditionally; write tools register ONLY when github.write is
true. We assert both halves against the ACTUAL registered tools (no host).
"""

from __future__ import annotations

from pathlib import Path

from ghplugin import register

READ_TOOLS = {
    "github_get_pr",
    "github_get_issue",
    "github_list_issues",
    "github_get_commit_diff",
    "github_pr_diff",
    "github_path_exists",
    "github_ci_runs",
    "github_run_failure",
    "github_read_file",
    "github_read_pr_file",
    "github_repo_contents",
    "github_status",  # the self-diagnosis probe — always on, no write gate (v0.6.0)
    "github_list_prs",  # the PM verbs (v0.7.0)
    "github_issue_comments",
    "github_search_issues",
}
WRITE_TOOLS = {
    "github_create_issue",
    "github_comment",
    "github_create_pr",
    "github_edit_pr",
    "github_merge_pr",
    "github_close",
    "github_set_labels",
    "github_set_assignees",
}


def test_read_only_by_default(make_registry):
    """No config → read tools only, NO write tools."""
    reg = make_registry({})
    register(reg)
    names = set(reg.tool_names)
    assert READ_TOOLS <= names, f"missing read tools: {READ_TOOLS - names}"
    assert not (WRITE_TOOLS & names), f"write tools leaked into a read-only agent: {WRITE_TOOLS & names}"


def test_write_false_is_read_only(make_registry):
    reg = make_registry({"write": False})
    register(reg)
    assert not (WRITE_TOOLS & set(reg.tool_names))


def test_write_true_adds_write_tools(make_registry):
    """github.write: true → read tools AND write tools."""
    reg = make_registry({"write": True})
    register(reg)
    names = set(reg.tool_names)
    assert READ_TOOLS <= names
    assert WRITE_TOOLS <= names, f"write tools missing when write=true: {WRITE_TOOLS - names}"


def test_tools_have_descriptions(make_registry):
    """Every registered tool must have a description (an f-string docstring → None)."""
    reg = make_registry({"write": True})
    register(reg)
    for t in reg.tools:
        desc = getattr(t, "description", None)
        assert desc, f"tool {getattr(t, 'name', t)!r} has no description"


def test_github_status_is_never_write_gated(make_registry):
    """The model must be able to self-diagnose a broken `gh` on a read-only agent."""
    reg = make_registry({"write": False})
    register(reg)
    assert "github_status" in reg.tool_names


async def test_tools_default_repo_is_live_not_a_register_time_snapshot(make_registry, monkeypatch):
    """v0.6.0 — an `onboard_project` / Settings edit mid-session must reach the tools'
    omitted-repo fallback on the NEXT call, with no re-register. (Here the host has no
    live_config, so the snapshot dict IS the live config — editing it is the edit.)"""
    from unittest.mock import AsyncMock, patch

    monkeypatch.delenv("GITHUB_DEFAULT_REPO", raising=False)
    monkeypatch.delenv("GH_REPO", raising=False)
    reg = make_registry({"default_repo": "o/first"})
    register(reg)
    tool = {t.name: t for t in reg.tools}["github_list_issues"]
    fake = AsyncMock(return_value=(0, "[]", ""))
    with patch("ghplugin.read_tools.run_gh", fake):
        await tool.ainvoke({})
        assert fake.call_args.args[0][fake.call_args.args[0].index("--repo") + 1] == "o/first"
        reg.config["default_repo"] = "o/second"  # the operator edits Settings
        await tool.ainvoke({})
        assert fake.call_args.args[0][fake.call_args.args[0].index("--repo") + 1] == "o/second"


def test_register_prefers_the_hosts_live_config_getter(make_registry):
    """A host with `live_config` (protoAgent ≥ the live-config seam) is read per call —
    including the `token` secret, which is wired into the gh runner."""
    from ghplugin import gh_cli

    class _Live(make_registry):
        def __init__(self, config):
            super().__init__(config)
            self.live = dict(config)

        def live_config(self):
            return self.live

    reg = _Live({"default_repo": "o/snap", "token": ""})
    register(reg)
    try:
        assert gh_cli.resolve_token() is None
        reg.live["token"] = "ghp_pasted"  # pasted in Settings ▸ GitHub, no restart
        assert gh_cli.resolve_token() == "ghp_pasted" and gh_cli.token_source() == "config"
    finally:
        gh_cli.set_token_getter(None)


def test_register_starts_the_setup_probe_only_when_the_host_has_the_seam(make_registry, monkeypatch):
    """The setup-gap seam is newer than this plugin's floor: with it, a background probe
    reports `gh`/`auth` gaps (and clears them when healthy); without it, nothing runs."""
    import threading
    from unittest.mock import patch

    from ghplugin import status as status_mod

    started: list[threading.Thread] = []
    real = status_mod.probe_in_background

    def spy(registry, default_repo="", repos=None):
        t = real(registry, default_repo, repos)
        if t:
            started.append(t)
        return t

    class _Seam(make_registry):
        def __init__(self, config):
            super().__init__(config)
            self.gaps: dict = {}

        def report_setup_gap(self, key, message):
            self.gaps[key] = message

    with patch("ghplugin.status.probe_in_background", spy), patch("ghplugin.status.resolve_gh", return_value=None):
        reg = _Seam({})
        register(reg)
        for t in started:
            t.join(timeout=5)
    assert started and not started[0].is_alive()
    assert reg.gaps["gh"] and "not installed" in reg.gaps["gh"] and reg.gaps["auth"] is None

    started.clear()
    with patch("ghplugin.status.probe_in_background", spy):
        plain = make_registry({})
        register(plain)  # no seam → no thread
    assert started == []


async def test_configured_default_repo_skips_the_picker_computation(make_registry, monkeypatch):
    """With default_repo set, a tool call must not compute the picker (registry + git
    remotes) at all — the common path stays free of subprocess work."""
    from unittest.mock import AsyncMock, patch

    from ghplugin import projects

    monkeypatch.delenv("GITHUB_DEFAULT_REPO", raising=False)
    monkeypatch.delenv("GH_REPO", raising=False)
    calls = []
    monkeypatch.setattr(projects, "remote_repos", lambda: (calls.append(1), [])[1])
    reg = make_registry({"default_repo": "o/set", "repos": ["o/other"]})
    register(reg)
    tool = {t.name: t for t in reg.tools}["github_list_issues"]
    with patch("ghplugin.read_tools.run_gh", AsyncMock(return_value=(0, "[]", ""))):
        await tool.ainvoke({})
    assert calls == []  # picker never computed
    reg.config["default_repo"] = ""
    with patch("ghplugin.read_tools.run_gh", AsyncMock(return_value=(0, "[]", ""))):
        await tool.ainvoke({})
    assert calls == [1]  # …only when the default is unset


def test_inventory_counts_match_the_docs(make_registry):
    """README / PROTO.md say 15 read / 8 write / 3 review = 26. Recount from what
    register() actually produces so the docs can't drift from the code again."""
    reg = make_registry({"write": True})
    register(reg)
    names = set(reg.tool_names)
    review = {"github_review_comment", "github_review_approve", "github_review_request_changes"}
    assert len(READ_TOOLS) == 15 and len(WRITE_TOOLS) == 8 and len(review) == 3
    assert names == READ_TOOLS | WRITE_TOOLS | review and len(names) == 26
    for doc in ("README.md", "PROTO.md"):
        text = (Path(__file__).resolve().parent.parent / doc).read_text()
        assert "15" in text and ("= 26" in text or "26 tools" in text), f"{doc} inventory stale"
