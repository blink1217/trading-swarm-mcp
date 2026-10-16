"""Champion source resolution: SWARM_MCP_CHAMPION_GENOME file override, else
Firestore ``champion_genome/latest`` — fail closed, cached 300 s per process.

The Firestore import is lazy (the [remote] extra), so these tests inject a stub
``google.cloud.firestore`` module instead of requiring credentials.
"""
from __future__ import annotations

import json
import sys
import types

import pytest

from swarm_mcp import champion, tournament_runner

CHAMPION_DOC = {
    "genome_id": "aaaabbbbcccc",
    "genome": {"schema_version": 3, "registry_promoted": True},
    "previous_champion": "dddd",
    "promoted_at": "2026-10-01T00:00:00Z",
}


def _fake_firestore(doc_data: dict | None, project_seen: list[str] | None = None):
    """Build a stub google.cloud.firestore module tree."""
    fake_fs = types.ModuleType("firestore")

    class _Client:
        def __init__(self, project=None):
            if project_seen is not None:
                project_seen.append(project)

        def collection(self, name):
            if name != champion.CHAMPION_DOC_COLLECTION:
                raise AssertionError(f"unexpected collection {name}")
            return self

        def document(self, doc_id):
            if doc_id != champion.CHAMPION_DOC_ID:
                raise AssertionError(f"unexpected doc id {doc_id}")
            return self

        def get(self):
            class _Snap:
                def __init__(inner):
                    inner.data = doc_data

                @property
                def exists(inner):
                    return doc_data is not None

                def to_dict(inner):
                    return dict(doc_data) if doc_data else None
            return _Snap()

    fake_fs.Client = _Client
    return fake_fs


def _inject_firestore(monkeypatch, doc_data):
    fake_google = types.ModuleType("google.cloud")
    fake_google.firestore = _fake_firestore(doc_data)
    fake_root = types.ModuleType("google")
    fake_root.cloud = fake_google
    # both `from google.cloud import firestore` lookups resolve via sys.modules
    monkeypatch.setitem(sys.modules, "google", fake_root)
    monkeypatch.setitem(sys.modules, "google.cloud", fake_google)
    monkeypatch.setitem(sys.modules, "google.cloud.firestore", fake_google.firestore)


@pytest.fixture(autouse=True)
def _fresh_cache():
    champion.reset_cache()
    yield
    champion.reset_cache()


@pytest.fixture
def no_override(monkeypatch):
    monkeypatch.delenv("SWARM_MCP_CHAMPION_GENOME", raising=False)


def test_firestore_doc_present(no_override, monkeypatch):
    seen: list[str] = []
    _inject_firestore(monkeypatch, dict(CHAMPION_DOC))
    loaded = champion.load_champion_genome(use_cache=False)
    assert loaded["genome"]["registry_promoted"] is True
    assert loaded["genome_id"] == "aaaabbbbcccc"
    assert loaded["champion_source"].startswith("firestore:initiative-121-prod/")
    assert loaded["promoted_at"] == "2026-10-01T00:00:00Z"


def test_project_env_override(no_override, monkeypatch):
    seen: list[str] = []
    fake_google = types.ModuleType("google.cloud")
    fake_google.firestore = _fake_firestore(dict(CHAMPION_DOC), seen)
    fake_root = types.ModuleType("google")
    fake_root.cloud = fake_google
    monkeypatch.setitem(sys.modules, "google", fake_root)
    monkeypatch.setitem(sys.modules, "google.cloud", fake_google)
    monkeypatch.setitem(sys.modules, "google.cloud.firestore", fake_google.firestore)
    monkeypatch.setenv("SWARM_MCP_CHAMPION_PROJECT", "alt-project")
    champion.load_champion_genome(use_cache=False)
    assert any(p == "alt-project" for p in seen)


def test_firestore_doc_missing_fails_closed(no_override, monkeypatch):
    _inject_firestore(monkeypatch, None)
    with pytest.raises(RuntimeError, match="champion unavailable"):
        champion.load_champion_genome(use_cache=False)


def test_firestore_doc_without_genome_field_fails_closed(no_override, monkeypatch):
    _inject_firestore(monkeypatch, {"genome_id": "x"})
    with pytest.raises(RuntimeError, match="champion unavailable"):
        champion.load_champion_genome(use_cache=False)


def test_import_error_fails_closed(no_override, monkeypatch):
    # no file override, no working Firestore import â€” the lazy import must be
    # fail-closed even though the real client IS installed on this machine.
    monkeypatch.delenv("SWARM_MCP_CHAMPION_GENOME", raising=False)
    monkeypatch.setitem(sys.modules, "google.cloud.firestore", None)
    champion.reset_cache()
    with pytest.raises(RuntimeError, match="champion unavailable"):
        champion.load_champion_genome(use_cache=False)


def test_file_override_wins_over_firestore(no_override, monkeypatch, tmp_path):
    p = tmp_path / "champ.json"
    p.write_text(json.dumps({"schema_version": 2, "screen": {}}), encoding="utf-8")
    monkeypatch.setenv("SWARM_MCP_CHAMPION_GENOME", str(p))
    loaded = champion.load_champion_genome()
    assert loaded["genome"] == {"schema_version": 2, "screen": {}}
    assert loaded["champion_source"] == f"file:{p}"
    assert loaded["genome_id"] is None


def test_cache_ttl(no_override, monkeypatch):
    _inject_firestore(monkeypatch, dict(CHAMPION_DOC))
    first = champion.load_champion_genome()
    # within the TTL the cache returns the same object without re-reading Firestore
    assert champion.load_champion_genome() is first
    champion.reset_cache()
    second = champion.load_champion_genome(use_cache=True)
    assert second is not first and second["genome"] == first["genome"]
