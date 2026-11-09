"""The Claude Code plugin ships with the package, so both carry one version."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

from glab_cli_extras import __version__

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "coding-agent-plugins" / "claude-code"


def test_the_package_the_plugin_and_the_marketplace_entry_have_one_version() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    plugin = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    marketplace = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))
    (entry,) = [p for p in marketplace["plugins"] if p["name"] == plugin["name"]]
    assert {
        "pyproject.toml": project,
        "glab_cli_extras.__version__": __version__,
        "plugin.json": plugin["version"],
        "marketplace.json": entry["version"],
    } == dict.fromkeys(("pyproject.toml", "glab_cli_extras.__version__", "plugin.json", "marketplace.json"), project)


def test_the_marketplace_points_at_the_plugin_dir() -> None:
    marketplace = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))
    assert marketplace["name"] == "fprochazka-glab-cli-extras"
    (entry,) = marketplace["plugins"]
    assert entry["name"] == "glab"
    assert (ROOT / entry["source"]).resolve() == PLUGIN
