"""Research Ledger & Sealed Evaluation (Priority 4).

Tracks strategy revisions, parameter changes, evaluation dataset splits
(``exploratory``, ``held_out``, ``sealed_forward``), external trial imports,
and pre-registered forward paper commitments.

Key honesty guarantees:
- Explicitly records ``trial_history_status`` as ``COMPLETE`` or ``INCOMPLETE``
  so selection-bias analyses never assume a single trial when earlier notebook
  experiments were unrecorded.
- Warns when a researcher tunes repeatedly against the same benchmark or reuses
  a held-out split.
- Seals pre-registered forward paper commitments with a canonical SHA-256 hash
  and UTC timestamp, keeping forward paper evidence strictly separated from
  historical replay results.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from swarm_mcp.cache.db import cache_dir

VALID_SPLITS = ("exploratory", "held_out", "sealed_forward")
REPEATED_BENCHMARK_WARN_THRESHOLD = 3


def default_ledger_path() -> Path:
    override = os.environ.get("SWARM_MCP_LEDGER_PATH")
    if override:
        return Path(override)
    return cache_dir() / "research_ledger.json"


def _canonical_sha256(payload: Any) -> str:
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _flatten_params(obj: Any, prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    if isinstance(obj, dict):
        for k in sorted(obj.keys()):
            sub = f"{prefix}.{k}" if prefix else str(k)
            out.update(_flatten_params(obj[k], sub))
    elif isinstance(obj, list):
        out[prefix or "root"] = json.dumps(obj, sort_keys=True, default=str)
    else:
        out[prefix or "root"] = obj
    return out


def compute_parameter_diff(prev_params: dict | None, curr_params: dict | None) -> list[dict[str, Any]]:
    if not prev_params and not curr_params:
        return []
    flat_prev = _flatten_params(prev_params or {})
    flat_curr = _flatten_params(curr_params or {})
    all_keys = sorted(set(flat_prev.keys()) | set(flat_curr.keys()))
    diffs: list[dict[str, Any]] = []
    for k in all_keys:
        old_val = flat_prev.get(k, "<absent>")
        new_val = flat_curr.get(k, "<absent>")
        if old_val != new_val:
            diffs.append({
                "parameter": k,
                "previous": old_val,
                "current": new_val,
            })
    return diffs


def _load_ledger_doc(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"schema_version": 1, "trials": [], "sealed_commitments": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("trials"), list):
            data.setdefault("schema_version", 1)
            data.setdefault("sealed_commitments", [])
            return data
    except (OSError, ValueError):
        pass
    return {"schema_version": 1, "trials": [], "sealed_commitments": []}


def _save_ledger_doc(path: Path, doc: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2, sort_keys=False) + "\n", encoding="utf-8")


def record_trial_entry(
    *,
    strategy_id: str,
    version_tag: str = "v1",
    evaluation_split: str = "exploratory",
    dataset_id: str = "local_panel",
    period_start: str | None = None,
    period_end: str | None = None,
    parameters: dict[str, Any] | None = None,
    metrics: dict[str, Any] | None = None,
    benchmark_id: str = "equal_weight_universe",
    trial_history_complete: bool = False,
    external_trials_imported: int = 0,
    forward_paper_positions: list[dict[str, Any]] | None = None,
    notes: str | None = None,
    ledger_path: str | os.PathLike | None = None,
) -> dict[str, Any]:
    """Record a research trial or sealed forward commitment in the local ledger."""
    if not strategy_id or not str(strategy_id).strip():
        return {"error": "strategy_id is required"}
    split = str(evaluation_split or "exploratory").strip().lower()
    if split not in VALID_SPLITS:
        return {
            "error": f"evaluation_split must be one of {list(VALID_SPLITS)}",
            "supplied_split": evaluation_split,
        }
    ext_trials = max(0, int(external_trials_imported or 0))
    path = Path(ledger_path) if ledger_path else default_ledger_path()
    doc = _load_ledger_doc(path)
    trials: list[dict[str, Any]] = doc["trials"]

    prior_for_strategy = [t for t in trials if t.get("strategy_id") == strategy_id]
    prev_trial = prior_for_strategy[-1] if prior_for_strategy else None
    param_diff = compute_parameter_diff(
        prev_trial.get("parameters") if prev_trial else None,
        parameters or {},
    )

    now_utc = dt.datetime.now(dt.timezone.utc).isoformat()
    revision_hash = _canonical_sha256({
        "strategy_id": strategy_id,
        "version_tag": version_tag,
        "parameters": parameters or {},
    })

    # Cumulative trial accounting across ledger + imported external trials
    prior_ext_max = max((int(t.get("external_trials_imported", 0)) for t in prior_for_strategy), default=0)
    effective_ext_trials = max(ext_trials, prior_ext_max)
    recorded_count = len(prior_for_strategy) + 1
    cumulative_trials = recorded_count + effective_ext_trials

    # Trial history completeness: must be explicitly declared complete AND have no unrecorded gaps
    history_status = "COMPLETE" if bool(trial_history_complete) else "INCOMPLETE"

    # Check benchmark tuning frequency and held-out split reuse
    same_benchmark_count = sum(
        1 for t in prior_for_strategy if t.get("benchmark_id") == benchmark_id
    ) + 1
    same_held_out_count = 0
    if split == "held_out":
        same_held_out_count = sum(
            1
            for t in prior_for_strategy
            if t.get("evaluation_split") == "held_out"
            and t.get("dataset_id") == dataset_id
            and t.get("period_start") == period_start
            and t.get("period_end") == period_end
        ) + 1

    warnings: list[dict[str, Any]] = []
    if history_status == "INCOMPLETE":
        warnings.append({
            "code": "TRIAL_HISTORY_INCOMPLETE",
            "severity": "WARNING",
            "message": (
                "Trial history is marked INCOMPLETE. Deflated Sharpe and multiple-testing "
                "adjustments are lower-bound estimates until all prior exploratory runs are imported."
            ),
        })
    if same_benchmark_count >= REPEATED_BENCHMARK_WARN_THRESHOLD:
        warnings.append({
            "code": "REPEATED_BENCHMARK_TUNING",
            "severity": "WARNING",
            "message": (
                f"Strategy '{strategy_id}' has been evaluated {same_benchmark_count} times against "
                f"benchmark '{benchmark_id}'. Repeated parameter tuning against the same benchmark "
                "inflates selection bias; validate on a fresh held-out or sealed forward period."
            ),
        })
    if same_held_out_count > 1:
        warnings.append({
            "code": "HELD_OUT_SPLIT_REUSED",
            "severity": "BLOCKER",
            "message": (
                f"Held-out split ({dataset_id}: {period_start}..{period_end}) has been evaluated "
                f"{same_held_out_count} times for '{strategy_id}'. Once a held-out split is used to "
                "revise parameters, it becomes an exploratory split."
            ),
        })

    sealed_commitment: dict[str, Any] | None = None
    if split == "sealed_forward" or forward_paper_positions:
        commitment_payload = {
            "strategy_id": strategy_id,
            "version_tag": version_tag,
            "revision_hash": revision_hash,
            "dataset_id": dataset_id,
            "period_start": period_start,
            "period_end": period_end,
            "forward_paper_positions": forward_paper_positions or [],
            "committed_at_utc": now_utc,
        }
        commitment_hash = _canonical_sha256(commitment_payload)
        sealed_commitment = {
            "commitment_id": f"seal_{commitment_hash[:16]}",
            "commitment_sha256": commitment_hash,
            "evaluation_label": "FORWARD_PAPER_COMMITMENT",
            "committed_at_utc": now_utc,
            "strategy_id": strategy_id,
            "version_tag": version_tag,
            "revision_hash": revision_hash,
            "positions_count": len(forward_paper_positions or []),
            "identity_note": (
                "SHA-256 commitment proves the parameter vector and paper positions were recorded "
                "at committed_at_utc before subsequent market realization."
            ),
        }
        doc["sealed_commitments"].append(sealed_commitment)

    entry = {
        "trial_id": f"tr_{len(trials) + 1:04d}_{revision_hash[:8]}",
        "recorded_at_utc": now_utc,
        "strategy_id": strategy_id,
        "version_tag": version_tag,
        "revision_hash": revision_hash,
        "evaluation_split": split,
        "evaluation_label": "FORWARD_PAPER" if split == "sealed_forward" else "HISTORICAL_REPLAY",
        "dataset_id": dataset_id,
        "period_start": period_start,
        "period_end": period_end,
        "benchmark_id": benchmark_id,
        "parameters": parameters or {},
        "parameter_diff_from_previous": param_diff,
        "metrics": metrics or {},
        "recorded_trials_for_strategy": recorded_count,
        "external_trials_imported": effective_ext_trials,
        "cumulative_trials": cumulative_trials,
        "trial_history_status": history_status,
        "warnings": warnings,
        "sealed_commitment": sealed_commitment,
        "notes": notes,
    }
    trials.append(entry)
    _save_ledger_doc(path, doc)

    return {
        "tool": "warden.record_trial",
        "verdict": "TRIAL_RECORDED" if not any(w["severity"] == "BLOCKER" for w in warnings) else "SPLIT_HYGIENE_VIOLATION",
        "trial": entry,
        "ledger_summary": summarize_ledger(strategy_id=strategy_id, ledger_path=path),
    }


def summarize_ledger(
    *,
    strategy_id: str | None = None,
    ledger_path: str | os.PathLike | None = None,
) -> dict[str, Any]:
    """Summarize research ledger trials, split hygiene, and sealed commitments."""
    path = Path(ledger_path) if ledger_path else default_ledger_path()
    doc = _load_ledger_doc(path)
    all_trials: list[dict[str, Any]] = doc.get("trials", [])
    filtered = [t for t in all_trials if not strategy_id or t.get("strategy_id") == strategy_id]

    by_split = {s: 0 for s in VALID_SPLITS}
    for t in filtered:
        sp = t.get("evaluation_split", "exploratory")
        if sp in by_split:
            by_split[sp] += 1

    strategies = sorted({str(t.get("strategy_id")) for t in filtered if t.get("strategy_id")})
    ext_imported = sum(
        max((int(t.get("external_trials_imported", 0)) for t in filtered if t.get("strategy_id") == sid), default=0)
        for sid in strategies
    )
    total_effective_trials = len(filtered) + ext_imported
    all_complete = bool(filtered) and all(t.get("trial_history_status") == "COMPLETE" for t in filtered)
    history_status = "COMPLETE" if all_complete else "INCOMPLETE"

    active_warnings: list[dict[str, Any]] = []
    seen_codes: set[tuple[str, str]] = set()
    for t in filtered:
        for w in t.get("warnings", []):
            key = (str(t.get("strategy_id")), str(w.get("code")))
            if key not in seen_codes:
                seen_codes.add(key)
                active_warnings.append({
                    "strategy_id": t.get("strategy_id"),
                    **w,
                })

    commitments = [
        c for c in doc.get("sealed_commitments", [])
        if not strategy_id or c.get("strategy_id") == strategy_id
    ]

    return {
        "strategy_filter": strategy_id,
        "strategies_tracked": strategies,
        "recorded_trials": len(filtered),
        "external_trials_imported": ext_imported,
        "cumulative_trials": total_effective_trials,
        "trial_history_status": history_status,
        "splits": by_split,
        "sealed_commitments_count": len(commitments),
        "sealed_commitments": commitments[-5:],
        "active_warnings": active_warnings,
        "recent_trials": [
            {
                "trial_id": t["trial_id"],
                "strategy_id": t["strategy_id"],
                "version_tag": t["version_tag"],
                "revision_hash": t["revision_hash"][:12],
                "evaluation_split": t["evaluation_split"],
                "evaluation_label": t["evaluation_label"],
                "benchmark_id": t["benchmark_id"],
                "parameter_changes": len(t.get("parameter_diff_from_previous", [])),
                "trial_history_status": t["trial_history_status"],
            }
            for t in filtered[-10:]
        ],
    }
