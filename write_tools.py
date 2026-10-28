"""GitHub WRITE tools over `gh` — GATED. Registered ONLY when `github.write: true`.

These mutate GitHub, so they're behind the per-agent write gate (see __init__.py).
Each validates its repo with `bad_repo()`, runs via `run_gh()`, and degrades to a
readable `Error: ...` string.

The set:
  - github_create_issue  — `gh issue create` (returns the new issue URL). Runs the SAME
    body gate the user-only `/issue` command enforces (gh_issue.missing_sections): a
    body without a Problem section (+ repro for a bug, a direction/acceptance for a
    feature) is refused with the scaffold, never posted.
  - github_comment       — `gh issue comment` (works for PRs too — a PR is an issue).
  - github_create_pr     — `gh pr create` (returns the new PR URL).
  - github_edit_pr       — `gh pr edit` (+ `gh pr ready`) to change title/body/draft.
  - github_edit_issue    — `gh issue edit` to change an issue's title and/or body
    (refuses when neither is given, never posting an empty edit).
  - github_merge_pr      — `gh pr merge`. IRREVERSIBLE, so it refuses without an
    explicit ``confirm=true`` and offers a ``dry_run`` preview (defence-in-depth on
    top of the per-agent write gate).
  - github_close         — close/reopen an issue or PR (`gh {issue,pr} close|reopen`).
  - github_set_labels    — add/remove labels (`gh {issue,pr} edit --add/--remove-label`).
  - github_create_label  — `gh label create` a new label (never `--force`; an existing
    label is reported as such, not overwritten).
  - github_set_assignees — add/remove assignees (`gh {issue,pr} edit --add/--remove-assignee`).
  - github_reply_thread  — reply INSIDE a PR review thread (GraphQL
    addPullRequestReviewThreadReply), optionally resolving it in the same call.
  - github_resolve_thread — resolve / unresolve a PR review thread (GraphQL
    resolveReviewThread / unresolveReviewThread). Unresolved threads keep a PR's QA check
    from clearing; ``github_review_threads`` lists them with their ids.

Issue-vs-PR matters for close/labels/assignees (different `gh` subcommands), so those
take a ``kind`` ("issue" | "pr"); commenting does not (a PR is an issue for comments).

Keep tool docstrings PLAIN string literals (an f-string docstring → __doc__ is None →
the tool ships with no description).
"""

from __future__ import annotations

import re

from langchain_core.tools import tool

from .gh_cli import bad_repo, check_gh_error, parse_json, run_gh
from .gh_issue import infer_kind, labels_for, missing_sections, resolve_repo, scaffold_for


def _csv(value: str) -> list[str]:
    """Split a comma-separated arg into cleaned, non-empty tokens (order-preserving)."""
    return [tok.strip() for tok in (value or "").split(",") if tok.strip()]


def _pr_number(url: str) -> str:
    """The PR number from a ``…/pull/<n>`` URL, or "" when it isn't one."""
    tail = (url or "").rstrip("/").rsplit("/", 1)[-1]
    return tail if tail.isdigit() else ""


_THREAD_ID_RE = re.compile(r"^PRRT_[A-Za-z0-9_-]{6,}$")


def _bad_thread_id(thread_id: str) -> str | None:
    """A review-thread node id is ``PRRT_…`` — refuse anything else before ``gh`` runs
    (a comment id or a PR number here is the common mistake)."""
    if not _THREAD_ID_RE.match(str(thread_id or "").strip()):
        return (
            f"Error: {thread_id!r} is not a review-thread id (they look like PRRT_…). "
            "List them with github_review_threads."
        )
    return None


async def _thread_mutation(mutation: str, thread_id: str, body: str = "") -> tuple[dict | None, str | None]:
    """Run one review-thread GraphQL mutation; returns (data, error string)."""
    if body:
        q = (
            "mutation($id:ID!,$body:String!){addPullRequestReviewThreadReply(input:"
            "{pullRequestReviewThreadId:$id,body:$body}){comment{url}}}"
        )
        args = ["api", "graphql", "-f", f"query={q}", "-f", f"id={thread_id}", "-f", f"body={body}"]
    else:
        q = f"mutation($id:ID!){{{mutation}(input:{{threadId:$id}}){{thread{{id isResolved}}}}}}"
        args = ["api", "graphql", "-f", f"query={q}", "-f", f"id={thread_id}"]
    rc, out, serr = await run_gh(args)
    if gh_err := check_gh_error(rc, serr):
        return None, gh_err
    d, perr = parse_json(out, dict)
    if perr:
        return None, perr
    if d.get("errors"):
        msg = "; ".join(str(e.get("message") or e) for e in d["errors"] if isinstance(e, dict)) or str(d["errors"])
        return None, f"Error: GitHub refused the review-thread change: {msg}"
    return d.get("data") or {}, None


def get_write_tools(default_repo="", emit=None) -> list:
    """Build the write tools. ``default_repo`` (``owner/name``, or a zero-arg getter
    returning it — the live-config case) is used whenever a tool's ``repo`` arg is
    omitted, so an agent with one configured repo needn't repeat it.

    ``emit`` is the host's namespaced event-bus seam (ADR 0039; ``registry.emit``). When
    given, PR lifecycle events are broadcast as ``github.pr.opened`` / ``github.pr.merged``
    so consumers (e.g. a fleet activity feed) can surface them without polling. Optional
    and fully guarded — an older host, or a bus error, never fails a tool."""

    def _emit(topic: str, data: dict) -> None:
        if not emit:
            return
        try:
            emit(topic, data)
        except Exception:  # noqa: BLE001 — telemetry must never break a write tool
            pass

    @tool
    async def github_create_issue(
        title: str, repo: str = "", body: str = "", labels: str = "", kind: str = "generic"
    ) -> str:
        """Create an issue; returns its URL. Search first (github_search_issues) so you don't file a duplicate. The body is GATED like /issue; a failing body is NOT posted (you get what's missing + a scaffold): at least 80 chars with a `## Problem` (or Motivation/Context) section; kind=bug also needs `## Steps to reproduce` / Evidence / Expected-vs-actual; kind=feature needs `## Proposed direction` or `## Acceptance`. kind also adds the type label (bug / enhancement); a generic call labelled bug / enhancement is gated as that kind. labels: comma-separated."""
        repo = resolve_repo(repo, default_repo) or ""
        if err := bad_repo(repo):
            return err
        kind = (kind or "generic").strip().lower()
        if kind not in ("bug", "feature", "generic"):
            return f"Error: kind must be 'bug', 'feature' or 'generic' (got {kind!r})."
        kind = infer_kind(kind, _csv(labels))
        if miss := missing_sections(body or "", kind):
            return (
                "Not filed — the issue body is missing " + "; ".join(miss) + ". "
                "Add the section(s) and call again. Scaffold for a "
                f"{kind} issue:\n```\n{scaffold_for(kind)}```"
            )
        args = ["issue", "create", "--repo", repo, "--title", title, "--body", body]
        for label in labels_for(kind, _csv(labels)):
            args += ["--label", label]
        rc, out, serr = await run_gh(args)
        if gh_err := check_gh_error(rc, serr, repo=repo):
            return gh_err
        return out.strip()

    @tool
    async def github_comment(number: int, body: str, repo: str = "") -> str:
        """Comment on an issue or PR (Markdown); returns the comment URL."""
        repo = resolve_repo(repo, default_repo) or ""
        if err := bad_repo(repo):
            return err
        args = ["issue", "comment", str(number), "--repo", repo, "--body", body]
        rc, out, serr = await run_gh(args)
        if gh_err := check_gh_error(rc, serr, repo=repo):
            return gh_err
        return out.strip()

    @tool
    async def github_create_pr(head: str, title: str, repo: str = "", body: str = "", base: str = "main") -> str:
        """Open a PR from head into base; returns its URL."""
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
        if gh_err := check_gh_error(rc, serr, repo=repo):
            return gh_err
        url = out.strip()
        _emit(
            "pr.opened",
            {"repo": repo, "number": _pr_number(url), "title": title, "url": url, "head": head, "base": base},
        )
        return url

    @tool
    async def github_edit_pr(number: int, repo: str = "", title: str = "", body: str = "", state: str = "") -> str:
        """Edit a PR's title / body (omit = unchanged) and/or draft state: state=ready marks it ready for review, draft converts it back, empty leaves it."""
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
            if gh_err := check_gh_error(rc, serr, repo=repo):
                return gh_err
            url = out.strip()
        if state:
            args = ["pr", "ready", str(number), "--repo", repo] + (["--undo"] if state == "draft" else [])
            rc, out, serr = await run_gh(args)
            if gh_err := check_gh_error(rc, serr, repo=repo):
                return gh_err
            url = url or out.strip()
        return url or f"Edited PR #{number} in {repo}."

    @tool
    async def github_edit_issue(number: int, repo: str = "", title: str = "", body: str = "") -> str:
        """Edit an issue's title and/or body (omit = unchanged; at least one is required). Returns the issue URL."""
        repo = resolve_repo(repo, default_repo) or ""
        if err := bad_repo(repo):
            return err
        if not (title or body):
            return "Error: nothing to change — pass title and/or body."
        args = ["issue", "edit", str(number), "--repo", repo]
        if title:
            args += ["--title", title]
        if body:
            args += ["--body", body]
        rc, out, serr = await run_gh(args)
        if gh_err := check_gh_error(rc, serr, repo=repo):
            return gh_err
        return out.strip() or f"Edited issue #{number} in {repo}."

    @tool
    async def github_merge_pr(
        number: int,
        repo: str = "",
        method: str = "squash",
        delete_branch: bool = False,
        subject: str = "",
        body: str = "",
        sha: str = "",
        dry_run: bool = False,
        confirm: bool = False,
    ) -> str:
        """Merge a PR. IRREVERSIBLE: refused unless confirm=true (even with the write gate on); dry_run=true previews instead. method: squash | merge | rebase. subject / body set the merge commit (squash/merge only, refused on rebase); a single-commit squash lands that COMMIT's subject, not the PR title, unless subject is set. sha: the FULL head sha you verified; the merge then happens only if the head is still that commit (can't combine with delete_branch)."""
        repo = resolve_repo(repo, default_repo) or ""
        if err := bad_repo(repo):
            return err
        if method not in ("squash", "merge", "rebase"):
            return f"Error: method must be 'squash', 'merge', or 'rebase' (got {method!r})."
        if method == "rebase" and (subject or body):
            return "Error: subject/body only apply to squash or merge — GitHub ignores them on a rebase merge."
        sha = (sha or "").strip().lower()
        if sha and not re.fullmatch(r"[0-9a-f]{40}", sha):
            return f"Error: sha must be the full 40-character head commit sha (got {sha!r})."
        if sha and delete_branch:
            return "Error: sha and delete_branch can't be combined — merge pinned, then delete the branch separately."
        if dry_run:
            extra = " and delete its branch" if delete_branch else ""
            detail = ""
            if subject:
                detail += f' with subject "{subject}"'
            if body:
                detail += f'{" and" if subject else " with"} body "{body}"'
            return (
                f"DRY RUN — would merge PR #{number} in {repo} via {method}{extra}{detail}. "
                "Re-call with confirm=true to do it."
            )
        if not confirm:
            return (
                f"Refusing to merge PR #{number} in {repo} without confirm=true — merging is irreversible. "
                "Re-call with confirm=true, or dry_run=true to preview."
            )
        if sha:
            # The REST merge endpoint takes the expected head and refuses (409) when the
            # branch moved — `gh pr merge` has no equivalent we can trust not to hang.
            args = ["api", "-X", "PUT", f"repos/{repo}/pulls/{number}/merge", "-f", f"sha={sha}"]
            args += ["-f", f"merge_method={method}"]
            if subject:
                args += ["-f", f"commit_title={subject}"]
            if body:
                args += ["-f", f"commit_message={body}"]
            rc, out, serr = await run_gh(args)
            if rc and ("409" in (serr or "") or "head branch was modified" in (serr or "").lower()):
                return (
                    f"Not merged: PR #{number}'s head is no longer {sha[:12]} — something was pushed after your "
                    "check. Re-verify the new head (reviews and checks) before merging it."
                )
            if gh_err := check_gh_error(rc, serr, repo=repo):
                return gh_err
            data, err = parse_json(out)
            if err:
                return err
            if not data.get("merged"):
                return f"Not merged: {data.get('message') or out[:200]}"
            _emit("pr.merged", {"repo": repo, "number": str(number), "method": method})
            return f"Merged PR #{number} in {repo} via {method} at head {sha[:12]} → merge commit {data.get('sha')}."
        args = ["pr", "merge", str(number), "--repo", repo, f"--{method}"]
        if delete_branch:
            args.append("--delete-branch")
        if subject:
            args += ["--subject", subject]
        if body:
            args += ["--body", body]
        rc, out, serr = await run_gh(args)
        if gh_err := check_gh_error(rc, serr, repo=repo):
            return gh_err
        _emit("pr.merged", {"repo": repo, "number": str(number), "method": method})
        return out.strip() or f"Merged PR #{number} in {repo} via {method}."

    @tool
    async def github_close(
        number: int, repo: str = "", kind: str = "issue", reopen: bool = False, comment: str = ""
    ) -> str:
        """Close an issue or PR, or reopen it (reopen=true). kind: issue | pr. comment is posted with a close (ignored on reopen)."""
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
        if gh_err := check_gh_error(rc, serr, repo=repo):
            return gh_err
        return out.strip() or f"{'Reopened' if reopen else 'Closed'} {kind} #{number} in {repo}."

    @tool
    async def github_set_labels(
        number: int, repo: str = "", add: str = "", remove: str = "", kind: str = "issue"
    ) -> str:
        """Add and/or remove labels (comma-separated) on an issue or PR (kind: issue | pr). A label that doesn't exist yet: github_create_label first."""
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
        if gh_err := check_gh_error(rc, serr, repo=repo):
            return gh_err
        return out.strip() or f"Updated labels on {kind} #{number} in {repo}."

    @tool
    async def github_create_label(name: str, repo: str = "", color: str = "", description: str = "") -> str:
        """Create a repo label (color: 6-char hex without #; random if omitted). Never overwrites: an existing label is reported and left untouched."""
        repo = resolve_repo(repo, default_repo) or ""
        if err := bad_repo(repo):
            return err
        if not (name or "").strip():
            return "Error: label name is required."
        args = ["label", "create", name, "--repo", repo]
        if color:
            args += ["--color", color]
        if description:
            args += ["--description", description]
        rc, out, serr = await run_gh(args)
        low = (serr or "").lower()
        if rc != 0 and ("already exists" in low or "already_exists" in low):
            return f"Label {name!r} already exists in {repo} — not overwritten (github_create_label never forces)."
        if gh_err := check_gh_error(rc, serr, repo=repo):
            return gh_err
        return out.strip() or f"Created label {name!r} in {repo}."

    @tool
    async def github_set_assignees(
        number: int, repo: str = "", add: str = "", remove: str = "", kind: str = "issue"
    ) -> str:
        """Add and/or remove assignees (comma-separated usernames) on an issue or PR (kind: issue | pr)."""
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
        if gh_err := check_gh_error(rc, serr, repo=repo):
            return gh_err
        return out.strip() or f"Updated assignees on {kind} #{number} in {repo}."

    @tool
    async def github_reply_thread(thread_id: str, body: str, resolve: bool = False) -> str:
        """Reply INSIDE a PR review thread (thread_id PRRT_… from github_review_threads); resolve=true also resolves it. After a fix round, answer each finding where it was raised (what changed, where); a PR-level github_comment does NOT answer or clear a thread."""
        if err := _bad_thread_id(thread_id):
            return err
        if not (body or "").strip():
            return "Error: body is empty — say what you changed (or why no change is needed)."
        data, err = await _thread_mutation("", thread_id.strip(), body)
        if err:
            return err
        url = (((data or {}).get("addPullRequestReviewThreadReply") or {}).get("comment") or {}).get("url") or ""
        if not resolve:
            return url or "Replied."
        _, err = await _thread_mutation("resolveReviewThread", thread_id.strip())
        if err:
            return f"Replied ({url}), but resolving failed: {err}"
        return f"Replied ({url}) and resolved the thread."

    @tool
    async def github_resolve_thread(thread_id: str, unresolve: bool = False) -> str:
        """Resolve (or unresolve=true) a PR review thread (id from github_review_threads). Unresolved threads block the QA check and auto-merge; resolve only once the finding is fixed or answered, preferably via github_reply_thread with resolve=true."""
        if err := _bad_thread_id(thread_id):
            return err
        mutation = "unresolveReviewThread" if unresolve else "resolveReviewThread"
        data, err = await _thread_mutation(mutation, thread_id.strip())
        if err:
            return err
        thread = ((data or {}).get(mutation) or {}).get("thread") or {}
        state = "resolved" if thread.get("isResolved") else "unresolved"
        return f"Thread {thread_id.strip()} is now {state}."

    return [
        github_create_issue,
        github_comment,
        github_create_pr,
        github_edit_pr,
        github_edit_issue,
        github_merge_pr,
        github_close,
        github_set_labels,
        github_create_label,
        github_set_assignees,
        github_reply_thread,
        github_resolve_thread,
    ]
