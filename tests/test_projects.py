"""The ADR 0095 managed-projects projection — `projects:` feeds the repo picker.

Host-free: `graph.sdk` doesn't exist in this suite, so the default path exercises
the real "no host" degrade. Where a host IS needed, a fake `graph.sdk` module is
installed in `sys.modules` (the standalone-plugin pattern) — nothing imports the
host at module scope, so this stays a pure unit test.
"""

from __future__ import annotations

import sys
import types

import pytest
from ghplugin.gh_issue import effective_default_repo
from ghplugin.projects import effective_repos, registry_repos


@pytest.fixture
def fake_host(monkeypatch):
    """Install a fake `graph.sdk` whose config() returns the given projects list."""

    def _install(projects, *, raises: bool = False, fence=None):
        sdk = types.ModuleType("graph.sdk")

        def config():
            if raises:
                raise RuntimeError("config not loaded")
            return types.SimpleNamespace(projects=projects, filesystem_projects=fence)

        sdk.config = config
        graph = types.ModuleType("graph")
        graph.sdk = sdk
        monkeypatch.setitem(sys.modules, "graph", graph)
        monkeypatch.setitem(sys.modules, "graph.sdk", sdk)

    return _install


# ── degrade paths — the plugin floor stays 0.27.0, the registry is 0.115.0+ ──


def test_no_host_yields_no_repos():
    """The host-free case, and equally any pre-0.115.0 host: never raises."""
    assert registry_repos() == []


def test_host_without_the_registry_yields_no_repos(fake_host):
    """A pre-0.115.0 host has no `projects` attribute at all."""
    sdk = types.ModuleType("graph.sdk")
    sdk.config = lambda: types.SimpleNamespace()  # no .projects
    graph = types.ModuleType("graph")
    graph.sdk = sdk
    sys.modules["graph"], sys.modules["graph.sdk"] = graph, sdk
    try:
        assert registry_repos() == []
    finally:
        del sys.modules["graph.sdk"], sys.modules["graph"]


def test_a_raising_host_config_yields_no_repos(fake_host):
    """Config not loaded yet must not take the picker down with it."""
    fake_host([], raises=True)
    assert registry_repos() == []


# ── the projection itself ──


def test_registry_repos_are_deduped_in_config_order(fake_host):
    fake_host(
        [
            {"name": "a", "path": "/a", "github": "o/a"},
            {"name": "b", "path": "/b"},  # no github — contributes nothing
            {"name": "c", "path": "/c", "github": "o/c"},
            {"name": "d", "path": "/d", "github": "o/a"},  # dupe
            {"name": "e", "path": "/e", "github": "   "},  # blank
            "not-a-dict",
        ]
    )
    assert registry_repos() == ["o/a", "o/c"]


def test_explicit_repos_lead_and_the_registry_follows(fake_host):
    """An explicit picker list is ADDED TO by the registry, never replaced by it and
    never allowed to hide it: the operator's entries keep their order (and so the
    default-repo resolution), the registry's come after, deduped. v0.4.0's
    explicit-wins made one typed list bury every later onboard_project forever."""
    fake_host(
        [{"name": "a", "path": "/a", "github": "o/registry"}, {"name": "b", "path": "/b", "github": "o/explicit"}]
    )
    assert effective_repos(["o/explicit", "o/other"]) == ["o/explicit", "o/other", "o/registry"]


def test_registry_also_reads_github_bindings_on_the_legacy_fence_override(fake_host):
    """A pre-registry instance (or one written by the pre-#2925 onboard tool) carries
    `github:` on filesystem.projects entries — those repos count, after the registry's."""
    fake_host(
        [{"name": "a", "path": "/a", "github": "o/reg"}],
        fence=[
            {"name": "x", "path": "/x", "github": "o/fence"},
            {"name": "y", "path": "/y"},
            {"name": "z", "path": "/z", "github": "o/reg"},
        ],
    )
    assert registry_repos() == ["o/reg", "o/fence"]


def test_registry_fills_an_empty_picker(fake_host):
    fake_host([{"name": "a", "path": "/a", "github": "o/a"}])
    assert effective_repos([]) == ["o/a"]
    assert effective_repos(None) == ["o/a"]


def test_blank_only_explicit_list_falls_through(fake_host):
    """`repos: ["", "  "]` is not a configured list — it's an empty one."""
    fake_host([{"name": "a", "path": "/a", "github": "o/a"}])
    assert effective_repos(["", "   "]) == ["o/a"]


# ── composition with the default-repo resolution ──


def test_default_repo_falls_through_to_the_registry(fake_host):
    """effective_default_repo stays PURE — the registry arrives as its `repos` arg,
    so /issue, the tools and the picker keep agreeing on one answer."""
    fake_host([{"name": "a", "path": "/a", "github": "o/a"}])
    assert effective_default_repo("", effective_repos([])) == "o/a"
    # explicit default still beats everything
    assert effective_default_repo("o/explicit", effective_repos([])) == "o/explicit"
