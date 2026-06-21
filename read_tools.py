"""GitHub READ tools over `gh` — always registered (read-only is the safe default).

Six are ported from protoAgent's tools/github_tools.py (PRs, issues, diffs, CI). Two
new ones (`github_read_file`, `github_repo_contents`) are STUBBED — the team builds
them out (see the TODOs). Each tool requires an explicit `owner/name` repo and
degrades to a readable `Error: ...` string when `gh`/auth is unavailable.
"""

from __future__ import annotations

import json
import re

from langchain_core.tools import tool

from .gh_cli import bad_repo, check_gh_error, run_gh

# Error-relevant lines to surface from a failed CI log (github_run_failure).
_CI_ERR_RE = re.compile(
    r"(error|fail|✕|✗|×|not ok|exit code|command not found|exception|traceback|"
    r"assertion|timeout|expected .* to|cannot |refused|unauthorized|forbidden|panic|fatal)",
    re.IGNORECASE,
)


def get_read_tools() -> list:
    @tool
    async def github_get_pr(repo: str, number: int) -> str:
        """Fetch a GitHub pull request: title, state, author, body, and changed files.

        Args:
            repo: Repository as ``owner/name`` (required, no default).
            number: PR number.
        """
        if err := bad_repo(repo):
            return err
        rc, out, serr = await run_gh(
            [
                "pr",
                "view",
                str(number),
                "--repo",
                repo,
                "--json",
                "number,title,state,author,body,additions,deletions,files,url",
            ]
        )
        if gh_err := check_gh_error(rc, serr):
            return gh_err
        try:
            d = json.loads(out)
        except json.JSONDecodeError:
            return f"Error: could not parse gh output: {out[:200]}"
        files = ", ".join(f.get("path", "?") for f in (d.get("files") or [])[:20])
        return (
            f"PR #{d.get('number')} [{d.get('state')}] {d.get('title')}\n"
            f"by {(d.get('author') or {}).get('login', '?')} | "
            f"+{d.get('additions', 0)}/-{d.get('deletions', 0)} | {d.get('url')}\n"
            f"files: {files or '(none)'}\n\n{(d.get('body') or '').strip()[:2000]}"
        )

    @tool
    async def github_get_issue(repo: str, number: int) -> str:
        """Fetch a GitHub issue: title, state, author, labels, and body.

        Args:
            repo: Repository as ``owner/name`` (required, no default).
            number: Issue number.
        """
        if err := bad_repo(repo):
            return err
        rc, out, serr = await run_gh(
            ["issue", "view", str(number), "--repo", repo, "--json", "number,title,state,author,labels,body,url"]
        )
        if gh_err := check_gh_error(rc, serr):
            return gh_err
        try:
            d = json.loads(out)
        except json.JSONDecodeError:
            return f"Error: could not parse gh output: {out[:200]}"
        labels = ", ".join(lbl.get("name", "") for lbl in (d.get("labels") or []))
        return (
            f"Issue #{d.get('number')} [{d.get('state')}] {d.get('title')}\n"
            f"by {(d.get('author') or {}).get('login', '?')} | labels: {labels or '(none)'} | "
            f"{d.get('url')}\n\n{(d.get('body') or '').strip()[:2000]}"
        )

    @tool
    async def github_list_issues(repo: str, state: str = "open", limit: int = 20) -> str:
        """List GitHub issues for a repo.

        Args:
            repo: Repository as ``owner/name`` (required, no default).
            state: ``open`` | ``closed`` | ``all`` (default ``open``).
            limit: Max issues to return (1-50, default 20).
        """
        if err := bad_repo(repo):
            return err
        if state not in ("open", "closed", "all"):
            return f"Error: state must be open|closed|all (got {state!r})."
        limit = max(1, min(int(limit), 50))
        rc, out, serr = await run_gh(
            [
                "issue",
                "list",
                "--repo",
                repo,
                "--state",
                state,
                "--limit",
                str(limit),
                "--json",
                "number,title,state,labels",
            ]
        )
        if gh_err := check_gh_error(rc, serr):
            return gh_err
        try:
            items = json.loads(out)
        except json.JSONDecodeError:
            return f"Error: could not parse gh output: {out[:200]}"
        if not items:
            return f"No {state} issues in {repo}."
        lines = [f"{len(items)} {state} issue(s) in {repo}:"]
        for it in items:
            labels = ",".join(lbl.get("name", "") for lbl in (it.get("labels") or []))
            lines.append(
                f"  #{it.get('number')} [{it.get('state')}] {it.get('title')}" + (f"  ({labels})" if labels else "")
            )
        return "\n".join(lines)

    @tool
    async def github_get_commit_diff(repo: str, ref: str, max_chars: int = 8000) -> str:
        """Fetch a commit's metadata + unified diff.

        Args:
            repo: Repository as ``owner/name`` (required, no default).
            ref: Commit SHA (or ref) to inspect.
            max_chars: Truncate the diff at this many characters (default 8000).
        """
        if err := bad_repo(repo):
            return err
        rc, out, serr = await run_gh(
            ["api", f"repos/{repo}/commits/{ref}", "-H", "Accept: application/vnd.github.diff"]
        )
        if gh_err := check_gh_error(rc, serr):
            return gh_err
        diff = out.strip()
        if not diff:
            return f"No diff for {repo}@{ref} (empty or merge commit)."
        if len(diff) > max_chars:
            diff = diff[:max_chars] + f"\n… (truncated at {max_chars} chars)"
        return f"Commit {repo}@{ref}:\n\n{diff}"

    @tool
    async def github_ci_runs(repo: str, branch: str = "", limit: int = 15) -> str:
        """List recent GitHub Actions runs for a repo — for CI triage.

        Args:
            repo: Repository as ``owner/name`` (required, no default).
            branch: Optional branch filter (e.g. ``main``).
            limit: Max runs to return (capped at 50).

        Feed a failing run's id to ``github_run_failure`` to see why it failed.
        """
        if err := bad_repo(repo):
            return err
        args = [
            "run",
            "list",
            "--repo",
            repo,
            "--limit",
            str(max(1, min(int(limit), 50))),
            "--json",
            "databaseId,name,status,conclusion,headBranch,event,createdAt,url",
        ]
        if branch.strip():
            args += ["--branch", branch.strip()]
        rc, out, serr = await run_gh(args)
        if gh_err := check_gh_error(rc, serr):
            return gh_err
        try:
            runs = json.loads(out)
        except json.JSONDecodeError:
            return f"Error: could not parse gh output: {out[:200]}"
        if not runs:
            return f"No recent runs for {repo}" + (f" on {branch}" if branch.strip() else "")
        lines = [
            f"#{r.get('databaseId')} [{r.get('conclusion') or r.get('status')}] "
            f"{r.get('name')} ({r.get('headBranch')} · {r.get('event')}) — {r.get('url')}"
            for r in runs
        ]
        return f"{repo} — {len(runs)} recent run(s):\n" + "\n".join(lines)

    @tool
    async def github_run_failure(repo: str, run_id: int, max_lines: int = 40) -> str:
        """Explain why a GitHub Actions run failed — the error lines from its failed steps.

        Args:
            repo: Repository as ``owner/name`` (required, no default).
            run_id: The run id (``databaseId`` from ``github_ci_runs``).
            max_lines: Cap on error lines returned (capped at 80).
        """
        if err := bad_repo(repo):
            return err
        cap = max(5, min(int(max_lines), 80))
        rc, out, serr = await run_gh(["run", "view", str(run_id), "--repo", repo, "--log-failed"], timeout=60)
        if gh_err := check_gh_error(rc, serr):
            return gh_err
        raw = [ln.rstrip() for ln in out.splitlines() if ln.strip()]
        seen: set = set()
        uniq: list[str] = []
        for ln in raw:
            msg = ln.split("\t")[-1]
            if _CI_ERR_RE.search(msg):
                key = msg[:120]
                if key not in seen:
                    seen.add(key)
                    uniq.append(msg[:200])
        picked = uniq[-cap:] if uniq else [ln.split("\t")[-1][:200] for ln in raw[-cap:]]
        if not picked:
            return f"Run {run_id} in {repo}: no failed-step log lines (run may not have failed, or its logs expired)."
        return f"{repo} run {run_id} — failure log ({len(picked)} line(s)):\n" + "\n".join(picked)

    # ── NEW read tools — STUBBED. Build these out (this is what lets an agent research
    # any repo over `gh` without registering an fs project per repo). ────────────────
    @tool
    async def github_read_file(repo: str, path: str, ref: str = "") -> str:
        """Read a single file's contents from a GitHub repo.

        Args:
            repo: Repository as ``owner/name`` (required, no default).
            path: Path to the file within the repo (e.g. ``docs/guide.md``).
            ref: Optional branch / tag / SHA (default: the repo's default branch).

        TODO(team): implement via `gh api repos/{repo}/contents/{path}?ref={ref}` with
        `Accept: application/vnd.github.raw` (returns the raw file body), or
        `gh api .../contents/... --jq .content | base64 -d`. Validate repo with
        bad_repo(); cap the returned size; return a readable Error on failure.
        """
        if err := bad_repo(repo):
            return err
        args = ["api", f"repos/{repo}/contents/{path}", "-H", "Accept: application/vnd.github.raw+json"]
        if ref.strip():
            args += ["-f", f"ref={ref}"]
        rc, out, serr = await run_gh(args)
        if gh_err := check_gh_error(rc, serr):
            return gh_err
        if len(out) > 20000:
            out = out[:20000] + "\n… (truncated at 20000 chars)"
        return out

    @tool
    async def github_repo_contents(repo: str, path: str = "", ref: str = "") -> str:
        """List the contents (files + dirs) of a path in a GitHub repo.

        Args:
            repo: Repository as ``owner/name`` (required, no default).
            path: Directory path within the repo (default: repo root).
            ref: Optional branch / tag / SHA (default: the repo's default branch).

        TODO(team): implement via `gh api repos/{repo}/contents/{path}?ref={ref}` —
        returns a JSON array of {name, path, type, size}. Format a compact listing;
        validate repo; return a readable Error on failure.
        """
        if err := bad_repo(repo):
            return err
        return "Error: github_repo_contents is not implemented yet (stub — to be built by the team)."

    return [
        github_get_pr,
        github_get_issue,
        github_list_issues,
        github_get_commit_diff,
        github_ci_runs,
        github_run_failure,
        github_read_file,
        github_repo_contents,
    ]
