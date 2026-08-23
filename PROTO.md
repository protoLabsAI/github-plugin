# PROTO.md — agent instructions for github-plugin

The canonical instruction file for any agent (human or AI) working in this repo.
`CLAUDE.md` / `AGENTS.md` are thin pointers here — edit **this** file. Read it before
writing code.

## 1. What this is

A **standalone protoAgent plugin** (ADR 0001/0019/0027): read **and** write GitHub
tools over the `gh` CLI, with **per-agent write gating**. The host loads it via the
`register(registry)` seam; the manifest (`protoagent.plugin.yaml`) is read as data.

| Layer | What |
|---|---|
| Runtime | Python ≥ 3.11; `langchain-core` (`@tool`) is provided by the host |
| Auth | a non-empty `github.token` secret (Settings) is injected and wins; else the env passes through and gh's own order applies (`GH_TOKEN` > `GITHUB_TOKEN` > `gh auth login` keyring) |
| Repo | `protoLabsAI/github-plugin`, ships `enabled: false` (install ≠ enable ≠ trust) |

## 2. Commands — the PR gate

These must pass before a PR opens (host-free — no protoAgent needed):

```bash
pip install -r requirements-dev.txt      # includes the PINNED ruff==0.15.10
ruff check . && ruff format --check .   # lint + format
pytest -q                                # the suite
```

There is no other runner. `ruff` + `pytest` are the sole gate. `ruff` is pinned (the
same `0.15.10` as protoAgent core and projectBoard-plugin) in BOTH `requirements-dev.txt`
and `ci.yml` — a floating ruff fails `format --check` on correctly-formatted code; bump
the pin in both places together.

## 3. Where everything lives

```
protoagent.plugin.yaml   # manifest (id: github, config_section: github; write/default_repo/repos + the `token` secret)
__init__.py              # register() — gating wiring (read always; write iff github.write) + /issue + live-config getters
gh_cli.py                # vendored async `gh` runner: binary resolution (PATH + install dirs), token
                         #   injection (config secret > env), check_gh_error CLASSIFICATION, bad_repo
status.py                # the first-run probe: compute_status / summarize_status / report_gaps (setup-gap seam)
projects.py              # repo sources: host projects: registry (ADR 0095) + checkout `origin` remote parsing
read_tools.py            # 15 read tools (6 ported core + file/contents/pr-file/path-exists/pr-diff + status + list_prs/comments/search)
write_tools.py           # 8 write tools (create/edit/merge/close/comment/labels/assignees) — gated
review_tools.py          # 3 verdict tools (comment/approve/request-changes, guarded) — gated
gh_issue.py              # /issue chat command logic + repo resolution (resolve_repo, default_repo_error)
api.py                   # routers — public PAGES (view/new-issue) + gated data routes (config/status/issues/prs/issue)
view.py                  # PAGE = read-only board; NEW_ISSUE_PAGE = file-an-issue form; shared setup card. --pl-* themed
tests/                   # host-free pytest (gating + version coherence + routes via TestClient + the no-raise sweep)
```

**Console surfaces (ADR 0026/0038/0042/0057).** `register()` mounts two routers when the
host exposes `register_router` (guarded — degrade-safe): the PAGES on the PUBLIC
`/plugins/github` prefix (iframe-loadable; a page-load can't carry a bearer) and the
DATA routes on the GATED `/api/plugins/github` prefix (pages fetch them with the DS
plugin-kit's `apiFetch`, which attaches the operator bearer from the postMessage
handshake). Pages are vanilla JS themed entirely from `--pl-*` tokens (no host build).
Two manifest `views`, three surfaces, deliberately split:
- **`/view`** — the READ-ONLY board (Issues/PRs tabs + repo picker + state filter). Right
  dock + a ⌘K morph (`palette: inline`). No writes — it's a viewer.
- **`/new-issue`** — the compact file-an-issue form. A util-bar widget pill (`utility`,
  opens its dialog) AND a distinct ⌘K page (`palette: { path }`). `POST /issue` reuses the
  SAME `file_issue` gate path as the `/issue` command, so they can't diverge.
Filing lives in the widget + palette, NOT the board (keep the board read-only).

## 4. The gating design — DO NOT BREAK IT

`register()` registers **read tools unconditionally** and **write tools ONLY when
`github.write` is true** (each agent's config decides — ADR 0019). A research/Lead
agent stays read-only; a coding/PM agent gets write. `tests/test_register.py` asserts
both halves — keep it green.

**`/issue` is the user-only chat path; the `github_create_issue` agent tool exists
behind `github.write`.** A PERSON files from the composer on any agent — including a
read-only one — via `/issue`, registered through the host's `register_chat_command`
seam (the logic lives in `gh_issue.py`); the MODEL files via `github_create_issue`
only when the agent's write gate is on (the PM archetype requires it). Both go
through the same `file_issue` (gate check + `gh issue create`), so they can't
diverge. The seam call is guarded by `hasattr(registry, "register_chat_command")`, so
on an older host without it `/issue` is skipped and the tools still load
(degrade-safe). It routes to the configured `default_repo`/`repos` (never a silent
default). `tests/test_issue_command.py` asserts both the seam-present and
legacy-host paths — keep them green.

**Config is read LIVE.** `register()` builds getters over `registry.live_config` (the
snapshot on an older host) and every consumer — the tools' default repo, `/issue`, the
data routes, the `token` secret — reads through them per call. Never capture a config
value at register time: an `onboard_project` mid-session, or a token pasted in
Settings, must be seen by the very next call.

## 5. Tools (all implemented — 15 read / 8 write / 3 review = 26)

Each tool mocks `run_gh` in its test and asserts the exact argv + readable errors.
`tests/test_no_raise_sweep.py` additionally invokes EVERY registered tool against a
`run_gh` stub returning a dict / a list / garbage / an error and asserts a `str` comes
back — a tool that raises kills the whole agent turn, so no tool may. Add a tool ⇒ the
sweep covers it automatically (it enumerates `register()`'s output).

**Read (always on)** — 6 ported core tools (`github_get_pr`, `github_get_issue`,
`github_list_issues`, `github_get_commit_diff`, `github_ci_runs`, `github_run_failure`),
`github_pr_diff`, the content readers (`github_read_file` — raw file; `github_read_pr_file`
— a file at a PR's head; `github_repo_contents` — directory listing, says "is a file —
use github_read_file" on a file; `github_path_exists` — the EXISTS/MISSING probe), and
`github_status` — is `gh` installed / authenticated / as whom / which default repo, the
self-diagnosis tool the model calls when another tool errors (no `write` gate), and the
PM verbs (v0.7.0): `github_list_prs` (reuses `api.fetch_prs`, so the tool and the board
can't disagree — draft / review decision / merge state per row), `github_issue_comments`
(`gh issue view --json comments`; works on PRs; newest `limit` in chronological order,
bodies ≤ 1000 chars), `github_search_issues` (`gh search issues --repo … -- <query>`: flags first, the query
LAST after `--` so a leading qualifier like `-label:bug` isn't read as a flag; `state:
all` = no `--state` flag, gh only knows open|closed — documented as "dedupe before
filing").
`github_get_pr` carries the merge-readiness picture: `reviewDecision`, `mergeable`,
`mergeStateStatus`, `statusCheckRollup` summarised (N pass / fail / pending + the
failing names — both the CheckRun and StatusContext shapes), `latestReviews` (ONE per
reviewer, their most recent — so bot COMMENTED reviews can't bury a human's
CHANGES_REQUESTED; older gh falls back to the NEWEST `reviews`), `isDraft`; total output
bounded at 12k chars (`github_issue_comments` too).

**Write (gated on `github.write`)** —
`github_create_issue` (**body-gated**: the SAME `missing_sections` gate the `/issue`
command enforces — a thin body, or a `bug` without repro / a `feature` without a
direction-or-acceptance section, is refused with the scaffold and never posted; `kind`
also adds the type label via `labels_for`, and a `generic` call whose labels carry
`bug` / `enhancement` is gated as that kind because protoAgent's CI issue gate keys on
the label. The section regexes are in LOCKSTEP with `.github/workflows/issue-gate.yml`
in protoAgent — change both, `test_section_regexes_match_protoagents_ci_gate` pins a
sample per alternative) / `github_comment` / `github_create_pr` (return the new URL),
`github_edit_pr` (`gh pr edit` + `gh pr ready [--undo]`),
`github_merge_pr` (`gh pr merge` — **refuses without `confirm=true`**, offers `dry_run`),
`github_close` (close/reopen issue|pr), and `github_set_labels` / `github_set_assignees`
(`gh {issue,pr} edit --add/--remove-{label,assignee}`). Issue-vs-PR ops take `kind`.

**Review (gated on `github.write`)** — `github_review_comment` (always allowed),
`github_review_approve` / `github_review_request_changes` (refused while CI is pending
or unreadable, and on the agent's own PR — guards live in the tool, below the model).

New write op? Validate `bad_repo()`, build argv, `run_gh()`, degrade to `Error: ...`,
add it to `get_write_tools()`'s return list **and** `WRITE_TOOLS` in `test_register.py`,
and mirror an existing test. Anything irreversible (merge) must be `confirm`-guarded.

**Errors are classified** (`gh_cli.check_gh_error`, categories from `error_kind`): `gh`
exit 4 / "gh auth login" ⇒ `Error: GitHub CLI is not authenticated — …` with the hint
branched on `token_source()` (a rejected env/config token says "replace/unset it", not
"run gh auth login"); GraphQL "Could not resolve to a Repository"
⇒ `Error: repo 'o/n' not found or not accessible`; HTTP 404 ⇒ not found (with the repo
named when known); 403/429 + "rate limit" ⇒ `Error: GitHub API rate limit hit — retry …`;
binary missing ⇒ `Error: gh CLI is not installed or not on PATH (looked in …)`. Anything
else keeps the `Error (gh exit N): <stderr>` shape. Add a category here, not in a tool.

**First-run status** (`status.py`): `GET /api/plugins/github/status` → `{gh_path,
gh_version, authenticated, login, host, token_source, error, default_repo, repos,
default_repo_error}` from `gh --version` + `gh auth status --json hosts` (text fallback
for an older `gh`); never raises. The views render a setup card from it; the
`github_status` tool returns `summarize_status()`. **Every status computation reports**
to the host's `report_setup_gap` seam (guarded — no seam, no-op): `probe_in_background()`
at register time (a daemon thread, off the boot path) and `compute_and_report()` from
`/status` and the tool, edge-triggered (a failing key gets its message, a passing key
gets `None`) so the operator banner clears on the very Re-check that sees the recovery.
Each probe drops the cached `gh` path first — a miss is never cached — so a `gh`
installed after boot is found by the next call.
**Never block the loop on the picker**: `effective_default_repo` takes the picker as a
getter and short-circuits when `default_repo` is set; the routes and `github_status`
run the resolution via `asyncio.to_thread`; checkout remotes are cached 10 min.

## 6. Rules

- **Host-free.** NEVER import `graph.*` / `plugins.*` at module top — the suite runs
  with only `requirements-dev.txt`. Keep any host imports lazy (inside functions).
- **`@tool` docstrings must be PLAIN string literals** — an f-string docstring makes
  `__doc__` None and the tool ships with no description (the model can't see it).
- **Every tool requires an `owner/name` repo** — validate with `bad_repo()`. The
  fallback when the arg is omitted is the LIVE configured default (`default_repo` >
  first picker entry; the picker = `github.repos` ∪ the host's `projects:` registry ∪
  the `origin` remote of each registered checkout / `project_board.repo`), then the
  `GITHUB_DEFAULT_REPO` / `GH_REPO` env **logged at INFO when it fires**. No repo
  anywhere ⇒ an error, never a guess. A malformed `default_repo` is the NAMED
  `default_repo_error` (#23) on the plugin's `GET /config` / `/status`, the issue route,
  the `github_status` tool and the views' setup card — i.e. on READ; the host's Settings
  save itself is not gated (the plugin has no save route).
- **Never raise out of a tool.** A tool that raises kills the agent's whole turn. Every
  `gh` result is typed-checked before use (the contents API returns a dict for a file,
  a list for a directory); the no-raise sweep test enforces this for every tool.
- **DO NOT FABRICATE.** Use real `gh` invocations; verify the actual `gh api` shape
  before relying on it. No placeholder/guessed command flags.
- **Don't add runtime pip deps.** Test-only deps go in `requirements-dev.txt`; real
  runtime deps would go in the manifest's `requires_pip` (operator-installed).

## 7. Release

A release is a version bump + a tag + a GitHub release; hosts pin the tag in
`plugins.lock`. `tests/test_version.py` asserts the manifest and `pyproject.toml`
versions match — bump BOTH.

```bash
# 1. bump `version:` in protoagent.plugin.yaml AND `version =` in pyproject.toml (lockstep)
# 2. land the PR on main (the gates above must be green)
# 3. tag + release from the merged main
git checkout main && git pull
git tag -a vX.Y.Z -m "vX.Y.Z — <one-line summary>"
git push origin vX.Y.Z
gh release create vX.Y.Z --title "vX.Y.Z" --notes "<the PR body / changelog>"
```

There is no CHANGELOG file — the PR body is the changelog; paste it into the release
notes. Installed hosts pick the new version up with
`python -m server plugin update github` (or by re-pinning `plugins.lock`).

## 8. Agent-scratch

`.proto/` is the coding agent's own scratch — gitignored, never commit.
