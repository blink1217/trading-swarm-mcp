"""audit_features: lookahead/actuals violations, fail-closed tiers, coverage gate."""
from __future__ import annotations

import pytest

from helpers import run_async

from swarm_mcp.tools import warden_tools


def test_flags_banned_actuals_source_and_pre_tape_feature():
    manifest = [
        {"name": "mom_5d", "source": "bars_1day", "value_ts": "2026-01-15", "as_of": "2026-01-15"},
        {"name": "open_meteo_forecast_anomaly", "source": "open-meteo.archive",
         "value_ts": "2024-06-01", "as_of": "2024-06-01"},
        {"name": "finnhub_sentiment", "source": "finnhub",
         "value_ts": "2024-06-01", "as_of": "2024-06-01"},
    ]
    r = run_async(warden_tools.audit_features(manifest, tape_started="2025-01-01"))
    assert r["verdict"] == "VIOLATIONS_FOUND"
    by_name = {f["name"]: f for f in r["features"]}
    assert by_name["mom_5d"]["status"] == "OK"
    assert by_name["open_meteo_forecast_anomaly"]["status"] == "VIOLATION"
    assert "actuals" in by_name["open_meteo_forecast_anomaly"]["detail"]
    assert by_name["finnhub_sentiment"]["status"] == "VIOLATION"
    assert "predates tape start" in by_name["finnhub_sentiment"]["detail"]


def test_backdated_decision_row_flagged():
    manifest = [
        {"name": "rsi_14", "source": "bars_1day", "value_ts": "2026-01-15", "as_of": "2026-01-15"},
    ]
    r = run_async(warden_tools.audit_features(manifest, tape_started="2026-02-01"))
    assert r["verdict"] == "VIOLATIONS_FOUND"
    assert any("predates tape start" in v for v in r["violations"])


def test_clean_manifest_passes():
    manifest = [
        {"name": "rsi_14", "source": "bars_1day", "value_ts": "2026-01-15", "as_of": "2026-01-15"},
        {"name": "atr", "source": "bars_1day", "value_ts": "2026-01-15", "as_of": "2026-01-15"},
    ]
    r = run_async(warden_tools.audit_features(manifest, tape_started="2025-01-01"))
    assert r["verdict"] == "CLEAN"
    assert r["violations"] == []


def test_tier_bc_without_tape_baseline_fails_closed():
    manifest = [
        {"name": "earnings_flag", "source": "finnhub", "value_ts": "2026-01-15", "as_of": "2026-01-15"},
    ]
    r = run_async(warden_tools.audit_features(manifest))
    assert r["verdict"] == "VIOLATIONS_FOUND"
    assert any("tape_started" in v for v in r["violations"])


def test_unknown_feature_fails_closed_to_tier_c():
    manifest = [
        {"name": "mystery_alpha", "source": "some_vendor", "value_ts": "2026-01-15", "as_of": "2026-01-15"},
    ]
    r = run_async(warden_tools.audit_features(manifest))
    f = r["features"][0]
    assert f["tier"] == "C"
    assert f["status"] == "VIOLATION"


def test_coverage_below_min_flagged():
    r = run_async(warden_tools.audit_features(
        [], selected_symbols=["A", "B", "C", "D", "E"], snapshot_universe=["A", "B"]))
    assert r["coverage"]["fraction"] == pytest.approx(0.4)
    assert r["coverage"]["meets"] is False
    assert r["coverage"]["min_coverage"] == 0.90


def test_coverage_none_selection_is_callers_choice():
    r = run_async(warden_tools.audit_features([], selected_symbols=[]))
    assert r["coverage"]["fraction"] is None
    assert r["coverage"]["meets"] is True
