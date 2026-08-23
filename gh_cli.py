"""Async `gh` CLI runner — vendored so the plugin is host-free.

A thin wrapper around the GitHub CLI: binary resolution, timeout + kill,
missing-binary detection, token injection, and error CLASSIFICATION.

Binary: ``gh`` is resolved via ``shutil.which`` and, when PATH doesn't carry it
(a Linux desktop build launched from a .desktop file has no shell PATH
augmentation; macOS GUI apps likewise), a scan of the usual install dirs
(``/opt/homebrew/bin``, ``/usr/local/bin``, ``~/.local/bin``, ``/usr/bin``). The
resolved path is cached per process.

Auth: a non-empty ``github.token`` secret (Settings ▸ GitHub, wired via
``set_token_getter`` at register time) is injected as ``GH_TOKEN`` and wins.
Otherwise the child env is passed through untouched and gh's own precedence applies
(``GH_TOKEN`` > ``GITHUB_TOKEN`` > the ``gh auth login`` keyring). No token is
required for public-repo reads at low volume.

Errors: ``check_gh_error`` turns a failed run into ONE readable ``Error: ...``
string the model (or a person) can act on — not-authenticated, repo-not-found,
rate-limited, binary-missing — instead of raw stderr.

Adapted from protoAgent's tools/gh_cli.py (which was adapted from the quinn fleet).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

_COMMAND_TIMEOUT = 30

# Both reads and writes require an explicit `owner/name` — there is deliberately NO
# silent default (a forgotten repo must error, not fire at the wrong repository).
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")

# `gh`'s own exit code for "you need to authenticate" (cli/cli: exitcode.AuthRequired = 4).
GH_EXIT_AUTH_REQUIRED = 4

# Where `gh` lands when a package manager installs it but the launching process has
# no shell PATH (desktop builds). Scanned in this order AFTER `shutil.which`.
_FALLBACK_BIN_DIRS = ("/opt/homebrew/bin", "/usr/local/bin", "~/.local/bin", "/usr/bin")


def auth_hint(source: str | None = None) -> str:
    """What to DO about "not authenticated", branched on where the rejected token came
    from — "run gh auth login" is wrong advice when an env/config token is what `gh`
    is rejecting. ``source`` defaults to the live ``token_source()``."""
    src = source if source is not None else token_source()
    if src == "config":
        return (
            "the token saved in Settings ▸ GitHub (github.token) was rejected — replace it there, "
            "or clear it to fall back to `gh auth login`"
        )
    if src == "env":
        return (
            "the GH_TOKEN / GITHUB_TOKEN in the agent's environment was rejected — fix or unset it, "
            "or paste a working token in Settings ▸ GitHub (github.token)"
        )
    return "run `gh auth login` in a terminal, or paste a token in Settings ▸ GitHub (github.token)"


def bad_repo(repo: str) -> str | None:
    """Validate an `owner/name` repo slug; return an Error string if invalid, else None."""
    if not repo or not REPO_RE.match(repo):
        return (
            f"Error: no usable repo (got {repo!r}). Pass repo='owner/name', or set a default "
            "in Settings ▸ GitHub (github.default_repo / repos) so it can be omitted."
        )
    return None


# ── binary resolution ─────────────────────────────────────────────────────────────

_gh_path: str | None = None
_gh_resolved = False


def resolve_gh() -> str | None:
    """The absolute path of the `gh` binary, or None when it can't be found. PATH
    first (`shutil.which`), then the well-known install dirs. A HIT is cached per
    process; a MISS is never cached — an operator who installs `gh` after boot must
    be seen by the very next call (Re-check, the next tool), not the next restart.
    ``reset_gh_cache()`` drops a hit (a stale path, the status probe, tests)."""
    global _gh_path, _gh_resolved
    if _gh_resolved and _gh_path:
        return _gh_path
    found = shutil.which("gh")
    if not found:
        for d in _FALLBACK_BIN_DIRS:
            cand = Path(d).expanduser() / "gh"
            if cand.is_file() and os.access(cand, os.X_OK):
                found = str(cand)
                break
    if found:
        _gh_path, _gh_resolved = found, True
    return found


def reset_gh_cache() -> None:
    """Forget the cached binary path (so the next call re-resolves)."""
    global _gh_path, _gh_resolved
    _gh_path, _gh_resolved = None, False


def gh_search_dirs() -> list[str]:
    """Where `gh` is looked for — PATH plus the fallback dirs (for error messages)."""
    return [*(p for p in os.environ.get("PATH", "").split(os.pathsep) if p), *_FALLBACK_BIN_DIRS]


# ── token resolution ──────────────────────────────────────────────────────────────

# A zero-arg getter returning the plugin's configured token ("" when unset). Wired by
# register() to read the LIVE config (so a token pasted in Settings works without a
# restart); None when no host wired one (the host-free suite, an older host).
_token_getter: Callable[[], str] | None = None


def set_token_getter(getter: Callable[[], str] | None) -> None:
    """Install (or clear) the config-token getter. The getter is called per `gh` run
    and must never raise — it's wrapped anyway, a failing getter means "no token"."""
    global _token_getter
    _token_getter = getter


def _config_token() -> str:
    if _token_getter is None:
        return ""
    try:
        return str(_token_getter() or "").strip()
    except Exception:  # noqa: BLE001 — a broken getter must not take `gh` down
        return ""


def resolve_token() -> str | None:
    """The token `gh` will use: the plugin's configured secret first (Settings ▸
    GitHub), else the ambient env in gh's OWN order (GH_TOKEN, then GITHUB_TOKEN),
    else None (gh's keyring login). Informational — see ``gh_env`` for what's injected."""
    return _config_token() or os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or None


def token_source() -> str:
    """Where the effective token comes from: ``config`` | ``env`` | ``none``."""
    if _config_token():
        return "config"
    if os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN"):
        return "env"
    return "none"


def gh_env() -> dict:
    """The child env for a `gh` run. ONLY a non-empty config token is injected (as
    GH_TOKEN, the var gh reads first, and GITHUB_TOKEN so nothing stale shadows it) —
    it's what the person just pasted and it's visible in the UI. Otherwise the ambient
    env is passed through UNTOUCHED, so gh's own precedence (GH_TOKEN > GITHUB_TOKEN >
    keyring) applies exactly as it would in a terminal."""
    env = os.environ.copy()
    token = _config_token()
    if token:
        env["GH_TOKEN"] = token
        env["GITHUB_TOKEN"] = token
    return env


# ── the runner ────────────────────────────────────────────────────────────────────

# The stderr stand-in for a missing binary — check_gh_error recognises it.
_MISSING_BINARY = "gh CLI is not installed or not on PATH."


async def run_gh(args: list[str], timeout: int = _COMMAND_TIMEOUT) -> tuple[int, str, str]:
    """Run a `gh` command, returning (returncode, stdout, stderr). Times out (killing
    the process), and reports a clean error when `gh` isn't installed instead of raising."""
    binary = resolve_gh()
    if not binary:
        return 127, "", _MISSING_BINARY

    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            binary,
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=gh_env(),
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return (
            proc.returncode or 0,
            stdout.decode(errors="replace").strip(),
            stderr.decode(errors="replace").strip(),
        )
    except asyncio.TimeoutError:
        if proc is not None:
            with contextlib.suppress(ProcessLookupError):  # it exited between the timeout and the kill
                proc.kill()
        return 1, "", f"gh command timed out after {timeout}s"
    except FileNotFoundError:
        reset_gh_cache()  # the cached path went stale (uninstalled mid-process)
        return 127, "", _MISSING_BINARY
    except PermissionError:
        return 126, "", f"gh binary at {binary} is not executable."
    except OSError as e:
        # ENOEXEC (a foreign-arch / truncated ~/.local/bin/gh), EMFILE, E2BIG … — a
        # spawn failure is a tool ERROR, never an exception through the tool layer.
        return 126, "", f"could not run gh at {binary}: {e}"


# ── output parsing ────────────────────────────────────────────────────────────────


def parse_json(out: str, expect: type | tuple[type, ...] = dict) -> tuple[Any, str | None]:
    """``(value, None)`` when ``out`` is JSON of the ``expect``ed type (``dict``,
    ``list``, or a tuple of them), else ``(None, "Error: ...")``. The ONE place a `gh --json` / `gh api`
    body is trusted: the contents API hands back a dict for a file and a list for a
    directory, a `--jq` can yield a bare scalar, and a tool that does ``.get`` on the
    wrong type raises through the tool layer and kills the agent's turn."""
    try:
        value = json.loads(out)
    except (ValueError, TypeError):
        return None, f"Error: could not parse gh output: {(out or '')[:200]}"
    if not isinstance(value, expect):
        names = " or ".join(t.__name__ for t in (expect if isinstance(expect, tuple) else (expect,)))
        return None, (
            f"Error: unexpected gh output (expected a JSON {names}, got {type(value).__name__}): {(out or '')[:200]}"
        )
    return value, None


def dicts(items) -> list[dict]:
    """Only the dict rows of a `gh --json` list — nulls/scalars in a row list are skipped,
    and a value that isn't a list at all (a nested scalar like ``"reviews": 42``) is ``[]``.
    Total by design: a tool feeds it any nested field without a type check of its own."""
    if not isinstance(items, list):
        return []
    return [x for x in items if isinstance(x, dict)]


# ── error classification ──────────────────────────────────────────────────────────

_RATE_LIMIT_RESET_RE = re.compile(r"(?:retry after|reset(?:s)? (?:at|in)|try again (?:in|after))\s*([^\n.]+)", re.I)
_NOT_FOUND_REPO_RE = re.compile(r"Could not resolve to a Repository with the name '([^']+)'", re.I)


def error_kind(returncode: int, stderr: str) -> str | None:
    """The CATEGORY of a failed `gh` run — ``None`` on success, else one of
    ``missing_binary`` | ``auth`` | ``rate_limit`` | ``not_found`` | ``generic`` —
    checked in that order. ``classify_gh_error`` renders it; a caller that must
    branch on the category (``github_path_exists``: a 404 is MISSING, anything else
    is an error) uses this directly so the two can't disagree."""
    if returncode == 0:
        return None
    blob = stderr or ""
    low = blob.lower()
    if _MISSING_BINARY in blob or returncode == 127:
        return "missing_binary"
    if (
        returncode == GH_EXIT_AUTH_REQUIRED
        or "gh auth login" in low
        or "not logged in" in low
        or "authentication required" in low
        or "bad credentials" in low
    ):
        return "auth"
    if "rate limit" in low and ("403" in low or "429" in low or "exceeded" in low):
        return "rate_limit"
    if _NOT_FOUND_REPO_RE.search(blob) or "http 404" in low:
        return "not_found"
    return "generic"


def classify_gh_error(returncode: int, stderr: str, *, repo: str = "") -> str | None:
    """Map a failed `gh` run to ONE actionable ``Error: ...`` string, or None if it
    succeeded. The categories (``error_kind``), in the order they're checked:

    - binary missing (the runner's stand-in stderr, or exit 127);
    - not authenticated (gh's exit 4, or stderr pointing at `gh auth login`) — the
      hint is branched on ``token_source()``: a rejected env/config token says so,
      instead of the wrong "run gh auth login";
    - rate-limited (HTTP 403/429 + "rate limit");
    - repo not found / not accessible (GraphQL "Could not resolve to a Repository",
      or an HTTP 404);
    - anything else → the generic ``Error (gh exit N): <stderr>`` (unchanged shape,
      existing tests and callers match on it).
    """
    kind = error_kind(returncode, stderr)
    if kind is None:
        return None
    blob = stderr or ""
    if kind == "missing_binary":
        return f"Error: gh CLI is not installed or not on PATH (looked in {', '.join(gh_search_dirs())})."
    if kind == "auth":
        return f"Error: GitHub CLI is not authenticated — {auth_hint()}."
    if kind == "rate_limit":
        m = _RATE_LIMIT_RESET_RE.search(blob)
        when = f" — retry after {m.group(1).strip()}" if m else " — retry in a few minutes"
        return f"Error: GitHub API rate limit hit{when}."
    if kind == "not_found":
        if m := _NOT_FOUND_REPO_RE.search(blob):
            return f"Error: repo '{m.group(1)}' not found or not accessible (check the owner/name and your token's scopes)."
        where = (
            f"repo '{repo}' not found or not accessible, or the path/ref/number doesn't exist in it"
            if repo
            else "not found"
        )
        return f"Error: {where} (HTTP 404 — check the owner/name, the path/ref/number, and your token's scopes)."
    return f"Error (gh exit {returncode}): {blob[:500]}"


def check_gh_error(returncode: int, stderr: str, *, repo: str = "") -> str | None:
    """Return a formatted `Error: ...` string if the command failed, else None.
    Classified (auth / not-found / rate-limit / missing binary) — see
    ``classify_gh_error``; generic failures keep the ``Error (gh exit N): …`` shape."""
    return classify_gh_error(returncode, stderr, repo=repo)
