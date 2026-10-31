#!/usr/bin/env python3
"""Generate canonical Quant Swarm Audit demo fixtures and run the end-to-end
guided workflow (`audit.preflight -> audit.quote -> audit.run -> audit.status
-> audit.report -> audit.compare`).

Fixtures created under `examples/audit_demo/`:
  1. `fixture_flawed/`       — realistic 180-session daily backtest with injected
                               cost fragility, top-5 trade concentration (>75%) +
                               38% single-name position cap breach, and feature
                               timestamp lookahead leakage.
  2. `fixture_clean/`        — remediated 180-session reference backtest with
                               broad trade dispersion, <=18% position sizing,
                               >2.4x cost breakeven buffer, and causal Tier-A/B
                               feature provenance.
  3. `fixture_inconclusive/` — short 18-session / 7-trade sample that
                               deterministically triggers `INSUFFICIENT_EVIDENCE`.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import numpy as np
import pandas as pd

from swarm_mcp.tools import audit_tools

DEMO_DIR = Path(__file__).resolve().parent
SITE_SAMPLE_HTML = (
    DEMO_DIR.parents[2] / "1.21.Initiative" / "public" / "mcp" / "examples" / "sample-audit-report.html"
)


def build_fixtures(base_dir: Path = DEMO_DIR) -> dict[str, Path]:
    """Deterministically materialize the three canonical fixture directories."""
    dates_180 = pd.bdate_range(end="2026-10-15", periods=180)
    dates_18 = pd.bdate_range(end="2026-10-15", periods=18)

    # --- 1. fixture_flawed ----------------------------------------------------
    flawed_dir = base_dir / "fixture_flawed"
    flawed_dir.mkdir(parents=True, exist_ok=True)
    rng_f = np.random.default_rng(101)
    # Looks good on gross headline return, but fragile on costs + tail drawdowns
    ret_f = rng_f.normal(0.00055, 0.0095, size=len(dates_180))
    # Inject a few sharp high-vol drawdown days
    ret_f[40:45] = [-0.032, -0.029, -0.035, -0.024, -0.021]
    pd.DataFrame({
        "ts": [d.date().isoformat() for d in dates_180],
        "strategy_return": np.round(ret_f, 6),
    }).to_csv(flawed_dir / "returns.csv", index=False)

    # 35 trades where top 5 outlier trades carry >75% of positive PnL,
    # mean gross edge is only ~9.5 bps (below 15 bps round-trip cost),
    # and position_pct breaches the 0.25 house cap (0.38).
    syms = ["NVDA", "AAPL", "MSFT", "AMZN", "META", "TSLA", "AMD", "AVGO"]
    trades_f = []
    for i in range(35):
        ts_str = dates_180[min(i * 5, len(dates_180) - 1)].date().isoformat()
        if i < 5:
            pnl_bps = 68.0 - i * 3.0  # 5 huge outlier winners
            pos_pct = 0.38 if i < 2 else 0.28  # breaches 25% house floor
        elif i < 20:
            pnl_bps = 1.5
            pos_pct = 0.15
        else:
            pnl_bps = -4.0
            pos_pct = 0.15
        trades_f.append({
            "ts": ts_str,
            "symbol": syms[i % len(syms)],
            "pnl_bps": round(pnl_bps, 2),
            "position_pct": pos_pct,
        })
    pd.DataFrame(trades_f).to_csv(flawed_dir / "trades.csv", index=False)

    (flawed_dir / "cost_assumptions.json").write_text(
        json.dumps({
            "spread_bps": 2.0,
            "slippage_bps": 3.0,
            "adverse_selection_bps": 2.0,
            "commission_bps": 0.5,
        }, indent=2) + "\n",
        encoding="utf-8",
    )

    (flawed_dir / "features_manifest.json").write_text(
        json.dumps({
            "as_of": "2026-10-15T20:00:00Z",
            "features": [
                {
                    "name": "mom_20d",
                    "source": "bars_1day",
                    "available_at": "2026-10-15T20:00:00Z",
                    "coverage": 0.99,
                },
                {
                    "name": "earnings_flag",
                    "source": "point_in_time_calendar",
                    "available_at": "2026-10-18T16:00:00Z",  # 3 days AFTER as_of (lookahead leak!)
                    "coverage": 0.96,
                },
                {
                    "name": "weather_hdd_shock",
                    "source": "open-meteo.archive",  # banned archive actuals source!
                    "available_at": "2026-10-15T12:00:00Z",
                    "coverage": 0.95,
                },
            ],
        }, indent=2) + "\n",
        encoding="utf-8",
    )

    # --- 2. fixture_clean -----------------------------------------------------
    clean_dir = base_dir / "fixture_clean"
    clean_dir.mkdir(parents=True, exist_ok=True)
    rng_c = np.random.default_rng(202)
    ret_c = rng_c.normal(0.0011, 0.0062, size=len(dates_180))
    ret_c = np.clip(ret_c, -0.014, 0.022)
    pd.DataFrame({
        "ts": [d.date().isoformat() for d in dates_180],
        "strategy_return": np.round(ret_c, 6),
    }).to_csv(clean_dir / "returns.csv", index=False)

    bench_c = rng_c.normal(0.00035, 0.0070, size=len(dates_180))
    pd.DataFrame({
        "ts": [d.date().isoformat() for d in dates_180],
        "return": np.round(bench_c, 6),
    }).to_csv(clean_dir / "benchmark.csv", index=False)

    # 45 well-dispersed trades, position_pct <= 0.18 (under 0.25 cap),
    # mean gross edge ~37 bps/trade (> 2.4x round-trip cost of 15 bps),
    # top-5 trade share ~16% (< 45% threshold).
    trades_c = []
    for i in range(45):
        ts_str = dates_180[min(i * 4, len(dates_180) - 1)].date().isoformat()
        pnl_bps = 56.0 - (i % 10) * 1.8 if i % 6 != 5 else -8.0
        trades_c.append({
            "ts": ts_str,
            "symbol": syms[i % len(syms)],
            "pnl_bps": round(pnl_bps, 2),
            "position_pct": 0.16 if i % 2 == 0 else 0.14,
        })
    pd.DataFrame(trades_c).to_csv(clean_dir / "trades.csv", index=False)

    (clean_dir / "cost_assumptions.json").write_text(
        json.dumps({
            "spread_bps": 2.0,
            "slippage_bps": 3.0,
            "adverse_selection_bps": 2.0,
            "commission_bps": 0.5,
        }, indent=2) + "\n",
        encoding="utf-8",
    )

    (clean_dir / "features_manifest.json").write_text(
        json.dumps({
            "as_of": "2026-10-15T20:00:00Z",
            "features": [
                {
                    "name": "mom_20d",
                    "source": "bars_1day",
                    "available_at": "2026-10-15T20:00:00Z",
                    "coverage": 1.0,
                },
                {
                    "name": "atr",
                    "source": "bars_1day",
                    "available_at": "2026-10-15T20:00:00Z",
                    "coverage": 1.0,
                },
                {
                    "name": "vol_ratio_20",
                    "source": "bars_1day",
                    "available_at": "2026-10-15T20:00:00Z",
                    "coverage": 0.98,
                },
            ],
        }, indent=2) + "\n",
        encoding="utf-8",
    )

    # --- 3. fixture_inconclusive ----------------------------------------------
    inc_dir = base_dir / "fixture_inconclusive"
    inc_dir.mkdir(parents=True, exist_ok=True)
    rng_i = np.random.default_rng(303)
    ret_i = rng_i.normal(0.0015, 0.008, size=len(dates_18))
    pd.DataFrame({
        "ts": [d.date().isoformat() for d in dates_18],
        "strategy_return": np.round(ret_i, 6),
    }).to_csv(inc_dir / "returns.csv", index=False)

    trades_i = [
        {
            "ts": dates_18[i * 2].date().isoformat(),
            "symbol": syms[i % len(syms)],
            "pnl_bps": 28.0 if i % 2 == 0 else -8.0,
            "position_pct": 0.12,
        }
        for i in range(7)
    ]
    pd.DataFrame(trades_i).to_csv(inc_dir / "trades.csv", index=False)

    return {
        "fixture_flawed": flawed_dir,
        "fixture_clean": clean_dir,
        "fixture_inconclusive": inc_dir,
    }


async def run_all_demos() -> dict:
    paths = build_fixtures(DEMO_DIR)

    # 1. Run fixture_flawed
    pf_flawed = await audit_tools.preflight(
        returns_path=str(paths["fixture_flawed"] / "returns.csv"),
        trades_path=str(paths["fixture_flawed"] / "trades.csv"),
        cost_assumptions_path=str(paths["fixture_flawed"] / "cost_assumptions.json"),
        features_manifest_path=str(paths["fixture_flawed"] / "features_manifest.json"),
        strategy_name="momentum_breakout_v1_flawed",
    )
    q_flawed = await audit_tools.quote(preflight_id=pf_flawed["preflight_id"], mode="local")
    run_flawed = await audit_tools.run(
        preflight_id=pf_flawed["preflight_id"],
        approval_token=q_flawed["approval_token"],
        idempotency_key="demo-flawed-v1",
        max_credits=0,
        mode="local",
    )

    # 2. Run fixture_clean
    pf_clean = await audit_tools.preflight(
        returns_path=str(paths["fixture_clean"] / "returns.csv"),
        trades_path=str(paths["fixture_clean"] / "trades.csv"),
        benchmark_path=str(paths["fixture_clean"] / "benchmark.csv"),
        cost_assumptions_path=str(paths["fixture_clean"] / "cost_assumptions.json"),
        features_manifest_path=str(paths["fixture_clean"] / "features_manifest.json"),
        strategy_name="momentum_breakout_v2_remediated",
    )
    q_clean = await audit_tools.quote(preflight_id=pf_clean["preflight_id"], mode="local")
    run_clean = await audit_tools.run(
        preflight_id=pf_clean["preflight_id"],
        approval_token=q_clean["approval_token"],
        idempotency_key="demo-clean-v2",
        max_credits=0,
        mode="local",
    )

    # 3. Run fixture_inconclusive
    pf_inc = await audit_tools.preflight(
        returns_path=str(paths["fixture_inconclusive"] / "returns.csv"),
        trades_path=str(paths["fixture_inconclusive"] / "trades.csv"),
        strategy_name="short_sample_18d_experiment",
    )
    q_inc = await audit_tools.quote(preflight_id=pf_inc["preflight_id"], mode="local")
    run_inc = await audit_tools.run(
        preflight_id=pf_inc["preflight_id"],
        approval_token=q_inc["approval_token"],
        idempotency_key="demo-inconclusive-v1",
        max_credits=0,
        mode="local",
    )

    # Export individual report + combined comparison HTML report
    local_sample_html = DEMO_DIR / "sample-audit-report.html"
    await audit_tools.report(
        job_id=run_flawed["job_id"],
        output_json_path=str(DEMO_DIR / "fixture_flawed" / "audit_report.json"),
        output_markdown_path=str(DEMO_DIR / "fixture_flawed" / "audit_report.md"),
    )
    await audit_tools.report(
        job_id=run_clean["job_id"],
        output_json_path=str(DEMO_DIR / "fixture_clean" / "audit_report.json"),
        output_markdown_path=str(DEMO_DIR / "fixture_clean" / "audit_report.md"),
    )
    comp = await audit_tools.compare(
        job_id_a=run_flawed["job_id"],
        job_id_b=run_clean["job_id"],
        output_html_path=str(local_sample_html),
    )
    if SITE_SAMPLE_HTML.parent.exists():
        SITE_SAMPLE_HTML.write_text(local_sample_html.read_text(encoding="utf-8"), encoding="utf-8")

    return {
        "flawed": run_flawed,
        "clean": run_clean,
        "inconclusive": run_inc,
        "comparison": comp,
    }


if __name__ == "__main__":
    out = asyncio.run(run_all_demos())
    print(json.dumps({
        "flawed_assessment": out["flawed"]["overall_assessment"],
        "clean_assessment": out["clean"]["overall_assessment"],
        "inconclusive_assessment": out["inconclusive"]["overall_assessment"],
        "remediation_verdict": out["comparison"]["remediation_verdict"],
    }, indent=2))
