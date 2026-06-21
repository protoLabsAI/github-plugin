"""GitHub WRITE tools over `gh` — GATED. Registered ONLY when `github.write: true`.

These mutate GitHub, so they're behind the per-agent write gate (see __init__.py).
All are STUBBED — the team builds them out (see the TODOs). When implemented, each
must validate its repo with `bad_repo()`, run via `run_gh()`, and degrade to a
readable `Error: ...` string.

Build guidance:
  - github_create_issue → `gh issue create --repo {repo} --title ... --body ...`
    (return the new issue URL).
  - github_comment       → `gh issue comment {number} --repo {repo} --body ...`
    (works for PRs too — a PR is an issue). Take an `is_pr`/number and post a comment.
  - github_create_pr     → `gh pr create --repo {repo} --head {branch} --base {base}
    --title ... --body ...` (return the new PR URL). Note: the projectBoard loop opens
    its own PRs; this tool is for ad-hoc PRs the agent raises directly.

Keep tool docstrings PLAIN string literals (an f-string docstring → __doc__ is None →
the tool ships with no description).
"""

from __future__ import annotations

from langchain_core.tools import tool

from .gh_cli import bad_repo, check_gh_error, run_gh


def get_write_tools() -> list:
    @tool
    async def github_create_issue(repo: str, title: str, body: str = "", labels: str = "") -> str:
        """Create a GitHub issue.

        Args:
            repo: Repository as ``owner/name`` (required, no default).
            title: Issue title.
            body: Issue body (Markdown).
            labels: Optional comma-separated label names.

        Returns the new issue URL.
        """
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
    async def github_comment(repo: str, number: int, body: str) -> str:
        """Add a comment to a GitHub issue or pull request.

        Args:
            repo: Repository as ``owner/name`` (required, no default).
            number: Issue or PR number (a PR is an issue for commenting).
            body: The comment body (Markdown).

        Returns the new comment URL.
        """
        if err := bad_repo(repo):
            return err
        args = ["issue", "comment", str(number), "--repo", repo, "--body", body]
        rc, out, serr = await run_gh(args)
        if gh_err := check_gh_error(rc, serr):
            return gh_err
        return out.strip()

    @tool
    async def github_create_pr(repo: str, head: str, title: str, body: str = "", base: str = "main") -> str:
        """Open a pull request.

        Args:
            repo: Repository as ``owner/name`` (required, no default).
            head: The branch with the changes.
            title: PR title.
            body: PR body (Markdown).
            base: Base branch to merge into (default ``main``).

        Returns the new PR URL.
        """
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

    return [github_create_issue, github_comment, github_create_pr]
