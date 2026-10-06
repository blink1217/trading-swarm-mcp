"""Agent onboarding surface (7.5): .mcp.json + SKILL.md carry no secrets, the
remote URLs match the hosted endpoints, and the README agent snippet keeps the
honesty rules."""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_mcp_json_parses_without_secrets():
    doc = json.loads((ROOT / ".mcp.json").read_text(encoding="utf-8"))
    remotes = doc["remote"]
    assert remotes["data"].endswith("/mcp/data")
    assert remotes["warden"].endswith("/mcp/warden")
    assert remotes["gym"].endswith("/mcp/gym")
    blob = json.dumps(doc)
    assert "121_" not in blob and "oat_" not in blob and "apiToken" not in blob
    assert doc["auth"]["oauth"]["scopes"] == ["mcp:tools"]


def test_skill_md_keeps_the_honesty_rules():
    text = (ROOT / "SKILL.md").read_text(encoding="utf-8")
    assert "verbatim" in text
    assert "never upgrade a raw p-value" in text
    assert "not investment advice" in text
    # no provider or restricted dataset names in the agent surface
    for banned in ("benzinga", "alpaca", "altdata", "news_alpaca", "finnhub"):
        assert banned not in text.lower(), banned


def test_readme_agent_snippet():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "quote its" in readme and "verbatim" in readme
    assert "never upgrade a raw p-value" in readme
