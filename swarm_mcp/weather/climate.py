"""Operating-area weather engine for the data server.

Weather is derived from two keyless Open-Meteo windows per OPERATING AREA of
the underlying — the 14-day forecast and the same 14 calendar days one year
prior (baseline) — so a London forecast never stands in for a product made
and sold in New York. Areas come from the shipped operating-area registry
(10-K-style made/sold footprint) or from caller-supplied coordinates.

Every output value is derived: sums, means, ratios, counts, and label buckets.
No raw per-day provider series are echoed to the caller.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import math
import time
from pathlib import Path

import httpx

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
FORECAST_DAYS = 14

_DAILY_COMMON = (
    "temperature_2m_max,temperature_2m_min,precipitation_sum,snowfall_sum,"
    "wind_speed_10m_max,wind_gusts_10m_max"
)
_FORECAST_DAILY = f"{_DAILY_COMMON},precipitation_probability_max"

BASE = 65.0
HEAT_DAY_F = 90.0
FREEZE_DAY_F = 32.0
WET_DAY_IN = 0.1

_SEM = asyncio.Semaphore(4)
_CACHE: dict[tuple[float, float], tuple[float, dict]] = {}
_CACHE_TTL_S = 1800.0
_REGISTRY_CACHE: dict | None = None

DATA_DIR = Path(__file__).resolve().parents[1] / "data"
REGISTRY_PATH = DATA_DIR / "operating_areas.json"

_shared_client: httpx.AsyncClient | None = None
TRANSPORT: httpx.AsyncBaseTransport | None = None


def set_transport(transport: httpx.AsyncBaseTransport | None) -> None:
    """Test hook: inject an httpx transport (e.g. MockTransport) into the
    shared client; pass None to return to the live network."""
    global TRANSPORT
    TRANSPORT = transport


def reset_cache() -> None:
    _CACHE.clear()


async def _client() -> httpx.AsyncClient:
    global _shared_client
    if _shared_client is None:
        _shared_client = httpx.AsyncClient(timeout=15, transport=TRANSPORT,
                                           headers={"User-Agent": "1.21-initiative/quant-swarm"})
    return _shared_client


async def _close_client() -> None:
    global _shared_client
    if _shared_client is not None:
        await _shared_client.aclose()
        _shared_client = None


def _reg(_x: float | None, n: int = 1) -> float | None:
    if _x is None or not math.isfinite(_x):
        return None
    return round(_x, n)


def _mean(xs: list[float]) -> float | None:
    xs = [x for x in xs if x is not None and math.isfinite(x)]
    return sum(xs) / len(xs) if xs else None


def _load_registry() -> dict:
    global _REGISTRY_CACHE
    if _REGISTRY_CACHE is None:
        with open(REGISTRY_PATH, encoding="utf-8") as f:
            _REGISTRY_CACHE = json.load(f)["registry"]
    return _REGISTRY_CACHE


def registry_symbols() -> list[str]:
    return list(_load_registry())


def registry_entry(symbol: str) -> dict | None:
    entry = _load_registry().get(symbol.strip().upper())
    if entry is None:
        return None
    return {"symbol": symbol.strip().upper(), "sector": entry["sector"],
            "segment": entry.get("segment", ""), "areas": entry["areas"]}


def _windows() -> tuple[dt.date, dt.date, dt.date, dt.date]:
    fc_start = dt.datetime.now(dt.timezone.utc).date()
    fc_end = fc_start + dt.timedelta(days=FORECAST_DAYS - 1)
    try:
        base_start = fc_start.replace(year=fc_start.year - 1)
    except ValueError:
        base_start = fc_start.replace(year=fc_start.year - 1, day=28)
    base_end = fc_end.replace(year=fc_end.year - 1)
    return fc_start, fc_end, base_start, base_end


def _cluster_key(lat: float, lon: float) -> tuple[float, float]:
    return (round(lat, 1), round(lon, 1))


async def _get_windows(client: httpx.AsyncClient, lat: float, lon: float) -> dict:
    fc_start, fc_end, base_start, base_end = _windows()
    common = {"latitude": lat, "longitude": lon, "timezone": "auto",
              "temperature_unit": "fahrenheit", "wind_speed_unit": "mph",
              "precipitation_unit": "inch"}
    fc_r = await client.get(FORECAST_URL, params={
        **common, "daily": _FORECAST_DAILY, "forecast_days": FORECAST_DAYS})
    fc_r.raise_for_status()
    base_r = await client.get(ARCHIVE_URL, params={
        **common, "daily": _DAILY_COMMON,
        "start_date": base_start.isoformat(), "end_date": base_end.isoformat()})
    base_r.raise_for_status()
    return {
        "forecast": fc_r.json().get("daily", {}) or {},
        "baseline": base_r.json().get("daily", {}) or {},
        "forecast_start": fc_start.isoformat(),
        "forecast_end": fc_end.isoformat(),
        "baseline_start": base_start.isoformat(),
        "baseline_end": base_end.isoformat(),
    }


async def _windows_for_cluster(client: httpx.AsyncClient, lat: float, lon: float) -> dict | None:
    key = _cluster_key(lat, lon)
    now = time.monotonic()
    hit = _CACHE.get(key)
    if hit and now - hit[0] < _CACHE_TTL_S:
        return hit[1]
    async with _SEM:
        hit = _CACHE.get(key)
        if hit and time.monotonic() - hit[0] < _CACHE_TTL_S:
            return hit[1]
        try:
            data = await _get_windows(client, lat, lon)
        except Exception:
            data = {"error": "provider_fetch_failed"}
        _CACHE[key] = (time.monotonic(), data)
        return data


def _daily_arrays(daily: dict, keys: list[str]) -> dict[str, list[float]]:
    out = {}
    for k in keys:
        vals = daily.get(k)
        if not isinstance(vals, list):
            out[k] = []
            continue
        cleaned = []
        for v in vals:
            try:
                cleaned.append(float(v))
            except (TypeError, ValueError):
                continue
        out[k] = cleaned
    return out


def _window_metrics(daily: dict) -> dict:
    keys = ["temperature_2m_max", "temperature_2m_min", "precipitation_sum",
            "snowfall_sum", "wind_speed_10m_max", "wind_gusts_10m_max"]
    a = _daily_arrays(daily, keys)
    tmax, tmin = a["temperature_2m_max"], a["temperature_2m_min"]
    mid = [(h + l) / 2.0 for h, l in zip(tmax, tmin)]
    hdd = sum(max(0.0, BASE - m) for m in mid)
    cdd = sum(max(0.0, m - BASE) for m in mid)
    heat_days = sum(1 for h in tmax if h is not None and h >= HEAT_DAY_F)
    freeze_days = sum(1 for l in tmin if l is not None and l <= FREEZE_DAY_F)
    wet_days = sum(1 for p in a["precipitation_sum"] if p is not None and p >= WET_DAY_IN)
    snow_days = sum(1 for s in a["snowfall_sum"] if s is not None and s > 0.0)
    return {
        "tmean_f": _reg(_mean(mid), 1),
        "hdd": _reg(hdd, 1),
        "cdd": _reg(cdd, 1),
        "heat_days_ge90f": int(heat_days),
        "freeze_days_le32f": int(freeze_days),
        "wet_days_ge0_1in": int(wet_days),
        "snow_days": int(snow_days),
        "precip_in": _reg(sum(a["precipitation_sum"]), 2),
        "snow_in": _reg(sum(a["snowfall_sum"]), 2),
        "max_wind_mph": _reg(max(a["wind_speed_10m_max"]) if a["wind_speed_10m_max"] else None, 1),
        "max_gust_mph": _reg(max(a["wind_gusts_10m_max"]) if a["wind_gusts_10m_max"] else None, 1),
    }


def _temp_bucket(anomaly_f: float | None) -> str | None:
    if anomaly_f is None:
        return None
    if anomaly_f >= 8.0:
        return "extreme_warm"
    if anomaly_f >= 3.0:
        return "warm"
    if anomaly_f <= -8.0:
        return "extreme_cold"
    if anomaly_f <= -3.0:
        return "cold"
    return "near_normal"


def _precip_ratio(fc: float | None, base: float | None) -> float | None:
    if fc is None:
        return None
    if not base or base <= 0.0:
        return 1.0 if fc < 0.05 else None
    return fc / base


def _precip_bucket(ratio: float | None, base: float | None, fc: float | None) -> str | None:
    if base is None or base <= 0.0:
        if fc is not None and fc >= 0.05:
            return "new_rain"
        return "near_normal"
    if ratio is None:
        return None
    if ratio >= 2.0:
        return "extreme_wet"
    if ratio >= 1.3:
        return "wet"
    if ratio <= 0.4:
        return "extreme_dry"
    if ratio <= 0.7:
        return "dry"
    return "near_normal"


def _snow_outlook(fc_snow: float | None, fc_days: int, base_days: int) -> str | None:
    if fc_snow is None:
        return None
    if fc_days > 0:
        return "heavy_snow_risk" if fc_snow >= 4.0 else "snow"
    if base_days > 0 and fc_days == 0:
        return "snow_absent_vs_year_ago"
    return "none"


def _wind_ratio(fc: float | None, base: float | None) -> float | None:
    if fc is None or not base or base <= 0.0:
        return None
    return fc / base


def _pct_delta(fc: float | None, base: float | None) -> float | None:
    if fc is None or base is None:
        return None
    if abs(base) < 1e-9:
        return None
    return (fc - base) / abs(base) * 100.0


def area_climate(lat: float, lon: float, windows: dict) -> dict:
    """Derived climate metrics for one operating area (pure function of the
    two Open-Meteo windows). No raw per-day series are returned."""
    fc = windows.get("forecast", {})
    base = windows.get("baseline", {})
    if windows.get("error") or (not fc and not base):
        return {"status": "FETCH_ERROR", "detail": windows.get("error", "provider_fetch_failed")}
    fc_m = _window_metrics(fc)
    base_m = _window_metrics(base)
    anomaly = None
    if fc_m["tmean_f"] is not None and base_m["tmean_f"] is not None:
        anomaly = round(fc_m["tmean_f"] - base_m["tmean_f"], 1)
    p_ratio = _precip_ratio(fc_m["precip_in"], base_m["precip_in"])
    signal_labels = []
    t_bucket = _temp_bucket(anomaly)
    if t_bucket in ("extreme_warm", "warm", "extreme_cold", "cold"):
        signal_labels.append(t_bucket)
    snow_l = _snow_outlook(fc_m["snow_in"], fc_m["snow_days"], base_m["snow_days"])
    if snow_l in ("heavy_snow_risk", "snow", "snow_absent_vs_year_ago"):
        signal_labels.append(snow_l)
    if fc_m["freeze_days_le32f"] and base_m["freeze_days_le32f"] == 0:
        signal_labels.append("freeze_days_vs_none_year_ago")
    if fc_m["heat_days_ge90f"] and base_m["heat_days_ge90f"] == 0:
        signal_labels.append("heat_days_vs_none_year_ago")
    p_bucket = _precip_bucket(p_ratio, base_m["precip_in"], fc_m["precip_in"])
    if p_bucket in ("extreme_wet", "wet", "extreme_dry", "dry", "new_rain"):
        signal_labels.append(p_bucket)
    probs = _daily_arrays(fc, ["precipitation_probability_max"])["precipitation_probability_max"]
    return {
        "forecast": {
            "tmean_f": fc_m["tmean_f"],
            "hdd_14d": fc_m["hdd"],
            "cdd_14d": fc_m["cdd"],
            "heat_days_ge90f": fc_m["heat_days_ge90f"],
            "freeze_days_le32f": fc_m["freeze_days_le32f"],
            "wet_days": fc_m["wet_days_ge0_1in"],
            "snow_days": fc_m["snow_days"],
            "precip_in": fc_m["precip_in"],
            "snow_in": fc_m["snow_in"],
            "max_wind_mph": fc_m["max_wind_mph"],
            "max_gust_mph": fc_m["max_gust_mph"],
            "precip_prob_max_mean_pct": _reg(_mean(probs), 0),
        },
        "baseline_year_ago": {
            "tmean_f": base_m["tmean_f"],
            "hdd_14d": base_m["hdd"],
            "cdd_14d": base_m["cdd"],
            "heat_days_ge90f": base_m["heat_days_ge90f"],
            "freeze_days_le32f": base_m["freeze_days_le32f"],
            "wet_days": base_m["wet_days_ge0_1in"],
            "snow_days": base_m["snow_days"],
            "precip_in": base_m["precip_in"],
            "snow_in": base_m["snow_in"],
            "max_wind_mph": base_m["max_wind_mph"],
            "max_gust_mph": base_m["max_gust_mph"],
        },
        "anomaly": {
            "temp_anomaly_f": anomaly,
            "temp_bucket": t_bucket,
            "hdd_pct_vs_year_ago": _reg(_pct_delta(fc_m["hdd"], base_m["hdd"]), 0),
            "cdd_pct_vs_year_ago": _reg(_pct_delta(fc_m["cdd"], base_m["cdd"]), 0),
            "freeze_days_delta": (fc_m["freeze_days_le32f"] - base_m["freeze_days_le32f"]
                                  if base_m["freeze_days_le32f"] is not None else None),
            "heat_days_delta": (fc_m["heat_days_ge90f"] - base_m["heat_days_ge90f"]
                                if base_m["heat_days_ge90f"] is not None else None),
            "precip_ratio_vs_year_ago": _reg(p_ratio, 2),
            "precip_bucket": p_bucket,
            "wind_ratio_vs_year_ago": _reg(_wind_ratio(fc_m["max_wind_mph"], base_m["max_wind_mph"]), 2),
            "snow_outlook": snow_l,
        },
        "signals": signal_labels,
        "windows": {
            "forecast": f'{windows.get("forecast_start")}..{windows.get("forecast_end")}',
            "baseline": f'{windows.get("baseline_start")}..{windows.get("baseline_end")}',
            "baseline_note": "same 14 calendar days one year prior",
        },
    }


def _wmean(metrics: list[dict], weights: list[float], sub: str, field: str) -> float | None:
    vals = []
    ws = []
    for m, w in zip(metrics, weights):
        v = (m.get(sub) or {}).get(field)
        if v is not None and isinstance(v, (int, float)):
            vals.append(v)
            ws.append(w)
    if not vals:
        return None
    return _reg(sum(v * w for v, w in zip(vals, ws)) / (sum(ws) or 1.0),
                2 if isinstance(vals[0], float) else 1)


def aggregate_areas(areas: list[dict], metrics: list[dict]) -> dict:
    """Weighted symbol-level aggregate over its operating areas."""
    ok = [(a, m) for a, m in zip(areas, metrics) if m.get("status") != "FETCH_ERROR"]
    out: dict = {"areas_covered": len(ok), "areas_total": len(areas)}
    if not ok:
        return out
    w_sum = sum(a["weight"] for a in areas) or 1.0
    areas_ok = [a for a, _ in ok]
    metrics_ok = [m for _, m in ok]
    weights = [a["weight"] / w_sum for a in areas_ok]
    anom = [(m["anomaly"]["temp_anomaly_f"], w) for m, w in zip(metrics_ok, weights)
            if m["anomaly"]["temp_anomaly_f"] is not None]
    anomaly_f = round(sum(v * w for v, w in anom) / sum(w for _, w in anom), 1) if anom else None
    pvals = [(m["anomaly"]["precip_ratio_vs_year_ago"], w) for m, w in zip(metrics_ok, weights)
             if m["anomaly"]["precip_ratio_vs_year_ago"] is not None]
    precip_ratio = (round(sum(v * w for v, w in pvals) / sum(w for _, w in pvals), 2) if pvals else None)
    p_bucket = None
    if precip_ratio is not None:
        p_bucket = (_precip_bucket(precip_ratio, 1.0, 1.0))
    notable = []
    for a, m in zip(areas_ok, metrics_ok):
        for s in m["signals"]:
            notable.append({"area": a["name"],
                            "weight_pct": round(100 * a["weight"] / w_sum, 1),
                            "condition": s})
    notable.sort(key=lambda n: -n["weight_pct"])
    out.update({
        "temp_anomaly_f": anomaly_f,
        "temp_bucket": _temp_bucket(anomaly_f),
        "precip_ratio_vs_year_ago": precip_ratio,
        "precip_bucket": p_bucket,
        "weighted_hdd_14d": _wmean(metrics_ok, weights, "forecast", "hdd_14d"),
        "weighted_cdd_14d": _wmean(metrics_ok, weights, "forecast", "cdd_14d"),
        "weighted_heat_days_ge90f": _wmean(metrics_ok, weights, "forecast", "heat_days_ge90f"),
        "weighted_freeze_days_le32f": _wmean(metrics_ok, weights, "forecast", "freeze_days_le32f"),
        "weighted_precip_in": _wmean(metrics_ok, weights, "forecast", "precip_in"),
        "notable": notable[:6],
    })
    return out


async def fetch_symbol_climate(symbol: str, client: httpx.AsyncClient | None = None,
                               areas_override: list[dict] | None = None) -> dict:
    """Full derived climate snapshot for one symbol across its operating areas.

    `areas_override` replaces the registry footprint for this symbol with
    caller-supplied made/sold geographies [{name?, lat, lon, weight?}].
    Returns UNKNOWN_SYMBOL status when no footprint is available.
    """
    sym = symbol.strip().upper()
    entry = registry_entry(sym)
    raw_areas = areas_override if areas_override is not None else (entry["areas"] if entry else None)
    if not raw_areas:
        return {"symbol": sym, "status": "UNKNOWN_SYMBOL",
                "detail": "no operating areas in the registry — pass areas=[...] for this underlying",
                "registry_coverage": len(registry_symbols())}
    client = client or await _client()
    areas = []
    for a in raw_areas:
        areas.append({"name": str(a.get("name") or "operating area"),
                      "lat": float(a["lat"]), "lon": float(a["lon"]),
                      "weight": float(a.get("weight", 1.0) or 1.0)})
    metrics = []
    for a in areas:
        windows = await _windows_for_cluster(client, a["lat"], a["lon"])
        metrics.append(area_climate(a["lat"], a["lon"], windows))
    areas_out = []
    for a, m in zip(areas, metrics):
        row = {"area": a["name"], "lat": round(a["lat"], 2), "lon": round(a["lon"], 2),
               "weight": a["weight"]}
        if m.get("status") == "FETCH_ERROR":
            row["status"] = "FETCH_ERROR"
        else:
            row.update(m)
        areas_out.append(row)
    agg = aggregate_areas(areas, metrics)
    return {
        "symbol": sym,
        "status": "OK" if agg["areas_covered"] else "FETCH_ERROR",
        "sector": (entry or {}).get("sector"),
        "segment": (entry or {}).get("segment"),
        "footprint": f"{agg['areas_covered']}/{agg['areas_total']} operating areas",
        "geography_note": ("weather is per operating area where the product is made and/or sold "
                           "— never the listing exchange or HQ city"),
        "aggregate": {k: v for k, v in agg.items() if k != "notable"},
        "areas": areas_out,
        "notable": agg["notable"],
        "evidence_note": ("14-day FORECAST anomaly — never valid for a past decision date; "
                          "point-in-time claims require recorded tape values"),
        "methodology": ("derived from keyless Open-Meteo: 14-day forecast vs the same 14 calendar days "
                        "one year prior (baseline), exposure-weighted per area. Sums, means, ratios, "
                        "counts and buckets only — no raw per-day series are returned"),
    }


async def close() -> None:
    await _close_client()
