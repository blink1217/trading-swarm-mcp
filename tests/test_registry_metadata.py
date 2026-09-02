"""Registry metadata cannot drift from the package: server.json must pin the
same version as pyproject.toml, name the real package + console scripts, and
point the remote entry at the hosted streamable-HTTP endpoint."""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _pyproject_version() -> str:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    m = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    assert m, "pyproject.toml has no version"
    return m.group(1)


def _pyproject_name() -> str:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    m = re.search(r'^name\s*=\s*"([^"]+)"', text, re.MULTILINE)
    assert m, "pyproject.toml has no name"
    return m.group(1)


def _server_json() -> dict:
    return json.loads((ROOT / "server.json").read_text(encoding="utf-8"))


def test_server_json_versions_match_pyproject():
    expected = _pyproject_version()
    meta = _server_json()
    packages = meta.get("packages") or []
    assert packages, "server.json lists no packages"
    for pkg in packages:
        assert pkg["version"] == expected, (
            f"server.json package {pkg.get('identifier')} pins {pkg.get('version')}, "
            f"pyproject.toml is {expected}")
    assert meta["version"] == expected, (
        f"server.json top-level version is {meta.get('version')}, "
        f"pyproject.toml is {expected}")


def test_server_json_identity():
    meta = _server_json()
    assert meta["name"] == "io.github.blink1217/quant-swarm"
    for server in ("swarm-data-mcp", "swarm-warden-mcp", "swarm-gym-mcp"):
        assert any(
            server in [a["value"] for a in p["packageArguments"] if a["type"] == "positional"]
            for p in meta["packages"]
        ), f"{server} missing"
        assert all(p["registryType"] == "pypi" for p in meta["packages"])
        assert all(p["identifier"] == "quant-swarm" for p in meta["packages"])
    for pkg in meta["packages"]:
        args = pkg.get("runtimeArguments")
        assert args, f"runtimeArguments missing for {pkg['identifier']}"
        assert {"type": "named", "name": "--from", "value": "quant-swarm"} in args, (
            f"runtime --from quant-swarm missing for {pkg['identifier']}")
    remotes = meta.get("remotes") or []
    assert remotes and remotes[0]["type"] == "streamable-http"
    assert remotes[0]["url"].startswith("https://swarm-mcp-")
    assert remotes[0]["url"].endswith("/mcp/data")


def test_launcher_scripts_reference_pyproject_package_name():
    """Launcher artifacts must invoke the package under its pyproject name, so
    the uvx/registry command line and the launcher scripts stay in sync."""
    name = _pyproject_name()
    files = [
        ROOT / "npm" / "bin" / "quant-swarm-mcp.js",
        ROOT / "scripts" / "make_deeplinks.py",
        ROOT / "smithery.yaml",
        ROOT / ".cursor" / "mcp.json",
    ]
    for path in files:
        assert path.exists(), f"{path.relative_to(ROOT)} does not exist"
        assert name in path.read_text(encoding="utf-8"), (
            f"{path.relative_to(ROOT)} must reference pyproject name {name!r}")


def test_remote_base_url_is_single_sourced():
    """The hosted endpoint URL must agree across every artifact that states
    it, so a region/project move cannot silently break remote clients."""
    from urllib.parse import urlparse

    from swarm_mcp import server_meta
    from swarm_mcp.servers.http_server import DEFAULT_ALLOWED_HOSTS

    base = server_meta.REMOTE_BASE_URL
    assert base.startswith("https://swarm-mcp-"), "canonical remote URL in server_meta"
    assert urlparse(base).hostname in DEFAULT_ALLOWED_HOSTS, (
        "http_server's allowlist must include the canonical remote host")
    meta = _server_json()
    assert meta["remotes"][0]["url"].startswith(base), "server.json remote must match server_meta"
    files = [
        ROOT / "npm" / "bin" / "quant-swarm-mcp.js",
        ROOT / "integrations" / "llama-index" / "llama_index" / "tools" / "quant_swarm" / "base.py",
        ROOT / "integrations" / "langchain" / "README.md",
        ROOT / "README.md",
    ]
    for path in files:
        assert base in path.read_text(encoding="utf-8"), (
            f"{path.relative_to(ROOT)} must reference the canonical remote URL")