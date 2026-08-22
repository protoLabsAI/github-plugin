"""HTTP for the GitHub console board — the view PAGE + its gated data routes.

Two routers, the notes-plugin pattern (ADR 0026/0038/0042):
  - the view PAGE under the PUBLIC ``/plugins/github`` prefix (a browser iframe
    page-load can't carry a bearer, so the page itself is public chrome); and
  - the DATA routes under the GATED ``/api/plugins/github`` prefix (the operator
    bearer gate), fetched from inside the loaded page with the postMessage handshake
    token (the DS plugin-kit's ``apiFetch``).

The data logic lives in plain async functions (``fetch_issues`` / ``fetch_prs`` /
``status.compute_status``) so the suite can test it host-free; the FastAPI imports
stay lazy inside the build_* functions (fastapi is the host's at runtime).

``GET /status`` is the first-run probe (v0.6.0): is `gh` installed, authenticated,
as whom — the views render a setup card from it when something's missing, and
``/config`` names a malformed ``default_repo`` (#23) instead of feeding it to `gh`.
"""

from __future__ import annotations

import asyncio
import json

from .gh_cli import bad_repo, check_gh_error, resolve_gh, run_gh

# The JSON fields we ask `gh` for — kept lean: enough for a board row + the detail link.
_ISSUE_FIELDS = "number,title,state,author,labels,url,createdAt,comments"
_PR_FIELDS = "number,title,state,author,labels,url,createdAt,isDraft,headRefName,reviewDecision"


def gh_available() -> bool:
    """Whether the `gh` CLI can be found (PATH, then the usual install dirs) — the
    same resolver the runner uses, so this and a tool can never disagree."""
    return resolve_gh() is not None


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
    if gh_err := check_gh_error(rc, serr, repo=repo):
        return {"error": gh_err}
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
    if gh_err := check_gh_error(rc, serr, repo=repo):
        return {"error": gh_err}
    try:
        return {"items": json.loads(out or "[]")}
    except json.JSONDecodeError:
        return {"error": f"Error: could not parse gh output: {out[:200]}"}


def _repos(cfg: dict) -> list[str]:
    """The picker list — the explicit ``github.repos`` entries (first, operator order)
    UNION the host's ADR 0095 managed-projects registry (v0.115.0+) UNION the repos
    parsed from the registered checkouts' ``origin`` remotes (last). ``[]`` with no host
    and nothing configured. See projects.py."""
    from .projects import effective_repos

    return effective_repos(cfg.get("repos"))


def resolve_config(cfg: dict) -> dict:
    """The resolved picker/default for a config dict — ONE computation shared by
    ``/config``, ``/status`` and the issue route, so they can't disagree:
    ``{"repos": [...], "default_repo": "...", "default_repo_error": str|None}``.

    A malformed ``github.default_repo`` (not ``owner/name``, #23) is NOT fed into the
    picker or used as the default — it's reported as the named ``default_repo_error``
    and the default falls through to the first good picker entry (the views render
    the error so the person fixes the field rather than seeing a raw `gh` failure).
    """
    from .gh_issue import default_repo_error, effective_default_repo

    raw_default = str(cfg.get("default_repo") or "").strip()
    err = default_repo_error(raw_default)
    repos = _repos(cfg)
    default = effective_default_repo("" if err else raw_default, repos)
    # The picker is built from `repos`. A very common config sets `default_repo` but
    # leaves `repos` empty — without folding the default in, the picker has zero
    # options and the board shows "nothing to select" even though a repo IS configured.
    # Surface the resolved default as a selectable option (first), deduped.
    selectable = [default, *repos] if default and default not in repos else repos
    return {"repos": selectable, "default_repo": default, "default_repo_error": err}


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


def build_data_router(cfg, registry=None):
    """The board's DATA routes — mounted under the GATED ``/api/plugins/github`` prefix.

    ``cfg`` is either the config dict OR a zero-arg callable returning it. Pass a callable
    (e.g. ``registry.live_config``) so the board reflects config edits WITHOUT a server
    restart: a hot-reload can't re-mount this router, but reading the config per request
    picks up the freshly-saved repos/default_repo. A plain dict (tests, older host) is a
    fixed snapshot. ``/issue`` reuses the SAME gate-checked `file_issue` path as the
    `/issue` chat command, so the dialog and the command can never diverge.

    ``registry`` (optional) is the host registry: ``/status`` reports its result to the
    ``report_setup_gap`` seam through it, so the operator banner clears on the very
    Re-check that sees `gh` installed / signed in. ``None`` ⇒ status only.

    The picker/default resolution may parse git remotes (blocking, cached) — every
    route runs it in a worker thread, never on the event loop.
    """
    from fastapi import APIRouter, Body

    from .gh_issue import IssueRequest, file_issue, labels_for, resolve_repo
    from .status import compute_and_report

    get_cfg = cfg if callable(cfg) else (lambda: cfg)

    async def _resolved() -> dict:
        current = get_cfg() or {}
        return await asyncio.to_thread(resolve_config, current)

    router = APIRouter()

    @router.get("/config")
    async def _config() -> dict:
        resolved = await _resolved()
        return {**resolved, "gh_available": gh_available()}

    @router.get("/status")
    async def _status() -> dict:
        """The first-run probe: `gh` path + version, auth state (login/host), the token
        source, and the resolved repos — never raises (a failure is ``error``). Reports
        to the host's setup-gap seam (clears on recovery) when a registry was given."""
        resolved = await _resolved()
        st = await compute_and_report(registry, resolved["default_repo"], resolved["repos"])
        st["default_repo_error"] = resolved["default_repo_error"]
        return st

    @router.get("/issues")
    async def _issues(repo: str, state: str = "open") -> dict:
        return await fetch_issues(repo, state)

    @router.get("/prs")
    async def _prs(repo: str, state: str = "open") -> dict:
        return await fetch_prs(repo, state)

    @router.post("/issue")
    async def _create_issue(body: dict = Body(...)) -> dict:
        resolved = await _resolved()
        kind = (body.get("kind") or "generic").lower()
        if kind not in ("bug", "feature", "generic"):
            kind = "generic"
        title = (body.get("title") or "").strip()
        issue_body = (body.get("body") or "").strip()
        explicit = str(body.get("repo") or "").strip()
        repo = resolve_repo(explicit, resolved["default_repo"])
        labels = labels_for(kind, [str(x) for x in (body.get("labels") or [])])
        dry_run = bool(body.get("dry_run"))
        if not title:
            return {"ok": False, "error": "Title is required."}
        if not repo:
            # No usable repo anywhere — and if the configured default is the reason
            # (malformed, #23), say THAT, by name, rather than "set one".
            if resolved["default_repo_error"]:
                return {"ok": False, "error": resolved["default_repo_error"]}
            return {"ok": False, "error": "No target repo — set one in Settings ▸ GitHub, or pick one."}
        if bad_repo(repo):
            return {"ok": False, "error": f"Repo must be 'owner/name' (got {repo!r})."}
        return await file_issue(
            IssueRequest(title=title, body=issue_body, kind=kind, repo=repo, labels=labels, dry_run=dry_run)
        )

    return router
