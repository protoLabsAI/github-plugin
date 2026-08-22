# github-plugin

Read **and** write GitHub tools for [protoAgent](https://github.com/protoLabsAI/protoAgent)
over the [`gh`](https://cli.github.com) CLI, with **per-agent write gating**.

The **read** tools are always on; the **write** tools load **only when an agent's
config sets `github.write: true`** — so a research/Lead agent stays read-only while a
coding/PM agent gets write, purely by its own per-instance config. Supersedes the
read-only in-tree `github` plugin.

## Tools (all implemented)

**Read** (always, 15): `github_get_pr` (the merge-readiness picture: review decision,
mergeability, a checks summary with the failing check names, the reviews),
`github_list_prs` (the PR board: draft / review decision / merge state per row),
`github_get_issue`, `github_list_issues`, `github_issue_comments` (the thread on an
issue or PR), `github_search_issues` (dedupe **before** filing), `github_get_commit_diff`,
`github_pr_diff`, `github_ci_runs`, `github_run_failure`, `github_read_file`,
`github_read_pr_file`, `github_repo_contents`, `github_path_exists`, and `github_status`
(is `gh` installed / signed in, as whom, which default repo — the self-diagnosis probe
the model calls when another tool errors).

**Write** (only when `github.write: true`, 8): `github_create_issue` (body-**gated** —
the same Problem / repro / acceptance sections the `/issue` command requires; a thin
body gets the scaffold back, never posted), `github_comment`, `github_create_pr`,
`github_edit_pr`, `github_merge_pr` (`confirm`-guarded), `github_close`,
`github_set_labels`, `github_set_assignees`.

**Review** (also behind `github.write`, 3): `github_review_comment`,
`github_review_approve`, `github_review_request_changes` — the formal verdict tools,
with the CI-terminal and self-review guards enforced inside the tool (ADR 0078).

26 tools in all, every one covered by the no-raise sweep.

Plus the **user-only `/issue` chat command** (file an issue from the composer on any
agent, without the model) and two console views — the read-only Issues/PRs **board**
and the **New issue** form — both of which show a **setup card** when `gh` is missing
or not signed in.

Every tool returns a readable, classified error instead of raw `gh` stderr: not
authenticated (with the fix), repo not found, API rate limit, `gh` not installed.

## Install & enable

```bash
python -m server plugin install https://github.com/protoLabsAI/github-plugin
```

Install ≠ enable. To turn it on, add to `config/langgraph-config.yaml`:

```yaml
plugins:
  enabled: [github]
github:
  write: false          # read-only; set true ONLY for agents that should mutate GitHub
  default_repo: ""      # owner/name — the repo tools and /issue use when none is passed
  repos: []             # repo picker list; the host's projects: registry is added automatically
```

**Auth**: a non-empty `github.token` secret (Settings ▸ GitHub — a personal access token
with `repo` scope) is injected into every `gh` run and wins. Otherwise the environment is
passed through untouched and `gh`'s own precedence applies: `GH_TOKEN` > `GITHUB_TOKEN` >
the `gh auth login` keyring. Public-repo reads need none at low volume. When auth fails,
the error says which of those was rejected and what to do about it.
`gh` is found on PATH or in the usual install dirs (`/opt/homebrew/bin`, `/usr/local/bin`,
`~/.local/bin`, `/usr/bin`) — a desktop build launched without a shell PATH still works.

**Repos**: a tool's omitted `repo` falls back to `default_repo`, else the first picker
entry. The picker is `github.repos` ∪ the host's managed-projects registry (ADR 0095,
`projects:` entries with a `github:` binding) ∪ — last resort — the `owner/name` parsed
from the `origin` remote of each registered checkout (and `project_board.repo`). All of
it is read live: a Settings edit or an `onboard_project` mid-session is seen by the
next call. A malformed `default_repo` is a named error on the plugin's `GET /config` and
in the views' setup card (not on Settings save), never fed to `gh`.

## Develop

```bash
pip install -r requirements-dev.txt          # pins ruff==0.15.10
ruff check . && ruff format --check . && pytest -q
```

Host-free — the suite needs no protoAgent host. See [PROTO.md](./PROTO.md).
