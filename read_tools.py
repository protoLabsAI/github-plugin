"""GitHub READ tools over `gh` — always registered (read-only is the safe default).

Fifteen tools, all implemented: six ported from protoAgent's tools/github_tools.py
(PRs, issues, diffs, CI), the repo-content readers (`github_read_file`,
`github_read_pr_file`, `github_repo_contents`, `github_path_exists`), `github_pr_diff`,
`github_status` (the self-diagnosis probe — is `gh` installed / authenticated), and the
PM verbs (v0.7.0): `github_list_prs` (the PR board: draft / review decision / merge
state), `github_issue_comments` (the thread on an issue or PR), and
`github_search_issues` (dedupe BEFORE filing). `github_get_pr` carries the merge
readiness picture — review decision, mergeability, a checks summary, the reviews.
Each tool takes an `owner/name` repo — or falls back to the configured default when
it's omitted (a LIVE getter, so a Settings edit or an onboarded project is seen by the
next call) — and degrades to a readable, classified `Error: ...` string (not
authenticated / repo not found / rate-limited / `gh` missing) instead of raising.
"""

from __future__ import annotations

import asyncio
import re

from langchain_core.tools import tool

from .gh_cli import bad_repo, check_gh_error, dicts, error_kind, parse_json, run_gh
from .gh_issue import current_default, default_repo_error, resolve_repo

# Error-relevant lines to surface from a failed CI log (github_run_failure).
_CI_ERR_RE = re.compile(
    r"(error|fail|✕|✗|×|not ok|exit code|command not found|exception|traceback|"
    r"assertion|timeout|expected .* to|cannot |refused|unauthorized|forbidden|panic|fatal)",
    re.IGNORECASE,
)


_MAX_PR_CHARS = 12000  # github_get_pr's total output bound
_MAX_REVIEWS = 10
_MAX_COMMENT_CHARS = 1000

# statusCheckRollup carries two shapes (verified against gh 2.92): a CheckRun
# {name, status: COMPLETED|IN_PROGRESS|QUEUED|…, conclusion: SUCCESS|FAILURE|SKIPPED|
# CANCELLED|NEUTRAL|TIMED_OUT|ACTION_REQUIRED|""} and a StatusContext {context,
# state: SUCCESS|PENDING|FAILURE|ERROR|EXPECTED}.
_CHECK_FAIL = {"FAILURE", "ERROR", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE"}
_CHECK_SKIP = {"SKIPPED", "NEUTRAL", "STALE"}


def _login(actor) -> str:
    """``author.login`` from a gh actor object — ``?`` for a missing/null/scalar actor."""
    return str(actor.get("login") or "?") if isinstance(actor, dict) else "?"


def _one_line(text: str, cap: int) -> str:
    """Collapse whitespace and cap — for a review/comment excerpt on one line."""
    t = " ".join((text or "").split())
    return t if len(t) <= cap else t[: cap - 1] + "…"


def _bounded(text: str, cap: int) -> str:
    return text if len(text) <= cap else text[:cap] + f"\n… (truncated at {cap} chars)"


def _summarize_checks(rollup) -> str:
    """``N pass / N fail / N pending (/ N skipped) — failing: a, b`` from a PR's
    statusCheckRollup; ``none`` when the commit has no checks."""
    items = dicts(rollup)
    if not items:
        return "none"
    n_pass = n_fail = n_pending = n_skip = 0
    failing: list[str] = []
    for c in items:
        name = str(c.get("name") or c.get("context") or "?")
        if c.get("__typename") == "StatusContext" or ("state" in c and "status" not in c):
            state = str(c.get("state") or "").upper()
            if state == "SUCCESS":
                n_pass += 1
            elif state in _CHECK_FAIL:
                n_fail += 1
                failing.append(name)
            else:  # PENDING / EXPECTED / unknown
                n_pending += 1
            continue
        status = str(c.get("status") or "").upper()
        conclusion = str(c.get("conclusion") or "").upper()
        if status and status != "COMPLETED":
            n_pending += 1
        elif conclusion == "SUCCESS":
            n_pass += 1
        elif conclusion in _CHECK_FAIL:
            n_fail += 1
            failing.append(name)
        elif conclusion in _CHECK_SKIP:
            n_skip += 1
        else:
            n_pending += 1
    parts = f"{n_pass} pass / {n_fail} fail / {n_pending} pending" + (f" / {n_skip} skipped" if n_skip else "")
    if failing:
        parts += " — failing: " + ", ".join(failing[:10]) + (" …" if len(failing) > 10 else "")
    return parts


def get_read_tools(default_repo="", repos=None, registry=None) -> list:
    """Build the read tools. ``default_repo`` (``owner/name``, or a zero-arg getter
    returning it — the live-config case) is used whenever a tool's ``repo`` arg is
    omitted, so an agent with one configured repo needn't repeat it. ``repos`` (a list
    or a getter) is the picker list, surfaced by ``github_status`` only — which also
    reports its result to the host's setup-gap seam through ``registry`` (optional)
    so the operator banner clears when the model's own check sees a recovery."""

    def _repos() -> list[str]:
        try:
            return list((repos() if callable(repos) else repos) or [])
        except Exception:  # noqa: BLE001
            return []

    @tool
    async def github_get_pr(number: int, repo: str = "") -> str:
        """Fetch a GitHub pull request — the merge-readiness picture in one call: title,
        state, draft?, author, branch, review decision, mergeability + merge state, a
        checks summary (N pass / N fail / N pending + the failing check names), the
        reviews (author, verdict, excerpt), changed files, and the body.

        Args:
            repo: Repository as ``owner/name``. Omit to use the agent's configured default repo.
            number: PR number.
        """
        repo = resolve_repo(repo, default_repo) or ""
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
                "number,title,state,isDraft,author,body,additions,deletions,files,url,headRefName,baseRefName,"
                "reviewDecision,mergeable,mergeStateStatus,statusCheckRollup,reviews,latestReviews",
            ]
        )
        if gh_err := check_gh_error(rc, serr, repo=repo):
            return gh_err
        d, perr = parse_json(out, dict)
        if perr:
            return perr
        files = ", ".join(str(f.get("path", "?")) for f in dicts(d.get("files"))[:20])
        draft = " (DRAFT)" if d.get("isDraft") else ""
        checks = _summarize_checks(d.get("statusCheckRollup"))
        reviews = dicts(d.get("reviews"))
        # `latestReviews` = ONE review per reviewer, their most recent — the set that
        # actually decides reviewDecision, so a dozen bot COMMENTED reviews can never
        # push a human's CHANGES_REQUESTED out of view. Older gh without the field (or
        # a PR with none) falls back to the NEWEST `reviews`, never the oldest.
        latest = dicts(d.get("latestReviews")) or reviews[-_MAX_REVIEWS:]
        review_lines = [
            f"  - {_login(r.get('author'))} "
            f"[{r.get('state') or '?'}]"
            + (f": {_one_line(str(r.get('body') or ''), 300)}" if str(r.get("body") or "").strip() else "")
            for r in latest[-_MAX_REVIEWS:]
        ]
        if len(reviews) > len(review_lines):
            review_lines.append(f"  … {len(reviews) - len(review_lines)} more review(s) (older / superseded)")
        text = (
            f"PR #{d.get('number')} [{d.get('state')}]{draft} {d.get('title')}\n"
            f"branch: {d.get('headRefName', '?')} -> {d.get('baseRefName', '?')}\n"
            f"by {_login(d.get('author'))} | "
            f"+{d.get('additions', 0)}/-{d.get('deletions', 0)} | {d.get('url')}\n"
            f"review decision: {d.get('reviewDecision') or 'none yet'} | "
            f"mergeable: {d.get('mergeable') or '?'} | merge state: {d.get('mergeStateStatus') or '?'}\n"
            f"checks: {checks}\n"
            f"reviews ({len(reviews)}):" + ("\n" + "\n".join(review_lines) if review_lines else " none") + "\n"
            f"files: {files or '(none)'}\n\n{(d.get('body') or '').strip()[:2000]}"
        )
        return _bounded(text, _MAX_PR_CHARS)

    @tool
    async def github_get_issue(number: int, repo: str = "") -> str:
        """Fetch a GitHub issue: title, state, author, labels, and body.

        Args:
            repo: Repository as ``owner/name``. Omit to use the agent's configured default repo.
            number: Issue number.
        """
        repo = resolve_repo(repo, default_repo) or ""
        if err := bad_repo(repo):
            return err
        rc, out, serr = await run_gh(
            ["issue", "view", str(number), "--repo", repo, "--json", "number,title,state,author,labels,body,url"]
        )
        if gh_err := check_gh_error(rc, serr, repo=repo):
            return gh_err
        d, perr = parse_json(out, dict)
        if perr:
            return perr
        labels = ", ".join(str(lbl.get("name", "")) for lbl in dicts(d.get("labels")))
        return (
            f"Issue #{d.get('number')} [{d.get('state')}] {d.get('title')}\n"
            f"by {_login(d.get('author'))} | labels: {labels or '(none)'} | "
            f"{d.get('url')}\n\n{(d.get('body') or '').strip()[:2000]}"
        )

    @tool
    async def github_list_issues(repo: str = "", state: str = "open", limit: int = 20) -> str:
        """List GitHub issues for a repo.

        Args:
            repo: Repository as ``owner/name``. Omit to use the agent's configured default repo.
            state: ``open`` | ``closed`` | ``all`` (default ``open``).
            limit: Max issues to return (1-50, default 20).
        """
        repo = resolve_repo(repo, default_repo) or ""
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
        if gh_err := check_gh_error(rc, serr, repo=repo):
            return gh_err
        items, perr = parse_json(out, list)
        if perr:
            return perr
        items = dicts(items)
        if not items:
            return f"No {state} issues in {repo}."
        lines = [f"{len(items)} {state} issue(s) in {repo}:"]
        for it in items:
            labels = ",".join(str(lbl.get("name", "")) for lbl in dicts(it.get("labels")))
            lines.append(
                f"  #{it.get('number')} [{it.get('state')}] {it.get('title')}" + (f"  ({labels})" if labels else "")
            )
        return "\n".join(lines)

    @tool
    async def github_get_commit_diff(ref: str, repo: str = "", max_chars: int = 8000) -> str:
        """Fetch a commit's metadata + unified diff.

        Args:
            repo: Repository as ``owner/name``. Omit to use the agent's configured default repo.
            ref: Commit SHA (or ref) to inspect.
            max_chars: Truncate the diff at this many characters (default 8000).
        """
        repo = resolve_repo(repo, default_repo) or ""
        if err := bad_repo(repo):
            return err
        rc, out, serr = await run_gh(
            ["api", f"repos/{repo}/commits/{ref}", "-H", "Accept: application/vnd.github.diff"]
        )
        if gh_err := check_gh_error(rc, serr, repo=repo):
            return gh_err
        diff = out.strip()
        if not diff:
            return f"No diff for {repo}@{ref} (empty or merge commit)."
        if len(diff) > max_chars:
            diff = diff[:max_chars] + f"\n… (truncated at {max_chars} chars)"
        return f"Commit {repo}@{ref}:\n\n{diff}"

    @tool
    async def github_pr_diff(number: int, repo: str = "", max_chars: int = 12000) -> str:
        """Fetch a pull request's full unified diff — for reviewing the change itself.

        Args:
            repo: Repository as ``owner/name``. Omit to use the agent's configured default repo.
            number: PR number.
            max_chars: Truncate the diff at this many characters (default 12000).
        """
        repo = resolve_repo(repo, default_repo) or ""
        if err := bad_repo(repo):
            return err
        rc, out, serr = await run_gh(["pr", "diff", str(number), "--repo", repo])
        if gh_err := check_gh_error(rc, serr, repo=repo):
            return gh_err
        diff = out.strip()
        if not diff:
            return f"No diff for {repo}#{number} (empty PR or diff unavailable)."
        if len(diff) > max_chars:
            diff = diff[:max_chars] + f"\n… (truncated at {max_chars} chars)"
        return f"PR {repo}#{number} diff:\n\n{diff}"

    @tool
    async def github_ci_runs(repo: str = "", branch: str = "", limit: int = 15) -> str:
        """List recent GitHub Actions runs for a repo — for CI triage.

        Args:
            repo: Repository as ``owner/name``. Omit to use the agent's configured default repo.
            branch: Optional branch filter (e.g. ``main``).
            limit: Max runs to return (capped at 50).

        Feed a failing run's id to ``github_run_failure`` to see why it failed.
        """
        repo = resolve_repo(repo, default_repo) or ""
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
        if gh_err := check_gh_error(rc, serr, repo=repo):
            return gh_err
        runs, perr = parse_json(out, list)
        if perr:
            return perr
        runs = dicts(runs)
        if not runs:
            return f"No recent runs for {repo}" + (f" on {branch}" if branch.strip() else "")
        lines = [
            f"#{r.get('databaseId')} [{r.get('conclusion') or r.get('status')}] "
            f"{r.get('name')} ({r.get('headBranch')} · {r.get('event')}) — {r.get('url')}"
            for r in runs
        ]
        return f"{repo} — {len(runs)} recent run(s):\n" + "\n".join(lines)

    @tool
    async def github_run_failure(run_id: int, repo: str = "", max_lines: int = 40) -> str:
        """Explain why a GitHub Actions run failed — the error lines from its failed steps.

        Args:
            repo: Repository as ``owner/name``. Omit to use the agent's configured default repo.
            run_id: The run id (``databaseId`` from ``github_ci_runs``).
            max_lines: Cap on error lines returned (capped at 80).
        """
        repo = resolve_repo(repo, default_repo) or ""
        if err := bad_repo(repo):
            return err
        cap = max(5, min(int(max_lines), 80))
        rc, out, serr = await run_gh(["run", "view", str(run_id), "--repo", repo, "--log-failed"], timeout=60)
        if gh_err := check_gh_error(rc, serr, repo=repo):
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

    # ── Repo-content readers — what lets an agent research ANY repo over `gh` without
    # registering an fs project per repo. ───────────────────────────────────────────
    @tool
    async def github_read_file(path: str, repo: str = "", ref: str = "") -> str:
        """Read a single file's raw contents from a GitHub repo (capped at 20000 chars).

        Args:
            repo: Repository as ``owner/name``. Omit to use the agent's configured default repo.
            path: Path to the file within the repo (e.g. ``docs/guide.md``). For a directory
                use ``github_repo_contents``; for a file as it is IN a PR use ``github_read_pr_file``.
            ref: Optional branch / tag / SHA (default: the repo's default branch).
        """
        repo = resolve_repo(repo, default_repo) or ""
        if err := bad_repo(repo):
            return err
        args = ["api", f"repos/{repo}/contents/{path}", "-H", "Accept: application/vnd.github.raw+json"]
        if ref.strip():
            args += ["-f", f"ref={ref}"]
        rc, out, serr = await run_gh(args)
        if gh_err := check_gh_error(rc, serr, repo=repo):
            return gh_err
        if len(out) > 20000:
            out = out[:20000] + "\n… (truncated at 20000 chars)"
        return out

    @tool
    async def github_read_pr_file(number: int, path: str, repo: str = "") -> str:
        """Read a file as it exists IN a pull request — at the PR's head commit.

        Use this for every code-context read while reviewing a PR. Unlike
        ``github_read_file``, there is no ref to get wrong: the head SHA is resolved
        server-side from the PR number, so the file you get is the PR's version,
        including anything the PR adds.

        Args:
            number: PR number.
            repo: Repository as ``owner/name``. Omit to use the agent's configured default repo.
            path: Path to the file within the repo (e.g. ``src/lib/queries.ts``).
        """
        repo = resolve_repo(repo, default_repo) or ""
        if err := bad_repo(repo):
            return err
        # The ref is resolved HERE, from the PR — never supplied by the caller. A
        # model that omits (or mistypes) a ref on a plain read silently gets the
        # DEFAULT branch, i.e. the pre-PR file: it then "confirms" that symbols the
        # PR adds don't exist, which reads as a blocker finding on correct code.
        rc, out, serr = await run_gh(["api", f"repos/{repo}/pulls/{number}", "--jq", ".head.sha"])
        if gh_err := check_gh_error(rc, serr, repo=repo):
            return gh_err
        head = out.strip()
        if not head:
            return f"Error: could not resolve the head SHA for {repo}#{number}."
        rc, out, serr = await run_gh(
            [
                "api",
                f"repos/{repo}/contents/{path}",
                "-H",
                "Accept: application/vnd.github.raw+json",
                "-f",
                f"ref={head}",
            ]
        )
        if gh_err := check_gh_error(rc, serr, repo=repo):
            # Fail LOUD, never fall back to the default branch: a silent fallback is
            # exactly the bug this tool exists to prevent.
            return f"Error reading {path} at {repo}#{number} head {head[:12]}: {gh_err}"
        if len(out) > 20000:
            out = out[:20000] + "\n… (truncated at 20000 chars)"
        return f"{path} @ {repo}#{number} head {head[:12]}:\n\n{out}"

    @tool
    async def github_path_exists(path: str, repo: str = "", ref: str = "") -> str:
        """Check whether a path exists in a GitHub repo — the grounding probe for
        review claims about external references.

        A diff is often only correct if something OUTSIDE it exists (a cross-repo
        `COPY --from` path, a workspace package, an action ref). The answer is
        authoritative: EXISTS means the assumption holds (don't invent a problem
        around it); MISSING means the dependency is really absent (that's the
        finding). A tool error means UNVERIFIED — report a Gap, never a severity.

        Args:
            repo: Repository as ``owner/name`` — may be a DIFFERENT repo than the
                PR under review (that is the point). Omit for the default repo.
            path: Repo-relative path to check (file or directory).
            ref: Optional branch / tag / SHA (default: the repo's default branch).
        """
        repo = resolve_repo(repo, default_repo) or ""
        if err := bad_repo(repo):
            return err
        if not path.strip():
            return "Error: `path` is empty."
        clean = path.strip().strip("/")
        args = ["api", f"repos/{repo}/contents/{clean}"]
        if ref.strip():
            args += ["-f", f"ref={ref.strip()}"]
        rc, out, serr = await run_gh(args)
        if rc == 0:
            return f"EXISTS: {repo}/{clean}" + (f" @ {ref.strip()}" if ref.strip() else "")
        # Classify FIRST: an auth / rate-limit / missing-binary failure is UNVERIFIED
        # (the classified error), never a MISSING verdict. Only a real 404 is MISSING —
        # and a 404 can also mean the repo itself is inaccessible to this token.
        kind = error_kind(rc, serr or out)
        if kind == "not_found":
            return (
                f"MISSING: {repo}/{clean}"
                + (f" @ {ref.strip()}" if ref.strip() else "")
                + " — the path does not exist (or the repo is inaccessible to this token; HTTP 404)."
            )
        return (
            check_gh_error(rc, serr or out, repo=repo)
            or f"Error (gh exit {rc}): could not verify {repo}/{clean} — treat as UNVERIFIED (a Gap, not a finding)."
        )

    @tool
    async def github_repo_contents(repo: str = "", path: str = "", ref: str = "") -> str:
        """List the contents (files + dirs) of a path in a GitHub repo.

        Args:
            repo: Repository as ``owner/name``. Omit to use the agent's configured default repo.
            path: Directory path within the repo (default: repo root).
            ref: Optional branch / tag / SHA (default: the repo's default branch).
        """
        repo = resolve_repo(repo, default_repo) or ""
        if err := bad_repo(repo):
            return err
        args = ["api", f"repos/{repo}/contents/{path}" if path else f"repos/{repo}/contents"]
        if ref.strip():
            args += ["-f", f"ref={ref}"]
        rc, out, serr = await run_gh(args)
        if gh_err := check_gh_error(rc, serr, repo=repo):
            return gh_err
        items, perr = parse_json(out, (dict, list))
        if perr:
            return perr
        # The contents API returns a LIST for a directory but a single OBJECT for a
        # file (or symlink/submodule). Iterating the object raised AttributeError
        # through the tool layer and killed the whole agent turn — say what it is.
        if isinstance(items, dict):
            kind = items.get("type") or "file"
            return f"Error: '{path or '.'}' is a {kind}, not a directory — use github_read_file to read it."
        items = dicts(items)
        if not items:
            return f"No contents in {repo}/{path or '.'}."
        type_map = {"file": "FILE", "dir": "DIR ", "symlink": "LINK", "submodule": "SUB "}
        lines = [f"{repo}/{path or '.'} — {len(items)} item(s):"]
        for entry in items:
            etype = entry.get("type", "")
            t = type_map.get(etype, (etype or "?").upper())[:4]
            size = "0" if etype == "dir" else str(entry.get("size", 0))
            lines.append(f"{t:4s} {size:>8s}  {entry.get('name', '?')}  ({entry.get('path', '')})")
        return "\n".join(lines)

    @tool
    async def github_list_prs(repo: str = "", state: str = "open", limit: int = 30) -> str:
        """List pull requests — the PR board: number, title, author, state, draft?, branch,
        review decision and merge state per row. Use ``github_get_pr`` for one PR's full picture.

        Args:
            repo: Repository as ``owner/name``. Omit to use the agent's configured default repo.
            state: ``open`` (default) | ``closed`` | ``merged`` | ``all``.
            limit: Max PRs to return (1-100, default 30).
        """
        from .api import fetch_prs

        repo = resolve_repo(repo, default_repo) or ""
        if err := bad_repo(repo):
            return err
        try:
            capped = max(1, min(int(limit), 100))
        except (TypeError, ValueError):
            capped = 30
        res = await fetch_prs(repo, state, capped)
        if res.get("error"):
            return str(res["error"])
        items = dicts(res.get("items"))
        if not items:
            return f"No {state} pull requests in {repo}."
        lines = [f"{len(items)} {state} pull request(s) in {repo}:"]
        for it in items:
            author = _login(it.get("author"))
            flags = [
                x
                for x in (("draft" if it.get("isDraft") else ""), it.get("reviewDecision"), it.get("mergeStateStatus"))
                if x
            ]
            lines.append(
                f"  #{it.get('number')} [{it.get('state')}] {it.get('title')} — {author} | "
                f"{it.get('headRefName', '?')} -> {it.get('baseRefName', '?')}"
                + (f" | {', '.join(str(f) for f in flags)}" if flags else "")
                + f" | {it.get('url')}"
            )
        return "\n".join(lines)

    @tool
    async def github_issue_comments(number: int, repo: str = "", limit: int = 30) -> str:
        """Read the comment thread on an issue or pull request (a PR is an issue for
        comments): author, date and body per comment, newest ``limit`` in chronological
        order. Use it to catch up on a discussion before replying or deciding.

        Args:
            repo: Repository as ``owner/name``. Omit to use the agent's configured default repo.
            number: Issue or PR number.
            limit: Max comments to return (1-100, default 30 — the most recent ones).
        """
        repo = resolve_repo(repo, default_repo) or ""
        if err := bad_repo(repo):
            return err
        try:
            capped = max(1, min(int(limit), 100))
        except (TypeError, ValueError):
            capped = 30
        rc, out, serr = await run_gh(["issue", "view", str(number), "--repo", repo, "--json", "comments"])
        if gh_err := check_gh_error(rc, serr, repo=repo):
            return gh_err
        d, perr = parse_json(out, dict)
        if perr:
            return perr
        comments = dicts(d.get("comments"))
        if not comments:
            return f"No comments on {repo}#{number}."
        shown = comments[-capped:]
        head = f"{len(comments)} comment(s) on {repo}#{number}" + (
            f" — showing the last {len(shown)}" if len(shown) < len(comments) else ""
        )
        lines = [head + ":"]
        for c in shown:
            author = _login(c.get("author"))
            body = " ".join(str(c.get("body") or "").split())
            if len(body) > _MAX_COMMENT_CHARS:
                body = body[: _MAX_COMMENT_CHARS - 1] + "…"
            lines.append(f"--- {author} · {c.get('createdAt') or '?'}\n{body or '(empty)'}")
        return _bounded("\n".join(lines), _MAX_PR_CHARS)

    @tool
    async def github_search_issues(query: str, repo: str = "", state: str = "open", limit: int = 20) -> str:
        """Search a repo's issues by text — DEDUPE BEFORE FILING: call this with the
        gist of a problem before ``github_create_issue`` and reference or reopen a
        match instead of filing a duplicate. Returns number, state, title and URL per hit.

        Args:
            query: Free-text search (GitHub issue search syntax; e.g. ``"default_repo" label:bug``).
            repo: Repository as ``owner/name``. Omit to use the agent's configured default repo.
            state: ``open`` (default) | ``closed`` | ``all``.
            limit: Max results (1-100, default 20).
        """
        repo = resolve_repo(repo, default_repo) or ""
        if err := bad_repo(repo):
            return err
        if not (query or "").strip():
            return "Error: `query` is empty — say what you're looking for."
        if state not in ("open", "closed", "all"):
            return f"Error: state must be open|closed|all (got {state!r})."
        try:
            capped = max(1, min(int(limit), 100))
        except (TypeError, ValueError):
            capped = 20
        # Flags first, the query LAST after `--`: a query starting with a qualifier
        # like `-label:bug` is otherwise parsed as an unknown shorthand flag (verified
        # live against gh 2.92 — with `--` it works).
        args = ["search", "issues", "--repo", repo, "--limit", str(capped), "--json", "number,title,state,url"]
        if state != "all":  # gh's --state is open|closed only; "all" = no filter
            args += ["--state", state]
        args += ["--", query.strip()]
        rc, out, serr = await run_gh(args)
        if gh_err := check_gh_error(rc, serr, repo=repo):
            return gh_err
        items, perr = parse_json(out or "[]", list)
        if perr:
            return perr
        items = dicts(items)
        if not items:
            return f"No {state} issues in {repo} match {query.strip()!r} — nothing to dedupe against."
        lines = [f"{len(items)} {state} issue(s) in {repo} matching {query.strip()!r}:"]
        for it in items:
            lines.append(f"  #{it.get('number')} [{it.get('state')}] {it.get('title')} — {it.get('url')}")
        return "\n".join(lines)

    @tool
    async def github_status() -> str:
        """Check whether the GitHub CLI is installed and authenticated, and which repo the
        other github_* tools default to. Call this FIRST when any github_* tool returns an
        authentication / not-found / CLI error, or before GitHub work on a fresh machine —
        it says exactly what's wrong and what the operator must do (install `gh`, run
        `gh auth login`, or paste a token in Settings ▸ GitHub). Takes no arguments.
        """
        from .status import compute_and_report, summarize_status

        # The getters may parse git remotes (blocking, cached) — off the event loop.
        default, picker = await asyncio.to_thread(lambda: (current_default(default_repo), _repos()))
        st = await compute_and_report(registry, default, picker)
        text = summarize_status(st)
        if bad := default_repo_error(default):
            text += f" NOTE: {bad}"
        return text

    return [
        github_get_pr,
        github_get_issue,
        github_list_issues,
        github_get_commit_diff,
        github_pr_diff,
        github_path_exists,
        github_ci_runs,
        github_run_failure,
        github_read_file,
        github_read_pr_file,
        github_repo_contents,
        github_list_prs,
        github_issue_comments,
        github_search_issues,
        github_status,
    ]
