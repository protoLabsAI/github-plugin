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
| Auth | `gh` ambient auth, or `GITHUB_TOKEN`/`GH_TOKEN` from the env |
| Repo | `protoLabsAI/github-plugin`, ships `enabled: false` (install ≠ enable ≠ trust) |

## 2. Commands — the PR gate

These must pass before a PR opens (host-free — no protoAgent needed):

```bash
pip install -r requirements-dev.txt ruff
ruff check . && ruff format --check .   # lint + format
pytest -q                                # the suite
```

There is no other runner. `ruff` + `pytest` are the sole gate.

## 3. Where everything lives

```
protoagent.plugin.yaml   # manifest (id: github, config_section: github; write/default_repo/repos)
__init__.py              # register() — gating wiring (read always; write iff github.write) + /issue
gh_cli.py                # vendored async `gh` runner (run_gh, check_gh_error, bad_repo)
read_tools.py            # 8 read tools (6 ported core + read_file/repo_contents)
write_tools.py           # 8 write tools (create/edit/merge/close/comment/labels/assignees) — gated
gh_issue.py              # /issue chat command logic (user-only; gate-checked; configured repo)
tests/                   # host-free pytest (gating + version coherence)
```

## 4. The gating design — DO NOT BREAK IT

`register()` registers **read tools unconditionally** and **write tools ONLY when
`github.write` is true** (each agent's config decides — ADR 0019). A research/Lead
agent stays read-only; a coding/PM agent gets write. `tests/test_register.py` asserts
both halves — keep it green.

**`/issue` is a user-only chat command, not an agent tool** — creating an issue is a
write the model must not do autonomously, so `register()` registers it via the host's
`register_chat_command` seam (the logic lives in `gh_issue.py`). The call is guarded
by `hasattr(registry, "register_chat_command")`, so on an older host without the seam
`/issue` is skipped and the tools still load (degrade-safe). It routes to the
configured `default_repo`/`repos` (never a silent default). `tests/test_issue_command.py`
asserts both the seam-present and legacy-host paths — keep them green.

## 5. Tools (all implemented)

Each tool mocks `run_gh` in its test and asserts the exact argv + readable errors.

**Read (always on)** — 6 ported core tools (`github_get_pr`, `github_get_issue`,
`github_list_issues`, `github_get_commit_diff`, `github_ci_runs`, `github_run_failure`)
plus `github_read_file` (`gh api .../contents/{path}`, raw) and `github_repo_contents`
(directory listing).

**Write (gated on `github.write`)** —
`github_create_issue` / `github_comment` / `github_create_pr` (return the new URL),
`github_edit_pr` (`gh pr edit` + `gh pr ready [--undo]`),
`github_merge_pr` (`gh pr merge` — **refuses without `confirm=true`**, offers `dry_run`),
`github_close` (close/reopen issue|pr), and `github_set_labels` / `github_set_assignees`
(`gh {issue,pr} edit --add/--remove-{label,assignee}`). Issue-vs-PR ops take `kind`.

New write op? Validate `bad_repo()`, build argv, `run_gh()`, degrade to `Error: ...`,
add it to `get_write_tools()`'s return list **and** `WRITE_TOOLS` in `test_register.py`,
and mirror an existing test. Anything irreversible (merge) must be `confirm`-guarded.

## 6. Rules

- **Host-free.** NEVER import `graph.*` / `plugins.*` at module top — the suite runs
  with only `requirements-dev.txt`. Keep any host imports lazy (inside functions).
- **`@tool` docstrings must be PLAIN string literals** — an f-string docstring makes
  `__doc__` None and the tool ships with no description (the model can't see it).
- **Every tool requires an explicit `owner/name` repo** — validate with `bad_repo()`;
  there is no silent default (a forgotten repo must error, not hit the wrong repo).
- **DO NOT FABRICATE.** Use real `gh` invocations; verify the actual `gh api` shape
  before relying on it. No placeholder/guessed command flags.
- **Don't add runtime pip deps.** Test-only deps go in `requirements-dev.txt`; real
  runtime deps would go in the manifest's `requires_pip` (operator-installed).

## 7. Agent-scratch

`.proto/` is the coding agent's own scratch — gitignored, never commit.
