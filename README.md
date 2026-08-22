# github-plugin

Read **and** write GitHub tools for [protoAgent](https://github.com/protoLabsAI/protoAgent)
over the [`gh`](https://cli.github.com) CLI, with **per-agent write gating**.

The **read** tools are always on; the **write** tools load **only when an agent's
config sets `github.write: true`** — so a research/Lead agent stays read-only while a
coding/PM agent gets write, purely by its own per-instance config. Supersedes the
read-only in-tree `github` plugin.

## Tools (all implemented)

**Read** (always, 11): `github_get_pr`, `github_get_issue`, `github_list_issues`,
`github_get_commit_diff`, `github_pr_diff`, `github_ci_runs`, `github_run_failure`,
`github_read_file`, `github_read_pr_file`, `github_repo_contents`, `github_path_exists`,
and `github_status` (is `gh` installed / signed in, as whom, which default repo — the
self-diagnosis probe the model calls when another tool errors).

**Write** (only when `github.write: true`, 8): `github_create_issue`, `github_comment`,
`github_create_pr`, `github_edit_pr`, `github_merge_pr` (`confirm`-guarded),
`github_close`, `github_set_labels`, `github_set_assignees`.

**Review** (also behind `github.write`, 3): `github_review_comment`,
`github_review_approve`, `github_review_request_changes` — the formal verdict tools,
with the CI-terminal and self-review guards enforced inside the tool (ADR 0078).

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

**Auth**, in precedence order: the `github.token` secret (Settings ▸ GitHub — a personal
access token with `repo` scope) > `GITHUB_TOKEN` / `GH_TOKEN` in the environment >
`gh`'s own keyring login (`gh auth login`). Public-repo reads need none at low volume.
`gh` is found on PATH or in the usual install dirs (`/opt/homebrew/bin`, `/usr/local/bin`,
`~/.local/bin`, `/usr/bin`) — a desktop build launched without a shell PATH still works.

**Repos**: a tool's omitted `repo` falls back to `default_repo`, else the first picker
entry. The picker is `github.repos` ∪ the host's managed-projects registry (ADR 0095,
`projects:` entries with a `github:` binding) ∪ — last resort — the `owner/name` parsed
from the `origin` remote of each registered checkout (and `project_board.repo`). All of
it is read live: a Settings edit or an `onboard_project` mid-session is seen by the
next call. A malformed `default_repo` is a named error, never fed to `gh`.

## Develop

```bash
pip install -r requirements-dev.txt          # pins ruff==0.15.10
ruff check . && ruff format --check . && pytest -q
```

Host-free — the suite needs no protoAgent host. See [PROTO.md](./PROTO.md).
