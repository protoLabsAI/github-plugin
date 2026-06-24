"""The /issue chat command is wired through register() — and degrades gracefully.

register() owns the user-only `/issue` command via the host's `register_chat_command`
seam. On a host that HAS the seam it registers; on an older host that lacks it (the
`_LegacyRegistry` fake), `/issue` is skipped but the tools still load. The handler
routes through the plugin's configured default repo (no host coupling).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from ghplugin import register

_GOOD_BUG_BODY = (
    "## Problem\nThe parser crashes on empty input and we should handle it gracefully "
    "across the whole pipeline instead of raising.\n## Steps to reproduce\nRun it with an empty string."
)


def test_issue_registered_when_seam_present(make_registry):
    reg = make_registry({})
    register(reg)
    assert "issue" in reg.chat_commands  # the /issue command is owned by the plugin
    assert "github_get_pr" in reg.tool_names  # tools still load too


def test_issue_handler_uses_configured_default_repo(make_registry):
    """The handler routes to the plugin's configured repo (here, first of `repos`)."""
    reg = make_registry({"repos": ["o/configured"]})
    register(reg)
    handler = reg.chat_commands["issue"]

    async def run():
        fake = AsyncMock(return_value=(0, "https://github.com/o/configured/issues/9", ""))
        with patch("ghplugin.gh_issue.run_gh", fake):
            out = await handler(f"Crash on empty --bug\n{_GOOD_BUG_BODY}", "sess-1")
        assert "Filed in o/configured" in out
        args = fake.call_args.args[0]
        assert args[args.index("--repo") + 1] == "o/configured"

    import asyncio

    asyncio.run(run())


def test_legacy_host_skips_issue_but_loads_tools(make_legacy_registry):
    """A host without register_chat_command must not crash — tools load, /issue skipped."""
    reg = make_legacy_registry({"write": True})
    register(reg)  # must not raise
    assert "github_get_pr" in reg.tool_names
    assert "github_create_issue" in reg.tool_names  # write gate still works
    assert not hasattr(reg, "chat_commands")


def test_issue_registered_even_when_write_off(make_registry):
    """`/issue` is its own user-only command — independent of the write tool gate."""
    reg = make_registry({"write": False})
    register(reg)
    assert "issue" in reg.chat_commands
    assert "github_create_issue" not in reg.tool_names  # write tools stay gated off
