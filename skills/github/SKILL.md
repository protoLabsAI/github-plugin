---
name: github
description: >-
  Use when doing GitHub work through the github_* tools — reviewing a PR, answering
  review threads after a fix round, triaging CI, filing or deduping issues, merging, or
  cutting a release (dispatch → watch → tag → notes → assets). The tool descriptions
  carry the call-time rules; this is the workflow manual behind them.
---

# GitHub — workflows for the github_* tools

**Repo argument.** Every tool's `repo` is `owner/name`; omit it to use the agent's
configured default (`github_status` names it). Any auth / not-found / "gh missing" error:
call `github_status` first — it says exactly what the operator must fix.

## Reviewing a PR

1. `github_get_pr` — merge-readiness: review decision, merge state, checks summary
   (failing names), the latest review per reviewer, changed files.
2. `github_pr_diff` for the change; `github_read_pr_file` for every code-context read
   (the PR's head version — no ref to get wrong). Use `github_read_file` only for a
   file on a branch / tag / SHA outside the PR.
3. Claims about things OUTSIDE the diff (a cross-repo `COPY --from` path, a workspace
   package, an action ref): ground them with `github_path_exists`, possibly in another
   repo. EXISTS = don't invent a problem; MISSING = the finding; tool error = a Gap.
4. Verdict: `github_review_comment` is always allowed. `github_review_approve` /
   `github_review_request_changes` are refused while checks run, when CI is unreadable,
   and on your own PR — fall back to a comment review when refused.

**Paging big files.** Reads are capped at 20000 chars, cut at a line boundary, with a
footer: `… [truncated: showed lines 1-540 of 870 (20000-char cap); continue with
start_line=541]`, `… [showed lines A-B of N; end of file]` for a full range. Continue
with the `start_line` it names. A 404 on a file read lists the nearest existing parent
directory — fix the path from that listing rather than guessing siblings.

## After a fix round

`github_review_threads` → for each finding, `github_reply_thread` (what changed and
where) with `resolve=true`. Unresolved threads keep the QA check red and block
auto-merge; a PR-level `github_comment` does not clear them.

## CI triage

`github_ci_runs` (optionally by branch) → `github_run_failure <databaseId>` for the
failing steps' error lines; `github_run_jobs` for per-job status of one run.

## Filing issues

`github_search_issues` with the gist first; reference or reopen a match. Then
`github_create_issue`; the body gate (same as `/issue`) wants `## Problem` (or
Motivation/Context), plus `## Steps to reproduce` / Evidence / Expected-vs-actual for a
bug, or `## Proposed direction` / `## Acceptance` for a feature. A refused body comes
back with a scaffold — fill it in and retry.

## Merging

`github_merge_pr` with `dry_run=true` to preview, then `confirm=true`. Pass the FULL head
`sha` you reviewed so a later push makes GitHub refuse instead of merging unreviewed code
(not combinable with `delete_branch`). A single-commit squash lands that commit's subject,
not the PR title — set `subject` when the title is what should land on main.

## Releasing

1. `github_dispatch_workflow` (e.g. `prepare-release.yml`, `inputs: bump=minor`,
   `confirm=true`) → returns the run id.
2. `github_run_jobs <run_id>` until `completed`; success only if conclusion is `success`.
3. `github_create_tag` at the release PR's MERGE commit (full sha), `confirm=true`;
   `verify_version` checks `pyproject.toml` there. Tags are never moved.
4. `github_release <tag>` to check draft / Latest / assets. "Latest" is GitHub's promoted
   release, not the newest tag.
5. `github_edit_release` (full replacement notes) / `github_upload_release_asset`, then
   read an asset back with `github_release_asset`.
