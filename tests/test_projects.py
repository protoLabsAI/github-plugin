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


# ── v0.6.0: a local checkout's `origin` remote becomes owner/name ──


@pytest.fixture(autouse=True)
def _fresh_remote_cache():
    from ghplugin.projects import reset_remote_cache

    reset_remote_cache()
    yield
    reset_remote_cache()


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://github.com/protoLabsAI/github-plugin.git", "protoLabsAI/github-plugin"),
        ("https://github.com/protoLabsAI/github-plugin", "protoLabsAI/github-plugin"),
        ("git@github.com:protoLabsAI/github-plugin.git", "protoLabsAI/github-plugin"),
        ("ssh://git@github.com/protoLabsAI/github-plugin.git", "protoLabsAI/github-plugin"),
        ("https://github.com/o/n/", "o/n"),
        ("https://gitlab.com/o/n.git", None),  # not GitHub — never masquerades
        ("", None),
        ("not a url", None),
    ],
)
def test_repo_from_remote_url(url, expected):
    from ghplugin.projects import repo_from_remote_url

    assert repo_from_remote_url(url) == expected


def _git_repo(tmp_path, name, origin=None):
    """A real (empty) git checkout, optionally with an origin remote."""
    import subprocess

    d = tmp_path / name
    d.mkdir()
    subprocess.run(["git", "init", "-q", str(d)], check=True)
    if origin:
        subprocess.run(["git", "-C", str(d), "remote", "add", "origin", origin], check=True)
    return d


def test_repo_from_checkout_parses_the_origin_remote(tmp_path):
    from ghplugin.projects import repo_from_checkout

    d = _git_repo(tmp_path, "proj", "git@github.com:o/proj.git")
    assert repo_from_checkout(str(d)) == "o/proj"


def test_repo_from_checkout_degrades_for_non_repos_and_remoteless_repos(tmp_path):
    from ghplugin.projects import repo_from_checkout

    assert repo_from_checkout(str(tmp_path / "missing")) is None  # no such dir
    (tmp_path / "plain").mkdir()
    assert repo_from_checkout(str(tmp_path / "plain")) is None  # not a git repo
    assert repo_from_checkout(str(_git_repo(tmp_path, "noremote"))) is None  # no origin
    assert repo_from_checkout("") is None


def test_repo_from_checkout_is_cached_per_path(tmp_path, monkeypatch):
    import subprocess

    from ghplugin import projects

    d = _git_repo(tmp_path, "proj", "https://github.com/o/proj")
    assert projects.repo_from_checkout(str(d)) == "o/proj"
    calls = []
    real = subprocess.run
    monkeypatch.setattr(projects.subprocess, "run", lambda *a, **k: (calls.append(a), real(*a, **k))[1])
    assert projects.repo_from_checkout(str(d)) == "o/proj"
    assert calls == []  # served from the cache — a per-call getter never forks git per tool call


def test_remote_repos_come_from_registry_paths_and_the_board_repo(tmp_path, monkeypatch):
    """A registry project with only a `path` (no github:), and project_board.repo, are
    checkouts — their origin names the repo. Bound entries' paths are scanned too (the
    remote may differ from the declared binding), after the unbound ones."""
    import sys
    import types

    from ghplugin.projects import checkout_paths, effective_repos, remote_repos

    unbound = _git_repo(tmp_path, "unbound", "git@github.com:o/unbound.git")
    bound = _git_repo(tmp_path, "bound", "git@github.com:o/bound-remote.git")
    board = _git_repo(tmp_path, "board", "https://github.com/o/board.git")

    sdk = types.ModuleType("graph.sdk")
    sdk.config = lambda: types.SimpleNamespace(
        projects=[
            {"name": "u", "path": str(unbound)},
            {"name": "b", "path": str(bound), "github": "o/bound"},
        ],
        filesystem_projects=None,
        plugin_config={"project_board": {"repo": str(board)}},
    )
    graph = types.ModuleType("graph")
    graph.sdk = sdk
    monkeypatch.setitem(sys.modules, "graph", graph)
    monkeypatch.setitem(sys.modules, "graph.sdk", sdk)

    assert checkout_paths() == [str(unbound), str(bound), str(board)]
    assert remote_repos() == ["o/unbound", "o/bound-remote", "o/board"]
    # Order of the picker: explicit, then registry bindings, then remotes (last resort), deduped.
    assert effective_repos(["o/explicit"]) == ["o/explicit", "o/bound", "o/unbound", "o/bound-remote", "o/board"]
    assert effective_repos([], include_remotes=False) == ["o/bound"]


def test_board_repo_dot_sentinel_is_ignored(monkeypatch):
    """projectBoard's `repo: "."` is its UNCONFIGURED default (it refuses to build there) —
    never parse the server's cwd as the agent's repo."""
    import sys
    import types

    from ghplugin.projects import board_repo_path, checkout_paths

    for sentinel in (".", "", None):
        sdk = types.ModuleType("graph.sdk")
        sdk.config = lambda s=sentinel: types.SimpleNamespace(
            projects=[], filesystem_projects=None, plugin_config={"project_board": {"repo": s}}
        )
        graph = types.ModuleType("graph")
        graph.sdk = sdk
        monkeypatch.setitem(sys.modules, "graph", graph)
        monkeypatch.setitem(sys.modules, "graph.sdk", sdk)
        assert board_repo_path() == ""
        assert checkout_paths() == []


def test_no_host_means_no_remotes():
    from ghplugin.projects import board_repo_path, checkout_paths, remote_repos

    assert checkout_paths() == [] and remote_repos() == [] and board_repo_path() == ""
