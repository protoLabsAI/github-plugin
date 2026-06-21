# github-plugin

Read **and** write GitHub tools for [protoAgent](https://github.com/protoLabsAI/protoAgent)
over the [`gh`](https://cli.github.com) CLI, with **per-agent write gating**.

The **read** tools are always on; the **write** tools load **only when an agent's
config sets `github.write: true`** — so a research/Lead agent stays read-only while a
coding/PM agent gets write, purely by its own per-instance config. Supersedes the
read-only in-tree `github` plugin.

## Tools

**Read** (always): `github_get_pr`, `github_get_issue`, `github_list_issues`,
`github_get_commit_diff`, `github_ci_runs`, `github_run_failure`, `github_read_file`*,
`github_repo_contents`*.

**Write** (only when `github.write: true`): `github_create_issue`*, `github_comment`*,
`github_create_pr`*.

*\* stubbed — being built out; see [PROTO.md §5](./PROTO.md).*

## Install & enable

```bash
python -m server plugin install https://github.com/protoLabsAI/github-plugin
```

Install ≠ enable. To turn it on, add to `config/langgraph-config.yaml`:

```yaml
plugins:
  enabled: [github]
github:
  write: false   # read-only; set true ONLY for agents that should mutate GitHub
```

Auth: `gh` uses its own ambient auth (`gh auth login`) or `GITHUB_TOKEN`/`GH_TOKEN`.

## Develop

```bash
pip install -r requirements-dev.txt ruff
ruff check . && ruff format --check . && pytest -q
```

Host-free — the suite needs no protoAgent host. See [PROTO.md](./PROTO.md).
