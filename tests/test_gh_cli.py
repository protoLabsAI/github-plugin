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


def test_resolve_gh_never_caches_a_miss(tmp_path, monkeypatch):
    """No gh at boot → the operator installs it → the NEXT call sees it (no restart).
    Caching the miss meant Re-check said 'not installed' forever while /config's
    availability check said true."""
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setattr(gh_cli, "_FALLBACK_BIN_DIRS", ())
    assert resolve_gh() is None
    fake = tmp_path / "gh"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    assert resolve_gh() == str(fake)  # seen immediately


def test_resolve_gh_caches_a_hit_until_reset(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setattr(gh_cli, "_FALLBACK_BIN_DIRS", ())
    fake = tmp_path / "gh"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    assert resolve_gh() == str(fake)
    fake.unlink()  # uninstalled mid-process
    assert resolve_gh() == str(fake)  # the hit is cached …
    gh_cli.reset_gh_cache()
    assert resolve_gh() is None  # … until a probe resets it


async def test_status_probe_resets_the_cache_so_recheck_sees_both_transitions(tmp_path, monkeypatch):
    """Re-check after installing gh → found; Re-check after removing it → gone."""
    from ghplugin.api import gh_available
    from ghplugin.status import compute_status

    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setattr(gh_cli, "_FALLBACK_BIN_DIRS", ())
    assert (await compute_status())["gh_path"] is None and gh_available() is False
    fake = tmp_path / "gh"
    fake.write_text("#!/bin/sh\necho 'gh version 9.9.9 (2030-01-01)'\n")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    st = await compute_status()
    assert st["gh_path"] == str(fake) and st["gh_version"] == "9.9.9" and gh_available() is True
    fake.unlink()
    assert (await compute_status())["gh_path"] is None and gh_available() is False  # agree again


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


def test_env_token_is_passed_through_untouched_in_ghs_own_order(monkeypatch):
    """With no config token the child env is the ambient env, verbatim — gh applies
    its own precedence (GH_TOKEN > GITHUB_TOKEN > keyring). We never rewrite GH_TOKEN
    from GITHUB_TOKEN (that inverted gh's order)."""
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_github")
    monkeypatch.setenv("GH_TOKEN", "ghp_gh")
    assert resolve_token() == "ghp_gh"  # GH_TOKEN first, like gh
    assert token_source() == "env"
    env = gh_env()
    assert env["GH_TOKEN"] == "ghp_gh" and env["GITHUB_TOKEN"] == "ghp_github"  # untouched
    monkeypatch.delenv("GH_TOKEN")
    assert resolve_token() == "ghp_github" and "GH_TOKEN" not in gh_env()  # nothing injected


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


# ── the runner never raises ──────────────────────────────────────────────────────


async def test_run_gh_spawn_oserror_is_an_error_tuple(tmp_path, monkeypatch):
    """ENOEXEC (a foreign-arch / truncated ~/.local/bin/gh), EMFILE … — OSError from the
    spawn must come back as (126, '', reason), not escape through the tool layer."""
    import errno

    monkeypatch.setattr(gh_cli, "resolve_gh", lambda: "/fake/gh")

    async def boom(*a, **k):
        raise OSError(errno.ENOEXEC, "Exec format error")

    monkeypatch.setattr(gh_cli.asyncio, "create_subprocess_exec", boom)
    rc, out, serr = await run_gh(["--version"])
    assert rc == 126 and out == "" and "could not run gh at /fake/gh" in serr and "Exec format error" in serr
    assert check_gh_error(rc, serr).startswith("Error (gh exit 126): could not run gh at /fake/gh")


async def test_run_gh_timeout_kill_on_an_exited_process_does_not_raise(monkeypatch):
    """The process can exit between the timeout and the kill — ProcessLookupError from
    kill() must be swallowed, the timeout still reported."""
    import asyncio

    monkeypatch.setattr(gh_cli, "resolve_gh", lambda: "/fake/gh")

    class _Proc:
        returncode = None

        async def communicate(self):
            await asyncio.sleep(10)

        def kill(self):
            raise ProcessLookupError()

    async def spawn(*a, **k):
        return _Proc()

    monkeypatch.setattr(gh_cli.asyncio, "create_subprocess_exec", spawn)
    rc, out, serr = await run_gh(["--version"], timeout=0)
    assert rc == 1 and "timed out" in serr


async def test_run_gh_permission_error_is_126(monkeypatch):
    monkeypatch.setattr(gh_cli, "resolve_gh", lambda: "/fake/gh")

    async def boom(*a, **k):
        raise PermissionError("denied")

    monkeypatch.setattr(gh_cli.asyncio, "create_subprocess_exec", boom)
    rc, _out, serr = await run_gh(["--version"])
    assert rc == 126 and "not executable" in serr


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


def test_auth_hint_branches_on_the_token_source(monkeypatch):
    """'run gh auth login' is WRONG advice when a token is what gh is rejecting."""
    from ghplugin.gh_cli import auth_hint

    assert auth_hint("none").startswith("run `gh auth login`")
    assert "GH_TOKEN / GITHUB_TOKEN in the agent's environment was rejected" in auth_hint("env")
    assert "token saved in Settings ▸ GitHub (github.token) was rejected" in auth_hint("config")
    # the classified error follows the LIVE source
    monkeypatch.setenv("GH_TOKEN", "ghp_bad")
    assert "environment was rejected" in check_gh_error(4, "")
    set_token_getter(lambda: "ghp_bad_config")
    assert "Settings ▸ GitHub (github.token) was rejected — replace it there" in check_gh_error(4, "")


def test_error_kind_categories():
    from ghplugin.gh_cli import error_kind

    assert error_kind(0, "whatever") is None
    assert error_kind(127, "") == "missing_binary"
    assert error_kind(4, "") == "auth"
    assert error_kind(1, "HTTP 403: API rate limit exceeded") == "rate_limit"
    assert error_kind(1, "gh: Not Found (HTTP 404)") == "not_found"
    assert error_kind(1, "GraphQL: Could not resolve to a Repository with the name 'o/n'.") == "not_found"
    assert error_kind(1, "HTTP 500") == "generic"


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
