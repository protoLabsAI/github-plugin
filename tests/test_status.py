"""status.py — the first-run probe, its summary, and the setup-gap reporting.

`run_gh` / `resolve_gh` are patched in the `ghplugin.status` namespace so nothing
shells out; the probe must NEVER raise — every failure is a status with `error` set.
"""

from __future__ import annotations

import json
import threading
from unittest.mock import AsyncMock, patch

from ghplugin.status import (
    GAP_AUTH,
    GAP_GH,
    compute_status,
    gaps_for,
    probe_in_background,
    report_gaps,
    summarize_status,
)

_OK_JSON = json.dumps(
    {
        "hosts": {
            "github.com": [
                {"state": "success", "active": True, "host": "github.com", "login": "kj", "tokenSource": "keyring"}
            ]
        }
    }
)
_BAD_ACTIVE_JSON = json.dumps(
    {
        "hosts": {
            "github.com": [
                {
                    "state": "error",
                    "error": 'non-200 OK status code: 401 Unauthorized body: "..."\nmore',
                    "active": True,
                    "host": "github.com",
                    "login": "",
                    "tokenSource": "GH_TOKEN",
                },
                {"state": "success", "active": False, "host": "github.com", "login": "kj", "tokenSource": "keyring"},
            ]
        }
    }
)


def _gh(*, version="gh version 2.92.0 (2026-04-28)\nhttps://...", auth=(0, _OK_JSON, "")):
    async def run_gh(args, timeout=None, **kw):
        if args == ["--version"]:
            return (0, version, "")
        if args[:2] == ["auth", "status"]:
            return auth
        raise AssertionError(f"unexpected gh call: {args}")

    return run_gh


# ── compute_status ───────────────────────────────────────────────────────────────


async def test_missing_binary():
    with patch("ghplugin.status.resolve_gh", return_value=None):
        st = await compute_status("o/n", ["o/n"])
    assert st["gh_path"] is None and st["authenticated"] is False
    assert "not installed" in st["error"]
    assert st["default_repo"] == "o/n" and st["repos"] == ["o/n"]
    for k in (
        "gh_path",
        "gh_version",
        "authenticated",
        "login",
        "host",
        "error",
        "default_repo",
        "repos",
        "token_source",
    ):
        assert k in st


async def test_authenticated_via_json():
    with (
        patch("ghplugin.status.resolve_gh", return_value="/opt/homebrew/bin/gh"),
        patch("ghplugin.status.run_gh", new=AsyncMock(side_effect=_gh())),
    ):
        st = await compute_status()
    assert st["gh_path"] == "/opt/homebrew/bin/gh"
    assert st["gh_version"] == "2.92.0"
    assert st["authenticated"] is True and st["login"] == "kj" and st["host"] == "github.com"
    assert st["error"] is None


async def test_active_account_failing_is_not_authenticated_even_with_a_good_inactive_one():
    """gh uses the ACTIVE account — a rejected GH_TOKEN with a healthy keyring login
    behind it still fails every command, so status must say so (first line only)."""
    with (
        patch("ghplugin.status.resolve_gh", return_value="/usr/bin/gh"),
        patch("ghplugin.status.run_gh", new=AsyncMock(side_effect=_gh(auth=(0, _BAD_ACTIVE_JSON, "")))),
    ):
        st = await compute_status()
    assert st["authenticated"] is False and st["login"] is None
    assert st["error"].startswith("non-200 OK status code: 401") and "\n" not in st["error"]


async def test_not_logged_in_json_is_empty_hosts():
    with (
        patch("ghplugin.status.resolve_gh", return_value="/usr/bin/gh"),
        patch(
            "ghplugin.status.run_gh",
            new=AsyncMock(side_effect=_gh(auth=(0, '{"hosts":{}}', "You are not logged into any GitHub hosts."))),
        ),
    ):
        st = await compute_status()
    assert st["authenticated"] is False and st["error"] == "not logged in"


async def test_text_fallback_for_an_older_gh_without_json():
    """An older gh rejects --json on auth status; the probe re-runs plain and parses text."""
    calls = []

    async def run_gh(args, timeout=None, **kw):
        calls.append(args)
        if args == ["--version"]:
            return (0, "gh version 2.30.0 (2023-06-01)", "")
        if "--json" in args:
            return (1, "", "unknown flag: --json")
        return (0, "", "github.com\n  ✓ Logged in to github.com account kj (keyring)\n  - Active account: true")

    with (
        patch("ghplugin.status.resolve_gh", return_value="/usr/bin/gh"),
        patch("ghplugin.status.run_gh", new=AsyncMock(side_effect=run_gh)),
    ):
        st = await compute_status()
    assert st["authenticated"] is True and st["login"] == "kj" and st["host"] == "github.com"
    assert ["auth", "status"] in calls  # the plain re-run happened


async def test_text_fallback_not_logged_in():
    async def run_gh(args, timeout=None, **kw):
        if args == ["--version"]:
            return (0, "gh version 2.30.0", "")
        if "--json" in args:
            return (1, "", "unknown flag: --json")
        return (1, "", "You are not logged into any GitHub hosts. To log in, run: gh auth login")

    with (
        patch("ghplugin.status.resolve_gh", return_value="/usr/bin/gh"),
        patch("ghplugin.status.run_gh", new=AsyncMock(side_effect=run_gh)),
    ):
        st = await compute_status()
    assert st["authenticated"] is False and st["error"] == "not logged in"


async def test_version_failure_is_an_error_not_an_exception():
    async def run_gh(args, timeout=None, **kw):
        return (1, "", "dyld: missing library")

    with (
        patch("ghplugin.status.resolve_gh", return_value="/usr/bin/gh"),
        patch("ghplugin.status.run_gh", new=AsyncMock(side_effect=run_gh)),
    ):
        st = await compute_status()
    assert st["gh_path"] == "/usr/bin/gh" and st["gh_version"] is None
    assert "`gh --version` failed" in st["error"]


async def test_a_raising_runner_never_escapes():
    with (
        patch("ghplugin.status.resolve_gh", return_value="/usr/bin/gh"),
        patch("ghplugin.status.run_gh", new=AsyncMock(side_effect=RuntimeError("boom"))),
    ):
        st = await compute_status()
    assert st["authenticated"] is False and "status probe failed: RuntimeError: boom" in st["error"]


# ── summary + gaps ───────────────────────────────────────────────────────────────


def test_summary_missing_binary_tells_how_to_install():
    text = summarize_status({"gh_path": None, "default_repo": "", "repos": []})
    assert "NOT installed" in text and "cli.github.com" in text and "gh auth login" in text
    assert "No default repo configured" in text


def test_summary_unauthenticated_tells_how_to_fix():
    text = summarize_status(
        {
            "gh_path": "/usr/bin/gh",
            "gh_version": "2.9",
            "authenticated": False,
            "error": "not logged in",
            "default_repo": "o/n",
            "repos": ["o/n", "o/m"],
            "token_source": "env",
        }
    )
    assert "NOT authenticated (not logged in)" in text
    assert "gh auth login" in text and "Settings ▸ GitHub (github.token)" in text
    assert "Default repo: o/n." in text and "2 repo(s) in the picker: o/n, o/m" in text
    assert "GITHUB_TOKEN/GH_TOKEN env" in text


def test_summary_healthy_is_one_paragraph():
    text = summarize_status(
        {
            "gh_path": "/usr/bin/gh",
            "gh_version": "2.9",
            "authenticated": True,
            "login": "kj",
            "host": "github.com",
            "default_repo": "o/n",
            "repos": ["o/n"],
            "token_source": "config",
        }
    )
    assert text.startswith("GitHub CLI v2.9 is installed at /usr/bin/gh and authenticated as kj on github.com.")
    assert "\n" not in text and "Settings ▸ GitHub (github.token)" in text


def test_gaps_for_each_state():
    assert gaps_for({"gh_path": None}) == {GAP_GH: gaps_for({"gh_path": None})[GAP_GH], GAP_AUTH: None}
    assert "not installed" in gaps_for({"gh_path": None})[GAP_GH]
    g = gaps_for({"gh_path": "/x/gh", "authenticated": False, "error": "not logged in"})
    assert g[GAP_GH] is None and "not authenticated (not logged in)" in g[GAP_AUTH]
    assert gaps_for({"gh_path": "/x/gh", "authenticated": True}) == {GAP_GH: None, GAP_AUTH: None}


class _Seam:
    def __init__(self, raise_on_call=False):
        self.calls: list[tuple[str, str | None]] = []
        self.raise_on_call = raise_on_call

    def report_setup_gap(self, key, message):
        if self.raise_on_call:
            raise RuntimeError("seam broke")
        self.calls.append((key, message))


def test_report_gaps_uses_the_seam_and_clears_when_healthy():
    seam = _Seam()
    assert report_gaps(seam, {"gh_path": "/x/gh", "authenticated": False, "error": "e"}) is True
    assert dict(seam.calls) == {GAP_GH: None, GAP_AUTH: dict(seam.calls)[GAP_AUTH]}
    assert dict(seam.calls)[GAP_AUTH].startswith("GitHub CLI is not authenticated")
    seam.calls.clear()
    report_gaps(seam, {"gh_path": "/x/gh", "authenticated": True})
    assert dict(seam.calls) == {GAP_GH: None, GAP_AUTH: None}  # both CLEARED


def test_report_gaps_without_the_seam_is_a_noop():
    assert report_gaps(object(), {"gh_path": None}) is False


def test_report_gaps_swallows_a_raising_seam():
    assert report_gaps(_Seam(raise_on_call=True), {"gh_path": None}) is False


# ── the background probe ─────────────────────────────────────────────────────────


def test_probe_not_started_without_the_seam():
    assert probe_in_background(object(), "o/n", []) is None


def test_probe_runs_off_thread_reports_and_evaluates_getters_in_thread():
    seam = _Seam()
    seen = {}

    def default_getter():
        seen["thread"] = threading.current_thread().name
        return "o/n"

    with patch("ghplugin.status.resolve_gh", return_value=None):
        t = probe_in_background(seam, default_getter, lambda: ["o/n"])
        assert t is not None
        t.join(timeout=5)
    assert not t.is_alive()
    assert seen["thread"] == "github-plugin-status-probe"  # evaluated IN the thread, not at register()
    assert dict(seam.calls)[GAP_GH] and dict(seam.calls)[GAP_AUTH] is None
