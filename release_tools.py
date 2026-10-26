"""GitHub RELEASE tools over `gh` — what an agent needs to cut a tagged release end to end.

A release is a chain of side effects in a fixed order: dispatch a prepare workflow, merge
its PR at a verified head, tag the MERGED commit, watch the tag-triggered workflows, fix
the release's notes, then dispatch a build that attaches artifacts. Each link here is one
small tool, so the agent can verify between them instead of running a script blind.

Read (always on):
  - github_run_jobs      — one Actions run and its jobs (status / conclusion per job): the
    per-platform legs of a build, which `github_ci_runs` (one row per run) can't show.
  - github_release       — a release by tag: draft / prerelease / whether it's Latest, its
    assets (name + size) and the head of its body.
  - github_release_asset — the TEXT of one small release asset (e.g. `release-notes.md`).

Write (only when `github.write: true`, each ``confirm``-guarded like `github_merge_pr`):
  - github_dispatch_workflow   — `gh workflow run` + finds the run it started (id + url).
  - github_create_tag          — an ANNOTATED tag at an exact commit, via the git API. Never
    moves an existing tag, and by default checks the commit's `pyproject.toml` version
    matches the tag before it tags.
  - github_edit_release        — replace a release's notes (body).
  - github_upload_release_asset — upload text as an asset under EXACTLY the given name
    (`gh release upload f#label` would set a LABEL and upload under the file's own name).

Keep tool docstrings PLAIN string literals (an f-string docstring → __doc__ is None →
the tool ships with no description).
"""

from __future__ import annotations

import asyncio
import base64
import os
import re
import tempfile
from datetime import datetime, timedelta, timezone

from langchain_core.tools import tool

from .gh_cli import bad_repo, check_gh_error, dicts, error_kind, parse_json, run_gh
from .gh_issue import resolve_repo

_TAG_RE = re.compile(r"^v?\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_WORKFLOW_RE = re.compile(r"^(?:[\w.-]+\.ya?ml|\d+)$")
_REF_RE = re.compile(r"^[\w./-]{1,200}$")
_INPUT_KEY_RE = re.compile(r"^[A-Za-z_][\w-]{0,99}$")
_ASSET_RE = re.compile(r"^[\w][\w.-]{0,99}$")
_VERSION_RE = re.compile(r'^version\s*=\s*"([^"]+)"', re.M)

_BODY_HEAD = 3000  # chars of a release body shown by github_release
_ASSET_MAX = 200_000  # bytes of a text asset github_release_asset returns
_NOTES_MAX = 125_000  # GitHub's release-body limit
# How long github_dispatch_workflow looks for the run it started (the API lists a
# dispatched run a few seconds after `gh workflow run` returns).
_FIND_RUN_TRIES = 6
_FIND_RUN_DELAY = 3.0


def _bad_tag(tag: str) -> str | None:
    if not _TAG_RE.match(tag or ""):
        return f"Error: {tag!r} is not a release tag (expected vX.Y.Z, e.g. v1.4.0)."
    return None


def _parse_ts(value) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _parse_inputs(inputs: str) -> tuple[list[tuple[str, str]], str | None]:
    """``"bump=minor, dry_run=true"`` (comma or newline separated) → [(k, v), …]."""
    pairs = []
    for raw in re.split(r"[,\n]", inputs or ""):
        item = raw.strip()
        if not item:
            continue
        key, sep, value = item.partition("=")
        key, value = key.strip(), value.strip()
        if not sep or not _INPUT_KEY_RE.match(key):
            return [], f"Error: workflow input {item!r} must be key=value (e.g. bump=minor)."
        pairs.append((key, value))
    return pairs, None


def get_release_read_tools(default_repo="") -> list:
    """The read-only release tools (no write gate)."""

    @tool
    async def github_run_jobs(run_id: int, repo: str = "") -> str:
        """Show one GitHub Actions run and every job in it — status and conclusion per job.

        Args:
            run_id: The run's id (from github_dispatch_workflow or github_ci_runs).
            repo: Repository as ``owner/name``. Omit to use the agent's configured default repo.

        Use it to watch a multi-job workflow (a desktop build's per-platform legs, a
        release's publish steps): the run is done when its status is ``completed``, and
        it succeeded only if its conclusion is ``success``. A failed job's log is one
        github_run_failure call away.
        """
        repo = resolve_repo(repo, default_repo) or ""
        if err := bad_repo(repo):
            return err
        rc, out, serr = await run_gh(["api", f"repos/{repo}/actions/runs/{int(run_id)}"])
        if gh_err := check_gh_error(rc, serr, repo=repo):
            return gh_err
        run, err = parse_json(out)
        if err:
            return err
        rc, out, serr = await run_gh(["api", f"repos/{repo}/actions/runs/{int(run_id)}/jobs?per_page=100"])
        if gh_err := check_gh_error(rc, serr, repo=repo):
            return gh_err
        data, err = parse_json(out)
        if err:
            return err
        jobs = dicts(data.get("jobs"))
        lines = [
            f"Run {run_id} · {run.get('name') or '?'} · {run.get('event') or '?'} · "
            f"status={run.get('status') or '?'} conclusion={run.get('conclusion') or '-'}",
            f"head {str(run.get('head_sha') or '?')[:12]} on {run.get('head_branch') or '?'} · {run.get('html_url') or ''}",
        ]
        if not jobs:
            lines.append("(no jobs yet)")
        for j in jobs:
            lines.append(f"- {j.get('name') or '?'}: {j.get('status') or '?'} / {j.get('conclusion') or '-'}")
        return "\n".join(lines)

    @tool
    async def github_release(tag: str, repo: str = "") -> str:
        """Show a GitHub release by tag: draft / prerelease, whether it is the repo's Latest
        release, its assets (name and size) and the start of its notes.

        Args:
            tag: The release tag, e.g. ``v1.4.0``.
            repo: Repository as ``owner/name``. Omit to use the agent's configured default repo.

        "Latest" is GitHub's promoted release, not the newest tag: a release created
        with latest=false only becomes Latest when something promotes it.
        """
        repo = resolve_repo(repo, default_repo) or ""
        if err := bad_repo(repo) or _bad_tag(tag):
            return err
        rc, out, serr = await run_gh(["api", f"repos/{repo}/releases/tags/{tag}"])
        if gh_err := check_gh_error(rc, serr, repo=repo):
            return gh_err
        rel, err = parse_json(out)
        if err:
            return err
        latest = "?"
        rc, lout, _ = await run_gh(["api", f"repos/{repo}/releases/latest"])
        if rc == 0:
            lrel, lerr = parse_json(lout)
            if not lerr:
                latest = str(lrel.get("tag_name") or "?")
        assets = dicts(rel.get("assets"))
        body = str(rel.get("body") or "")
        lines = [
            f"{rel.get('name') or tag} ({rel.get('tag_name') or tag}) · {rel.get('html_url') or ''}",
            f"draft={bool(rel.get('draft'))} prerelease={bool(rel.get('prerelease'))} "
            f"latest={'yes' if latest == tag else f'no (Latest is {latest})'} "
            f"published={rel.get('published_at') or '-'}",
            f"assets ({len(assets)}):",
        ]
        lines += [f"- {a.get('name') or '?'} ({a.get('size') or 0} bytes)" for a in assets] or ["- (none)"]
        lines.append(f"notes ({len(body)} chars):")
        lines.append(
            body[:_BODY_HEAD] + (f"\n… [{len(body) - _BODY_HEAD} more chars]" if len(body) > _BODY_HEAD else "")
        )
        return "\n".join(lines)

    @tool
    async def github_release_asset(tag: str, name: str, repo: str = "") -> str:
        """Read the text of one small asset attached to a release (e.g. ``release-notes.md``).

        Args:
            tag: The release tag, e.g. ``v1.4.0``.
            name: The asset's exact file name.
            repo: Repository as ``owner/name``. Omit to use the agent's configured default repo.

        Returns the asset's text (up to 200 KB). Binary assets aren't readable here.
        """
        repo = resolve_repo(repo, default_repo) or ""
        if err := bad_repo(repo) or _bad_tag(tag):
            return err
        rc, out, serr = await run_gh(["api", f"repos/{repo}/releases/tags/{tag}"])
        if gh_err := check_gh_error(rc, serr, repo=repo):
            return gh_err
        rel, err = parse_json(out)
        if err:
            return err
        match = next((a for a in dicts(rel.get("assets")) if a.get("name") == name), None)
        if not match:
            names = ", ".join(str(a.get("name")) for a in dicts(rel.get("assets"))) or "none"
            return f"Error: release {tag} has no asset named {name!r} (assets: {names})."
        if int(match.get("size") or 0) > _ASSET_MAX:
            return f"Error: asset {name!r} is {match.get('size')} bytes — too large to read as text here."
        rc, out, serr = await run_gh(
            ["api", f"repos/{repo}/releases/assets/{match.get('id')}", "-H", "Accept: application/octet-stream"]
        )
        if gh_err := check_gh_error(rc, serr, repo=repo):
            return gh_err
        return out

    return [github_run_jobs, github_release, github_release_asset]


def get_release_write_tools(default_repo="") -> list:
    """The release tools that change GitHub — registered only behind `github.write`."""

    @tool
    async def github_dispatch_workflow(
        workflow: str, repo: str = "", inputs: str = "", ref: str = "main", confirm: bool = False
    ) -> str:
        """Start a manually-dispatched (workflow_dispatch) GitHub Actions workflow and return
        the run it started.

        Args:
            workflow: The workflow FILE name (e.g. ``prepare-release.yml``) or its numeric id.
            repo: Repository as ``owner/name``. Omit to use the agent's configured default repo.
            inputs: The workflow's inputs as ``key=value`` pairs, comma or newline separated
                (e.g. ``bump=minor, dry_run=true`` or ``tag=v1.4.0``).
            ref: The branch or tag whose copy of the workflow runs (default ``main``).
            confirm: Must be true — a dispatch has real effects (opens PRs, builds, publishes).

        Returns the new run's id and url — watch it with github_run_jobs.
        """
        repo = resolve_repo(repo, default_repo) or ""
        if err := bad_repo(repo):
            return err
        if not _WORKFLOW_RE.match(workflow or ""):
            return f"Error: {workflow!r} is not a workflow file name (e.g. release.yml) or id."
        if not _REF_RE.match(ref or "") or ref.startswith("-"):
            return f"Error: {ref!r} is not a branch or tag name."
        pairs, err = _parse_inputs(inputs)
        if err:
            return err
        shown = ", ".join(f"{k}={v}" for k, v in pairs) or "no inputs"
        if not confirm:
            return (
                f"Refusing to dispatch {workflow} on {repo}@{ref} ({shown}) without confirm=true — "
                "a dispatch has real effects. Re-call with confirm=true."
            )
        started = datetime.now(timezone.utc) - timedelta(seconds=10)  # runner/API clock slack
        args = ["workflow", "run", workflow, "--repo", repo, "--ref", ref]
        for k, v in pairs:
            args += ["-f", f"{k}={v}"]
        rc, _out, serr = await run_gh(args)
        if gh_err := check_gh_error(rc, serr, repo=repo):
            return gh_err
        for attempt in range(_FIND_RUN_TRIES):
            if attempt:
                await asyncio.sleep(_FIND_RUN_DELAY)
            rc, out, _ = await run_gh(
                ["api", f"repos/{repo}/actions/workflows/{workflow}/runs?event=workflow_dispatch&per_page=10"]
            )
            data, perr = parse_json(out) if rc == 0 else (None, "x")
            if perr or not isinstance(data.get("workflow_runs"), list):
                break  # the listing is unreadable; polling it again won't help
            for run in dicts(data.get("workflow_runs")):
                at = _parse_ts(run.get("created_at"))
                if at and at >= started and run.get("head_branch") == ref:
                    return (
                        f"Dispatched {workflow} on {repo}@{ref} ({shown}): run {run.get('id')} · "
                        f"{run.get('html_url') or ''}. Watch it with github_run_jobs."
                    )
        return (
            f"Dispatched {workflow} on {repo}@{ref} ({shown}), but its run isn't listed yet — "
            "find it with github_ci_runs in a minute (don't dispatch again)."
        )

    @tool
    async def github_create_tag(
        tag: str, sha: str, repo: str = "", message: str = "", verify_version: bool = True, confirm: bool = False
    ) -> str:
        """Create an ANNOTATED tag at an exact commit and push it (via the git API).

        Args:
            tag: The tag to create, e.g. ``v1.4.0``.
            sha: The FULL 40-character commit sha to tag — for a release, the release PR's
                merge commit, not whatever main points at now.
            repo: Repository as ``owner/name``. Omit to use the agent's configured default repo.
            message: The tag message (default ``Release <tag>``).
            verify_version: Check first that ``pyproject.toml`` at that commit declares the
                tag's version (default true; skipped when the repo has no pyproject.toml).
            confirm: Must be true — a pushed tag triggers release workflows and is never moved.

        Refuses when the tag already exists: a published tag is never moved.
        """
        repo = resolve_repo(repo, default_repo) or ""
        if err := bad_repo(repo) or _bad_tag(tag):
            return err
        sha = (sha or "").strip().lower()
        if not _SHA_RE.match(sha):
            return f"Error: sha must be a full 40-character commit sha (got {sha!r})."
        rc, _out, serr = await run_gh(["api", f"repos/{repo}/git/ref/tags/{tag}"])
        if rc == 0:
            return f"Error: tag {tag} already exists in {repo} — a published tag is never moved or recreated."
        if error_kind(rc, serr) != "not_found":
            return check_gh_error(rc, serr, repo=repo) or f"Error: could not check whether {tag} exists."
        note = ""
        if verify_version:
            rc, out, serr = await run_gh(["api", f"repos/{repo}/contents/pyproject.toml?ref={sha}"])
            if rc == 0:
                data, err = parse_json(out)
                if err:
                    return err
                try:
                    text = base64.b64decode(str(data.get("content") or "")).decode("utf-8", "replace")
                except (ValueError, TypeError):
                    return "Error: could not decode pyproject.toml at that commit."
                m = _VERSION_RE.search(text)
                want = tag.lstrip("v")
                if not m:
                    return f"Error: pyproject.toml at {sha[:12]} has no version line — not tagging."
                if m.group(1) != want:
                    return (
                        f"Error: pyproject.toml at {sha[:12]} declares version {m.group(1)}, not {want} — "
                        "that isn't the release commit for this tag. Not tagging."
                    )
                note = f" (pyproject.toml at that commit says {want})"
            elif error_kind(rc, serr) == "not_found":
                note = " (no pyproject.toml at that commit — version not checked)"
            else:
                return check_gh_error(rc, serr, repo=repo) or "Error: could not read pyproject.toml."
        if not confirm:
            return (
                f"Refusing to tag {tag} at {sha[:12]} in {repo}{note} without confirm=true — a pushed tag "
                "triggers release workflows and is never moved. Re-call with confirm=true."
            )
        msg = message.strip() or f"Release {tag}"
        rc, out, serr = await run_gh(
            [
                "api",
                f"repos/{repo}/git/tags",
                "-f",
                f"tag={tag}",
                "-f",
                f"message={msg}",
                "-f",
                f"object={sha}",
                "-f",
                "type=commit",
            ]
        )
        if gh_err := check_gh_error(rc, serr, repo=repo):
            return gh_err
        obj, err = parse_json(out)
        if err:
            return err
        obj_sha = str(obj.get("sha") or "")
        if not _SHA_RE.match(obj_sha):
            return f"Error: GitHub returned no tag object sha: {out[:200]}"
        rc, _out, serr = await run_gh(
            ["api", f"repos/{repo}/git/refs", "-f", f"ref=refs/tags/{tag}", "-f", f"sha={obj_sha}"]
        )
        if gh_err := check_gh_error(rc, serr, repo=repo):
            return gh_err
        return (
            f"Tagged {tag} at {sha[:12]} in {repo}{note} (annotated tag object {obj_sha[:12]}). "
            "Tag-triggered workflows start now — find them with github_ci_runs."
        )

    @tool
    async def github_edit_release(tag: str, notes: str, repo: str = "", confirm: bool = False) -> str:
        """Replace a release's notes (its body on the GitHub release page).

        Args:
            tag: The release tag, e.g. ``v1.4.0``.
            notes: The complete new notes, in Markdown. They REPLACE the old body.
            repo: Repository as ``owner/name``. Omit to use the agent's configured default repo.
            confirm: Must be true — the release page is public.
        """
        repo = resolve_repo(repo, default_repo) or ""
        if err := bad_repo(repo) or _bad_tag(tag):
            return err
        if not (notes or "").strip():
            return "Error: notes are empty — refusing to blank a release's body."
        if len(notes) > _NOTES_MAX:
            return f"Error: notes are {len(notes)} chars; GitHub allows {_NOTES_MAX}."
        if not confirm:
            return f"Refusing to replace the notes of {tag} in {repo} without confirm=true. Re-call with confirm=true."
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "notes.md")
            with open(path, "w", encoding="utf-8") as f:
                f.write(notes)
            rc, _out, serr = await run_gh(["release", "edit", tag, "--repo", repo, "--notes-file", path])
        if gh_err := check_gh_error(rc, serr, repo=repo):
            return gh_err
        return f"Replaced the notes of {tag} in {repo} ({len(notes)} chars)."

    @tool
    async def github_upload_release_asset(
        tag: str, name: str, content: str, repo: str = "", clobber: bool = False, confirm: bool = False
    ) -> str:
        """Upload text as a release asset under exactly ``name`` (e.g. ``release-notes.md``).

        Args:
            tag: The release tag, e.g. ``v1.4.0``.
            name: The asset's file name — it is published under exactly this name.
            content: The asset's text.
            repo: Repository as ``owner/name``. Omit to use the agent's configured default repo.
            clobber: Replace an existing asset of the same name (default false: refuse).
            confirm: Must be true — release assets are public, and some are read by updaters.

        Read it back with github_release_asset to check what landed.
        """
        repo = resolve_repo(repo, default_repo) or ""
        if err := bad_repo(repo) or _bad_tag(tag):
            return err
        if not _ASSET_RE.match(name or "") or name.startswith("."):
            return f"Error: {name!r} is not a plain file name (letters, digits, '.', '_', '-')."
        if not (content or "").strip():
            return "Error: content is empty — refusing to upload an empty asset."
        if not confirm:
            return (
                f"Refusing to upload {name} to {tag} in {repo}{' (replacing the old one)' if clobber else ''} "
                "without confirm=true. Re-call with confirm=true."
            )
        with tempfile.TemporaryDirectory() as d:
            # The file's BASENAME is the asset name: `path#label` would only set a label.
            path = os.path.join(d, name)
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
            args = ["release", "upload", tag, path, "--repo", repo]
            if clobber:
                args.append("--clobber")
            rc, _out, serr = await run_gh(args)
        if gh_err := check_gh_error(rc, serr, repo=repo):
            if "already exists" in (serr or "").lower():
                return f"Error: {tag} already has an asset named {name} — re-call with clobber=true to replace it."
            return gh_err
        return f"Uploaded {name} ({len(content.encode('utf-8'))} bytes) to {tag} in {repo}."

    return [
        github_dispatch_workflow,
        github_create_tag,
        github_edit_release,
        github_upload_release_asset,
    ]
