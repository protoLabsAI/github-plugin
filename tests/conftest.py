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

    def register_tool(self, t):
        self.tools.append(t)

    @property
    def tool_names(self) -> list[str]:
        return [getattr(t, "name", getattr(t, "__name__", "?")) for t in self.tools]


@pytest.fixture
def make_registry():
    return _Registry
