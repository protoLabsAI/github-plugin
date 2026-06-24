"""The per-agent write gate is the load-bearing behavior — test it directly.

Read tools register unconditionally; write tools register ONLY when github.write is
true. We assert both halves against the ACTUAL registered tools (no host).
"""

from __future__ import annotations

from ghplugin import register

READ_TOOLS = {
    "github_get_pr",
    "github_get_issue",
    "github_list_issues",
    "github_get_commit_diff",
    "github_ci_runs",
    "github_run_failure",
    "github_read_file",
    "github_repo_contents",
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
