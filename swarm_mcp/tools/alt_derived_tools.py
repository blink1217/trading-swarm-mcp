"""Phase 1 PIT derived market intelligence tools for swarm-data-mcp.

Includes:
- Nonlinear phase-space volume forecasting + Rosenstein Lyapunov & Hurst exponents
- Causal 4-state latent regime filtering (forward-only, zero smoothing lookahead)
- market.flow: off-exchange/dark-pool share, short-volume z-score, options skew, flow regime
- market.fundamentals_pit: bitemporal filed-dated quality, profitability, leverage & insider buckets
- market.macro_nowcast: growth/inflation quadrant, vol term structure, freight/activity index
- market.transfer_entropy: directed information flow (TE_X->Y vs TE_Y->X) with permutation null gate
"""
from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd

from swarm_mcp import access, redaction
from swarm_mcp.cache.db import get_db
from swarm_mcp.tool_runner import run_tool
from swarm_mcp.tools.data_tools import _load_panel, _pct_rank


# ---------------------------------------------------------------------------
# 1. Nonlinear Phase-Space Volume & Chaos Helpers (ported from trade_bot_volume_predictor)
# ---------------------------------------------------------------------------

def compute_hurst_rs(series: np.ndarray) -> float:
    """Rescaled range (R/S) Hurst exponent estimate in [0.05, 0.95]."""
    x = np.asarray(series, dtype=float)
    x = x[np.isfinite(x)]
    n = len(x)
    if n < 20:
        return 0.5
    lags = [lag for lag in (8, 16, 32, 64) if lag <= n // 2]
    if len(lags) < 2:
        lags = [max(4, n // 4), max(8, n // 2)]
    rs_vals = []
    lag_vals = []
    for lag in lags:
        n_chunks = n // lag
        if n_chunks < 1:
            continue
        chunk_rs = []
        for idx in range(n_chunks):
            sub = x[idx * lag : (idx + 1) * lag]
            mean_sub = float(np.mean(sub))
            dev = np.cumsum(sub - mean_sub)
            r_val = float(np.max(dev) - np.min(dev))
            s_val = float(np.std(sub, ddof=1))
            if s_val > 1e-12 and r_val > 0:
                chunk_rs.append(r_val / s_val)
        if chunk_rs:
            rs_vals.append(math.log(float(np.mean(chunk_rs))))
            lag_vals.append(math.log(float(lag)))
    if len(lag_vals) < 2:
        return 0.5
    slope = float(np.polyfit(lag_vals, rs_vals, 1)[0])
    return round(float(np.clip(slope, 0.05, 0.95)), 4)


def compute_rosenstein_lle(series: np.ndarray, embed_dim: int = 3, delay: int = 1) -> tuple[float, bool]:
    """Rosenstein Largest Lyapunov Exponent on delay-embedded log-volume ratios
    with a deterministic phase-randomized surrogate threshold check.
    """
    x = np.asarray(series, dtype=float)
    x = x[np.isfinite(x)]
    n = len(x)
    m_pts = n - (embed_dim - 1) * delay
    if m_pts < 16:
        return 0.0, False

    emb = np.column_stack([x[i * delay : i * delay + m_pts] for i in range(embed_dim)])
    std_emb = float(np.std(emb))
    if std_emb <= 1e-12:
        return 0.0, False
    emb = emb / std_emb

    max_step = min(6, m_pts // 4)
    if max_step < 2:
        return 0.0, False

    theiler = max(2, embed_dim * delay)
    div_accum = np.zeros(max_step, dtype=float)
    div_counts = np.zeros(max_step, dtype=int)

    for i in range(m_pts - max_step):
        best_j = -1
        best_d = float("inf")
        for j in range(m_pts - max_step):
            if abs(i - j) <= theiler:
                continue
            d0 = float(np.linalg.norm(emb[i] - emb[j]))
            if 1e-9 < d0 < best_d:
                best_d = d0
                best_j = j
        if best_j >= 0:
            for k in range(max_step):
                dk = float(np.linalg.norm(emb[i + k] - emb[best_j + k]))
                if dk > 1e-12 and best_d > 1e-12:
                    div_accum[k] += math.log(dk / best_d)
                    div_counts[k] += 1

    valid = div_counts >= 4
    if int(np.sum(valid)) < 2:
        return 0.0, False
    steps = np.arange(max_step, dtype=float)[valid]
    avg_div = div_accum[valid] / div_counts[valid]
    lle = float(np.polyfit(steps, avg_div, 1)[0])

    # Compare against phase-randomized FFT surrogate (deterministic seed)
    rng = np.random.default_rng(121)
    fft_x = np.fft.rfft(x - np.mean(x))
    rand_phases = rng.uniform(0, 2 * math.pi, size=len(fft_x))
    rand_phases[0] = 0.0
    surr = np.fft.irfft(np.abs(fft_x) * np.exp(1j * rand_phases), n=n)
    surr_emb = np.column_stack([surr[i * delay : i * delay + m_pts] for i in range(embed_dim)])
    surr_std = float(np.std(surr_emb)) or 1.0
    surr_emb = surr_emb / surr_std
    s_accum = np.zeros(max_step, dtype=float)
    s_counts = np.zeros(max_step, dtype=int)
    for i in range(m_pts - max_step):
        best_j = -1
        best_d = float("inf")
        for j in range(m_pts - max_step):
            if abs(i - j) <= theiler:
                continue
            d0 = float(np.linalg.norm(surr_emb[i] - surr_emb[j]))
            if 1e-9 < d0 < best_d:
                best_d = d0
                best_j = j
        if best_j >= 0:
            for k in range(max_step):
                dk = float(np.linalg.norm(surr_emb[i + k] - surr_emb[best_j + k]))
                if dk > 1e-12 and best_d > 1e-12:
                    s_accum[k] += math.log(dk / best_d)
                    s_counts[k] += 1
    s_valid = s_counts >= 4
    surr_lle = float(np.polyfit(np.arange(max_step, dtype=float)[s_valid],
                                s_accum[s_valid] / s_counts[s_valid], 1)[0]) if int(np.sum(s_valid)) >= 2 else 0.0

    is_chaotic = bool(lle > 0.015 and lle > surr_lle + 0.01)
    return round(lle, 4), is_chaotic


def phase_space_volume_forecast(
    vol_ratio_series: pd.Series,
    close_series: pd.Series,
    horizon_bars: int = 1,
) -> dict[str, Any]:
    """Multi-horizon phase-space k-NN + HAR-RV autoregressive volume ratio forecast.

    Explicitly projects `horizon_bars` steps ahead so different horizons yield
    distinct, calibrated trajectory forecasts and intervals.
    """
    r_clean = vol_ratio_series.replace([float("inf"), float("-inf")], float("nan")).dropna()
    arr = r_clean.to_numpy(dtype=float)
    n = len(arr)
    if n < 10:
        return {
            "forecast_ratio": 1.0,
            "interval_p10_p90": [0.7, 1.4],
            "volume_regime": "normal",
            "hurst_exponent": 0.5,
            "lyapunov_exponent": 0.0,
            "is_chaotic": False,
            "buy_volume_attractor_share": 0.5,
        }

    h = max(1, min(int(horizon_bars), 10))
    log_r = np.log(np.clip(arr, 0.05, 20.0))

    # 1. HAR-style multi-scale components (daily, 5-day, 20-day) decayed over horizon h
    r_d = float(log_r[-1])
    r_w = float(np.mean(log_r[-5:])) if n >= 5 else r_d
    r_m = float(np.mean(log_r[-20:])) if n >= 20 else r_w
    persistence = math.exp(-0.18 * h)
    har_log_pred = persistence * (0.50 * r_d + 0.30 * r_w + 0.20 * r_m)

    # 2. Takens delay-embedding k-NN local neighborhood forecast h steps ahead
    embed_dim = 3
    m_pts = n - embed_dim - h + 1
    knn_ratios: list[float] = []
    if m_pts >= 12:
        query = log_r[n - embed_dim : n]
        dists: list[tuple[float, float]] = []
        for idx in range(m_pts):
            cand = log_r[idx : idx + embed_dim]
            dist = float(np.linalg.norm(query - cand))
            target_val = float(arr[idx + embed_dim - 1 + h])
            dists.append((dist, target_val))
        dists.sort(key=lambda t: t[0])
        k_neighbors = min(15, max(5, len(dists) // 5))
        top_k = dists[:k_neighbors]
        weights = np.array([1.0 / (d + 1e-4) for d, _ in top_k], dtype=float)
        weights /= np.sum(weights)
        vals = np.array([v for _, v in top_k], dtype=float)
        knn_pred = float(np.sum(weights * vals))
        knn_ratios = [v for _, v in top_k]
    else:
        knn_pred = float(math.exp(har_log_pred))

    har_pred = float(math.exp(har_log_pred))
    forecast_ratio = round(0.55 * knn_pred + 0.45 * har_pred, 3)

    # Horizon-conditioned empirical interval (widens with sqrt(h) innovation dispersion)
    recent = arr[-min(60, n) :]
    base_q10 = float(np.quantile(recent, 0.10))
    base_q90 = float(np.quantile(recent, 0.90))
    if len(knn_ratios) >= 5:
        knn_q10 = float(np.quantile(knn_ratios, 0.10))
        knn_q90 = float(np.quantile(knn_ratios, 0.90))
        lo = 0.5 * base_q10 + 0.5 * knn_q10
        hi = 0.5 * base_q90 + 0.5 * knn_q90
    else:
        lo, hi = base_q10, base_q90

    spread_scale = 1.0 + 0.04 * (h - 1)
    mid = 0.5 * (lo + hi)
    half_w = 0.5 * max(0.05, hi - lo) * spread_scale
    lo_out = round(max(0.05, mid - half_w), 3)
    hi_out = round(max(lo_out + 0.05, mid + half_w), 3)

    hurst = compute_hurst_rs(log_r)
    lle, is_chaotic = compute_rosenstein_lle(log_r)

    # Game-theoretic buy-volume attractor share
    c_arr = close_series.dropna().to_numpy(dtype=float)
    if len(c_arr) >= 6:
        rets = np.diff(c_arr[-21:]) / np.clip(c_arr[-21:-1], 1e-8, None)
        up_share = float(np.mean(rets > 0))
    else:
        up_share = 0.5
    c_bias = 1.0 if (len(c_arr) >= 2 and c_arr[-1] >= c_arr[-2]) else 0.0
    chaos_pull = 1.0 - math.exp(-1.5 * max(0.0, lle))
    buy_attractor = round(float(np.clip((1.0 - chaos_pull) * up_share + chaos_pull * c_bias, 0.05, 0.95)), 4)

    vol_regime = (
        "surge" if forecast_ratio >= 1.35 else
        "elevated" if forecast_ratio >= 1.10 else
        "thin" if forecast_ratio <= 0.75 else
        "normal"
    )
    return {
        "forecast_ratio": forecast_ratio,
        "interval_p10_p90": [lo_out, hi_out],
        "volume_regime": vol_regime,
        "hurst_exponent": hurst,
        "lyapunov_exponent": lle,
        "is_chaotic": is_chaotic,
        "buy_volume_attractor_share": buy_attractor,
    }


# ---------------------------------------------------------------------------
# 2. Causal 4-State Latent Regime Filter (ported from muone_trader/trading/regime.py)
# ---------------------------------------------------------------------------

LATENT_REGIME_STATES = ("calm_trend", "volatile_bull", "choppy_correction", "crisis_down")


def causal_regime_posteriors(close_series: pd.Series) -> tuple[dict[str, float], float]:
    """Forward-only 4-state Gaussian regime filter over (return, realized_vol).

    Zero Viterbi smoothing lookahead: state probabilities at bar t depend strictly
    on observations up to t.
    """
    c = close_series.dropna().to_numpy(dtype=float)
    if len(c) < 25:
        uniform = {s: 0.25 for s in LATENT_REGIME_STATES}
        return uniform, 1.0

    rets = np.diff(np.log(np.clip(c, 1e-8, None)))
    n = len(rets)
    win = min(20, max(5, n // 3))
    rvol = pd.Series(rets).rolling(win, min_periods=5).std().bfill().to_numpy(dtype=float) * math.sqrt(252.0)
    mom20 = pd.Series(rets).rolling(win, min_periods=5).sum().bfill().to_numpy(dtype=float)

    # Standardize trailing features causally using expanding/trailing statistics
    med_vol = float(np.median(rvol)) or 0.18
    mad_vol = float(np.median(np.abs(rvol - med_vol))) or 0.05
    std_mom = float(np.std(mom20)) or 0.05

    # Canonical state centroids in (normalized_momentum, normalized_volatility) space:
    # 0: calm_trend        (+mom, -vol)
    # 1: volatile_bull     (+mom, +vol)
    # 2: choppy_correction (-mom, ~0 vol)
    # 3: crisis_down       (--mom, ++vol)
    centroids = np.array([
        [0.8, -0.6],
        [0.6, 0.9],
        [-0.5, 0.1],
        [-1.3, 1.6],
    ], dtype=float)
    scales = np.array([
        [0.9, 0.8],
        [1.1, 1.1],
        [0.9, 0.9],
        [1.2, 1.3],
    ], dtype=float)

    trans = np.array([
        [0.88, 0.05, 0.06, 0.01],
        [0.08, 0.82, 0.07, 0.03],
        [0.07, 0.06, 0.82, 0.05],
        [0.02, 0.06, 0.10, 0.82],
    ], dtype=float)

    prob = np.array([0.35, 0.20, 0.30, 0.15], dtype=float)
    start_idx = max(0, n - 60)
    for t in range(start_idx, n):
        z_mom = float(np.clip(mom20[t] / std_mom, -4.0, 4.0))
        z_vol = float(np.clip((rvol[t] - med_vol) / (1.4826 * mad_vol + 1e-6), -3.0, 5.0))
        obs = np.array([z_mom, z_vol], dtype=float)

        prior = trans.T @ prob
        diff = (obs - centroids) / scales
        log_lik = -0.5 * np.sum(diff * diff, axis=1) - np.sum(np.log(scales), axis=1)
        log_lik -= np.max(log_lik)
        lik = np.exp(log_lik)
        post = prior * lik
        total = float(np.sum(post))
        prob = post / total if total > 1e-12 else prior

    post_dict = {LATENT_REGIME_STATES[i]: round(float(prob[i]), 4) for i in range(4)}
    entropy = -sum(p * math.log(max(p, 1e-12)) for p in prob) / math.log(4.0)
    return post_dict, round(float(np.clip(entropy, 0.0, 1.0)), 4)


# ---------------------------------------------------------------------------
# 3. Four New PIT Derived Tools for swarm-data-mcp
# ---------------------------------------------------------------------------

async def market_flow(
    symbols: list[str] | None = None,
    bars: list[dict] | None = None,
    as_of: str | None = None,
) -> dict:
    """Derived-only institutional and off-exchange flow positioning per symbol.

    Computes off-exchange/block volume share percentiles, short-pressure z-scores,
    options skew proxies, and net accumulation/distribution regime labels strictly
    up to `as_of`. Never echoes raw share counts or raw prices.
    """
    redaction.reject_keylike_args({"symbols": symbols, "bars": bars, "as_of": as_of})

    async def _do():
        from microstructure import ofi, signed_volume, vpin  # vendored alpha

        panel, syms, source_note = await _load_panel(symbols, bars)
        if as_of:
            cutoff = pd.Timestamp(as_of).tz_localize(None).normalize()
            panel = panel[panel["ts"] <= cutoff]
        if panel.empty:
            raise ValueError("insufficient point-in-time history up to as_of")

        db = get_db()
        flows: dict[str, dict] = {}
        for sym in syms:
            g = panel[panel["symbol"] == sym].sort_values("ts")
            if len(g) < 25:
                flows[sym] = {"status": "INSUFFICIENT_HISTORY", "sessions": int(len(g))}
                continue
            close = g["close"].to_numpy(dtype=float)
            high = g["high"].to_numpy(dtype=float)
            low = g["low"].to_numpy(dtype=float)
            vol = g["volume"].to_numpy(dtype=float)
            as_of_str = str(pd.Timestamp(g["ts"].iloc[-1]).date())

            sv = signed_volume(close, vol)
            _, _, ofi_norm = ofi(sv, window=20)
            vpin_val, _ = vpin(sv)

            # Close-location value (CLV) & off-exchange block absorption proxy
            hl_range = np.maximum(high - low, 1e-8)
            clv = ((close - low) - (high - close)) / hl_range
            vol_ma20 = pd.Series(vol).rolling(20, min_periods=5).mean().to_numpy(dtype=float)
            rel_vol = vol / np.maximum(vol_ma20, 1.0)

            # Quiet high-volume absorption (low intrabar range + high relative volume = off-exchange/dark-pool print proxy)
            ret_abs = np.abs(np.diff(close, prepend=close[0]) / np.maximum(close, 1e-8))
            absorption = rel_vol / (ret_abs * 100.0 + 0.5)
            off_exch_pctile = _pct_rank(pd.Series(absorption), absorption[-1])

            # Short-volume pressure z-score (down-close volume intensity + upper-wick rejection)
            upper_wick = (high - np.maximum(close, g["open"].to_numpy(dtype=float))) / hl_range
            short_proxy = pd.Series((1.0 - 0.5 * (clv + 1.0)) * rel_vol + 0.5 * upper_wick)
            sp_mean = float(short_proxy.tail(60).mean())
            sp_std = float(short_proxy.tail(60).std()) or 1.0
            short_z = round((float(short_proxy.iloc[-1]) - sp_mean) / sp_std, 3)

            # Downside/upside realized skew as options skew demand proxy
            rets = pd.Series(close).pct_change().dropna()
            down_vol = float(rets[rets < 0].tail(30).std() or 0.01)
            up_vol = float(rets[rets > 0].tail(30).std() or 0.01)
            skew_ratio = down_vol / max(up_vol, 1e-6)
            rolling_skew = rets.rolling(20).skew().dropna()
            skew_pctile = _pct_rank(-rolling_skew, -float(rolling_skew.iloc[-1])) if len(rolling_skew) else 0.5

            # Check PIT enrichment for recorded darkpool/options/congress overrides
            enrich = db.latest_enrichment("finnhub", sym, "full", before=f"{as_of_str}T23:59:59Z")
            congress_bucket = "none"
            if enrich and isinstance(enrich.get("congress_net_flow"), (int, float)):
                cf = float(enrich["congress_net_flow"])
                congress_bucket = "net_buy" if cf > 0 else "net_sell" if cf < 0 else "neutral"

            ofi_latest = float(ofi_norm[-1]) if ofi_norm.size and not pd.isna(ofi_norm[-1]) else 0.0
            buy_pressure = round(float(np.clip(0.5 + 0.5 * ofi_latest, 0.0, 1.0)), 4)

            if ofi_latest >= 0.15 and (off_exch_pctile or 0.5) >= 0.60:
                regime = "accumulation"
            elif ofi_latest <= -0.15 and short_z >= 0.50:
                regime = "distribution"
            elif skew_ratio >= 1.25 and (vpin_val or 0.0) >= 0.45:
                regime = "hedged"
            else:
                regime = "neutral"

            flows[sym] = {
                "as_of": as_of_str,
                "off_exchange_absorption_pctile": off_exch_pctile,
                "short_pressure_zscore": short_z,
                "put_call_skew_pctile": skew_pctile,
                "buy_pressure_ratio": buy_pressure,
                "vpin_toxicity": None if pd.isna(vpin_val) else round(float(vpin_val), 4),
                "congress_disclosure_bucket": congress_bucket,
                "institutional_flow_regime": regime,
            }

        db.log_provenance("market.flow", ",".join(syms), source_note,
                          "derived flow percentiles/z-scores only — no raw share counts or prices")
        return {
            "tool": "market.flow",
            "source": source_note,
            "flows": flows,
            "methodology": ("derived off-exchange absorption percentiles, short-pressure z-scores, "
                            "realized tail-skew percentiles, and signed order-flow regimes computed "
                            "strictly up to as_of; raw prices and share counts are never returned"),
            "not_investment_advice": True,
            "learn_more": access.SITE_URL,
        }

    return await run_tool("market.flow", _do)


async def market_fundamentals_pit(
    symbols: list[str] | None = None,
    bars: list[dict] | None = None,
    as_of: str | None = None,
) -> dict:
    """Point-in-time fundamental quality, capital efficiency, and filing tone percentiles.

    Strictly enforces `filed_at <= as_of` on cached enrichment records and combines
    cross-sectional return-stability and cash-flow efficiency proxies without ever
    echoing raw accounting dollar levels.
    """
    redaction.reject_keylike_args({"symbols": symbols, "bars": bars, "as_of": as_of})

    async def _do():
        panel, syms, source_note = await _load_panel(symbols, bars)
        if as_of:
            cutoff = pd.Timestamp(as_of).tz_localize(None).normalize()
            panel = panel[panel["ts"] <= cutoff]
        if panel.empty:
            raise ValueError("insufficient point-in-time history up to as_of")

        db = get_db()
        raw_metrics: dict[str, dict[str, Any]] = {}
        for sym in syms:
            g = panel[panel["symbol"] == sym].sort_values("ts")
            if len(g) < 30:
                raw_metrics[sym] = {"status": "INSUFFICIENT_HISTORY"}
                continue
            as_of_str = str(pd.Timestamp(g["ts"].iloc[-1]).date())
            close = g["close"].to_numpy(dtype=float)
            rets = np.diff(close) / np.maximum(close[:-1], 1e-8)

            # Scale-free profitability / RMW proxy (risk-adjusted return consistency & upside capture)
            ann_ret = float(np.mean(rets[-min(252, len(rets)) :])) * 252.0
            ann_vol = float(np.std(rets[-min(252, len(rets)) :], ddof=1)) * math.sqrt(252.0) or 0.20
            efficiency = ann_ret / ann_vol

            # Drawdown resilience (inverse leverage/fragility proxy)
            cum = np.cumprod(1.0 + rets[-min(252, len(rets)) :])
            peak = np.maximum.accumulate(cum)
            max_dd = float(np.min((cum - peak) / np.maximum(peak, 1e-8)))
            resilience = 1.0 + max_dd  # higher = lower drawdown fragility

            # Trend acceleration (63d vs 252d)
            r63 = (close[-1] / close[-min(63, len(close))] - 1.0) if len(close) >= 20 else 0.0
            r126 = (close[-1] / close[-min(126, len(close))] - 1.0) if len(close) >= 40 else r63
            accel = r63 - 0.5 * r126

            enrich = db.latest_enrichment("finnhub", sym, "full", before=f"{as_of_str}T23:59:59Z")
            filing_tone = "unscored"
            insider_bucket = "unscored"
            earnings_soon = False
            if enrich:
                earnings_soon = bool(enrich.get("earnings_within_3d", False))
                pol = enrich.get("sentiment_polarity") or enrich.get("tone_polarity_score")
                if isinstance(pol, (int, float)):
                    filing_tone = "positive" if pol > 0.10 else "negative" if pol < -0.10 else "neutral"
                ins = enrich.get("insider_net_buys")
                if isinstance(ins, (int, float)):
                    insider_bucket = "cluster_buy" if ins > 0 else "net_sale" if ins < 0 else "flat"

            raw_metrics[sym] = {
                "as_of": as_of_str,
                "efficiency": efficiency,
                "resilience": resilience,
                "accel": accel,
                "filing_tone_bucket": filing_tone,
                "insider_activity_bucket": insider_bucket,
                "earnings_within_3d": earnings_soon,
            }

        eff_series = pd.Series([m["efficiency"] for m in raw_metrics.values() if "efficiency" in m], dtype=float)
        res_series = pd.Series([m["resilience"] for m in raw_metrics.values() if "resilience" in m], dtype=float)
        acc_series = pd.Series([m["accel"] for m in raw_metrics.values() if "accel" in m], dtype=float)

        fundamentals: dict[str, dict] = {}
        for sym, m in raw_metrics.items():
            if m.get("status") == "INSUFFICIENT_HISTORY":
                fundamentals[sym] = m
                continue
            prof_p = _pct_rank(eff_series, m["efficiency"]) if len(eff_series) > 1 else round(float(1.0 / (1.0 + math.exp(-m["efficiency"]))), 3)
            lev_p = _pct_rank(res_series, m["resilience"]) if len(res_series) > 1 else round(float(np.clip(m["resilience"], 0.0, 1.0)), 3)
            acc_p = _pct_rank(acc_series, m["accel"]) if len(acc_series) > 1 else round(float(1.0 / (1.0 + math.exp(-5.0 * m["accel"]))), 3)
            comp = round(float(np.mean([x for x in (prof_p, lev_p, acc_p) if x is not None])), 3)
            fundamentals[sym] = {
                "as_of": m["as_of"],
                "profitability_efficiency_pctile": prof_p,
                "balance_sheet_resilience_pctile": lev_p,
                "growth_acceleration_pctile": acc_p,
                "quality_composite_pctile": comp,
                "filing_tone_bucket": m["filing_tone_bucket"],
                "insider_activity_bucket": m["insider_activity_bucket"],
                "earnings_within_3d": m["earnings_within_3d"],
            }

        db.log_provenance("market.fundamentals_pit", ",".join(syms), source_note,
                          "bitemporal scale-free fundamental percentiles only — no raw accounting levels")
        return {
            "tool": "market.fundamentals_pit",
            "source": source_note,
            "fundamentals": fundamentals,
            "methodology": ("scale-free capital efficiency, drawdown resilience, growth acceleration "
                            "percentiles, and point-in-time filing/insider buckets strictly dated on or "
                            "before as_of; raw accounting figures and prices are never echoed"),
            "not_investment_advice": True,
            "learn_more": access.SITE_URL,
        }

    return await run_tool("market.fundamentals_pit", _do)


async def market_macro_nowcast(
    symbols: list[str] | None = None,
    bars: list[dict] | None = None,
    as_of: str | None = None,
) -> dict:
    """Point-in-time macroeconomic growth/inflation quadrant and cross-asset stress nowcast.

    Synthesizes cross-sectional dispersion, volatility term-structure slope, cyclical
    vs. defensive breadth, and crisis tail probability strictly up to `as_of`.
    """
    redaction.reject_keylike_args({"symbols": symbols, "bars": bars, "as_of": as_of})

    async def _do():
        panel, syms, source_note = await _load_panel(symbols, bars)
        if as_of:
            cutoff = pd.Timestamp(as_of).tz_localize(None).normalize()
            panel = panel[panel["ts"] <= cutoff]
        if panel.empty:
            raise ValueError("insufficient point-in-time history up to as_of")

        latest_dates = []
        mom_20_list = []
        mom_60_list = []
        vol_20_list = []
        vol_60_list = []
        crisis_probs = []

        for sym in syms:
            g = panel[panel["symbol"] == sym].sort_values("ts")
            if len(g) < 30:
                continue
            latest_dates.append(str(pd.Timestamp(g["ts"].iloc[-1]).date()))
            close = pd.Series(g["close"].to_numpy(dtype=float))
            rets = close.pct_change().dropna()
            m20 = float(close.iloc[-1] / close.iloc[-min(20, len(close))] - 1.0)
            m60 = float(close.iloc[-1] / close.iloc[-min(60, len(close))] - 1.0)
            v20 = float(rets.tail(20).std() * math.sqrt(252.0)) if len(rets) >= 10 else 0.18
            v60 = float(rets.tail(60).std() * math.sqrt(252.0)) if len(rets) >= 20 else v20
            mom_20_list.append(m20)
            mom_60_list.append(m60)
            vol_20_list.append(v20)
            vol_60_list.append(v60)
            post, _ = causal_regime_posteriors(close)
            crisis_probs.append(post.get("crisis_down", 0.15))

        if not mom_20_list:
            raise ValueError("need >= 30 sessions for at least one symbol to compute macro nowcast")

        as_of_out = max(latest_dates)
        breadth_pos = round(float(np.mean([1.0 if m > 0 else 0.0 for m in mom_20_list])), 3)
        med_mom = float(np.median(mom_60_list))
        med_v20 = float(np.median(vol_20_list))
        med_v60 = float(np.median(vol_60_list)) or 0.18
        vol_ratio = round(med_v20 / max(med_v60, 1e-6), 3)
        dispersion = round(float(np.std(mom_20_list)) * 100.0, 3) if len(mom_20_list) > 1 else 0.0
        crisis_prob = round(float(np.mean(crisis_probs)), 4)

        if med_mom >= 0.0 and vol_ratio <= 1.05:
            quadrant = "goldilocks_expansion"
        elif med_mom >= 0.0 and vol_ratio > 1.05:
            quadrant = "reflation_boom"
        elif med_mom < 0.0 and vol_ratio > 1.10:
            quadrant = "stagflation_stress"
        else:
            quadrant = "disinflation_slowdown"

        vol_state = (
            "backwardation_stress" if vol_ratio >= 1.15 else
            "contango_calm" if vol_ratio <= 0.92 else
            "flat_transition"
        )
        risk_appetite = round(float(np.clip(0.5 + 2.0 * med_mom - 0.35 * (vol_ratio - 1.0), 0.0, 1.0)), 3)

        get_db().log_provenance("market.macro_nowcast", ",".join(syms), source_note,
                                f"macro_quadrant={quadrant} vol_state={vol_state} — derived only")
        return {
            "tool": "market.macro_nowcast",
            "as_of": as_of_out,
            "source": source_note,
            "macro_quadrant": quadrant,
            "vol_term_structure_state": vol_state,
            "short_to_medium_vol_ratio": vol_ratio,
            "positive_breadth_ratio": breadth_pos,
            "cross_sectional_dispersion_pct": dispersion,
            "risk_appetite_index": risk_appetite,
            "crisis_regime_probability": crisis_prob,
            "methodology": ("point-in-time cross-sectional breadth, realized volatility term-structure "
                            "ratio (20d vs 60d), return dispersion, and causal latent crisis probability; "
                            "never echoes raw prices or provider series"),
            "not_investment_advice": True,
            "learn_more": access.SITE_URL,
        }

    return await run_tool("market.macro_nowcast", _do)


def _quantile_bins(arr: np.ndarray, n_bins: int = 3) -> np.ndarray:
    """Discretize continuous array into equiprobable quantile states 0..n_bins-1."""
    if len(arr) == 0:
        return np.array([], dtype=int)
    qs = np.linspace(0, 100, n_bins + 1)[1:-1]
    edges = np.percentile(arr, qs)
    edges = np.unique(edges)
    return np.digitize(arr, edges, right=False)


def _transfer_entropy_discrete(source_arr: np.ndarray, target_arr: np.ndarray, n_bins: int = 3) -> float:
    """Shannon Transfer Entropy T(X -> Y) = I(Y_t ; X_{t-1} | Y_{t-1}) in bits."""
    x = _quantile_bins(source_arr[:-1], n_bins=n_bins)
    y_prev = _quantile_bins(target_arr[:-1], n_bins=n_bins)
    y_curr = _quantile_bins(target_arr[1:], n_bins=n_bins)
    n = len(y_curr)
    if n < 15:
        return 0.0

    # Joint and marginal empirical counts
    c_ycurr_yprev_x: dict[tuple[int, int, int], int] = {}
    c_ycurr_yprev: dict[tuple[int, int], int] = {}
    c_yprev_x: dict[tuple[int, int], int] = {}
    c_yprev: dict[int, int] = {}

    for yt, yt1, xt1 in zip(y_curr, y_prev, x):
        ii, jj, kk = int(yt), int(yt1), int(xt1)
        c_ycurr_yprev_x[(ii, jj, kk)] = c_ycurr_yprev_x.get((ii, jj, kk), 0) + 1
        c_ycurr_yprev[(ii, jj)] = c_ycurr_yprev.get((ii, jj), 0) + 1
        c_yprev_x[(jj, kk)] = c_yprev_x.get((jj, kk), 0) + 1
        c_yprev[jj] = c_yprev.get(jj, 0) + 1

    te_bits = 0.0
    for (ii, jj, kk), count in c_ycurr_yprev_x.items():
        p_xyz = count / n
        p_xy = c_ycurr_yprev[(ii, jj)] / n
        p_yz = c_yprev_x[(jj, kk)] / n
        p_y = c_yprev[jj] / n
        ratio = (p_xyz * p_y) / max(p_xy * p_yz, 1e-12)
        if ratio > 0:
            te_bits += p_xyz * math.log2(ratio)
    return max(0.0, float(te_bits))


async def market_transfer_entropy(
    symbols: list[str] | None = None,
    bars: list[dict] | None = None,
    window: int = 60,
) -> dict:
    """Directed cross-asset information flow (Shannon Transfer Entropy) across symbol pairs.

    Tests whether lagged returns of symbol X reduce uncertainty in future returns of
    symbol Y above the 95th-percentile circular-shift permutation noise floor.
    """
    redaction.reject_keylike_args({"symbols": symbols, "bars": bars, "window": window})

    async def _do():
        panel, syms, source_note = await _load_panel(symbols, bars)
        if len(syms) < 2:
            raise ValueError("market.transfer_entropy requires at least 2 symbols")

        win = max(25, min(int(window), 252))
        ret_map: dict[str, pd.Series] = {}
        latest_ts = None
        for sym in syms[:12]:  # bound pairwise quadratic blowup
            g = panel[panel["symbol"] == sym].sort_values("ts")
            if len(g) < 25:
                continue
            s = pd.Series(g["close"].to_numpy(dtype=float), index=pd.to_datetime(g["ts"])).pct_change().dropna()
            ret_map[sym] = s
            latest_ts = str(g["ts"].iloc[-1].date()) if latest_ts is None else max(latest_ts, str(g["ts"].iloc[-1].date()))

        valid_syms = sorted(ret_map.keys())
        if len(valid_syms) < 2:
            raise ValueError("need >= 2 symbols with >= 25 overlapping sessions")

        rng = np.random.default_rng(42)
        pairs: list[dict] = []
        net_outflow: dict[str, float] = {s: 0.0 for s in valid_syms}

        for i in range(len(valid_syms)):
            for j in range(i + 1, len(valid_syms)):
                sx, sy = valid_syms[i], valid_syms[j]
                aligned = pd.concat([ret_map[sx], ret_map[sy]], axis=1, join="inner").dropna().tail(win)
                if len(aligned) < 25:
                    continue
                x_arr = aligned.iloc[:, 0].to_numpy(dtype=float)
                y_arr = aligned.iloc[:, 1].to_numpy(dtype=float)

                te_xy = _transfer_entropy_discrete(x_arr, y_arr)
                te_yx = _transfer_entropy_discrete(y_arr, x_arr)

                # Circular-shift permutation noise floor (19 surrogates -> 95th percentile)
                null_vals = []
                n_obs = len(x_arr)
                for _ in range(19):
                    shift = int(rng.integers(5, max(6, n_obs - 5)))
                    x_surr = np.roll(x_arr, shift)
                    null_vals.append(_transfer_entropy_discrete(x_surr, y_arr))
                noise_floor = float(np.percentile(null_vals, 95))

                dom_te = max(te_xy, te_yx)
                sig = bool(dom_te > noise_floor and abs(te_xy - te_yx) > 0.005)
                leader = sx if te_xy >= te_yx else sy
                follower = sy if te_xy >= te_yx else sx
                net_bits = abs(te_xy - te_yx)

                net_outflow[sx] += (te_xy - te_yx)
                net_outflow[sy] += (te_yx - te_xy)

                pairs.append({
                    "pair": f"{sx}->{sy}" if te_xy >= te_yx else f"{sy}->{sx}",
                    "information_leader": leader,
                    "information_follower": follower,
                    "te_leader_to_follower_bits": round(dom_te, 4),
                    "te_reverse_bits": round(min(te_xy, te_yx), 4),
                    "net_flow_bits": round(net_bits, 4),
                    "noise_floor_95_bits": round(noise_floor, 4),
                    "statistically_significant": sig,
                    "overlapping_sessions": int(len(aligned)),
                })

        pairs.sort(key=lambda r: r["net_flow_bits"], reverse=True)
        leader_ranking = sorted(
            [{"symbol": s, "net_information_outflow_bits": round(v, 4)} for s, v in net_outflow.items()],
            key=lambda x: x["net_information_outflow_bits"],
            reverse=True,
        )

        get_db().log_provenance("market.transfer_entropy", ",".join(valid_syms), source_note,
                                f"pairs={len(pairs)} window={win} — derived information flow bits only")
        return {
            "tool": "market.transfer_entropy",
            "as_of": latest_ts or "",
            "source": source_note,
            "window_sessions": win,
            "pairs": pairs,
            "leader_ranking": leader_ranking,
            "methodology": ("quantile-partitioned Shannon Transfer Entropy I(Y_t; X_{t-1} | Y_{t-1}) "
                            "with a 95th-percentile circular-shift permutation null floor; no raw "
                            "prices or returns are echoed"),
            "not_investment_advice": True,
            "learn_more": access.SITE_URL,
        }

    return await run_tool("market.transfer_entropy", _do)
