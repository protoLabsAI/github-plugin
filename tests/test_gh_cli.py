"""gh_cli — binary resolution, token precedence, and error CLASSIFICATION.

Host-free and network-free: the classifier is pure; the runner is exercised only
against a binary that doesn't exist (the missing-binary path must return, not raise).
"""

from __future__ import annotations

import os
import stat

import pytest
from ghplugin import gh_cli
from ghplugin.gh_cli import (
    check_gh_error,
    classify_gh_error,
    gh_env,
    resolve_gh,
    resolve_token,
    run_gh,
    set_token_getter,
    token_source,
)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    """Each test starts with no cached binary, no config token, no env token."""
    gh_cli.reset_gh_cache()
    set_token_getter(None)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    yield
    gh_cli.reset_gh_cache()
    set_token_getter(None)


# ── binary resolution ────────────────────────────────────────────────────────────


def test_resolve_gh_prefers_path(tmp_path, monkeypatch):
    fake = tmp_path / "gh"
    fake.write_text("#!/bin/sh\necho hi\n")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", str(tmp_path))
    assert resolve_gh() == str(fake)


def test_resolve_gh_falls_back_to_install_dirs_when_path_is_bare(tmp_path, monkeypatch):
    """A desktop build launched without a shell PATH still finds a brew/apt gh."""
    monkeypatch.setenv("PATH", str(tmp_path / "nothing-here"))
    fake_dir = tmp_path / "opt-bin"
    fake_dir.mkdir()
    fake = fake_dir / "gh"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setattr(gh_cli, "_FALLBACK_BIN_DIRS", (str(fake_dir),))
    assert resolve_gh() == str(fake)


def test_resolve_gh_is_cached_until_reset(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setattr(gh_cli, "_FALLBACK_BIN_DIRS", ())
    assert resolve_gh() is None
    # A binary appearing later isn't seen until the cache is reset.
    fake = tmp_path / "gh"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    assert resolve_gh() is None
    gh_cli.reset_gh_cache()
    assert resolve_gh() == str(fake)


async def test_run_gh_missing_binary_returns_not_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setattr(gh_cli, "_FALLBACK_BIN_DIRS", ())
    rc, out, serr = await run_gh(["--version"])
    assert rc == 127 and out == "" and "not installed" in serr
    assert check_gh_error(rc, serr).startswith("Error: gh CLI is not installed or not on PATH (looked in ")


# ── token precedence ─────────────────────────────────────────────────────────────


def test_no_token_anywhere():
    assert resolve_token() is None
    assert token_source() == "none"
    assert "GH_TOKEN" not in gh_env()


def test_env_token_is_injected(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_env")
    assert resolve_token() == "ghp_env"
    assert token_source() == "env"
    assert gh_env()["GH_TOKEN"] == "ghp_env"


def test_config_token_wins_over_env(monkeypatch):
    """The Settings ▸ GitHub secret beats an ambient env token — it's what the person
    just pasted, and it's visible in the UI where the env var is not."""
    monkeypatch.setenv("GH_TOKEN", "ghp_env")
    set_token_getter(lambda: "ghp_config")
    assert resolve_token() == "ghp_config"
    assert token_source() == "config"
    env = gh_env()
    assert env["GH_TOKEN"] == "ghp_config" and env["GITHUB_TOKEN"] == "ghp_config"


def test_blank_config_token_falls_through_to_env(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "ghp_env")
    set_token_getter(lambda: "   ")
    assert resolve_token() == "ghp_env"


def test_raising_token_getter_means_no_token():
    def boom():
        raise RuntimeError("config not loaded")

    set_token_getter(boom)
    assert resolve_token() is None and token_source() == "none"


# ── classification ───────────────────────────────────────────────────────────────


def test_success_is_none():
    assert check_gh_error(0, "") is None
    assert check_gh_error(0, "some warning on stderr") is None


@pytest.mark.parametrize(
    "rc,stderr",
    [
        (4, ""),  # gh's AuthRequired exit code, whatever it printed
        (1, "To get started with GitHub CLI, please run:  gh auth login"),
        (1, "You are not logged into any GitHub hosts. To log in, run: gh auth login"),
        (1, "HTTP 401: Bad credentials (https://api.github.com/user)"),
    ],
)
def test_not_authenticated(rc, stderr):
    err = check_gh_error(rc, stderr)
    assert err.startswith("Error: GitHub CLI is not authenticated — run `gh auth login`")
    assert "Settings ▸ GitHub (github.token)" in err


def test_repo_not_found_graphql():
    err = check_gh_error(1, "GraphQL: Could not resolve to a Repository with the name 'o/nope'. (repository)")
    assert err.startswith("Error: repo 'o/nope' not found or not accessible")


def test_http_404_names_the_repo_when_known():
    err = check_gh_error(1, "gh: Not Found (HTTP 404)", repo="o/n")
    assert err.startswith("Error: repo 'o/n' not found or not accessible")
    assert "HTTP 404" in err
    # Without a repo it's still a not-found, not a raw dump.
    assert check_gh_error(1, "gh: Not Found (HTTP 404)").startswith("Error: not found (HTTP 404")


def test_rate_limit_with_and_without_a_reset_hint():
    err = check_gh_error(1, "HTTP 403: API rate limit exceeded for 1.2.3.4. Retry after 12:34:56 UTC")
    assert err.startswith("Error: GitHub API rate limit hit — retry after 12:34:56 UTC")
    err = check_gh_error(1, "HTTP 403: rate limit exceeded")
    assert err == "Error: GitHub API rate limit hit — retry in a few minutes."


def test_plain_403_is_not_a_rate_limit():
    """A forbidden that isn't a rate limit keeps the generic shape (existing callers match on it)."""
    assert check_gh_error(1, "HTTP 403: forbidden") == "Error (gh exit 1): HTTP 403: forbidden"


def test_generic_failure_keeps_the_legacy_shape_and_caps_stderr():
    err = classify_gh_error(2, "x" * 900)
    assert err.startswith("Error (gh exit 2): ") and len(err) == len("Error (gh exit 2): ") + 500


def test_missing_binary_stderr_is_classified_even_with_exit_1():
    err = check_gh_error(1, "gh CLI is not installed or not on PATH.")
    assert err.startswith("Error: gh CLI is not installed or not on PATH (looked in ")
    assert "/usr/local/bin" in err


def test_search_dirs_include_path_and_fallbacks(monkeypatch):
    monkeypatch.setenv("PATH", f"/p1{os.pathsep}/p2")
    dirs = gh_cli.gh_search_dirs()
    assert dirs[:2] == ["/p1", "/p2"] and "/opt/homebrew/bin" in dirs
