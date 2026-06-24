"""HTTP for the GitHub console board — the view PAGE + its gated data routes.

Two routers, the notes-plugin pattern (ADR 0026/0038/0042):
  - the view PAGE under the PUBLIC ``/plugins/github`` prefix (a browser iframe
    page-load can't carry a bearer, so the page itself is public chrome); and
  - the DATA routes under the GATED ``/api/plugins/github`` prefix (the operator
    bearer gate), fetched from inside the loaded page with the postMessage handshake
    token (the DS plugin-kit's ``apiFetch``).

The data logic lives in plain async functions (``fetch_issues`` / ``fetch_prs``) so
the suite can test it host-free; the FastAPI imports stay lazy inside the build_*
functions (fastapi is the host's at runtime).
"""

from __future__ import annotations

import json
import shutil

from .gh_cli import bad_repo, run_gh

# The JSON fields we ask `gh` for — kept lean: enough for a board row + the detail link.
_ISSUE_FIELDS = "number,title,state,author,labels,url,createdAt,comments"
_PR_FIELDS = "number,title,state,author,labels,url,createdAt,isDraft,headRefName,reviewDecision"


def gh_available() -> bool:
    """Whether the `gh` CLI is on PATH (the board shows a hint when it isn't)."""
    return shutil.which("gh") is not None


def _norm_state(state: str) -> str | None:
    """Normalise the state filter to what `gh` accepts, or None if invalid."""
    s = (state or "open").strip().lower()
    return s if s in ("open", "closed", "merged", "all") else None


async def fetch_issues(repo: str, state: str = "open", limit: int = 30) -> dict:
    """List issues for ``repo`` as ``{"items": [...]}`` (or ``{"error": "..."}``).

    Each item is the raw `gh issue list --json` row (number/title/state/author/
    labels/url/createdAt/comments). PRs are excluded — `gh issue list` already omits them.
    """
    if err := bad_repo(repo):
        return {"error": err}
    norm = _norm_state(state)
    if norm is None:
        return {"error": f"Error: state must be open|closed|all (got {state!r})."}
    capped = max(1, min(int(limit), 100))
    rc, out, serr = await run_gh(
        ["issue", "list", "--repo", repo, "--state", norm, "--limit", str(capped), "--json", _ISSUE_FIELDS]
    )
    if rc != 0:
        return {"error": f"Error (gh exit {rc}): {serr[:300]}"}
    try:
        return {"items": json.loads(out or "[]")}
    except json.JSONDecodeError:
        return {"error": f"Error: could not parse gh output: {out[:200]}"}


async def fetch_prs(repo: str, state: str = "open", limit: int = 30) -> dict:
    """List pull requests for ``repo`` as ``{"items": [...]}`` (or ``{"error": "..."}``).

    Each item is the raw `gh pr list --json` row (adds isDraft/headRefName/reviewDecision).
    """
    if err := bad_repo(repo):
        return {"error": err}
    norm = _norm_state(state)
    if norm is None:
        return {"error": f"Error: state must be open|closed|merged|all (got {state!r})."}
    capped = max(1, min(int(limit), 100))
    rc, out, serr = await run_gh(
        ["pr", "list", "--repo", repo, "--state", norm, "--limit", str(capped), "--json", _PR_FIELDS]
    )
    if rc != 0:
        return {"error": f"Error (gh exit {rc}): {serr[:300]}"}
    try:
        return {"items": json.loads(out or "[]")}
    except json.JSONDecodeError:
        return {"error": f"Error: could not parse gh output: {out[:200]}"}


def _repos(cfg: dict) -> list[str]:
    return [str(r).strip() for r in (cfg.get("repos") or []) if str(r).strip()]


def build_view_router():
    """The PAGES — served under the PUBLIC ``/plugins/github`` prefix (ungated): the
    read-only board (``/view``) and the compact file-an-issue form (``/new-issue``)."""
    from fastapi import APIRouter
    from fastapi.responses import HTMLResponse

    from .view import NEW_ISSUE_PAGE, PAGE

    router = APIRouter()

    @router.get("/view")
    async def _view():
        return HTMLResponse(PAGE)

    @router.get("/new-issue")
    async def _new_issue():
        return HTMLResponse(NEW_ISSUE_PAGE)

    return router


def build_data_router(cfg: dict):
    """The board's DATA routes — mounted under the GATED ``/api/plugins/github`` prefix.

    Reads the plugin's own configured repo picker (no host coupling). ``/issue`` reuses
    the SAME gate-checked `file_issue` path as the `/issue` chat command, so the dialog
    and the command can never diverge.
    """
    from fastapi import APIRouter, Body

    from .gh_issue import IssueRequest, effective_default_repo, file_issue, labels_for, resolve_repo

    router = APIRouter()

    @router.get("/config")
    async def _config() -> dict:
        repos = _repos(cfg)
        return {
            "repos": repos,
            "default_repo": effective_default_repo(cfg.get("default_repo", ""), repos),
            "gh_available": gh_available(),
        }

    @router.get("/issues")
    async def _issues(repo: str, state: str = "open") -> dict:
        return await fetch_issues(repo, state)

    @router.get("/prs")
    async def _prs(repo: str, state: str = "open") -> dict:
        return await fetch_prs(repo, state)

    @router.post("/issue")
    async def _create_issue(body: dict = Body(...)) -> dict:
        kind = (body.get("kind") or "generic").lower()
        if kind not in ("bug", "feature", "generic"):
            kind = "generic"
        title = (body.get("title") or "").strip()
        issue_body = (body.get("body") or "").strip()
        repo = resolve_repo(body.get("repo"), effective_default_repo(cfg.get("default_repo", ""), _repos(cfg)))
        labels = labels_for(kind, [str(x) for x in (body.get("labels") or [])])
        dry_run = bool(body.get("dry_run"))
        if not title:
            return {"ok": False, "error": "Title is required."}
        if not repo:
            return {"ok": False, "error": "No target repo — set one in Settings ▸ GitHub, or pick one."}
        if bad_repo(repo):
            return {"ok": False, "error": f"Repo must be 'owner/name' (got {repo!r})."}
        return await file_issue(
            IssueRequest(title=title, body=issue_body, kind=kind, repo=repo, labels=labels, dry_run=dry_run)
        )

    return router
