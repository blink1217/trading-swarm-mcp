"""market.climate: operating-area weather research must be area-specific and
derived-only — same contract as the other market.* tools. Weather is computed
per made/sold operating area (never the listing exchange/HQ), returns
sums/means/ratios/counts/buckets only, and never echoes raw per-day provider
series. Caller-supplied `areas` cover underlyings outside the shipped registry.
"""
from __future__ import annotations

import datetime as dt
import json

import httpx
import pytest

from helpers import run_async
from swarm_mcp.tools import climate_tools
from swarm_mcp.weather import climate as climate_engine

N = 14


def _arr(v, n=N):
    return [v] * n


def _forecast_daily():
    return {
        "time": [f"2026-01-{i+1:02d}" for i in range(N)],
        "temperature_2m_max": _arr(88.0),
        "temperature_2m_min": _arr(72.0),
        "precipitation_sum": _arr(0.2),
        "snowfall_sum": _arr(0.0),
        "wind_speed_10m_max": _arr(25.0),
        "wind_gusts_10m_max": _arr(40.0),
        "precipitation_probability_max": _arr(55.0),
    }


def _baseline_daily():
    return {
        "time": [f"2025-01-{i+1:02d}" for i in range(N)],
        "temperature_2m_max": _arr(58.0),
        "temperature_2m_min": _arr(42.0),
        "precipitation_sum": _arr(0.07),
        "snowfall_sum": _arr(0.0),
        "wind_speed_10m_max": _arr(12.0),
        "wind_gusts_10m_max": _arr(20.0),
    }


@pytest.fixture
def mock_weather(monkeypatch):
    calls: list[str] = []
    seen = set()

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        calls.append(url)
        if "/forecast" in url:
            return httpx.Response(200, json={"daily": _forecast_daily()})
        return httpx.Response(200, json={"daily": _baseline_daily()})

    climate_engine.reset_cache()
    climate_engine.set_transport(httpx.MockTransport(handler))
    yield {"calls": calls}
    climate_engine.set_transport(None)
    run_async(climate_engine.close())
    climate_engine.reset_cache()


def _registry_symbol(name: str) -> dict:
    entry = climate_engine.registry_entry(name)
    assert entry is not None, f"{name} not in registry"
    return entry


def test_registry_is_area_specific(tmp_cache):
    xom = _registry_symbol("XOM")
    assert xom["sector"] == "energy"
    assert len(xom["areas"]) >= 4
    names = {a["name"].lower() for a in xom["areas"]}
    assert any("gulf coast" in n for n in names)
    assert all("lat" in a and "lon" in a and "weight" in a for a in xom["areas"])


def test_market_climate_registry_symbol_derived_only(tmp_cache, mock_weather):
    out = run_async(climate_tools.climate_snapshot(symbols=["XOM"]))
    assert out["tool"] == "market.climate"
    assert out["covered"] == ["XOM"]
    assert out["uncovered"] == []
    res = out["climate"]["XOM"]
    assert res["status"] == "OK"
    assert res["sector"] == "energy"
    assert res["footprint"].startswith("6/6")
    assert len(res["areas"]) == 6
    first = res["areas"][0]
    assert first["area"] and "weight" in first
    assert first["anomaly"]["temp_bucket"] == "extreme_warm"
    assert "extreme_warm" in first["signals"] and "extreme_wet" in first["signals"]
    assert first["forecast"]["hdd_14d"] == 0.0
    assert first["forecast"]["cdd_14d"] == 210.0
    assert first["baseline_year_ago"]["hdd_14d"] == 210.0
    assert abs(first["anomaly"]["precip_ratio_vs_year_ago"] - 2.86) < 0.01
    assert res["aggregate"]["temp_bucket"] == "extreme_warm"
    blob = json.dumps(out)
    assert "temperature_2m_max" not in blob
    assert '"precipitation_sum"' not in blob
    assert '"time"' not in blob
    assert out["not_investment_advice"] is True
    assert "operating area" in res["geography_note"]
    assert "London" not in blob or "never" in blob


def test_market_climate_unknown_symbol_requires_areas(tmp_cache, mock_weather):
    out = run_async(climate_tools.climate_snapshot(symbols=["ZZZZ"]))
    assert out["uncovered"] == ["ZZZZ"]
    res = out["climate"]["ZZZZ"]
    assert res["status"] == "UNKNOWN_SYMBOL"
    assert "registry_coverage" in res
    assert mock_weather["calls"] == []


def test_market_climate_caller_supplied_areas(tmp_cache, mock_weather):
    areas = [
        {"symbol": "ZZZZ", "name": "New York metro (made+sold)", "lat": 40.71, "lon": -74.0, "weight": 0.6},
        {"symbol": "ZZZZ", "name": "Chicago metro (made+sold)", "lat": 41.88, "lon": -87.63, "weight": 0.4},
    ]
    out = run_async(climate_tools.climate_snapshot(symbols=["ZZZZ"], areas=areas))
    res = out["climate"]["ZZZZ"]
    assert res["status"] == "OK"
    assert [a["area"] for a in res["areas"]] == ["New York metro (made+sold)", "Chicago metro (made+sold)"]
    assert res["footprint"].startswith("2/2")
    assert out["covered"] == ["ZZZZ"]


def test_market_climate_dedupes_shared_geography(tmp_cache, mock_weather):
    shared = {"symbol": "ZZZZ", "lat": 40.71, "lon": -74.0, "weight": 1.0}
    areas = [dict(shared, name="area one"), dict(shared, name="area two")]
    run_async(climate_tools.climate_snapshot(symbols=["ZZZZ"], areas=areas))
    forecast_calls = [u for u in mock_weather["calls"] if "/forecast" in u]
    archive_calls = [u for u in mock_weather["calls"] if "archive" in u]
    assert len(forecast_calls) == 1
    assert len(archive_calls) == 1


def test_market_climate_requires_symbols(tmp_cache, mock_weather):
    out = run_async(climate_tools.climate_snapshot(symbols=[]))
    assert "provide symbols" in out["error"]


def test_market_climate_rejects_bad_area_geo(tmp_cache, mock_weather):
    out = run_async(climate_tools.climate_snapshot(
        symbols=["ZZZZ"], areas=[{"symbol": "ZZZZ", "lat": 999.0, "lon": 0.0}]))
    assert "lat/lon out of range" in out["error"]
    out2 = run_async(climate_tools.climate_snapshot(
        symbols=["ZZZZ"], areas=[{"symbol": "", "lat": 40.0, "lon": -74.0}]))
    assert "needs a symbol" in out2["error"]


def test_area_climate_pure_math(tmp_cache):
    windows = {
        "forecast_start": "2026-01-01", "forecast_end": "2026-01-14",
        "baseline_start": "2025-01-01", "baseline_end": "2025-01-14",
        "forecast": _forecast_daily(), "baseline": _baseline_daily(),
    }
    m = climate_engine.area_climate(40.71, -74.0, windows)
    assert m["anomaly"]["temp_anomaly_f"] == 30.0
    assert m["anomaly"]["temp_bucket"] == "extreme_warm"
    assert m["forecast"]["cdd_14d"] == 210.0
    assert m["baseline_year_ago"]["hdd_14d"] == 210.0
    assert m["forecast"]["precip_in"] == 2.8
    assert m["forecast"]["precip_prob_max_mean_pct"] == 55.0
    assert m["forecast"]["max_wind_mph"] == 25.0
    assert m["anomaly"]["wind_ratio_vs_year_ago"] == pytest.approx(2.08, abs=0.01)


def test_area_climate_fetch_error_is_explicit(tmp_cache):
    m = climate_engine.area_climate(40.71, -74.0, {"error": "provider_fetch_failed"})
    assert m["status"] == "FETCH_ERROR"
