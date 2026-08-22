"""Setup status — is `gh` installed, is it authenticated, and as whom?

The first-run question. A fresh desktop install with the Project Manager archetype
enabled gets a GitHub rail that silently shows "No repositories configured" or a
raw `gh` stderr, with nothing telling the person that `gh` is missing or logged
out. This module answers that ONE question three ways from one computation:

- ``GET /api/plugins/github/status`` (api.py) → the setup card in both views;
- the ``github_status`` read tool → a one-paragraph summary the model can act on
  instead of guessing from a tool error;
- ``report_gaps`` → the host's operator-warning seam (``registry.report_setup_gap``,
  guarded — the seam is newer than this plugin's floor), called from a best-effort
  background probe at register time so the warning shows before anything is used.

Everything here is host-free and never raises: a probe failure is a status with
``error`` set, not an exception (the routes/tools/threads calling it must not die).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading

from .gh_cli import resolve_gh, run_gh, token_source

log = logging.getLogger("protoagent.plugins.github")

_PROBE_TIMEOUT = 10  # auth status makes a network call; keep the card snappy

_VERSION_RE = re.compile(r"gh version (\S+)")
# The text shape (older gh without --json): "✓ Logged in to github.com account kj (keyring)"
_LOGGED_IN_RE = re.compile(r"Logged in to (\S+) account (\S+)")
_FAILED_RE = re.compile(r"(?:X|✗)\s+Failed to log in to (\S+)")

# Setup-gap keys (the host surfaces them as operator warnings, one per key).
GAP_GH = "gh"
GAP_AUTH = "auth"

_AUTH_HINT = "run `gh auth login` in a terminal, or paste a token in Settings ▸ GitHub (github.token)"


def _empty(default_repo: str = "", repos: list[str] | None = None) -> dict:
    return {
        "gh_path": None,
        "gh_version": None,
        "authenticated": False,
        "login": None,
        "host": None,
        "token_source": token_source(),
        "error": None,
        "default_repo": default_repo,
        "repos": list(repos or []),
    }


def _parse_auth_json(out: str) -> tuple[bool, str | None, str | None, str | None]:
    """(authenticated, login, host, error) from `gh auth status --json hosts`.
    The ACTIVE account decides — a failing active token with a healthy inactive
    keyring login is still "not authenticated" (that's what gh would use)."""
    d = json.loads(out or "{}")
    hosts = d.get("hosts") or {}
    if not hosts:
        return False, None, None, "not logged in"
    accounts: list[dict] = [a for entries in hosts.values() for a in (entries or []) if isinstance(a, dict)]
    active = [a for a in accounts if a.get("active")] or accounts[:1]
    if not active:
        return False, None, None, "not logged in"
    a = active[0]
    host = str(a.get("host") or next(iter(hosts), "") or "") or None
    if str(a.get("state") or "") == "success":
        return True, str(a.get("login") or "") or None, host, None
    err = str(a.get("error") or "token rejected")
    # gh embeds the whole HTTP body; keep the first line only.
    return False, None, host, err.splitlines()[0][:200]


def _parse_auth_text(rc: int, out: str, serr: str) -> tuple[bool, str | None, str | None, str | None]:
    """Text fallback for a gh without `--json` on `auth status`."""
    blob = "\n".join(x for x in (out, serr) if x)
    # Blocks are separated by blank lines; the one with "Active account: true" decides.
    blocks = [b for b in re.split(r"\n\s*\n", blob) if b.strip()]
    active = [b for b in blocks if re.search(r"Active account:\s*true", b)] or blocks[:1]
    for b in active:
        if m := _LOGGED_IN_RE.search(b):
            return True, m.group(2), m.group(1), None
        if m := _FAILED_RE.search(b):
            detail = next(
                (ln.strip(" -") for ln in b.splitlines() if "token" in ln.lower() and "invalid" in ln.lower()), ""
            )
            return False, None, m.group(1), detail or "active token rejected"
    if rc != 0 or "not logged in" in blob.lower() or "gh auth login" in blob.lower():
        return False, None, None, "not logged in"
    return False, None, None, (serr or out or "could not read auth status")[:200]


async def compute_status(default_repo: str = "", repos: list[str] | None = None) -> dict:
    """Probe `gh` (binary → version → auth) and return the status dict:
    ``{gh_path, gh_version, authenticated, login, host, token_source, error,
    default_repo, repos}``. Never raises; every failure lands in ``error``."""
    st = _empty(default_repo, repos)
    try:
        path = resolve_gh()
        if not path:
            st["error"] = "gh CLI is not installed or not on PATH"
            return st
        st["gh_path"] = path

        rc, out, serr = await run_gh(["--version"], timeout=_PROBE_TIMEOUT)
        if rc != 0:
            st["error"] = f"`gh --version` failed (exit {rc}): {(serr or out)[:200]}"
            return st
        m = _VERSION_RE.search(out)
        st["gh_version"] = m.group(1) if m else (out.splitlines()[0][:40] if out else None)

        rc, out, serr = await run_gh(["auth", "status", "--json", "hosts"], timeout=_PROBE_TIMEOUT)
        if rc == 0 and out.lstrip().startswith("{"):
            try:
                ok, login, host, err = _parse_auth_json(out)
            except (ValueError, TypeError):
                ok, login, host, err = _parse_auth_text(rc, out, serr)
        else:
            if "unknown flag" in (serr or "").lower() or "--json" in (serr or ""):
                rc, out, serr = await run_gh(["auth", "status"], timeout=_PROBE_TIMEOUT)
            ok, login, host, err = _parse_auth_text(rc, out, serr)
        st.update(authenticated=ok, login=login, host=host, error=err)
        return st
    except Exception as e:  # noqa: BLE001 — a probe must never take a route/tool/thread down
        st["error"] = f"status probe failed: {type(e).__name__}: {e}"[:300]
        return st


def summarize_status(st: dict) -> str:
    """One paragraph a model (or a person) can act on — the same facts as the dict."""
    repos = st.get("repos") or []
    default = st.get("default_repo") or ""
    repo_bit = (
        f" Default repo: {default}."
        if default
        else " No default repo configured (pass repo='owner/name' per call, or set one in Settings ▸ GitHub)."
    ) + (
        f" {len(repos)} repo(s) in the picker: {', '.join(repos[:8])}{'…' if len(repos) > 8 else ''}." if repos else ""
    )
    src = st.get("token_source") or "none"
    src_bit = {
        "config": " Token source: Settings ▸ GitHub (github.token).",
        "env": " Token source: GITHUB_TOKEN/GH_TOKEN env.",
    }.get(src, "")

    if not st.get("gh_path"):
        return (
            "GitHub CLI is NOT installed (no `gh` on PATH or in the usual install dirs). "
            "Install it — https://cli.github.com (macOS: `brew install gh`; Debian/Ubuntu: `sudo apt install gh`) — "
            f"then {_AUTH_HINT}. Every github_* tool will return an error until then." + repo_bit
        )
    ver = f" v{st['gh_version']}" if st.get("gh_version") else ""
    if not st.get("authenticated"):
        why = f" ({st['error']})" if st.get("error") else ""
        return (
            f"GitHub CLI{ver} is installed at {st['gh_path']} but NOT authenticated{why}. "
            f"To fix: {_AUTH_HINT}. Read tools on public repos may still work at low volume; "
            "everything else will return 'not authenticated' until then." + src_bit + repo_bit
        )
    who = f" as {st['login']}" if st.get("login") else ""
    host = f" on {st['host']}" if st.get("host") else ""
    return (
        f"GitHub CLI{ver} is installed at {st['gh_path']} and authenticated{who}{host}. All github_* tools are usable."
        + src_bit
        + repo_bit
    )


def gaps_for(st: dict) -> dict[str, str | None]:
    """The setup-gap messages this status implies, keyed by gap id; ``None`` = clear."""
    gaps: dict[str, str | None] = {GAP_GH: None, GAP_AUTH: None}
    if not st.get("gh_path"):
        gaps[GAP_GH] = (
            "GitHub CLI (`gh`) is not installed or not on PATH — the GitHub tools and rail won't work. "
            "Install it from https://cli.github.com, then run `gh auth login`."
        )
        return gaps
    if not st.get("authenticated"):
        why = f" ({st['error']})" if st.get("error") else ""
        gaps[GAP_AUTH] = f"GitHub CLI is not authenticated{why} — {_AUTH_HINT}."
    return gaps


def report_gaps(registry, st: dict) -> bool:
    """Push this status' gaps to the host's ``report_setup_gap(key, message|None)`` seam
    when the host has one; clears a gap (``None``) when it's healthy. Guarded: an
    older host (no seam) or a seam that raises is a no-op. Returns whether it reported."""
    fn = getattr(registry, "report_setup_gap", None)
    if not callable(fn):
        return False
    try:
        for key, msg in gaps_for(st).items():
            fn(key, msg)
        return True
    except Exception:  # noqa: BLE001 — telemetry must never break register()
        log.debug("[github] report_setup_gap failed", exc_info=True)
        return False


def probe_in_background(registry, default_repo="", repos=None) -> threading.Thread | None:
    """Compute the status off the register() hot path and report its gaps. Only
    started when the host exposes the seam (nothing to report otherwise). The thread
    is a daemon and swallows everything — boot must never wait on, or die from, it.
    ``default_repo`` / ``repos`` may be values or zero-arg getters (evaluated IN the
    thread, so the git-remote parsing they may do never blocks register())."""
    if not callable(getattr(registry, "report_setup_gap", None)):
        return None

    def _run() -> None:
        try:
            d = default_repo() if callable(default_repo) else default_repo
            r = repos() if callable(repos) else repos
            st = asyncio.run(compute_status(str(d or ""), list(r or [])))
            report_gaps(registry, st)
            if st.get("gh_path") and st.get("authenticated"):
                log.info("[github] gh %s authenticated as %s", st.get("gh_version") or "?", st.get("login") or "?")
            else:
                log.warning("[github] setup gap: %s", summarize_status(st))
        except Exception:  # noqa: BLE001
            log.debug("[github] background status probe failed", exc_info=True)

    t = threading.Thread(target=_run, name="github-plugin-status-probe", daemon=True)
    t.start()
    return t
