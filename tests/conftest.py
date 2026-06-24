"""Test bootstrap — make the plugin importable with NO protoAgent host.

Multi-module plugin (modules use ``from .gh_cli import ...``): register a synthetic
package whose __path__ is the repo root, so relative imports resolve standalone.
Executing __init__.py is safe — it has no host-only imports at module top.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PKG = "ghplugin"  # synthetic package name (avoid clashing with PyGithub's `github`)

if PKG not in sys.modules:
    _spec = importlib.util.spec_from_file_location(PKG, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    assert _spec and _spec.loader
    _mod = importlib.util.module_from_spec(_spec)
    sys.modules[PKG] = _mod
    _spec.loader.exec_module(_mod)


class _Registry:
    """A fake registry to smoke-test register() with no host."""

    def __init__(self, config: dict | None = None):
        self.config = config or {}
        self.tools: list = []
        self.chat_commands: dict = {}  # token -> handler (the host's chat-command seam)
        self.routers: list = []  # {"router", "prefix"} (the host's router seam)

    def register_tool(self, t):
        self.tools.append(t)

    def register_chat_command(self, name: str, handler):
        self.chat_commands[name] = handler

    def register_router(self, router, prefix=None):
        self.routers.append({"router": router, "prefix": prefix})

    @property
    def tool_names(self) -> list[str]:
        return [getattr(t, "name", getattr(t, "__name__", "?")) for t in self.tools]

    @property
    def router_prefixes(self) -> list[str]:
        return [r["prefix"] for r in self.routers]


class _LegacyRegistry:
    """A host WITHOUT the chat-command seam (older protoAgent) — no
    ``register_chat_command``. register() must still load the tools and just skip
    ``/issue`` (graceful degrade)."""

    def __init__(self, config: dict | None = None):
        self.config = config or {}
        self.tools: list = []

    def register_tool(self, t):
        self.tools.append(t)

    @property
    def tool_names(self) -> list[str]:
        return [getattr(t, "name", getattr(t, "__name__", "?")) for t in self.tools]


@pytest.fixture
def make_registry():
    return _Registry


@pytest.fixture
def make_legacy_registry():
    return _LegacyRegistry
