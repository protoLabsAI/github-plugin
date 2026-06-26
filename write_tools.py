"""GitHub WRITE tools over `gh` — GATED. Registered ONLY when `github.write: true`.

These mutate GitHub, so they're behind the per-agent write gate (see __init__.py).
Each validates its repo with `bad_repo()`, runs via `run_gh()`, and degrades to a
readable `Error: ...` string.

The set:
  - github_create_issue  — `gh issue create` (returns the new issue URL).
  - github_comment       — `gh issue comment` (works for PRs too — a PR is an issue).
  - github_create_pr     — `gh pr create` (returns the new PR URL).
  - github_edit_pr       — `gh pr edit` (+ `gh pr ready`) to change title/body/draft.
  - github_merge_pr      — `gh pr merge`. IRREVERSIBLE, so it refuses without an
    explicit ``confirm=true`` and offers a ``dry_run`` preview (defence-in-depth on
    top of the per-agent write gate).
  - github_close         — close/reopen an issue or PR (`gh {issue,pr} close|reopen`).
  - github_set_labels    — add/remove labels (`gh {issue,pr} edit --add/--remove-label`).
  - github_set_assignees — add/remove assignees (`gh {issue,pr} edit --add/--remove-assignee`).

Issue-vs-PR matters for close/labels/assignees (different `gh` subcommands), so those
take a ``kind`` ("issue" | "pr"); commenting does not (a PR is an issue for comments).

Keep tool docstrings PLAIN string literals (an f-string docstring → __doc__ is None →
the tool ships with no description).
"""

from __future__ import annotations

from langchain_core.tools import tool

from .gh_cli import bad_repo, check_gh_error, run_gh
from .gh_issue import resolve_repo


def _csv(value: str) -> list[str]:
    """Split a comma-separated arg into cleaned, non-empty tokens (order-preserving)."""
    return [tok.strip() for tok in (value or "").split(",") if tok.strip()]


def get_write_tools(default_repo: str = "") -> list:
    """Build the write tools. ``default_repo`` (``owner/name``) is used whenever a tool's
    ``repo`` arg is omitted, so an agent with one configured repo needn't repeat it."""

    @tool
    async def github_create_issue(title: str, repo: str = "", body: str = "", labels: str = "") -> str:
        """Create a GitHub issue.

        Args:
            repo: Repository as ``owner/name``. Omit to use the agent's configured default repo.
            title: Issue title.
            body: Issue body (Markdown).
            labels: Optional comma-separated label names.

        Returns the new issue URL.
        """
        repo = resolve_repo(repo, default_repo) or ""
        if err := bad_repo(repo):
            return err
        args = ["issue", "create", "--repo", repo, "--title", title, "--body", body]
        for label in labels.split(","):
            label = label.strip()
            if label:
                args += ["--label", label]
        rc, out, serr = await run_gh(args)
        if gh_err := check_gh_error(rc, serr):
            return gh_err
        return out.strip()

    @tool
    async def github_comment(number: int, body: str, repo: str = "") -> str:
        """Add a comment to a GitHub issue or pull request.

        Args:
            repo: Repository as ``owner/name``. Omit to use the agent's configured default repo.
            number: Issue or PR number (a PR is an issue for commenting).
            body: The comment body (Markdown).

        Returns the new comment URL.
        """
        repo = resolve_repo(repo, default_repo) or ""
        if err := bad_repo(repo):
            return err
        args = ["issue", "comment", str(number), "--repo", repo, "--body", body]
        rc, out, serr = await run_gh(args)
        if gh_err := check_gh_error(rc, serr):
            return gh_err
        return out.strip()

    @tool
    async def github_create_pr(head: str, title: str, repo: str = "", body: str = "", base: str = "main") -> str:
        """Open a pull request.

        Args:
            repo: Repository as ``owner/name``. Omit to use the agent's configured default repo.
            head: The branch with the changes.
            title: PR title.
            body: PR body (Markdown).
            base: Base branch to merge into (default ``main``).

        Returns the new PR URL.
        """
        repo = resolve_repo(repo, default_repo) or ""
        if err := bad_repo(repo):
            return err
        args = [
            "pr",
            "create",
            "--repo",
            repo,
            "--head",
            head,
            "--base",
            base,
            "--title",
            title,
            "--body",
            body,
        ]
        rc, out, serr = await run_gh(args)
        if gh_err := check_gh_error(rc, serr):
            return gh_err
        return out.strip()

    @tool
    async def github_edit_pr(number: int, repo: str = "", title: str = "", body: str = "", state: str = "") -> str:
        """Edit a pull request's title/body and/or its draft state.

        Args:
            repo: Repository as ``owner/name``. Omit to use the agent's configured default repo.
            number: PR number.
            title: New title (omit to leave unchanged).
            body: New body / description (omit to leave unchanged).
            state: ``ready`` to mark ready for review, ``draft`` to convert back to a
                draft, or empty to leave the state unchanged.

        Returns the PR URL (or a short summary of what changed).
        """
        repo = resolve_repo(repo, default_repo) or ""
        if err := bad_repo(repo):
            return err
        if not (title or body or state):
            return "Error: nothing to change — pass title, body, and/or state."
        if state and state not in ("ready", "draft"):
            return f"Error: state must be 'ready' or 'draft' (got {state!r})."
        url = ""
        if title or body:
            args = ["pr", "edit", str(number), "--repo", repo]
            if title:
                args += ["--title", title]
            if body:
                args += ["--body", body]
            rc, out, serr = await run_gh(args)
            if gh_err := check_gh_error(rc, serr):
                return gh_err
            url = out.strip()
        if state:
            args = ["pr", "ready", str(number), "--repo", repo] + (["--undo"] if state == "draft" else [])
            rc, out, serr = await run_gh(args)
            if gh_err := check_gh_error(rc, serr):
                return gh_err
            url = url or out.strip()
        return url or f"Edited PR #{number} in {repo}."

    @tool
    async def github_merge_pr(
        number: int,
        repo: str = "",
        method: str = "squash",
        delete_branch: bool = False,
        dry_run: bool = False,
        confirm: bool = False,
    ) -> str:
        """Merge a pull request. IRREVERSIBLE — guarded.

        Args:
            repo: Repository as ``owner/name``. Omit to use the agent's configured default repo.
            number: PR number.
            method: ``squash`` (default), ``merge``, or ``rebase``.
            delete_branch: Delete the head branch after merging (default False).
            dry_run: If true, report what WOULD be merged without merging.
            confirm: Must be true to actually merge — a guard against accidental or
                autonomous merges even when the per-agent write gate is on.

        Returns the merge result, a dry-run preview, or a refusal asking for confirm.
        """
        repo = resolve_repo(repo, default_repo) or ""
        if err := bad_repo(repo):
            return err
        if method not in ("squash", "merge", "rebase"):
            return f"Error: method must be 'squash', 'merge', or 'rebase' (got {method!r})."
        if dry_run:
            extra = " and delete its branch" if delete_branch else ""
            return (
                f"DRY RUN — would merge PR #{number} in {repo} via {method}{extra}. Re-call with confirm=true to do it."
            )
        if not confirm:
            return (
                f"Refusing to merge PR #{number} in {repo} without confirm=true — merging is irreversible. "
                "Re-call with confirm=true, or dry_run=true to preview."
            )
        args = ["pr", "merge", str(number), "--repo", repo, f"--{method}"]
        if delete_branch:
            args.append("--delete-branch")
        rc, out, serr = await run_gh(args)
        if gh_err := check_gh_error(rc, serr):
            return gh_err
        return out.strip() or f"Merged PR #{number} in {repo} via {method}."

    @tool
    async def github_close(
        number: int, repo: str = "", kind: str = "issue", reopen: bool = False, comment: str = ""
    ) -> str:
        """Close (or reopen) an issue or pull request.

        Args:
            repo: Repository as ``owner/name``. Omit to use the agent's configured default repo.
            number: Issue or PR number.
            kind: ``issue`` (default) or ``pr`` — they use different gh subcommands.
            reopen: If true, reopen instead of close.
            comment: Optional comment to post alongside a close (ignored on reopen).

        Returns a short confirmation.
        """
        repo = resolve_repo(repo, default_repo) or ""
        if err := bad_repo(repo):
            return err
        if kind not in ("issue", "pr"):
            return f"Error: kind must be 'issue' or 'pr' (got {kind!r})."
        action = "reopen" if reopen else "close"
        args = [kind, action, str(number), "--repo", repo]
        if comment and not reopen:
            args += ["--comment", comment]
        rc, out, serr = await run_gh(args)
        if gh_err := check_gh_error(rc, serr):
            return gh_err
        return out.strip() or f"{'Reopened' if reopen else 'Closed'} {kind} #{number} in {repo}."

    @tool
    async def github_set_labels(
        number: int, repo: str = "", add: str = "", remove: str = "", kind: str = "issue"
    ) -> str:
        """Add and/or remove labels on an issue or pull request.

        Args:
            repo: Repository as ``owner/name``. Omit to use the agent's configured default repo.
            number: Issue or PR number.
            add: Comma-separated label names to add.
            remove: Comma-separated label names to remove.
            kind: ``issue`` (default) or ``pr``.

        Returns a short confirmation.
        """
        repo = resolve_repo(repo, default_repo) or ""
        if err := bad_repo(repo):
            return err
        if kind not in ("issue", "pr"):
            return f"Error: kind must be 'issue' or 'pr' (got {kind!r})."
        adds, removes = _csv(add), _csv(remove)
        if not adds and not removes:
            return "Error: pass at least one label to add or remove."
        args = [kind, "edit", str(number), "--repo", repo]
        for label in adds:
            args += ["--add-label", label]
        for label in removes:
            args += ["--remove-label", label]
        rc, out, serr = await run_gh(args)
        if gh_err := check_gh_error(rc, serr):
            return gh_err
        return out.strip() or f"Updated labels on {kind} #{number} in {repo}."

    @tool
    async def github_set_assignees(
        number: int, repo: str = "", add: str = "", remove: str = "", kind: str = "issue"
    ) -> str:
        """Add and/or remove assignees on an issue or pull request.

        Args:
            repo: Repository as ``owner/name``. Omit to use the agent's configured default repo.
            number: Issue or PR number.
            add: Comma-separated GitHub usernames to assign.
            remove: Comma-separated GitHub usernames to unassign.
            kind: ``issue`` (default) or ``pr``.

        Returns a short confirmation.
        """
        repo = resolve_repo(repo, default_repo) or ""
        if err := bad_repo(repo):
            return err
        if kind not in ("issue", "pr"):
            return f"Error: kind must be 'issue' or 'pr' (got {kind!r})."
        adds, removes = _csv(add), _csv(remove)
        if not adds and not removes:
            return "Error: pass at least one assignee to add or remove."
        args = [kind, "edit", str(number), "--repo", repo]
        for user in adds:
            args += ["--add-assignee", user]
        for user in removes:
            args += ["--remove-assignee", user]
        rc, out, serr = await run_gh(args)
        if gh_err := check_gh_error(rc, serr):
            return gh_err
        return out.strip() or f"Updated assignees on {kind} #{number} in {repo}."

    return [
        github_create_issue,
        github_comment,
        github_create_pr,
        github_edit_pr,
        github_merge_pr,
        github_close,
        github_set_labels,
        github_set_assignees,
    ]
