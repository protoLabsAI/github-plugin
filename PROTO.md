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
protoagent.plugin.yaml   # manifest (id: github, config_section: github, write: bool)
__init__.py              # register() — the GATING wiring (read always; write iff github.write)
gh_cli.py                # vendored async `gh` runner (run_gh, check_gh_error, bad_repo)
read_tools.py            # 6 ported read tools (done) + read_file/repo_contents (STUBS)
write_tools.py           # create_issue / comment / create_pr (STUBS — gated)
tests/                   # host-free pytest (gating + version coherence)
```

## 4. The gating design — DO NOT BREAK IT

`register()` registers **read tools unconditionally** and **write tools ONLY when
`github.write` is true** (each agent's config decides — ADR 0019). A research/Lead
agent stays read-only; a coding/PM agent gets write. `tests/test_register.py` asserts
both halves — keep it green.

## 5. What to build (the stubs)

Each stub has a `TODO(team)` with the exact `gh` command. Implement, then add a test
that mocks `run_gh` and asserts the tool formats the result (and errors readably):
- **`github_read_file`** — `gh api repos/{repo}/contents/{path}?ref={ref}` (raw media type).
- **`github_repo_contents`** — `gh api repos/{repo}/contents/{path}` → list of entries.
- **`github_create_issue` / `github_comment` / `github_create_pr`** — `gh issue create` /
  `gh issue comment` / `gh pr create`; return the new URL.

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
