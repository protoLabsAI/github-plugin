"""Manifest version must match pyproject — a release bumps both together."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


def test_manifest_pyproject_version_match():
    manifest = yaml.safe_load((ROOT / "protoagent.plugin.yaml").read_text())
    pyproject = (ROOT / "pyproject.toml").read_text()
    m = re.search(r'^version\s*=\s*"([^"]+)"', pyproject, re.M)
    assert m, "no version in pyproject.toml"
    assert manifest["version"] == m.group(1), f"manifest version {manifest['version']} != pyproject {m.group(1)}"


def test_manifest_required_fields():
    manifest = yaml.safe_load((ROOT / "protoagent.plugin.yaml").read_text())
    assert manifest["id"] == "github"
    assert manifest["config_section"] == "github"  # string, not a list
    assert manifest["config"]["write"] is False  # ships read-only
    assert manifest["enabled"] is False  # ships disabled
