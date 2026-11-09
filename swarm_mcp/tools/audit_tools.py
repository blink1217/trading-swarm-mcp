"""Quant Swarm Audit (`audit.*` namespace) — flagship guided MCP workflow.

Turns the 33 low-level primitives into a deterministic, 6-stage guided audit:
  1. `audit.preflight` — strictly local & free (0 credits). Validates researcher
     artifacts (CSV/Parquet/JSON or inline rows for strategy returns, trades,
     benchmark series, cost assumptions, and feature-provenance manifests) and
     returns a Coverage & Scope Matrix (Available checks, NOT_TESTED checks,
     and Outbound data preview).
  2. `audit.quote` — takes a validated preflight manifest and returns a
     deterministic execution plan, estimated runtime, exact credit & GBP cost,
     and an `approval_token` bound to the SHA-256 hash of inputs + config.
  3. `audit.run` — executes the audit. Enforces `approval_token` verification,
     `idempotency_key` deduplication (never double-bills on retries), and
     `max_credits` spend cap. Never charges for preflight refusals or errors.
  4. `audit.status` — read-only polling of an audit `job_id` at 0 credits.
  5. `audit.report` — generates the machine-readable JSON artifact,
     reproducibility bundle (input hashes, config, package version, seeds),
     and shareable HTML + Markdown reports where every finding follows:
     `{finding, category, evidence, why_it_matters, next_experiment, limitation}`.
  6. `audit.compare` — compares two completed audit bundles (before/after
     remediation or version A vs. version B) and outputs a structured diff of
     metrics, turnover, cost sensitivity, and resolved/new warnings.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import html
import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from swarm_mcp import vendor_path  # noqa: F401

from fill_model import one_way_cost_bps, round_trip_cost_bps  # vendored guardrails
from gates import MIN_COVERAGE, MIN_EPISODES  # vendored guardrails
from objective import MAX_GROSS_EXPOSURE_PCT, MAX_POSITION_PCT  # vendored guardrails
from provenance import BANNED_ACTUALS_SOURCES, feature_tier  # vendored guardrails

from swarm_mcp import access, metering, plans, redaction, request_context, server_meta, telemetry
from swarm_mcp.tool_runner import run_tool


async def _run_audit_tool(tool: str, fn, *, require_token: bool = False) -> dict:
    """Execute an `audit.*` tool.

    Strictly local audit operations (`preflight`, `quote(mode='local')`,
    `run(mode='local')`, `status`, `report`, `compare`) work out-of-the-box
    with zero network calls even before `SWARM_MCP_ACCESS_TOKEN` is configured.
    When a token is configured (or `require_token=True` for hosted runs),
    standard `run_tool` token verification and entitlement checks apply.
    """
    has_token = bool(
        request_context.current_token.get()
        or access.resolve_token()
    )
    if has_token or require_token:
        return await run_tool(tool, fn)
    import time
    t0 = time.perf_counter()
    try:
        out = await fn()
        telemetry.record(tool, True, (time.perf_counter() - t0) * 1000.0)
        return redaction.redact(out)
    except Exception as e:  # noqa: BLE001
        telemetry.record(tool, False, (time.perf_counter() - t0) * 1000.0)
        return {"tool": tool, "error": redaction.redact_text(f"{type(e).__name__}: {e}")}

DIAGNOSTIC_CATEGORIES = (
    "OBSERVED_PROBLEM",
    "SENSITIVITY_WARNING",
    "INSUFFICIENT_EVIDENCE",
    "PASSED",
    "NOT_TESTED",
)

DEFAULT_COST_ASSUMPTIONS = {
    "spread_bps": 2.0,
    "slippage_bps": 3.0,
    "adverse_selection_bps": 2.0,
    "commission_bps": 0.5,
}

HOSTED_AUDIT_CREDITS = 20
MIN_DAILY_OBSERVATIONS = 60
MIN_WEEKLY_BLOCKS = 20
MIN_TRADE_OBSERVATIONS = 20
_APPROVAL_SECRET = b"quant-swarm-audit-approval-v1"

# Process-level stores for preflight manifests, jobs, and idempotency keys
_PREFLIGHT_STORE: dict[str, dict[str, Any]] = {}
_JOB_STORE: dict[str, dict[str, Any]] = {}
_IDEMPOTENCY_STORE: dict[str, dict[str, Any]] = {}


def reset_audit_stores() -> None:
    """Clear in-memory audit stores (used by tests)."""
    _PREFLIGHT_STORE.clear()
    _JOB_STORE.clear()
    _IDEMPOTENCY_STORE.clear()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_json(obj: Any) -> str:
    canonical = json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return _sha256_bytes(canonical)


def _compute_approval_token(
    input_sha256: str,
    mode: str,
    credits_cost: int,
    seeds: list[int],
) -> str:
    msg = f"{input_sha256}|{mode}|{int(credits_cost)}|{server_meta.PACKAGE_VERSION}|{','.join(map(str, seeds))}"
    digest = hmac.new(_APPROVAL_SECRET, msg.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"aq1_{digest[:32]}"


def _load_tabular(path_str: str | None, inline_rows: list[dict] | None, label: str) -> tuple[pd.DataFrame | None, str | None, list[str]]:
    """Load CSV, Parquet, or JSON records from disk or inline rows and compute SHA-256."""
    errors: list[str] = []
    if inline_rows is not None:
        if not isinstance(inline_rows, list):
            return None, None, [f"{label}: inline rows must be a list of dicts"]
        if len(inline_rows) == 0:
            return pd.DataFrame(), _sha256_json([]), [f"{label}: inline rows list is empty"]
        df = pd.DataFrame(inline_rows)
        return df, _sha256_json(inline_rows), errors

    if not path_str:
        return None, None, errors

    p = Path(path_str)
    if not p.exists() or not p.is_file():
        return None, None, [f"{label}: file not found at {path_str}"]

    raw_bytes = p.read_bytes()
    file_hash = _sha256_bytes(raw_bytes)
    suffix = p.suffix.lower()
    try:
        if suffix == ".parquet":
            df = pd.read_parquet(p)
        elif suffix == ".json":
            data = json.loads(raw_bytes.decode("utf-8"))
            rows = data.get("rows", data) if isinstance(data, dict) else data
            df = pd.DataFrame(rows)
        else:
            df = pd.read_csv(p)
    except Exception as exc:  # noqa: BLE001
        return None, file_hash, [f"{label}: failed to parse {p.name} ({type(exc).__name__}: {exc})"]

    if df.empty:
        errors.append(f"{label}: {p.name} contains 0 rows")
    return df, file_hash, errors


def _load_json_dict(path_str: str | None, inline_dict: dict | None, label: str) -> tuple[dict | None, str | None, list[str]]:
    if inline_dict is not None:
        if not isinstance(inline_dict, dict):
            return None, None, [f"{label}: must be a JSON object / dict"]
        return dict(inline_dict), _sha256_json(inline_dict), []
    if not path_str:
        return None, None, []
    p = Path(path_str)
    if not p.exists() or not p.is_file():
        return None, None, [f"{label}: file not found at {path_str}"]
    raw_bytes = p.read_bytes()
    file_hash = _sha256_bytes(raw_bytes)
    try:
        parsed = json.loads(raw_bytes.decode("utf-8"))
        if not isinstance(parsed, dict):
            return None, file_hash, [f"{label}: root of {p.name} must be a JSON object"]
        return parsed, file_hash, []
    except Exception as exc:  # noqa: BLE001
        return None, file_hash, [f"{label}: failed to parse {p.name} ({type(exc).__name__}: {exc})"]


def _infer_frequency(timestamps: pd.Series) -> str:
    if len(timestamps) < 3:
        return "insufficient_rows"
    diffs = timestamps.sort_values().diff().dropna()
    median_days = diffs.dt.total_seconds().median() / 86400.0
    if 0.8 <= median_days <= 1.5:
        return "daily"
    if 4.0 <= median_days <= 8.0:
        return "weekly"
    if median_days < 0.8:
        return "intraday"
    return "irregular"


def _normalize_returns_df(df: pd.DataFrame) -> tuple[pd.DataFrame, list[str], dict[str, Any]]:
    errors: list[str] = []
    ts_col = next((c for c in ("ts", "date", "timestamp", "datetime") if c in df.columns), None)
    ret_col = next((c for c in ("strategy_return", "return", "ret", "net_return", "gross_return", "pnl_pct") if c in df.columns), None)
    if ts_col is None:
        errors.append("returns artifact missing timestamp column (expected one of: ts, date, timestamp, datetime)")
    if ret_col is None:
        errors.append("returns artifact missing return column (expected one of: strategy_return, return, ret, net_return, gross_return)")
    if errors:
        return pd.DataFrame(), errors, {}

    work = df.copy()
    work["_ts"] = pd.to_datetime(work[ts_col], utc=True, errors="coerce")
    bad_ts = int(work["_ts"].isna().sum())
    if bad_ts > 0:
        errors.append(f"returns artifact has {bad_ts} unparseable timestamp(s)")
    work["_ret"] = pd.to_numeric(work[ret_col], errors="coerce")
    bad_ret = int(work["_ret"].isna().sum())
    if bad_ret > 0:
        errors.append(f"returns artifact has {bad_ret} non-numeric/NaN return value(s)")

    dup_count = int(work["_ts"].duplicated().sum())
    if dup_count > 0:
        errors.append(f"returns artifact has {dup_count} duplicate timestamp(s) — point-in-time series must have unique session timestamps")

    if errors:
        return work, errors, {"duplicate_timestamps": dup_count, "invalid_timestamps": bad_ts, "nan_returns": bad_ret}

    work = work.sort_values("_ts").reset_index(drop=True)
    freq = _infer_frequency(work["_ts"])
    meta = {
        "row_count": int(len(work)),
        "start_ts": work["_ts"].iloc[0].date().isoformat(),
        "end_ts": work["_ts"].iloc[-1].date().isoformat(),
        "frequency": freq,
        "duplicate_timestamps": dup_count,
        "return_column": ret_col,
        "timestamp_column": ts_col,
    }
    return work, errors, meta


def _normalize_trades_df(df: pd.DataFrame | None) -> tuple[pd.DataFrame | None, list[str], dict[str, Any] | None]:
    if df is None:
        return None, [], None
    if df.empty:
        return df, ["trades artifact is empty (0 rows)"], None
    errors: list[str] = []
    ts_col = next((c for c in ("ts", "entry_ts", "date", "timestamp") if c in df.columns), None)
    sym_col = next((c for c in ("symbol", "ticker", "asset") if c in df.columns), None)
    pnl_col = next((c for c in ("pnl_bps", "net_pnl_bps", "gross_pnl_bps", "return_bps", "pnl", "return") if c in df.columns), None)
    notional_col = next((c for c in ("notional", "position_pct", "weight", "size_pct") if c in df.columns), None)

    if ts_col is None:
        errors.append("trades artifact missing timestamp column (ts, entry_ts, date, timestamp)")
    if sym_col is None:
        errors.append("trades artifact missing symbol column (symbol, ticker, asset)")
    if pnl_col is None:
        errors.append("trades artifact missing PnL/return column (pnl_bps, return_bps, pnl, return)")
    if errors:
        return None, errors, None

    work = df.copy()
    work["_ts"] = pd.to_datetime(work[ts_col], utc=True, errors="coerce")
    work["_symbol"] = work[sym_col].astype(str).str.strip().str.upper()
    work["_pnl_raw"] = pd.to_numeric(work[pnl_col], errors="coerce")
    if int(work["_ts"].isna().sum()) > 0 or int(work["_pnl_raw"].isna().sum()) > 0:
        errors.append("trades artifact contains invalid timestamps or non-numeric PnL values")
        return None, errors, None

    # Convert decimal return to bps if column is not already in bps
    is_bps = "bps" in pnl_col.lower()
    work["_pnl_bps"] = work["_pnl_raw"] if is_bps else work["_pnl_raw"] * 10000.0

    if notional_col is not None:
        work["_pos_val"] = pd.to_numeric(work[notional_col], errors="coerce").fillna(0.0)
    else:
        work["_pos_val"] = 0.10

    meta = {
        "trade_count": int(len(work)),
        "unique_symbols": int(work["_symbol"].nunique()),
        "symbols_sample": sorted(work["_symbol"].unique().tolist())[:10],
        "pnl_column": pnl_col,
        "size_column": notional_col,
    }
    return work, errors, meta


def _build_coverage_matrix(
    *,
    has_returns: bool,
    has_trades: bool,
    has_benchmark: bool,
    has_features_manifest: bool,
    returns_meta: dict[str, Any],
    trades_meta: dict[str, Any] | None,
    outbound_bytes_hosted: int,
) -> dict[str, Any]:
    available_checks: list[dict[str, Any]] = []
    not_tested_checks: list[dict[str, Any]] = []

    if has_returns:
        available_checks.extend([
            {
                "check_id": "sample_power_and_sharpe",
                "title": "Sample Depth, Effective Weekly Blocks & Bootstrap Sharpe",
                "description": (
                    f"Evaluates {returns_meta.get('row_count', 0)} return observations "
                    f"({returns_meta.get('start_ts')} to {returns_meta.get('end_ts')}) against the "
                    f"minimum sample floor ({MIN_DAILY_OBSERVATIONS} daily / {MIN_WEEKLY_BLOCKS} weekly blocks)."
                ),
                "inputs_used": ["returns"],
            },
            {
                "check_id": "net_cost_survival",
                "title": "Pessimistic Execution Cost & Stress Survival (1.0x / 1.5x / 2.0x)",
                "description": (
                    "Applies the house pessimistic fill model (spread + slippage + adverse selection on both legs) "
                    "and tests whether claimed edge survives 1.5x and 2.0x cost stress."
                ),
                "inputs_used": ["returns", "cost_assumptions"] + (["trades"] if has_trades else []),
            },
            {
                "check_id": "drawdown_and_tail_profile",
                "title": "Drawdown Depth, Tail Skew & Volatility-Regime Split",
                "description": (
                    "Decomposes equity curve drawdown, worst 5-session loss, and high-volatility vs "
                    "calm-regime return consistency."
                ),
                "inputs_used": ["returns"],
            },
        ])

    if has_trades:
        available_checks.append({
            "check_id": "trade_concentration_and_turnover",
            "title": "Trade Concentration, House Position Caps (25% / 60%) & Turnover Fragility",
            "description": (
                f"Audits {trades_meta.get('trade_count', 0) if trades_meta else 0} trade records for "
                "top-5 outlier PnL concentration, single-name position cap breaches (>25% equity), and turnover drag."
            ),
            "inputs_used": ["trades", "cost_assumptions"],
        })
    else:
        not_tested_checks.append({
            "check_id": "trade_concentration_and_turnover",
            "title": "Trade Concentration, House Position Caps & Turnover Fragility",
            "status": "NOT_TESTED",
            "reason": "No trade records artifact (`trades_path` or `trades_rows`) was supplied.",
            "required_input": "Timestamped trade records with symbol, PnL/return, and position size / notional.",
        })

    if has_benchmark:
        available_checks.append({
            "check_id": "benchmark_excess_and_beta",
            "title": "Benchmark Active Return, Beta & Information Ratio",
            "description": "Computes active return, tracking error, and market beta against the supplied benchmark series.",
            "inputs_used": ["returns", "benchmark"],
        })
    else:
        not_tested_checks.append({
            "check_id": "benchmark_excess_and_beta",
            "title": "Benchmark Active Return, Beta & Information Ratio",
            "status": "NOT_TESTED",
            "reason": "No benchmark series (`benchmark_path` or `benchmark_rows`) was supplied.",
            "required_input": "Timestamped benchmark return series covering the same session dates.",
        })

    if has_features_manifest:
        available_checks.append({
            "check_id": "feature_provenance_and_leakage",
            "title": "Point-in-Time Feature Provenance & Lookahead Timestamp Audit",
            "description": (
                "Audits every declared feature against banned actuals sources (e.g. archive weather/restated fundamentals), "
                "timestamp availability (`available_at <= as_of`), Tier-A/B/C classification, and universe coverage (>=90%)."
            ),
            "inputs_used": ["features_manifest"],
        })
    else:
        not_tested_checks.append({
            "check_id": "feature_provenance_and_leakage",
            "title": "Point-in-Time Feature Provenance & Lookahead Timestamp Audit",
            "status": "NOT_TESTED",
            "reason": "No feature provenance manifest (`features_manifest_path` or `features_manifest`) was supplied.",
            "required_input": "Feature manifest JSON declaring `as_of` and per-feature `source`, `available_at`, and `coverage`.",
        })

    # Always state explicitly what tabular artifacts cannot prove on their own
    not_tested_checks.append({
        "check_id": "strategy_code_lookahead_proof",
        "title": "Underlying Strategy Code & In-Sample Hyperparameter Snooping Proof",
        "status": "NOT_TESTED",
        "reason": (
            "Tabular return and trade logs cannot prove that the underlying signal code avoided future-bar indexing "
            "or multiple-testing selection bias across discarded parameter trials."
        ),
        "required_input": (
            "Hosted Shadow Tournament replay (`tournament.submit`) on sealed/causal features and the "
            "server-side Deflated Sharpe trials ledger."
        ),
    })

    return {
        "available_checks": available_checks,
        "not_tested_checks": not_tested_checks,
        "outbound_data_preview": {
            "local_mode": {
                "mode": "local",
                "bytes_sent": 0,
                "fields_sent": [],
                "destination": "none — 100% local execution on your machine",
            },
            "hosted_mode": {
                "mode": "hosted",
                "bytes_sent": outbound_bytes_hosted,
                "fields_sent": [
                    "strategy_returns_series (ts, ret)",
                    "cost_assumptions",
                    *(["trades_summary (ts, symbol, pnl_bps, size_pct)"] if has_trades else []),
                    *(["features_manifest"] if has_features_manifest else []),
                ],
                "destination": f"{server_meta.REMOTE_BASE_URL}/mcp/warden (only if mode='hosted' is explicitly approved)",
            },
        },
    }


async def preflight(
    returns_path: str | None = None,
    returns_rows: list[dict] | None = None,
    trades_path: str | None = None,
    trades_rows: list[dict] | None = None,
    benchmark_path: str | None = None,
    benchmark_rows: list[dict] | None = None,
    cost_assumptions_path: str | None = None,
    cost_assumptions: dict | None = None,
    features_manifest_path: str | None = None,
    features_manifest: dict | None = None,
    strategy_name: str = "candidate_strategy",
) -> dict:
    """Validate researcher artifacts locally (0 credits) and emit the Coverage & Scope Matrix."""
    redaction.reject_keylike_args({
        "returns_path": returns_path,
        "trades_path": trades_path,
        "benchmark_path": benchmark_path,
        "cost_assumptions_path": cost_assumptions_path,
        "features_manifest_path": features_manifest_path,
        "strategy_name": strategy_name,
    })

    async def _do():
        errors: list[str] = []
        artifact_hashes: dict[str, str] = {}

        ret_df, ret_hash, ret_load_err = _load_tabular(returns_path, returns_rows, "returns")
        errors.extend(ret_load_err)
        if ret_hash:
            artifact_hashes["returns"] = ret_hash

        trades_df, trades_hash, trades_load_err = _load_tabular(trades_path, trades_rows, "trades")
        errors.extend(trades_load_err)
        if trades_hash:
            artifact_hashes["trades"] = trades_hash

        bench_df, bench_hash, bench_load_err = _load_tabular(benchmark_path, benchmark_rows, "benchmark")
        errors.extend(bench_load_err)
        if bench_hash:
            artifact_hashes["benchmark"] = bench_hash

        cost_dict, cost_hash, cost_load_err = _load_json_dict(cost_assumptions_path, cost_assumptions, "cost_assumptions")
        errors.extend(cost_load_err)
        merged_costs = {**DEFAULT_COST_ASSUMPTIONS, **(cost_dict or {})}
        artifact_hashes["cost_assumptions"] = cost_hash or _sha256_json(merged_costs)

        manifest_dict, manifest_hash, manifest_load_err = _load_json_dict(
            features_manifest_path, features_manifest, "features_manifest"
        )
        errors.extend(manifest_load_err)
        if manifest_hash:
            artifact_hashes["features_manifest"] = manifest_hash

        returns_meta: dict[str, Any] = {}
        norm_ret = pd.DataFrame()
        if ret_df is None:
            errors.append("returns artifact is required (`returns_path` or `returns_rows`)")
        elif not ret_load_err:
            norm_ret, ret_val_err, returns_meta = _normalize_returns_df(ret_df)
            errors.extend(ret_val_err)

        norm_trades = None
        trades_meta = None
        if trades_df is not None and not trades_load_err:
            norm_trades, tr_val_err, trades_meta = _normalize_trades_df(trades_df)
            errors.extend(tr_val_err)

        has_benchmark = False
        if bench_df is not None and not bench_load_err and not bench_df.empty:
            has_benchmark = True

        has_manifest = manifest_dict is not None and isinstance(manifest_dict.get("features"), list)
        if manifest_dict is not None and not has_manifest:
            errors.append("features_manifest must contain a `features` list")

        canonical_input = {
            "strategy_name": strategy_name,
            "artifact_hashes": artifact_hashes,
            "costs": merged_costs,
            "returns_meta": returns_meta,
            "trades_meta": trades_meta,
            "has_benchmark": has_benchmark,
            "has_features_manifest": has_manifest,
        }
        input_sha256 = _sha256_json(canonical_input)
        preflight_id = f"pf_{input_sha256[:16]}"

        outbound_payload = {
            "strategy_name": strategy_name,
            "returns_rows": int(len(norm_ret)),
            "trades_rows": int(len(norm_trades)) if norm_trades is not None else 0,
            "costs": merged_costs,
            "features_manifest": manifest_dict if has_manifest else None,
        }
        outbound_bytes = len(json.dumps(outbound_payload, default=str).encode("utf-8")) + (
            int(len(norm_ret)) * 32 + (int(len(norm_trades)) * 48 if norm_trades is not None else 0)
        )

        coverage_matrix = _build_coverage_matrix(
            has_returns=not norm_ret.empty,
            has_trades=norm_trades is not None and not norm_trades.empty,
            has_benchmark=has_benchmark,
            has_features_manifest=has_manifest,
            returns_meta=returns_meta,
            trades_meta=trades_meta,
            outbound_bytes_hosted=outbound_bytes,
        )

        eligible = len(errors) == 0 and not norm_ret.empty
        record = {
            "preflight_id": preflight_id,
            "strategy_name": strategy_name,
            "eligible": eligible,
            "validation_errors": errors,
            "input_sha256": input_sha256,
            "artifact_hashes": artifact_hashes,
            "cost_assumptions": merged_costs,
            "returns_summary": returns_meta,
            "trades_summary": trades_meta,
            "has_benchmark": has_benchmark,
            "has_features_manifest": has_manifest,
            "coverage_matrix": coverage_matrix,
            # Store normalized data in-process for seamless chaining via preflight_id
            "_internal": {
                "norm_ret": norm_ret,
                "norm_trades": norm_trades,
                "bench_df": bench_df if has_benchmark else None,
                "manifest_dict": manifest_dict if has_manifest else None,
                "merged_costs": merged_costs,
            },
        }
        _PREFLIGHT_STORE[preflight_id] = record

        public_out = {k: v for k, v in record.items() if k != "_internal"}
        return {
            "tool": "audit.preflight",
            **public_out,
            "credits_charged": 0,
            "next_step": (
                f"Call `audit.quote(preflight_id='{preflight_id}', mode='local')` to get your deterministic "
                "execution plan, £0.00 cost guarantee, and approval_token."
                if eligible else
                "Fix the schema/timestamp validation_errors above and re-run `audit.preflight` (0 credits)."
            ),
        }

    return await _run_audit_tool("audit.preflight", _do)


async def quote(
    preflight_id: str | None = None,
    mode: str = "local",
    random_seeds: list[int] | None = None,
    returns_path: str | None = None,
    returns_rows: list[dict] | None = None,
    trades_path: str | None = None,
    trades_rows: list[dict] | None = None,
    benchmark_path: str | None = None,
    benchmark_rows: list[dict] | None = None,
    cost_assumptions_path: str | None = None,
    cost_assumptions: dict | None = None,
    features_manifest_path: str | None = None,
    features_manifest: dict | None = None,
    strategy_name: str = "candidate_strategy",
) -> dict:
    """Return a deterministic execution plan, credit/GBP quote, and input-bound approval_token."""
    redaction.reject_keylike_args({"preflight_id": preflight_id, "mode": mode})

    async def _do():
        norm_mode = (mode or "local").strip().lower()
        if norm_mode not in ("local", "hosted"):
            raise ValueError("mode must be 'local' or 'hosted'")

        pf = _PREFLIGHT_STORE.get(preflight_id or "")
        if pf is None:
            pf_res = await preflight(
                returns_path=returns_path,
                returns_rows=returns_rows,
                trades_path=trades_path,
                trades_rows=trades_rows,
                benchmark_path=benchmark_path,
                benchmark_rows=benchmark_rows,
                cost_assumptions_path=cost_assumptions_path,
                cost_assumptions=cost_assumptions,
                features_manifest_path=features_manifest_path,
                features_manifest=features_manifest,
                strategy_name=strategy_name,
            )
            pf = _PREFLIGHT_STORE.get(pf_res.get("preflight_id", ""))
            if pf is None:
                return pf_res

        if not pf["eligible"]:
            return {
                "tool": "audit.quote",
                "preflight_id": pf["preflight_id"],
                "eligible": False,
                "status": "PREFLIGHT_REFUSED",
                "validation_errors": pf["validation_errors"],
                "credits_cost": 0,
                "credits_charged": 0,
                "reason": "Preflight validation failed — no quote or approval_token issued until input errors are fixed.",
            }

        seeds = [int(s) for s in (random_seeds if random_seeds else [0, 1, 2, 3, 4])]
        credits_cost = 0 if norm_mode == "local" else HOSTED_AUDIT_CREDITS
        starter_rate = plans.CREDIT_PACKS["credits-10k"]["price_gbp"] / plans.CREDIT_PACKS["credits-10k"]["calls"]
        pro_rate = plans.CREDIT_PACKS["credits-100k"]["price_gbp"] / plans.CREDIT_PACKS["credits-100k"]["calls"]
        starter_gbp = round(credits_cost * starter_rate, 4)
        pro_gbp = round(credits_cost * pro_rate, 4)

        avail = pf["coverage_matrix"]["available_checks"]
        execution_plan = [
            {
                "step": idx + 1,
                "check_id": chk["check_id"],
                "title": chk["title"],
                "execution_location": norm_mode,
                "credits": 0 if norm_mode == "local" else (credits_cost if idx == 0 else 0),
            }
            for idx, chk in enumerate(avail)
        ]

        row_count = pf["returns_summary"].get("row_count", 0)
        est_seconds = round(0.15 + 0.0005 * row_count + (1.5 if norm_mode == "hosted" else 0.0), 2)
        approval_token = _compute_approval_token(
            input_sha256=pf["input_sha256"],
            mode=norm_mode,
            credits_cost=credits_cost,
            seeds=seeds,
        )

        return {
            "tool": "audit.quote",
            "preflight_id": pf["preflight_id"],
            "strategy_name": pf["strategy_name"],
            "eligible": True,
            "mode": norm_mode,
            "input_sha256": pf["input_sha256"],
            "package_version": server_meta.PACKAGE_VERSION,
            "random_seeds": seeds,
            "execution_plan": execution_plan,
            "not_tested_count": len(pf["coverage_matrix"]["not_tested_checks"]),
            "estimated_runtime_seconds": est_seconds,
            "credits_cost": credits_cost,
            "gbp_cost": {
                "starter_pack_gbp": starter_gbp,
                "pro_pack_gbp": pro_gbp,
                "display": (
                    "£0.00 (100% local execution, 0 credits)"
                    if credits_cost == 0
                    else f"{credits_cost} credits (£{starter_gbp:.3f} at Starter £19/10k rate, £{pro_gbp:.4f} at Pro £149/100k rate)"
                ),
            },
            "outbound_data_preview": pf["coverage_matrix"]["outbound_data_preview"][
                "local_mode" if norm_mode == "local" else "hosted_mode"
            ],
            "approval_token": approval_token,
            "credits_charged": 0,
            "next_step": (
                f"Call `audit.run(preflight_id='{pf['preflight_id']}', approval_token='{approval_token}', "
                f"idempotency_key='run-1', max_credits={credits_cost}, mode='{norm_mode}')`."
            ),
        }

    return await _run_audit_tool("audit.quote", _do, require_token=(str(mode or "local").strip().lower() == "hosted"))


def _evaluate_findings(pf: dict[str, Any], seeds: list[int]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Run all diagnostic checks and produce structured findings + summary metrics."""
    internal = pf["_internal"]
    norm_ret: pd.DataFrame = internal["norm_ret"]
    norm_trades: pd.DataFrame | None = internal["norm_trades"]
    bench_df: pd.DataFrame | None = internal["bench_df"]
    manifest_dict: dict | None = internal["manifest_dict"]
    costs: dict[str, float] = internal["merged_costs"]

    rets = norm_ret["_ret"].to_numpy(dtype=float)
    n_obs = len(rets)
    # Group by ISO year-week to count independent weekly blocks
    iso_cal = norm_ret["_ts"].dt.isocalendar()
    weekly_groups = norm_ret.groupby([iso_cal["year"], iso_cal["week"]])["_ret"].apply(
        lambda s: float(np.prod(1.0 + s.to_numpy(dtype=float)) - 1.0)
    )
    n_weeks = int(len(weekly_groups))

    mean_daily = float(np.mean(rets)) if n_obs > 0 else 0.0
    std_daily = float(np.std(rets, ddof=1)) if n_obs > 1 else 0.0
    sharpe_gross = (mean_daily / std_daily) * np.sqrt(252.0) if std_daily > 1e-12 else 0.0

    # Cumulative equity & drawdown
    equity_curve = np.cumprod(1.0 + rets)
    running_peak = np.maximum.accumulate(equity_curve)
    drawdowns = (equity_curve - running_peak) / np.maximum(running_peak, 1e-12)
    max_dd_pct = float(abs(np.min(drawdowns)) * 100.0) if n_obs > 0 else 0.0

    # Rolling 5-day worst loss
    if n_obs >= 5:
        roll5 = pd.Series(rets).rolling(5).apply(lambda x: float(np.prod(1.0 + x) - 1.0), raw=True).dropna()
        worst_5d_pct = float(roll5.min() * 100.0) if not roll5.empty else float(np.min(rets) * 100.0)
    else:
        worst_5d_pct = float(np.min(rets) * 100.0) if n_obs > 0 else 0.0

    # Bootstrap Sharpe CI across supplied seeds
    rng = np.random.default_rng(seeds[0] if seeds else 0)
    boot_sharpes: list[float] = []
    if n_obs >= 5 and std_daily > 1e-12:
        for s in (seeds or [0, 1, 2, 3, 4]):
            r_seed = np.random.default_rng(int(s))
            for _ in range(40):
                sample = r_seed.choice(rets, size=n_obs, replace=True)
                sd = float(np.std(sample, ddof=1))
                if sd > 1e-12:
                    boot_sharpes.append(float((np.mean(sample) / sd) * np.sqrt(252.0)))
    ci_lo = float(np.percentile(boot_sharpes, 5)) if boot_sharpes else sharpe_gross
    ci_hi = float(np.percentile(boot_sharpes, 95)) if boot_sharpes else sharpe_gross

    # Cost model calculations using vendored guardrails fill_model
    spread_bps = float(costs.get("spread_bps", 2.0))
    slippage_bps = float(costs.get("slippage_bps", 3.0))
    adv_bps = float(costs.get("adverse_selection_bps", 2.0))
    comm_bps = float(costs.get("commission_bps", 0.5))
    one_way_bps = float(one_way_cost_bps(spread_bps, slippage_bps, adv_bps)) + comm_bps
    rt_cost_bps = float(round_trip_cost_bps(spread_bps, slippage_bps, adv_bps)) + 2.0 * comm_bps

    if norm_trades is not None and not norm_trades.empty:
        n_trades = int(len(norm_trades))
        gross_trade_bps = float(norm_trades["_pnl_bps"].mean())
        total_gross_bps = float(norm_trades["_pnl_bps"].sum())
        trades_per_year = (n_trades / max(n_obs, 1)) * 252.0
    else:
        # Estimate active turnover from daily return sign flips if trades not provided
        active_days = int(np.sum(np.abs(rets) > 1e-6))
        n_trades = active_days
        gross_trade_bps = float(mean_daily * 10000.0)
        total_gross_bps = float(np.sum(rets) * 10000.0)
        trades_per_year = (active_days / max(n_obs, 1)) * 126.0

    net_trade_bps_1x = gross_trade_bps - rt_cost_bps
    net_trade_bps_1_5x = gross_trade_bps - (1.5 * rt_cost_bps)
    net_trade_bps_2x = gross_trade_bps - (2.0 * rt_cost_bps)
    breakeven_mult = round(gross_trade_bps / max(rt_cost_bps, 1e-6), 2)
    annual_cost_drag_bps = round(trades_per_year * rt_cost_bps, 1)
    net_total_bps_1x = round(total_gross_bps - n_trades * rt_cost_bps, 1)

    # Net daily return series approximation for Net Sharpe
    daily_cost_dec = (n_trades * rt_cost_bps / 10000.0) / max(n_obs, 1)
    net_rets = rets - daily_cost_dec
    sharpe_net = float((np.mean(net_rets) / np.std(net_rets, ddof=1)) * np.sqrt(252.0)) if n_obs > 1 and np.std(net_rets, ddof=1) > 1e-12 else 0.0

    findings: list[dict[str, Any]] = []
    underpowered = n_obs < MIN_DAILY_OBSERVATIONS or n_weeks < MIN_WEEKLY_BLOCKS

    # 1. Sample Depth & Statistical Power
    if underpowered:
        findings.append({
            "finding": "Sample Depth & Statistical Power Floor",
            "category": "INSUFFICIENT_EVIDENCE",
            "evidence": (
                f"Observed {n_obs} session(s) across {n_weeks} weekly block(s) "
                f"(minimum required: {MIN_DAILY_OBSERVATIONS} daily sessions and {MIN_WEEKLY_BLOCKS} weekly blocks). "
                f"Bootstrap 90% Sharpe CI spans [{ci_lo:.2f}, {ci_hi:.2f}]."
            ),
            "why_it_matters": (
                "Sub-threshold sample windows cannot distinguish genuine cross-sectional edge from single-regime luck; "
                "promoting on <20 weekly blocks inflates false-discovery rates above 50%."
            ),
            "next_experiment": (
                f"Extend the backtest or out-of-sample replay to at least {MIN_DAILY_OBSERVATIONS} trading sessions "
                f"({MIN_WEEKLY_BLOCKS}+ weeks) spanning multiple volatility regimes before interpreting Sharpe."
            ),
            "limitation": "Passing the sample-size floor is a necessary precondition, not proof of out-of-sample alpha.",
        })
    elif sharpe_gross <= 0.5 or ci_lo < 0.0:
        findings.append({
            "finding": "Sample Depth & Statistical Power Floor",
            "category": "SENSITIVITY_WARNING" if sharpe_gross > 0 else "OBSERVED_PROBLEM",
            "evidence": (
                f"Observed {n_obs} sessions ({n_weeks} weekly blocks); annualized gross Sharpe is {sharpe_gross:.2f} "
                f"with 90% bootstrap CI [{ci_lo:.2f}, {ci_hi:.2f}] crossing zero."
            ),
            "why_it_matters": (
                "A bootstrap confidence interval that crosses zero indicates the observed positive return is within "
                "1-sigma noise of a flat or losing strategy."
            ),
            "next_experiment": (
                "Decompose returns by market regime (`gym.label_regimes`) and test whether a single drawdown cluster "
                "or noisy entry filter is diluting the signal."
            ),
            "limitation": "Bootstrap resampling preserves marginal distribution but does not test structural regime breaks.",
        })
    else:
        findings.append({
            "finding": "Sample Depth & Statistical Power Floor",
            "category": "PASSED",
            "evidence": (
                f"Observed {n_obs} sessions across {n_weeks} weekly blocks (clears {MIN_DAILY_OBSERVATIONS}-session / "
                f"{MIN_WEEKLY_BLOCKS}-week floor). Gross Sharpe {sharpe_gross:.2f}, 90% bootstrap CI [{ci_lo:.2f}, {ci_hi:.2f}]."
            ),
            "why_it_matters": "Provides sufficient time-series degrees of freedom to evaluate regime stability and cost drag.",
            "next_experiment": "Proceed to multi-seed paired comparison against the live champion (`tournament.submit`).",
            "limitation": "Single-path historical depth does not account for unrecorded parameter trials (Deflated Sharpe).",
        })

    # 2. Pessimistic Execution Cost & Stress Survival
    if underpowered and norm_trades is not None and len(norm_trades) < MIN_TRADE_OBSERVATIONS:
        findings.append({
            "finding": "Pessimistic Execution Cost & Slippage Stress Survival",
            "category": "INSUFFICIENT_EVIDENCE",
            "evidence": (
                f"Round-trip pessimistic house cost is {rt_cost_bps:.1f} bps (one-way {one_way_bps:.1f} bps), "
                f"but only {len(norm_trades)} trade(s) and {n_obs} session(s) are available (need >= {MIN_TRADE_OBSERVATIONS} trades)."
            ),
            "why_it_matters": "Mean gross edge per trade cannot be reliably estimated against execution friction on a tiny trade count.",
            "next_experiment": f"Collect at least {MIN_TRADE_OBSERVATIONS} completed round-trip trades across normal and high-volatility sessions.",
            "limitation": "Uses constant bps fill assumptions rather than order-book depth impact (`market.microstructure`).",
        })
    elif net_trade_bps_1x <= 0.0:
        findings.append({
            "finding": "Pessimistic Execution Cost & Slippage Stress Survival",
            "category": "OBSERVED_PROBLEM",
            "evidence": (
                f"Mean gross edge is {gross_trade_bps:.2f} bps/trade, which is consumed by 1.0x pessimistic round-trip costs "
                f"of {rt_cost_bps:.2f} bps/trade -> net {net_trade_bps_1x:.2f} bps/trade (breakeven cost multiplier: {breakeven_mult:.2f}x; "
                f"annualized cost drag: {annual_cost_drag_bps:.1f} bps; net Sharpe: {sharpe_net:.2f})."
            ),
            "why_it_matters": (
                "Strategies whose gross edge per trade is smaller than realistic spread + slippage + adverse selection "
                "bleed capital deterministically in live execution."
            ),
            "next_experiment": (
                "Increase minimum holding horizon or signal conviction threshold to raise gross edge per trade above "
                f"{rt_cost_bps * 1.5:.1f} bps, or reduce turnover via a wider no-trade buffer (`warden.plan_rebalance`)."
            ),
            "limitation": "Assumes static per-trade bps friction; flash-crash tail slippage can exceed 2.0x normal assumptions.",
        })
    elif net_trade_bps_1_5x <= 0.0 or net_trade_bps_2x <= 0.0:
        findings.append({
            "finding": "Pessimistic Execution Cost & Slippage Stress Survival",
            "category": "SENSITIVITY_WARNING",
            "evidence": (
                f"Positive net edge at 1.0x costs (+{net_trade_bps_1x:.2f} bps/trade, net Sharpe {sharpe_net:.2f}), "
                f"but turns negative under 1.5x–2.0x cost stress (1.5x: {net_trade_bps_1_5x:.2f} bps/trade, "
                f"2.0x: {net_trade_bps_2x:.2f} bps/trade; breakeven multiplier: {breakeven_mult:.2f}x)."
            ),
            "why_it_matters": (
                "Live execution during volatility spikes or earnings gaps routinely widens effective spreads and adverse "
                "selection by 1.5x–2.0x over calm-market backtest assumptions."
            ),
            "next_experiment": (
                "Filter out entries when 20-day relative volume is thin or ATR percentile > 85%, and re-test "
                "breakeven cost multiplier above 2.0x."
            ),
            "limitation": "Does not model market-impact convexity for institutional notional sizes.",
        })
    else:
        findings.append({
            "finding": "Pessimistic Execution Cost & Slippage Stress Survival",
            "category": "PASSED",
            "evidence": (
                f"Gross edge of {gross_trade_bps:.2f} bps/trade survives 1.0x ({rt_cost_bps:.2f} bps -> net +{net_trade_bps_1x:.2f} bps) "
                f"and 2.0x pessimistic cost stress ({2.0 * rt_cost_bps:.2f} bps -> net +{net_trade_bps_2x:.2f} bps; "
                f"breakeven multiplier: {breakeven_mult:.2f}x; net Sharpe: {sharpe_net:.2f})."
            ),
            "why_it_matters": "Confirms a structural buffer above realistic bid-ask spread, slippage, and adverse selection.",
            "next_experiment": "Verify liquidity capacity on target symbols using `market.microstructure` before scaling notional.",
            "limitation": "Based on declared/house bps parameters; borrow fees for short legs are not included.",
        })

    # 3. Drawdown Depth & Volatility-Regime Profile
    vol_roll = pd.Series(rets).rolling(20).std(ddof=1)
    valid_vol = vol_roll.dropna()
    if len(valid_vol) >= 20:
        med_vol = float(valid_vol.median())
        high_vol_mask = (vol_roll > med_vol).to_numpy()
        calm_mask = (vol_roll <= med_vol).to_numpy()
        high_vol_bps = float(np.mean(rets[high_vol_mask]) * 10000.0) if np.any(high_vol_mask) else 0.0
        calm_bps = float(np.mean(rets[calm_mask]) * 10000.0) if np.any(calm_mask) else 0.0
    else:
        high_vol_bps = float(mean_daily * 10000.0)
        calm_bps = float(mean_daily * 10000.0)

    if underpowered:
        findings.append({
            "finding": "Drawdown Tail & Volatility-Regime Consistency",
            "category": "INSUFFICIENT_EVIDENCE",
            "evidence": (
                f"Max drawdown is {max_dd_pct:.2f}% and worst 5-session return is {worst_5d_pct:.2f}% over only {n_obs} sessions."
            ),
            "why_it_matters": "Drawdown distributions are fat-tailed; windows shorter than 60 sessions systematically understate tail risk.",
            "next_experiment": "Run `gym.probe_fragility` over a multi-year panel covering high-vol, drawdown, chop, melt-up, and gap-heavy regimes.",
            "limitation": "Historical max drawdown is a lower bound on future tail risk.",
        })
    elif max_dd_pct > 25.0 or worst_5d_pct < -12.0:
        findings.append({
            "finding": "Drawdown Tail & Volatility-Regime Consistency",
            "category": "OBSERVED_PROBLEM",
            "evidence": (
                f"Max drawdown reached {max_dd_pct:.2f}% (worst 5-session loss: {worst_5d_pct:.2f}%; "
                f"calm-regime mean: {calm_bps:.2f} bps/day vs high-vol mean: {high_vol_bps:.2f} bps/day)."
            ),
            "why_it_matters": "Drawdowns exceeding 25% or sharp 5-day crashes trigger risk-engine halts and force liquidation at the bottom.",
            "next_experiment": "Apply ATR-scaled position sizing (`warden.explain_sizing`) and halve exposure on overnight gaps > 1.2x ATR.",
            "limitation": "Evaluates close-to-close equity; intraday excursion can exceed close-to-close drawdown.",
        })
    elif high_vol_bps < -5.0 or max_dd_pct > 15.0:
        findings.append({
            "finding": "Drawdown Tail & Volatility-Regime Consistency",
            "category": "SENSITIVITY_WARNING",
            "evidence": (
                f"Max drawdown {max_dd_pct:.2f}% (worst 5-session return {worst_5d_pct:.2f}%); "
                f"calm-regime mean is +{calm_bps:.2f} bps/day vs high-volatility regime mean of {high_vol_bps:.2f} bps/day."
            ),
            "why_it_matters": "Negative expectancy in high-volatility regimes indicates the strategy is short volatility or vulnerable to regime shifts.",
            "next_experiment": "Gate new entries using `market.regime` state posteriors when `crisis_down` or `choppy_correction` exceeds 0.40.",
            "limitation": "Two-state rolling volatility split is coarser than the 5-regime gym matrix.",
        })
    else:
        findings.append({
            "finding": "Drawdown Tail & Volatility-Regime Consistency",
            "category": "PASSED",
            "evidence": (
                f"Max drawdown contained at {max_dd_pct:.2f}% (worst 5-session return: {worst_5d_pct:.2f}%); "
                f"positive mean return in both calm (+{calm_bps:.2f} bps/day) and elevated-volatility (+{high_vol_bps:.2f} bps/day) windows."
            ),
            "why_it_matters": "Demonstrates controlled tail excursion and positive expectancy across volatility environments.",
            "next_experiment": "Verify worst-regime margin across all 5 canonical regimes via `gym.probe_fragility`.",
            "limitation": "Close-to-close series does not capture intraday stop-out slippage.",
        })

    # 4. Trade Concentration, House Position Caps & Turnover Fragility
    top5_share_pct = 0.0
    max_pos_seen = 0.0
    if norm_trades is not None and not norm_trades.empty:
        tr_count = int(len(norm_trades))
        pos_pnl = norm_trades[norm_trades["_pnl_bps"] > 0]["_pnl_bps"].sort_values(ascending=False)
        total_pos_pnl = float(pos_pnl.sum()) if not pos_pnl.empty else 0.0
        top5_pnl = float(pos_pnl.head(5).sum()) if not pos_pnl.empty else 0.0
        top5_share_pct = round((top5_pnl / total_pos_pnl) * 100.0, 1) if total_pos_pnl > 1e-9 else 100.0
        max_pos_seen = float(norm_trades["_pos_val"].max())
        # Normalize if given in percent (e.g. 25.0 instead of 0.25) or notional (assume $100k base if > 1.0)
        if max_pos_seen > 100.0:
            max_pos_pct = max_pos_seen / 100000.0
        elif max_pos_seen > 1.0:
            max_pos_pct = max_pos_seen / 100.0
        else:
            max_pos_pct = max_pos_seen
        breaches = int((norm_trades["_pos_val"].apply(
            lambda v: (v / 100000.0 if v > 100.0 else (v / 100.0 if v > 1.0 else v)) > MAX_POSITION_PCT + 1e-9
        )).sum())

        if tr_count < MIN_TRADE_OBSERVATIONS:
            findings.append({
                "finding": "Trade Outlier Concentration & House Position Cap Compliance",
                "category": "INSUFFICIENT_EVIDENCE",
                "evidence": (
                    f"Only {tr_count} trade record(s) supplied (minimum {MIN_TRADE_OBSERVATIONS} required for "
                    f"concentration inference). Top-5 trades represent {top5_share_pct:.1f}% of gross gains."
                ),
                "why_it_matters": "With fewer than 20 trades, a single lucky outlier dominates headline PnL.",
                "next_experiment": f"Expand universe or sample horizon to record >= {MIN_TRADE_OBSERVATIONS} independent trades.",
                "limitation": "Does not inspect concurrent portfolio gross exposure across overlapping open positions.",
            })
        elif breaches > 0 or top5_share_pct > 65.0:
            findings.append({
                "finding": "Trade Outlier Concentration & House Position Cap Compliance",
                "category": "OBSERVED_PROBLEM",
                "evidence": (
                    f"Top-5 trades account for {top5_share_pct:.1f}% of positive gross PnL across {tr_count} trades; "
                    f"max single-position weight reached {max_pos_pct * 100:.1f}% of equity ({breaches} trade(s) breached "
                    f"the {MAX_POSITION_PCT * 100:.0f}% house cap)."
                ),
                "why_it_matters": (
                    "Backtests driven by a handful of oversized outlier trades fail in production when the live risk gate "
                    f"(`warden.validate_order`) clamps single-name exposure at {MAX_POSITION_PCT * 100:.0f}% of equity."
                ),
                "next_experiment": (
                    "Re-run the backtest with hard pre-trade position clamping (`max_position_pct <= 0.25`, "
                    "`max_gross_exposure_pct <= 0.60`) and winsorize top-5 trades to verify edge persists."
                ),
                "limitation": "Evaluates per-trade size; full intraday portfolio overlap requires timestamped position snapshots.",
            })
        elif top5_share_pct > 45.0:
            findings.append({
                "finding": "Trade Outlier Concentration & House Position Cap Compliance",
                "category": "SENSITIVITY_WARNING",
                "evidence": (
                    f"Zero breaches of the {MAX_POSITION_PCT * 100:.0f}% house position cap (max weight {max_pos_pct * 100:.1f}%), "
                    f"but top-5 trades contribute {top5_share_pct:.1f}% of positive gross PnL across {tr_count} trades."
                ),
                "why_it_matters": "Moderate right-tail concentration means missing a few breakout fills materially reduces realized Sharpe.",
                "next_experiment": "Test return stability when removing the top 3 trades from the sample.",
                "limitation": "Does not model partial fills on limit orders.",
            })
        else:
            findings.append({
                "finding": "Trade Outlier Concentration & House Position Cap Compliance",
                "category": "PASSED",
                "evidence": (
                    f"Across {tr_count} trades, top-5 trades account for {top5_share_pct:.1f}% of gross gains (well below 45% threshold) "
                    f"and max single-position size is {max_pos_pct * 100:.1f}% (0 breaches of the {MAX_POSITION_PCT * 100:.0f}% house floor)."
                ),
                "why_it_matters": "Confirms broad-based trade breadth and full compliance with live-capital position sizing invariants.",
                "next_experiment": "Validate multi-asset rebalance trade lists using `warden.plan_rebalance`.",
                "limitation": "Assumes trade timestamps reflect actual execution fill times.",
            })
    else:
        findings.append({
            "finding": "Trade Outlier Concentration & House Position Cap Compliance",
            "category": "NOT_TESTED",
            "evidence": "No trade records artifact (`trades_path` or `trades_rows`) was supplied.",
            "why_it_matters": "Without trade-level records, single-name concentration and 25% position-cap breaches cannot be audited.",
            "next_experiment": "Supply a trade log CSV/Parquet with `ts`, `symbol`, `pnl_bps`, and `position_pct` to enable this check.",
            "limitation": "Daily portfolio returns mask underlying single-stock position sizing violations.",
        })

    # 5. Point-in-Time Feature Provenance & Lookahead Audit
    if manifest_dict is not None and isinstance(manifest_dict.get("features"), list):
        as_of_raw = manifest_dict.get("as_of")
        as_of_ts = pd.to_datetime(as_of_raw, utc=True, errors="coerce") if as_of_raw else norm_ret["_ts"].iloc[-1]
        leaks: list[str] = []
        banned_sources: list[str] = []
        low_cov: list[str] = []
        tier_c_feats: list[str] = []

        for f in manifest_dict["features"]:
            fname = str(f.get("name", "unnamed"))
            fsrc = str(f.get("source", "")).strip()
            avail_raw = f.get("available_at")
            cov = f.get("coverage", 1.0)
            ftier = feature_tier(fname)

            if any(b in fsrc.lower() for b in BANNED_ACTUALS_SOURCES):
                banned_sources.append(f"{fname} (source={fsrc!r})")
            if avail_raw:
                avail_ts = pd.to_datetime(avail_raw, utc=True, errors="coerce")
                if pd.notna(avail_ts) and pd.notna(as_of_ts) and avail_ts > as_of_ts:
                    leaks.append(f"{fname} (available_at={avail_raw} > as_of={as_of_raw})")
            try:
                if float(cov) < MIN_COVERAGE:
                    low_cov.append(f"{fname} (coverage={float(cov):.2f} < {MIN_COVERAGE:.2f})")
            except (TypeError, ValueError):
                low_cov.append(f"{fname} (invalid coverage={cov!r})")
            if ftier == "C":
                tier_c_feats.append(fname)

        if leaks or banned_sources:
            findings.append({
                "finding": "Point-in-Time Feature Provenance & Lookahead Audit",
                "category": "OBSERVED_PROBLEM",
                "evidence": (
                    f"Detected {len(leaks)} timestamp lookahead leak(s) {leaks[:5]} and "
                    f"{len(banned_sources)} banned actuals source(s) {banned_sources[:5]}."
                ),
                "why_it_matters": (
                    "Features computed from timestamps after `as_of` or from archive actuals (rather than point-in-time "
                    "vintage releases) inject future knowledge into historical decisions and invalidate the backtest."
                ),
                "next_experiment": (
                    "Lag offending features to their public filing/release timestamp (`available_at <= as_of`) and "
                    "replace archive actuals with point-in-time vintage series (`warden.audit_features`)."
                ),
                "limitation": "Audits declared manifest metadata; does not parse arbitrary Python/C# feature-engineering code.",
            })
        elif low_cov or tier_c_feats:
            findings.append({
                "finding": "Point-in-Time Feature Provenance & Lookahead Audit",
                "category": "SENSITIVITY_WARNING",
                "evidence": (
                    f"Zero timestamp leaks, but {len(low_cov)} low-coverage feature(s) {low_cov[:5]} and "
                    f"{len(tier_c_feats)} Tier-C/unregistered feature(s) {tier_c_feats[:5]}."
                ),
                "why_it_matters": (
                    f"Features below {MIN_COVERAGE * 100:.0f}% universe coverage induce survivorship/selection bias, "
                    "and Tier-C features cannot be replayed on standard price panels."
                ),
                "next_experiment": "Restrict core genome rules to Tier-A causal price/volume features or supply point-in-time tape evidence.",
                "limitation": "Manifest timestamps are self-declared unless generated by `features.build`.",
            })
        else:
            findings.append({
                "finding": "Point-in-Time Feature Provenance & Lookahead Audit",
                "category": "PASSED",
                "evidence": (
                    f"All {len(manifest_dict['features'])} declared feature(s) passed point-in-time timestamp checks "
                    f"(`available_at <= as_of`), zero banned actuals sources, and coverage >= {MIN_COVERAGE * 100:.0f}%."
                ),
                "why_it_matters": "Verifies declared feature inputs are causally knowable at decision time.",
                "next_experiment": "Cross-check cross-sectional IC significance on alternative-data features with `pit.analysis`.",
                "limitation": "Validates the feature manifest contract; does not verify internal code implementation.",
            })
    else:
        findings.append({
            "finding": "Point-in-Time Feature Provenance & Lookahead Audit",
            "category": "NOT_TESTED",
            "evidence": "No feature provenance manifest (`features_manifest_path` or `features_manifest`) was supplied.",
            "why_it_matters": "Without a feature manifest, lookahead leakage from restated fundamentals or future timestamps cannot be ruled out.",
            "next_experiment": "Pass a `features_manifest` dict or JSON file declaring each feature's `source`, `available_at`, and `coverage`.",
            "limitation": "Return series alone cannot reveal whether features used future information.",
        })

    # 6. Benchmark Active Return (if benchmark supplied, else NOT_TESTED)
    if bench_df is not None and not bench_df.empty:
        b_norm, b_err, _ = _normalize_returns_df(bench_df)
        if not b_err and not b_norm.empty:
            merged = pd.merge(
                norm_ret[["_ts", "_ret"]],
                b_norm[["_ts", "_ret"]].rename(columns={"_ret": "_bret"}),
                on="_ts",
                how="inner",
            )
            if len(merged) >= 20:
                active = merged["_ret"].to_numpy(dtype=float) - merged["_bret"].to_numpy(dtype=float)
                ir = float((np.mean(active) / np.std(active, ddof=1)) * np.sqrt(252.0)) if np.std(active, ddof=1) > 1e-12 else 0.0
                active_bps_ann = float(np.mean(active) * 252.0 * 10000.0)
                findings.append({
                    "finding": "Benchmark Active Return & Information Ratio",
                    "category": "PASSED" if ir >= 0.5 else ("SENSITIVITY_WARNING" if ir >= 0.0 else "OBSERVED_PROBLEM"),
                    "evidence": (
                        f"Matched {len(merged)} overlapping sessions vs benchmark; annualized active return is "
                        f"{active_bps_ann:.1f} bps with Information Ratio {ir:.2f}."
                    ),
                    "why_it_matters": "Separates genuine active edge from passive market beta exposure.",
                    "next_experiment": "Verify active return stability across drawdown and chop regimes (`gym.paired_preview`).",
                    "limitation": "Linear benchmark subtraction does not capture nonlinear tail beta.",
                })
            else:
                findings.append({
                    "finding": "Benchmark Active Return & Information Ratio",
                    "category": "INSUFFICIENT_EVIDENCE",
                    "evidence": f"Only {len(merged)} overlapping session(s) between strategy returns and benchmark.",
                    "why_it_matters": "Active Information Ratio requires overlapping session history.",
                    "next_experiment": "Align benchmark timestamps with strategy session dates.",
                    "limitation": "Unaligned dates prevent active-return attribution.",
                })
    else:
        findings.append({
            "finding": "Benchmark Active Return & Information Ratio",
            "category": "NOT_TESTED",
            "evidence": "No benchmark return series (`benchmark_path` or `benchmark_rows`) was supplied.",
            "why_it_matters": "Without a benchmark series, passive beta exposure cannot be separated from active alpha.",
            "next_experiment": "Supply a benchmark CSV/Parquet (e.g. SPY or equal-weight universe) to compute active Information Ratio.",
            "limitation": "Absolute Sharpe can be inflated by secular bull-market drift.",
        })

    # 7. Strategy Code & Selection-Bias Boundary (always explicit)
    findings.append({
        "finding": "Underlying Strategy Code & Multiple-Testing Trial Ledger Boundary",
        "category": "NOT_TESTED",
        "evidence": "Tabular artifact audit only — strategy source code and discarded trial count were not evaluated locally.",
        "why_it_matters": (
            "Even a clean return series and manifest cannot prove that 500 parameter combinations were not swept "
            "behind the scenes to find one lucky backtest."
        ),
        "next_experiment": (
            "Submit the candidate genome to the hosted Shadow Tournament (`tournament.submit`) for paired replay "
            "against the live champion and Deflated Sharpe ratio accounting."
        ),
        "limitation": "Local tabular checks test the artifact you brought, not the research process that selected it.",
    })

    counts = {cat: sum(1 for f in findings if f["category"] == cat) for cat in DIAGNOSTIC_CATEGORIES}
    if counts["OBSERVED_PROBLEM"] > 0:
        overall = "PROBLEMS_OBSERVED"
    elif counts["INSUFFICIENT_EVIDENCE"] > 0:
        overall = "INSUFFICIENT_EVIDENCE"
    elif counts["SENSITIVITY_WARNING"] > 0:
        overall = "SENSITIVITY_WARNINGS"
    else:
        overall = "PASSED_LOCAL_AUDIT"

    metrics = {
        "n_observations": n_obs,
        "n_weekly_blocks": n_weeks,
        "n_trades": int(len(norm_trades)) if norm_trades is not None else 0,
        "sharpe_gross_annualized": round(sharpe_gross, 3),
        "sharpe_net_annualized": round(sharpe_net, 3),
        "sharpe_bootstrap_ci_90": [round(ci_lo, 3), round(ci_hi, 3)],
        "gross_edge_bps_per_trade": round(gross_trade_bps, 2),
        "round_trip_cost_bps_1x": round(rt_cost_bps, 2),
        "net_edge_bps_per_trade_1x": round(net_trade_bps_1x, 2),
        "net_edge_bps_per_trade_2x": round(net_trade_bps_2x, 2),
        "breakeven_cost_multiplier": breakeven_mult,
        "net_return_total_bps": net_total_bps_1x,
        "max_drawdown_pct": round(max_dd_pct, 2),
        "worst_5d_return_pct": round(worst_5d_pct, 2),
        "top5_pnl_share_pct": round(top5_share_pct, 1),
        "max_position_weight_pct": round(max_pos_seen * 100.0 if max_pos_seen <= 1.0 else max_pos_seen, 2),
        "counts_by_category": counts,
        "overall_assessment": overall,
    }
    return findings, metrics


async def run(
    approval_token: str,
    idempotency_key: str,
    max_credits: int = 0,
    preflight_id: str | None = None,
    mode: str = "local",
    random_seeds: list[int] | None = None,
    returns_path: str | None = None,
    returns_rows: list[dict] | None = None,
    trades_path: str | None = None,
    trades_rows: list[dict] | None = None,
    benchmark_path: str | None = None,
    benchmark_rows: list[dict] | None = None,
    cost_assumptions_path: str | None = None,
    cost_assumptions: dict | None = None,
    features_manifest_path: str | None = None,
    features_manifest: dict | None = None,
    strategy_name: str = "candidate_strategy",
) -> dict:
    """Execute the Quant Swarm Audit with deterministic approval, idempotency, and spend-cap gates."""
    # Note: approval_token and idempotency_key are explicit workflow parameters, so we do not pass
    # them to reject_keylike_args (which blocks generic 'token'/'key' names aimed at API credentials).
    redaction.reject_keylike_args({
        "preflight_id": preflight_id,
        "mode": mode,
        "returns_path": returns_path,
        "trades_path": trades_path,
        "strategy_name": strategy_name,
    })

    async def _do():
        if not idempotency_key or not str(idempotency_key).strip():
            return {
                "tool": "audit.run",
                "status": "REFUSED_MISSING_IDEMPOTENCY_KEY",
                "credits_charged": 0,
                "error": "idempotency_key is required to prevent duplicate execution or billing on retries.",
            }
        idem = str(idempotency_key).strip()
        norm_mode = (mode or "local").strip().lower()
        if norm_mode not in ("local", "hosted"):
            return {
                "tool": "audit.run",
                "status": "REFUSED_INVALID_MODE",
                "credits_charged": 0,
                "error": "mode must be 'local' or 'hosted'.",
            }

        pf = _PREFLIGHT_STORE.get(preflight_id or "")
        if pf is None:
            pf_res = await preflight(
                returns_path=returns_path,
                returns_rows=returns_rows,
                trades_path=trades_path,
                trades_rows=trades_rows,
                benchmark_path=benchmark_path,
                benchmark_rows=benchmark_rows,
                cost_assumptions_path=cost_assumptions_path,
                cost_assumptions=cost_assumptions,
                features_manifest_path=features_manifest_path,
                features_manifest=features_manifest,
                strategy_name=strategy_name,
            )
            pf = _PREFLIGHT_STORE.get(pf_res.get("preflight_id", ""))
            if pf is None:
                return {**pf_res, "tool": "audit.run", "credits_charged": 0}

        # 1. Idempotency check BEFORE any work or billing
        if idem in _IDEMPOTENCY_STORE:
            existing = _IDEMPOTENCY_STORE[idem]
            if existing["input_sha256"] != pf["input_sha256"] or existing["mode"] != norm_mode:
                return {
                    "tool": "audit.run",
                    "status": "IDEMPOTENCY_KEY_CONFLICT",
                    "idempotency_key": idem,
                    "credits_charged": 0,
                    "error": (
                        f"idempotency_key {idem!r} was already used for a different input_sha256 or mode. "
                        "Use a fresh idempotency_key for new inputs."
                    ),
                }
            cached_resp = dict(existing["response"])
            cached_resp["idempotent_replay"] = True
            cached_resp["credits_charged"] = 0
            return cached_resp

        # 2. Preflight eligibility refusal (NEVER charges credits)
        if not pf["eligible"]:
            return {
                "tool": "audit.run",
                "preflight_id": pf["preflight_id"],
                "status": "PREFLIGHT_REFUSED",
                "eligible": False,
                "validation_errors": pf["validation_errors"],
                "credits_charged": 0,
                "error": "Preflight validation failed — audit refused without charging credits.",
            }

        seeds = [int(s) for s in (random_seeds if random_seeds else [0, 1, 2, 3, 4])]
        required_credits = 0 if norm_mode == "local" else HOSTED_AUDIT_CREDITS

        # 3. Spend cap check (`max_credits`) BEFORE running or charging
        if int(max_credits) < required_credits:
            return {
                "tool": "audit.run",
                "preflight_id": pf["preflight_id"],
                "status": "SPEND_CAP_EXCEEDED",
                "required_credits": required_credits,
                "max_credits": int(max_credits),
                "credits_charged": 0,
                "error": (
                    f"Required credits ({required_credits}) exceed max_credits spend cap ({int(max_credits)}). "
                    "Execution refused before any charge."
                ),
            }

        # 4. Approval token verification (bound to input_sha256 + mode + credits + version + seeds)
        expected_token = _compute_approval_token(
            input_sha256=pf["input_sha256"],
            mode=norm_mode,
            credits_cost=required_credits,
            seeds=seeds,
        )
        if not approval_token or str(approval_token).strip() != expected_token:
            return {
                "tool": "audit.run",
                "preflight_id": pf["preflight_id"],
                "status": "APPROVAL_TOKEN_INVALID",
                "credits_charged": 0,
                "error": (
                    "Invalid or mismatched approval_token — inputs, mode, or seeds changed after `audit.quote`. "
                    "Call `audit.quote` to obtain a matching approval_token."
                ),
            }

        # 5. Hosted plan & metering gate (only when mode == 'hosted')
        credits_charged = 0
        if norm_mode == "hosted":
            ent = access.current_entitlement()
            if ent is not None and not ent.is_funded:
                return {
                    "tool": "audit.run",
                    "access": "UPGRADE_REQUIRED",
                    "reason": "plan_required",
                    "plan": ent.plan,
                    "upgrade_url": ent.upgrade_url or access.SITE_URL,
                    "credits_charged": 0,
                }
            tok = request_context.current_token.get() or access.resolve_token()
            if tok and not access.is_local_bootstrap():
                metering.charge(tok, "audit.run", required_credits)
            credits_charged = required_credits

        # 6. Execute diagnostic evaluation
        findings, metrics = _evaluate_findings(pf, seeds)
        job_hash = _sha256_json({
            "input_sha256": pf["input_sha256"],
            "mode": norm_mode,
            "seeds": seeds,
            "idempotency_key": idem,
        })
        job_id = f"audit_{job_hash[:16]}"
        created_at = dt.datetime.now(dt.timezone.utc).isoformat()

        reproducibility_bundle = {
            "job_id": job_id,
            "preflight_id": pf["preflight_id"],
            "strategy_name": pf["strategy_name"],
            "input_sha256": pf["input_sha256"],
            "artifact_hashes": pf["artifact_hashes"],
            "cost_assumptions": pf["cost_assumptions"],
            "mode": norm_mode,
            "package_version": server_meta.PACKAGE_VERSION,
            "random_seeds": seeds,
            "approval_token": expected_token,
            "idempotency_key": idem,
            "generated_at": created_at,
        }

        bundle = {
            "job_id": job_id,
            "preflight_id": pf["preflight_id"],
            "strategy_name": pf["strategy_name"],
            "status": "completed",
            "mode": norm_mode,
            "overall_assessment": metrics["overall_assessment"],
            "metrics": metrics,
            "findings": findings,
            "coverage_matrix": pf["coverage_matrix"],
            "reproducibility_bundle": reproducibility_bundle,
            "credits_charged": credits_charged,
        }
        _JOB_STORE[job_id] = bundle

        response = {
            "tool": "audit.run",
            "job_id": job_id,
            "preflight_id": pf["preflight_id"],
            "strategy_name": pf["strategy_name"],
            "status": "completed",
            "mode": norm_mode,
            "idempotency_key": idem,
            "idempotent_replay": False,
            "credits_charged": credits_charged,
            "overall_assessment": metrics["overall_assessment"],
            "counts_by_category": metrics["counts_by_category"],
            "headline_metrics": {
                "n_observations": metrics["n_observations"],
                "n_weekly_blocks": metrics["n_weekly_blocks"],
                "sharpe_gross_annualized": metrics["sharpe_gross_annualized"],
                "sharpe_net_annualized": metrics["sharpe_net_annualized"],
                "breakeven_cost_multiplier": metrics["breakeven_cost_multiplier"],
                "max_drawdown_pct": metrics["max_drawdown_pct"],
                "top5_pnl_share_pct": metrics["top5_pnl_share_pct"],
            },
            "input_sha256": pf["input_sha256"],
            "next_step": f"Call `audit.report(job_id='{job_id}')` to generate the full JSON, Markdown, and shareable HTML report.",
        }
        _IDEMPOTENCY_STORE[idem] = {
            "input_sha256": pf["input_sha256"],
            "mode": norm_mode,
            "response": response,
        }
        return response

    return await _run_audit_tool("audit.run", _do, require_token=(str(mode or "local").strip().lower() == "hosted"))


async def status(job_id: str) -> dict:
    """Read-only polling of an audit job by job_id (0 credits)."""
    redaction.reject_keylike_args({"job_id": job_id})

    async def _do():
        jid = (job_id or "").strip()
        bundle = _JOB_STORE.get(jid)
        if bundle is None:
            return {
                "tool": "audit.status",
                "job_id": jid,
                "status": "not_found",
                "credits_charged": 0,
                "error": f"No audit job found for job_id={jid!r} in the current session.",
            }
        return {
            "tool": "audit.status",
            "job_id": jid,
            "preflight_id": bundle["preflight_id"],
            "strategy_name": bundle["strategy_name"],
            "status": bundle["status"],
            "mode": bundle["mode"],
            "overall_assessment": bundle["overall_assessment"],
            "counts_by_category": bundle["metrics"]["counts_by_category"],
            "input_sha256": bundle["reproducibility_bundle"]["input_sha256"],
            "generated_at": bundle["reproducibility_bundle"]["generated_at"],
            "credits_charged": 0,
        }

    return await _run_audit_tool("audit.status", _do)


def _render_markdown_report(bundle: dict[str, Any]) -> str:
    m = bundle["metrics"]
    rb = bundle["reproducibility_bundle"]
    lines = [
        f"# Quant Swarm Audit Report — `{bundle['strategy_name']}`",
        "",
        f"- **Job ID:** `{bundle['job_id']}`",
        f"- **Overall Assessment:** `{bundle['overall_assessment']}`",
        f"- **Input SHA-256:** `{rb['input_sha256']}`",
        f"- **Package Version:** `quant-swarm=={rb['package_version']}`",
        f"- **Execution Mode:** `{bundle['mode']}` (`{bundle['credits_charged']} credits charged`)",
        "",
        "## Headline Metrics",
        "",
        "| Metric | Value |",
        "| --- | --- |",
        f"| Observations / Weekly Blocks | {m['n_observations']} sessions / {m['n_weekly_blocks']} weeks |",
        f"| Trade Count | {m['n_trades']} |",
        f"| Gross Annualized Sharpe | {m['sharpe_gross_annualized']:.2f} (90% CI: [{m['sharpe_bootstrap_ci_90'][0]:.2f}, {m['sharpe_bootstrap_ci_90'][1]:.2f}]) |",
        f"| Net Annualized Sharpe (1.0x House Costs) | {m['sharpe_net_annualized']:.2f} |",
        f"| Mean Gross Edge / Trade | {m['gross_edge_bps_per_trade']:.2f} bps |",
        f"| Round-Trip Pessimistic Cost (1.0x) | {m['round_trip_cost_bps_1x']:.2f} bps |",
        f"| Mean Net Edge / Trade (1.0x / 2.0x) | {m['net_edge_bps_per_trade_1x']:.2f} bps / {m['net_edge_bps_per_trade_2x']:.2f} bps |",
        f"| Breakeven Cost Multiplier | {m['breakeven_cost_multiplier']:.2f}x |",
        f"| Max Drawdown | {m['max_drawdown_pct']:.2f}% |",
        f"| Top-5 Trade PnL Concentration | {m['top5_pnl_share_pct']:.1f}% |",
        "",
        "## Structured Diagnostic Findings",
        "",
    ]
    for idx, f in enumerate(bundle["findings"], start=1):
        lines.extend([
            f"### {idx}. {f['finding']} — `{f['category']}`",
            f"- **Evidence:** {f['evidence']}",
            f"- **Why It Matters:** {f['why_it_matters']}",
            f"- **Next Experiment:** {f['next_experiment']}",
            f"- **Limitation:** {f['limitation']}",
            "",
        ])
    lines.extend([
        "## Reproducibility Bundle",
        "```json",
        json.dumps(rb, indent=2),
        "```",
        "",
        "*Generated by Quant Swarm Audit (`quant-swarm`). Diagnostic evidence only — not investment advice.*",
    ])
    return "\n".join(lines) + "\n"


def _render_html_report(bundle: dict[str, Any]) -> str:
    m = bundle["metrics"]
    rb = bundle["reproducibility_bundle"]
    esc = html.escape

    badge_colors = {
        "OBSERVED_PROBLEM": ("#fde8e8", "#9b1c1c", "#f8b4b4"),
        "SENSITIVITY_WARNING": ("#fef3c7", "#92400e", "#fcd34d"),
        "INSUFFICIENT_EVIDENCE": ("#e0f2fe", "#075985", "#7dd3fc"),
        "PASSED": ("#dcfce7", "#166534", "#86efac"),
        "NOT_TESTED": ("#f3f4f6", "#374151", "#d1d5db"),
    }

    finding_cards = []
    for idx, f in enumerate(bundle["findings"], start=1):
        bg, fg, border = badge_colors.get(f["category"], ("#f3f4f6", "#374151", "#d1d5db"))
        finding_cards.append(f"""
      <article class="finding-card" style="border-left: 4px solid {border};">
        <header class="finding-header">
          <h3>{idx}. {esc(str(f['finding']))}</h3>
          <span class="badge" style="background:{bg};color:{fg};border:1px solid {border};">{esc(str(f['category']))}</span>
        </header>
        <dl class="finding-grid">
          <div><dt>Evidence</dt><dd>{esc(str(f['evidence']))}</dd></div>
          <div><dt>Why it matters</dt><dd>{esc(str(f['why_it_matters']))}</dd></div>
          <div><dt>Next experiment</dt><dd>{esc(str(f['next_experiment']))}</dd></div>
          <div><dt>Limitation</dt><dd>{esc(str(f['limitation']))}</dd></div>
        </dl>
      </article>""")

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Quant Swarm Audit Report — {esc(str(bundle['strategy_name']))}</title>
  <style>
    :root {{
      --bg: #0b0f17;
      --panel: #111827;
      --border: #1f2937;
      --text: #f3f4f6;
      --muted: #9ca3af;
      --accent: #38bdf8;
      --mono: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      padding: 2rem 1rem;
      background: var(--bg);
      color: var(--text);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      line-height: 1.55;
    }}
    .container {{ max-width: 980px; margin: 0 auto; }}
    header.hero {{
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 1.5rem;
      margin-bottom: 1.5rem;
    }}
    .kicker {{ font-family: var(--mono); font-size: 0.8rem; color: var(--accent); text-transform: uppercase; letter-spacing: 0.06em; }}
    h1 {{ margin: 0.35rem 0 0.5rem; font-size: 1.65rem; }}
    .meta-row {{ display: flex; flex-wrap: wrap; gap: 1rem; font-family: var(--mono); font-size: 0.82rem; color: var(--muted); }}
    .metrics-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
      gap: 0.85rem;
      margin-bottom: 1.5rem;
    }}
    .metric-box {{
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 1rem;
    }}
    .metric-box span {{ display: block; font-size: 0.78rem; color: var(--muted); }}
    .metric-box strong {{ font-family: var(--mono); font-size: 1.25rem; margin-top: 0.25rem; display: block; }}
    .finding-card {{
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 1.25rem;
      margin-bottom: 1rem;
    }}
    .finding-header {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 1rem;
      margin-bottom: 0.75rem;
    }}
    .finding-header h3 {{ margin: 0; font-size: 1.05rem; }}
    .badge {{
      font-family: var(--mono);
      font-size: 0.75rem;
      font-weight: 600;
      padding: 0.2rem 0.55rem;
      border-radius: 4px;
      white-space: nowrap;
    }}
    .finding-grid {{ margin: 0; display: grid; gap: 0.5rem; }}
    .finding-grid div {{ display: grid; grid-template-columns: 140px 1fr; gap: 0.75rem; font-size: 0.92rem; }}
    .finding-grid dt {{ color: var(--muted); font-weight: 600; }}
    .finding-grid dd {{ margin: 0; }}
    pre {{
      background: #06090f;
      border: 1px solid var(--border);
      border-radius: 6px;
      padding: 1rem;
      overflow-x: auto;
      font-family: var(--mono);
      font-size: 0.8rem;
      color: #d1d5db;
    }}
    footer {{ margin-top: 2rem; font-size: 0.82rem; color: var(--muted); text-align: center; }}
  </style>
</head>
<body>
  <div class="container">
    <header class="hero">
      <div class="kicker">Quant Swarm Audit &middot; Reproducible Evidence Report</div>
      <h1>{esc(str(bundle['strategy_name']))}</h1>
      <div class="meta-row">
        <span>Job: {esc(str(bundle['job_id']))}</span>
        <span>Assessment: {esc(str(bundle['overall_assessment']))}</span>
        <span>Input SHA-256: {esc(str(rb['input_sha256'][:24]))}&hellip;</span>
        <span>Package: quant-swarm=={esc(str(rb['package_version']))}</span>
      </div>
    </header>

    <section class="metrics-grid" aria-label="Headline metrics">
      <div class="metric-box"><span>Observations / Weeks</span><strong>{m['n_observations']} / {m['n_weekly_blocks']}w</strong></div>
      <div class="metric-box"><span>Gross / Net Sharpe</span><strong>{m['sharpe_gross_annualized']:.2f} / {m['sharpe_net_annualized']:.2f}</strong></div>
      <div class="metric-box"><span>Net Edge (1.0x / 2.0x Cost)</span><strong>{m['net_edge_bps_per_trade_1x']:.1f} / {m['net_edge_bps_per_trade_2x']:.1f} bps</strong></div>
      <div class="metric-box"><span>Breakeven Cost Multiplier</span><strong>{m['breakeven_cost_multiplier']:.2f}x</strong></div>
      <div class="metric-box"><span>Max Drawdown</span><strong>{m['max_drawdown_pct']:.2f}%</strong></div>
      <div class="metric-box"><span>Top-5 Trade Share</span><strong>{m['top5_pnl_share_pct']:.1f}%</strong></div>
    </section>

    <section aria-label="Diagnostic findings">
      <h2>Structured Diagnostic Findings</h2>
      {"".join(finding_cards)}
    </section>

    <section aria-label="Reproducibility bundle">
      <h2>Reproducibility Bundle</h2>
      <pre><code>{esc(json.dumps(rb, indent=2))}</code></pre>
    </section>

    <footer>
      Generated by <code>quant-swarm=={esc(str(rb['package_version']))}</code> &middot; The 1.21 Initiative &middot; Diagnostic analysis only, not investment advice.
    </footer>
  </div>
</body>
</html>
"""


async def report(
    job_id: str,
    output_html_path: str | None = None,
    output_json_path: str | None = None,
    output_markdown_path: str | None = None,
) -> dict:
    """Generate the machine-readable JSON artifact, reproducibility bundle, and shareable HTML/Markdown report."""
    redaction.reject_keylike_args({
        "job_id": job_id,
        "output_html_path": output_html_path,
        "output_json_path": output_json_path,
        "output_markdown_path": output_markdown_path,
    })

    async def _do():
        jid = (job_id or "").strip()
        bundle = _JOB_STORE.get(jid)
        if bundle is None:
            return {
                "tool": "audit.report",
                "job_id": jid,
                "status": "not_found",
                "credits_charged": 0,
                "error": f"No completed audit job found for job_id={jid!r}. Run `audit.run` first.",
            }

        md_report = _render_markdown_report(bundle)
        html_report = _render_html_report(bundle)
        written_files: dict[str, str] = {}

        if output_html_path:
            hp = Path(output_html_path)
            hp.parent.mkdir(parents=True, exist_ok=True)
            hp.write_text(html_report, encoding="utf-8")
            written_files["html"] = str(hp)
        if output_json_path:
            jp = Path(output_json_path)
            jp.parent.mkdir(parents=True, exist_ok=True)
            jp.write_text(json.dumps(bundle, indent=2) + "\n", encoding="utf-8")
            written_files["json"] = str(jp)
        if output_markdown_path:
            mp = Path(output_markdown_path)
            mp.parent.mkdir(parents=True, exist_ok=True)
            mp.write_text(md_report, encoding="utf-8")
            written_files["markdown"] = str(mp)

        rb = bundle["reproducibility_bundle"]
        sha_prefix = str(rb.get("input_sha256", jid))[:12]
        verification_badge = {
            "share_url": f"https://1.21initiative.com/mcp/verify/{sha_prefix}",
            "badge_svg_url": f"https://1.21initiative.com/api/mcp/badge/{sha_prefix}.svg",
            "badge_markdown": (
                f"[![Quant Swarm Audit](https://1.21initiative.com/api/mcp/badge/{sha_prefix}.svg)]"
                f"(https://1.21initiative.com/mcp/verify/{sha_prefix})"
            ),
        }

        return {
            "tool": "audit.report",
            "job_id": jid,
            "strategy_name": bundle["strategy_name"],
            "overall_assessment": bundle["overall_assessment"],
            "metrics": bundle["metrics"],
            "findings": bundle["findings"],
            "coverage_matrix": bundle["coverage_matrix"],
            "reproducibility_bundle": bundle["reproducibility_bundle"],
            "verification_badge": verification_badge,
            "markdown_report": md_report,
            "html_report": html_report,
            "written_files": written_files,
            "credits_charged": 0,
            "not_investment_advice": True,
        }

    return await _run_audit_tool("audit.report", _do)


async def compare(
    job_id_a: str,
    job_id_b: str,
    output_html_path: str | None = None,
) -> dict:
    """Compare two completed audit bundles (before/after remediation or version A vs. version B)."""
    redaction.reject_keylike_args({
        "job_id_a": job_id_a,
        "job_id_b": job_id_b,
        "output_html_path": output_html_path,
    })

    async def _do():
        a = _JOB_STORE.get((job_id_a or "").strip())
        b = _JOB_STORE.get((job_id_b or "").strip())
        if a is None or b is None:
            missing = [jid for jid, rec in ((job_id_a, a), (job_id_b, b)) if rec is None]
            return {
                "tool": "audit.compare",
                "status": "not_found",
                "missing_job_ids": missing,
                "credits_charged": 0,
                "error": f"Audit job(s) not found: {missing}. Run `audit.run` for both candidates first.",
            }

        ma, mb = a["metrics"], b["metrics"]
        metric_keys = (
            "sharpe_gross_annualized",
            "sharpe_net_annualized",
            "gross_edge_bps_per_trade",
            "net_edge_bps_per_trade_1x",
            "net_edge_bps_per_trade_2x",
            "breakeven_cost_multiplier",
            "max_drawdown_pct",
            "top5_pnl_share_pct",
        )
        metrics_diff = {
            k: {
                "before": ma.get(k),
                "after": mb.get(k),
                "delta": round(float(mb.get(k, 0.0)) - float(ma.get(k, 0.0)), 3),
            }
            for k in metric_keys
        }

        fa_by_title = {f["finding"]: f for f in a["findings"]}
        fb_by_title = {f["finding"]: f for f in b["findings"]}
        bad_cats = {"OBSERVED_PROBLEM", "SENSITIVITY_WARNING"}

        resolved_findings: list[dict[str, str]] = []
        new_warnings: list[dict[str, str]] = []
        transitions: list[dict[str, str]] = []

        for title, fb in fb_by_title.items():
            fa = fa_by_title.get(title)
            cat_a = fa["category"] if fa else "NOT_TESTED"
            cat_b = fb["category"]
            transitions.append({
                "finding": title,
                "before_category": cat_a,
                "after_category": cat_b,
                "after_evidence": fb["evidence"],
            })
            if cat_a in bad_cats and cat_b == "PASSED":
                resolved_findings.append({
                    "finding": title,
                    "from": cat_a,
                    "to": cat_b,
                    "evidence_after": fb["evidence"],
                })
            elif cat_a not in bad_cats and cat_b in bad_cats:
                new_warnings.append({
                    "finding": title,
                    "from": cat_a,
                    "to": cat_b,
                    "evidence_after": fb["evidence"],
                })

        if len(resolved_findings) > 0 and len(new_warnings) == 0 and mb["counts_by_category"]["OBSERVED_PROBLEM"] == 0:
            remediation_verdict = "REMEDIATION_VERIFIED"
        elif mb["counts_by_category"]["OBSERVED_PROBLEM"] < ma["counts_by_category"]["OBSERVED_PROBLEM"]:
            remediation_verdict = "PARTIALLY_IMPROVED"
        elif len(new_warnings) > 0:
            remediation_verdict = "REGRESSION_DETECTED"
        else:
            remediation_verdict = "UNCHANGED"

        if output_html_path:
            # Render combined before/after HTML comparison report
            hp = Path(output_html_path)
            hp.parent.mkdir(parents=True, exist_ok=True)
            hp.write_text(_render_comparison_html(a, b, metrics_diff, transitions, resolved_findings, remediation_verdict), encoding="utf-8")

        return {
            "tool": "audit.compare",
            "job_id_a": a["job_id"],
            "job_id_b": b["job_id"],
            "strategy_a": a["strategy_name"],
            "strategy_b": b["strategy_name"],
            "assessment_before": a["overall_assessment"],
            "assessment_after": b["overall_assessment"],
            "remediation_verdict": remediation_verdict,
            "metrics_diff": metrics_diff,
            "resolved_findings": resolved_findings,
            "new_warnings": new_warnings,
            "finding_transitions": transitions,
            "input_sha256_a": a["reproducibility_bundle"]["input_sha256"],
            "input_sha256_b": b["reproducibility_bundle"]["input_sha256"],
            "credits_charged": 0,
            "not_investment_advice": True,
        }

    return await _run_audit_tool("audit.compare", _do)


def _render_comparison_html(
    a: dict[str, Any],
    b: dict[str, Any],
    metrics_diff: dict[str, dict[str, Any]],
    transitions: list[dict[str, str]],
    resolved: list[dict[str, str]],
    verdict: str,
) -> str:
    esc = html.escape
    rows_html = []
    labels = {
        "sharpe_gross_annualized": "Gross Annualized Sharpe",
        "sharpe_net_annualized": "Net Annualized Sharpe (1.0x House Costs)",
        "gross_edge_bps_per_trade": "Mean Gross Edge / Trade (bps)",
        "net_edge_bps_per_trade_1x": "Mean Net Edge / Trade @ 1.0x Cost (bps)",
        "net_edge_bps_per_trade_2x": "Mean Net Edge / Trade @ 2.0x Cost (bps)",
        "breakeven_cost_multiplier": "Breakeven Cost Multiplier (x)",
        "max_drawdown_pct": "Max Drawdown (%)",
        "top5_pnl_share_pct": "Top-5 Trade PnL Share (%)",
    }
    for k, d in metrics_diff.items():
        sign = "+" if d["delta"] > 0 else ""
        rows_html.append(
            f"<tr><th scope='row'>{esc(labels.get(k, k))}</th>"
            f"<td>{d['before']}</td><td>{d['after']}</td><td><strong>{sign}{d['delta']}</strong></td></tr>"
        )

    trans_html = []
    for t in transitions:
        trans_html.append(
            f"<tr><th scope='row'>{esc(t['finding'])}</th>"
            f"<td><code>{esc(t['before_category'])}</code></td>"
            f"<td><code>{esc(t['after_category'])}</code></td>"
            f"<td>{esc(t['after_evidence'])}</td></tr>"
        )

    # Also embed the full structured findings of both runs
    b_html = _render_html_report(b)
    # Inject comparison summary section right after hero header
    comp_section = f"""
    <section class="finding-card" style="border-left: 4px solid #38bdf8; margin-bottom: 1.5rem;">
      <header class="finding-header">
        <h2>Remediation Comparison: {esc(str(a['strategy_name']))} &rarr; {esc(str(b['strategy_name']))}</h2>
        <span class="badge" style="background:#dcfce7;color:#166534;border:1px solid #86efac;">{esc(verdict)}</span>
      </header>
      <p style="color:#9ca3af;margin-top:0;">
        Before Job: <code>{esc(str(a['job_id']))}</code> (<code>{esc(str(a['overall_assessment']))}</code>, SHA-256 <code>{esc(str(a['reproducibility_bundle']['input_sha256'][:16]))}&hellip;</code>)
        &rarr; After Job: <code>{esc(str(b['job_id']))}</code> (<code>{esc(str(b['overall_assessment']))}</code>, SHA-256 <code>{esc(str(b['reproducibility_bundle']['input_sha256'][:16]))}&hellip;</code>)
        &middot; Resolved findings: <strong>{len(resolved)}</strong>
      </p>
      <table style="width:100%;border-collapse:collapse;font-size:0.9rem;margin-bottom:1rem;">
        <thead>
          <tr style="border-bottom:1px solid #1f2937;text-align:left;color:#9ca3af;">
            <th style="padding:0.45rem;">Metric</th>
            <th style="padding:0.45rem;">Before ({esc(str(a['strategy_name']))})</th>
            <th style="padding:0.45rem;">After ({esc(str(b['strategy_name']))})</th>
            <th style="padding:0.45rem;">Delta</th>
          </tr>
        </thead>
        <tbody>
          {"".join(rows_html)}
        </tbody>
      </table>
      <table style="width:100%;border-collapse:collapse;font-size:0.86rem;">
        <thead>
          <tr style="border-bottom:1px solid #1f2937;text-align:left;color:#9ca3af;">
            <th style="padding:0.45rem;">Diagnostic Check</th>
            <th style="padding:0.45rem;">Before</th>
            <th style="padding:0.45rem;">After</th>
            <th style="padding:0.45rem;">Post-Remediation Evidence</th>
          </tr>
        </thead>
        <tbody>
          {"".join(trans_html)}
        </tbody>
      </table>
    </section>
    """
    return b_html.replace("</header>", "</header>\n" + comp_section, 1)
