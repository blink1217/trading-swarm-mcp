"""PIT-correlation significance (pit_analysis/v2) reader + explainer.

Artifacts (lake-relative, produced by the trade_bot_sharp ``pit-correlations`` verb /
``zoo pipeline`` L1 layer on Spearman cross-sectional rank IC, v2 methodology):

- ``pit_analysis/v2/LATEST``                       pointer naming the run to reload
- ``pit_analysis/v2/run=<id>/manifest.json``       per-family (``dataset|metric``) latest
  per-horizon stats: ``rolling_ir``, Newey-West two-sided ``p`` (overlap-corrected; lag
  ceil(horizon/5)), ``t_hac``, ``n_obs``, ``research_only``; family licence / staleness /
  diagnostic flags; and the methodology block (ic, neutralization, inference, floors)
- ``pit_analysis/v2/run=<id>/underlyings/<SYM>.json.gz``  per-date release points whose horizon
  keys carry the p-value string at that release (null when not judged)
- ``search_runs/zoo/<run>/l1_survivors.json``      the funnel gate (rolling 52-week IR > 0 AND
  BH q <= 0.05, with harmonic-mean p, the Newey-West t, pit_run_id and the licence class)

Verdict rule (keeps the MCP consistent with the funnel): a ``(dataset|metric|horizon)`` family is
SIGNIFICANT only when it is an L1 survivor of the SAME pit run (survivor rows carry ``pit_run_id``;
a recorded race mismatch or a missing gate artifact yields UNVERIFIED — a raw p-value is never
upgraded on its own). Restricted (licensed) families are withheld from every external surface.
Stale or diagnostic families are never judged. Raw metric values never leave this store.

Two data sources share the pure judging code:
- ``LakeSource`` reads the local/mirrored lake from ``SWARM_MCP_PIT_LAKE_ROOT`` (operator/offline).
- ``RelaySource`` consumes a site-relay ``POST /api/mcp/data/pit`` body (inputs only, no values).

Pure stdlib.
"""
from __future__ import annotations

import csv
import datetime as dt
import gzip
import hashlib
import json
import math
import os
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path

LAKE_ROOT_ENV = "SWARM_MCP_PIT_LAKE_ROOT"
PREFIX = "pit_analysis/v2"
SCHEMA = "pit_analysis/v2"
LEGACY_V1 = "pit_analysis/v1"

ROLLING_WINDOW = 52          # weeks (rolling IC window)
MIN_N_OBS = 26               # observation floor: below this p is null (not judged)
MIN_CROSS_SECTION = 8        # names per date for a rank IC
BH_Q_MAX = 0.05
COVERAGE_FLOOR = 0.5
HAC_LAG_RULE = "ceil(horizon_sessions / 5)"
WHY_V1 = ("v1 stores predate the Newey-West overlap correction and the size/sector "
          "neutralization — re-run `pit-correlations` / `zoo pipeline` (v2) first")

SIGNIFICANT = "SIGNIFICANT"
NOT_SIGNIFICANT = "NOT_SIGNIFICANT"
INSUFFICIENT = "INSUFFICIENT_OBSERVATIONS"
NOT_JUDGED = "NOT_JUDGED"
UNVERIFIED = "UNVERIFIED"
STATUS_ORDER = [SIGNIFICANT, UNVERIFIED, NOT_SIGNIFICANT, NOT_JUDGED, INSUFFICIENT]

HOW_TO_READ = (
    "Each row is a (dataset|metric|horizon) family. rolling_ir is the mean/std of the trailing "
    f"{ROLLING_WINDOW}-week Spearman cross-sectional rank IC, computed on a size/sector-neutralized "
    "metric against neutralized forward returns; p_value_two_sided is the Newey-West "
    f"({HAC_LAG_RULE} lags) two-sided p of the window mean, because horizons longer than the "
    "decision spacing overlap and the naive t = IR*sqrt(n_obs) would overstate significance. A "
    "family is SIGNIFICANT only if it passes the funnel's L1 gate (rolling IR > 0 AND "
    f"Benjamini-Hochberg q <= {BH_Q_MAX} across every family tested) - a raw p <= 0.05 alone is NOT "
    f"enough because thousands of families are tested. p is null (not judged) when n_obs < "
    f"{MIN_N_OBS}, when the family's latest release is stale, or when the family is a diagnostic-only "
    "raw level. research_only means the dataset covers under "
    f"{int(COVERAGE_FLOOR * 100)}% of the universe: treat it as research evidence, not a tradable "
    "signal. IR/p describe the family's cross-sectional predictive power across the whole "
    "universe, not a forecast for this one underlying.")

_lock = threading.Lock()
_run_cache: dict[tuple, object] = {}
_gate_cache: dict[tuple, dict] = {}


class PitStoreError(ValueError):
    """The PIT analysis store is absent/corrupt; the message says how to fix it."""


def lake_root() -> Path | None:
    raw = os.environ.get(LAKE_ROOT_ENV, "").strip()
    if not raw:
        return None
    root = Path(raw)
    if not root.is_dir():
        raise PitStoreError(f"{LAKE_ROOT_ENV}={raw!r} is not a directory")
    return root.resolve()


def reset_cache() -> None:
    with _lock:
        _run_cache.clear()
        _gate_cache.clear()


def _num(v) -> float | None:
    """Parse a JSON number that may be the strings "NaN"/"Infinity"; None when not finite."""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _fmt(x: float | None, nd: int = 3) -> str:
    return "n/a" if x is None else f"{x:.{nd}g}"


def _parse_utc(text) -> dt.datetime | None:
    """Parse an ISO-8601 UTC stamp (tolerates .NET's 7 fractional digits and a trailing Z)."""
    if not text:
        return None
    t = re.sub(r"(\.\d{6})\d+", r"\1", str(text).strip()).replace("Z", "+00:00")
    try:
        d = dt.datetime.fromisoformat(t)
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)


# --------------------------------------------------------------------------- bundle

@dataclass
class RunBundle:
    """Everything the pure explainer needs for the requested symbols, already stripped of
    restricted families and raw values on the relay path."""
    run_id: str
    generated_at_utc: str | None
    source: str | None
    timeframe: str | None
    horizons: dict[str, int]
    families: dict[str, dict]                       # metric_id -> manifest entry
    gate: dict
    shards: dict | None = None                      # lake path: symbol -> shard-ref
    methodology: dict | None = None
    known_limitations: list[str] = field(default_factory=list)
    datasets_rule: dict[str, str] = field(default_factory=dict)  # dataset -> staleness rule
    data_source: str = "lake"                       # lake | relay
    lite: bool = False                              # relay-side free-plan truncation
    lite_counts: dict[str, int] | None = None       # counts the relay computed over ALL families
    run_datasets: dict[str, dict] | None = None     # dataset -> {families, survivors, ...}
    restricted: dict | None = None                  # {families_withheld, reason} (relay path only)
    _lake_root: Path | None = None
    _run_dir: Path | None = None
    _shard_docs: dict = field(default_factory=dict) # symbol -> parsed shard doc (lake path)
    relay_shards: dict = field(default_factory=dict) # symbol -> {metric_id -> {date -> {h: p}}} (relay)


def _empty_gate() -> dict:
    return {"available": False, "source": None, "source_run": None, "survivors": {},
            "families_tested": None, "predates_pit_run": False, "pit_run_id": None,
            "placebo_survivors": None, "unreliable": False}


# --------------------------------------------------------------------------- lake path

def load_lake_run(root: Path) -> RunBundle:
    """Load the run named by LATEST (cached while unchanged; no new release => reuse)."""
    pointer_path = root / PREFIX / "LATEST"
    if not pointer_path.is_file():
        legacy = root / LEGACY_V1 / "LATEST"
        if legacy.is_file():
            raise PitStoreError(f"{LEGACY_V1}/LATEST exists but {WHY_V1}")
        raise PitStoreError(f"no {PREFIX}/LATEST pointer under {root} - run `pit-correlations` first")
    try:
        pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise PitStoreError(f"{PREFIX}/LATEST is unreadable: {e}") from e
    if pointer.get("schema_version") != SCHEMA or not pointer.get("run_id"):
        raise PitStoreError(
            f"{PREFIX}/LATEST is not a valid {SCHEMA} pointer ({WHY_V1})")
    manifest_path = root / str(pointer["manifest_path"]).replace("\\", "/")
    if not manifest_path.is_file():
        raise PitStoreError(f"LATEST names run {pointer['run_id']} but its manifest.json is missing")
    key = ("lake", str(root), str(pointer["run_id"]), manifest_path.stat().st_mtime)
    with _lock:
        hit = _run_cache.get(key)
    if hit is not None:
        return hit
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise PitStoreError(f"manifest.json for {pointer['run_id']} is unreadable: {e}") from e
    if manifest.get("schema_version") != SCHEMA:
        raise PitStoreError(
            f"manifest schema {manifest.get('schema_version')!r} != {SCHEMA}; {WHY_V1}")
    bundle = RunBundle(
        run_id=str(pointer["run_id"]),
        generated_at_utc=manifest.get("generated_at_utc"),
        source=manifest.get("source"),
        timeframe=manifest.get("timeframe"),
        horizons={str(k): int(v) for k, v in (manifest.get("horizons") or {}).items()},
        families={m["metric_id"]: m for m in manifest.get("metrics", []) if "metric_id" in m},
        shards={str(s["symbol"]).upper(): s for s in manifest.get("shards", []) if "symbol" in s},
        gate=_empty_gate(),
        methodology=manifest.get("methodology") or {},
        known_limitations=list(manifest.get("known_limitations", [])),
        data_source="lake",
        _lake_root=root,
        _run_dir=manifest_path.parent,
        run_datasets=_datasets_block(manifest),
    )
    caps = (bundle.methodology or {}).get("staleness_caps") or {}
    bundle.datasets_rule = {str(k): f"staleness cap {v} sessions" for k, v in caps.items()}
    with _lock:
        _run_cache.clear()
        _run_cache[key] = bundle
    return bundle


def _datasets_block(manifest: dict) -> dict[str, dict]:
    """Per-dataset rollup: family count, tested (non-diagnostic) count, survivor count,
    research-only count and the family ids' licence classes."""
    datasets: dict[str, dict] = {}
    for m in manifest.get("metrics", []):
        ds = m.get("dataset")
        entry = datasets.setdefault(ds, {"dataset": ds, "families": 0, "tested": 0,
                                         "restricted": 0})
        entry["families"] += 1
        if m.get("licence") == "restricted" or ds in ("news_alpaca", "news_forensic_features"):
            entry["restricted"] += 1
            continue
        if not m.get("diagnostic"):
            entry["tested"] += 1
    return datasets


def _shard_doc(bundle: RunBundle, symbol: str) -> tuple[dict, list[str]]:
    """Lake path: decompress + verify one symbol's shard document."""
    assert bundle._lake_root is not None and bundle._run_dir is not None
    ref = (bundle.shards or {}).get(symbol)
    if ref is None:
        raise PitStoreError(
            f"{symbol} has no shard in run {bundle.run_id} (not in the analysed universe)")
    path = bundle._lake_root / str(ref.get("path", "")).replace("\\", "/")
    resolved = path.resolve()
    if bundle._lake_root != resolved and bundle._lake_root not in resolved.parents:
        raise PitStoreError("shard path escapes the lake root")
    if not path.is_file():
        raise PitStoreError(f"shard for {symbol} is missing from the lake: {ref.get('path')}")
    cache_key = (symbol, str(path), path.stat().st_mtime)
    with _lock:
        hit = bundle._shard_docs.get(cache_key)
    if hit is not None:
        return hit
    try:
        raw = gzip.decompress(path.read_bytes())
    except (OSError, EOFError) as e:
        raise PitStoreError(f"shard for {symbol} is corrupt: {e}") from e
    caveats: list[str] = []
    want = str(ref.get("sha256", "")).upper()
    if want and hashlib.sha256(raw).hexdigest().upper() != want:
        caveats.append("shard sha256 differs from the manifest - treat per-date rows with caution")
    try:
        doc = json.loads(raw.decode("utf-8"))
    except ValueError as e:
        raise PitStoreError(f"shard for {symbol} is not valid JSON: {e}") from e
    inner = doc.get(symbol) or next((v for v in doc.values() if isinstance(v, dict)), {})
    out = (inner, caveats)
    with _lock:
        bundle._shard_docs[cache_key] = out
    return out


# --------------------------------------------------------------------------- relay path

def relay_source(body: dict) -> RunBundle:
    """Build a bundle from a relay response body (``POST /api/mcp/data/pit`` result). The relay
    already stripped values and restricted families; the judging stays here."""
    run = body.get("run") or {}
    gate = body.get("gate") or {}
    survivors = {(g.get("dataset"), g.get("metric"), g.get("horizon")): g
                 for g in (gate.get("survivors") or [])}
    lite = bool(body.get("lite"))
    families: dict[str, dict] = {}
    for m in (body.get("families") or {}).values():
        if isinstance(m, dict) and m.get("metric_id"):
            families[m["metric_id"]] = m
    bundle = RunBundle(
        run_id=str(run.get("run_id", "")),
        generated_at_utc=run.get("generated_at_utc"),
        source=run.get("source"),
        timeframe=run.get("timeframe"),
        horizons={str(k): int(v) for k, v in (run.get("horizons") or {}).items()},
        families=families,
        shards=None,
        gate={
            "available": bool(gate.get("available")),
            "source": gate.get("source"),
            "source_run": gate.get("source_run"),
            "survivors": survivors,
            "families_tested": gate.get("families_tested"),
            "predates_pit_run": False,
            "pit_run_id": gate.get("pit_run_id"),
            "placebo_survivors": gate.get("placebo_survivors"),
            "unreliable": bool(gate.get("unreliable")),
        },
        methodology=run.get("methodology") or {},
        known_limitations=list(run.get("known_limitations", [])),
        data_source="relay",
        lite=lite,
        lite_counts=body.get("counts"),
        run_datasets=body.get("run_datasets"),
        restricted=body.get("restricted"),
    )
    bundle.relay_shards = body.get("shards") or {}
    return bundle


def load_gate(bundle: RunBundle) -> dict:
    """Lake-path gate: l1_survivors.json from the latest zoo run (authoritative); otherwise the
    pit run's own survivors.csv (same gate columns, labelled as the fallback); otherwise
    unavailable. The Python explainer never upgrades a raw p-value on its own."""
    if bundle.data_source == "relay":
        return bundle.gate
    root = bundle._lake_root
    assert root is not None and bundle.run_id and bundle._run_dir is not None
    zoo = root / "search_runs" / "zoo"
    candidates: list[tuple[str, Path]] = []
    if zoo.is_dir():
        for d in sorted((p for p in zoo.iterdir() if p.is_dir()),
                        key=lambda p: p.name, reverse=True):
            f = d / "l1_survivors.json"
            if f.is_file():
                candidates.append(("l1_survivors.json", f))
                break
    csv_path = bundle._run_dir / "survivors.csv"
    if csv_path.is_file():
        candidates.append(("pit_analysis survivors.csv", csv_path))

    for source, path in candidates:
        key = ("gate", str(path), path.stat().st_mtime)
        with _lock:
            hit = _gate_cache.get(key)
        if hit is not None:
            bundle.gate = hit
            return hit
        try:
            survivors: dict[str, dict] = {}
            if path.suffix == ".json":
                rows = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(rows, list):
                    raise ValueError("l1_survivors.json is not a list")
            else:
                with path.open(encoding="utf-8", newline="") as fh:
                    rows = list(csv.DictReader(fh))
            for r in rows:
                fid = (r.get("dataset"), r.get("metric"), r.get("horizon"))
                survivors[fid] = _parse_survivor_row(r)
        except (OSError, ValueError, KeyError):
            continue
        counts = {}
        counts_path = path.parent / "l1_counts.json"
        if path.suffix == ".json" and counts_path.is_file():
            try:
                counts = json.loads(counts_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                counts = {}
        generated = _parse_utc(bundle.generated_at_utc)
        gate_mtime = dt.datetime.fromtimestamp(path.stat().st_mtime, dt.timezone.utc)
        gate = {
            "available": True, "source": source,
            "source_run": path.parent.name if path.suffix == ".json" else bundle.run_id,
            "survivors": survivors,
            "families_tested": counts.get("l1_metrics_tested"),
            "predates_pit_run": bool(generated and gate_mtime < generated),
            "pit_run_id": (rows[0].get("pit_run_id") if rows else None),
            "placebo_survivors": counts.get("l1_placebo_survivors"),
            "unreliable": bool(counts.get("l1_gate_unreliable")),
        }
        with _lock:
            _gate_cache[key] = gate
        bundle.gate = gate
        return gate
    bundle.gate = _empty_gate()
    return bundle.gate


def _parse_survivor_row(d: dict) -> dict:
    return {
        "rolling_ir": _num(d.get("rolling_ir", d.get("ir"))),
        "bh_q": _num(d.get("bh_q", d.get("q"))),
        "harmonic_mean_p": _num(d.get("harmonic_mean_p")),
        "pit_run_id": d.get("pit_run_id"),
        "methodology_version": d.get("methodology_version"),
        "research_only": str(d.get("research_only", False)).strip().lower() == "true",
    }


# --------------------------------------------------------------------------- pure judging

def diagnostic_of(stat: dict) -> bool:
    return bool(stat.get("diagnostic"))


def _run_mismatch(bundle: RunBundle, survivor: dict | None) -> bool:
    """The gate must belong to the same pit run when the survivor rows record one. A recorded id
    from a different run never upgrades a verdict (UNVERIFIED), while absence of a recorded id
    (the CSV fallback / older artifacts) is tolerated and covered by the staleness caveat."""
    if survivor is None:
        return False
    gate_run = survivor.get("pit_run_id") or bundle.gate.get("pit_run_id")
    return bool(gate_run and bundle.run_id and str(gate_run) != str(bundle.run_id))


def judge_horizon(horizon: str, stat: dict, survivor: dict | None, gate: dict,
                  run_mismatch: bool, point_p: str | None, has_point: bool) -> dict:
    """One horizon's self-contained statement. Never guesses; see the module docstring."""
    ir = _num(stat.get("rolling_ir"))
    p = _num(stat.get("p"))
    n_obs = int(stat.get("n_obs") or 0)
    research_only = bool(stat.get("research_only"))
    stale = bool(stat.get("stale"))
    t_hac = _num(stat.get("t_hac"))
    hac_lag = stat.get("hac_lag")

    base = {
        "horizon": horizon,
        "rolling_ir": ir,
        "p_value_two_sided": p,
        "n_obs": n_obs,
        "research_only": research_only,
        "t_hac": t_hac,
        "hac_lag": hac_lag,
        "p_value_at_release": _num(point_p) if has_point else None,
    }
    ro_note = (" RESEARCH-ONLY: the dataset covers under "
               f"{int(COVERAGE_FLOOR * 100)}% of the universe - research evidence, not a tradable "
               "signal.") if research_only else ""
    ir_s, p_s = _fmt(ir), _fmt(p)

    def lag_note() -> str:
        return f" (Newey-West lag {hac_lag})" if isinstance(hac_lag, int) else ""

    if gate.get("unreliable"):
        status, significant = UNVERIFIED, None
        text = (f"UNVERIFIED: {horizon} rolling IR {ir_s} and two-sided p {p_s} (n_obs {n_obs}) are on "
                f"file, but the run's shuffled-date placebo over-performed (the funnel flagged "
                f"l1_gate_unreliable), so this store cannot support a significance claim. Re-run the "
                f"funnel.{ro_note}")
    elif run_mismatch:
        status, significant = UNVERIFIED, None
        text = (f"UNVERIFIED: {horizon} rolling IR {ir_s}, two-sided p {p_s}, n_obs {n_obs} - but the "
                f"gate file belongs to a different pit run than the manifest, so the verdict cannot "
                f"be confirmed. Re-run the funnel.{ro_note}")
    elif stale:
        status, significant = NOT_JUDGED, None
        text = (f"NOT JUDGED (stale): {horizon} rolling IR {ir_s} over {n_obs} rolling IC observations "
                f"is on file, but the family's latest release is older than its dataset's "
                f"staleness cap, so the stats say nothing about the present. p is withheld.{ro_note}")
    elif diagnostic_of(stat):
        status, significant = NOT_JUDGED, None
        text = (f"NOT JUDGED (diagnostic): {horizon} is a raw accounting level kept for reference "
                f"only; the L1 gate tests its scale-free variants (growth, per-share, "
                f"assets-scaled) instead.{ro_note}")
    elif n_obs < MIN_N_OBS:
        status, significant = INSUFFICIENT, None
        text = (f"NOT JUDGED: {horizon} has only {n_obs} rolling IC observations (floor {MIN_N_OBS}), "
                f"so the p-value is null and no significance claim is made. This is absence of "
                f"evidence, not evidence of no effect.{ro_note}")
    elif not gate["available"]:
        status, significant = UNVERIFIED, None
        text = (f"UNVERIFIED: {horizon} rolling IR {ir_s}, two-sided p {p_s}, n_obs {n_obs}, but no L1 "
                f"gate artifact (l1_survivors.json) is available, so the Benjamini-Hochberg q <= "
                f"{BH_Q_MAX} test cannot be applied. A raw p-value is not reported as "
                f"significant.{ro_note}")
    elif survivor is not None:
        status, significant = SIGNIFICANT, True
        base.update(bh_q=survivor.get("bh_q"), harmonic_mean_p=survivor.get("harmonic_mean_p"))
        text = (f"SIGNIFICANT: {horizon} rolling 52-week IR {ir_s} > 0 on size/sector-neutralized "
                f"metric vs neutralized returns, Newey-West two-sided p {p_s} over {n_obs} rolling IC "
                f"observations{lag_note()}, and the family passes the L1 gate (BH q "
                f"{_fmt(survivor.get('bh_q'))} <= {BH_Q_MAX}, harmonic-mean p "
                f"{_fmt(survivor.get('harmonic_mean_p'))}).{ro_note}")
    else:
        status, significant = NOT_SIGNIFICANT, False
        if ir is None or ir <= 0:
            why = (f"rolling IR {ir_s} is not positive, so no positive-signal claim is possible "
                   f"(p is reported only for IR > 0)")
        elif p is not None and p <= BH_Q_MAX:
            why = (f"rolling IR {ir_s} with two-sided p {p_s} <= {BH_Q_MAX} looks nominal, but the "
                   f"family does NOT survive the Benjamini-Hochberg q <= {BH_Q_MAX} correction across "
                   f"all families tested - consistent with multiple-testing noise")
        else:
            why = f"rolling IR {ir_s}, two-sided p {p_s} is not below the significance threshold"
        text = (f"NOT SIGNIFICANT: {horizon} {why}; n_obs {n_obs}; it is not an L1 "
                f"survivor.{ro_note}")
    base.update(status=status, significant=significant, explanation=text)
    return base


def _rank_families(families: list[dict]) -> list[dict]:
    """Most-significant families first: best (verdict order, p, release p) per family."""
    order = {s: i for i, s in enumerate(STATUS_ORDER)}

    def p_key(h: dict):
        return (h["p_value_two_sided"] if h["p_value_two_sided"] is not None else 2.0,
                h["p_value_at_release"] if h["p_value_at_release"] is not None else 2.0)

    def best_key(fam: dict):
        best = min(fam["horizons"], key=lambda h: (order[h["status"]],) + p_key(h))
        return (order[best["status"]], *p_key(best), fam["family_id"])

    return sorted(families, key=best_key)


def _parse_as_of(as_of: str | None) -> str:
    if not as_of:
        return dt.datetime.now(dt.timezone.utc).date().isoformat()
    try:
        return dt.date.fromisoformat(as_of.strip()).isoformat()
    except ValueError:
        raise PitStoreError(f"as_of must be YYYY-MM-DD, got {as_of!r}") from None


def _restricted_dataset_ids() -> set[str]:
    """Datasets withheld from external surfaces: the shipped parity list
    (``swarm_mcp/data/restricted_sources.json``, identical to the C# and site copies)."""
    doc_path = Path(__file__).resolve().parent.parent / "data" / "restricted_sources.json"
    try:
        doc = json.loads(doc_path.read_text(encoding="utf-8"))
        return {str(d) for d in doc.get("datasets", [])}
    except (OSError, ValueError):
        return {"news_alpaca", "news_forensic_features"}


def explain_symbol(bundle: RunBundle, symbol: str, *, as_of: str | None = None,
                   horizons: list[str] | None = None, only_significant: bool = False,
                   max_families: int = 20) -> dict:
    """Explain PIT significance for one underlying from a loaded bundle (pure)."""
    symbol = symbol.strip().upper()
    cutoff = _parse_as_of(as_of)
    all_horizons = dict(bundle.horizons)
    if not all_horizons:
        raise PitStoreError(f"run {bundle.run_id} carries no horizons")
    if horizons:
        unknown = sorted(set(horizons) - set(all_horizons))
        if unknown:
            raise PitStoreError(f"unknown horizon(s) {unknown}; this run has {sorted(all_horizons)}")
        use_h = [h for h in sorted(all_horizons, key=lambda h: all_horizons[h])
                 if h in set(horizons)]
    else:
        use_h = sorted(all_horizons, key=lambda h: all_horizons[h])

    if bundle.data_source == "lake":
        gate = load_gate(bundle)
    else:
        gate = bundle.gate
    restricted = _restricted_dataset_ids()

    # per-symbol shard document
    if bundle.data_source == "lake":
        shard, caveats = _shard_doc(bundle, symbol)
    else:
        caveats = []
        shard = bundle.relay_shards.get(symbol)
        if shard is None:
            raise PitStoreError(f"{symbol} was not part of this relay call "
                                f"(covered: {sorted(bundle.relay_shards)})")

    families: list[dict] = []
    no_stats = 0
    no_point = 0
    withheld = 0
    counts = {s: 0 for s in STATUS_ORDER}
    for metric_id, by_date in shard.items():
        fam = bundle.families.get(metric_id)
        dataset = metric_id.split("|", 1)[0]
        if dataset in restricted or (fam and fam.get("licence") == "restricted"):
            withheld += 1
            continue
        if fam is None:
            no_stats += 1
            continue
        dates = sorted(d for d in by_date if d <= cutoff)
        if not dates:
            no_point += 1
            continue
        latest_date = dates[-1]
        point = by_date[latest_date]
        rows = []
        for h in use_h:
            stat = (fam.get("latest") or {}).get(h)
            if stat is None:
                continue
            # enrich the (cached) stat with the family-level flags the verdict needs
            stat = {**stat, "stale": bool(fam.get("stale")),
                    "diagnostic": bool(fam.get("diagnostic"))}
            sur = gate["survivors"].get((fam.get("dataset"), fam.get("metric"), h))
            rows.append(judge_horizon(
                h, stat, sur, gate, _run_mismatch(bundle, sur),
                point.get(h), h in point))
        if not rows:
            continue
        for r in rows:
            counts[r["status"]] += 1
        if only_significant and not any(r["status"] == SIGNIFICANT for r in rows):
            continue
        sig_h = [r["horizon"] for r in rows if r["status"] == SIGNIFICANT]
        statement = (f"significant at {', '.join(sig_h)}; other horizons as listed"
                     if sig_h else "no horizon is significant under the L1 gate")
        families.append({
            "family_id": metric_id,
            "dataset": fam.get("dataset"),
            "metric": fam.get("metric"),
            "licence": fam.get("licence", "open"),
            "validation": {
                "neutralization": (bundle.methodology or {}).get("neutralization", "log_dv20+sector"),
                "inference": (bundle.methodology or {}).get("inference", "newey_west_bartlett"),
                "staleness_cap": bundle.datasets_rule.get(fam.get("dataset")),
                "stale": any(r["status"] == NOT_JUDGED for r in rows),
            },
            "release_point": {
                "date": latest_date,
                "note": "latest release at or before as_of; raw metric values are never returned (derived-only)",
            },
            "statement": statement,
            "horizons": rows,
        })

    families = _rank_families(families)
    total = len(families)
    max_families = max(1, min(int(max_families), 100))
    shown = families[:max_families]

    if bundle.lite and bundle.lite_counts:
        # relay recomputed the rule over ALL requested families server-side (lowercase keys);
        # normalize into the verdict-keyed counts and trust them verbatim (the local shard
        # data only carries the lite top families)
        relay_map = {"significant": SIGNIFICANT, "not_significant": NOT_SIGNIFICANT,
                     "unverified": UNVERIFIED, "not_judged": NOT_JUDGED,
                     "insufficient_observations": INSUFFICIENT}
        for relay_key, status_key in relay_map.items():
            if relay_key in bundle.lite_counts:
                counts[status_key] = int(bundle.lite_counts[relay_key])
        tested = int(bundle.lite_counts.get("family_horizon_tests") or 0)
    else:
        tested = sum(counts.values())
    sig = counts[SIGNIFICANT]
    headline = _headline(symbol, bundle, gate, counts, tested, withheld)

    caveats = [_strip_public(c) for c in caveats]
    if bundle.data_source == "relay":
        caveats.append("per-symbol data came from the site relay (inputs only; no metric values)")
    if gate["available"] and bundle.data_source == "lake" and gate["source"] != "l1_survivors.json":
        caveats.append(f"gate taken from {gate['source']} (l1_survivors.json not found); "
                       f"same IR>0 / BH q<=0.05 rule")
    if gate.get("predates_pit_run"):
        caveats.append("the gate file is older than the latest pit_analysis run - re-run the funnel "
                       "to refresh it")
    if gate.get("unreliable"):
        caveats.append("shuffled-date placebo over-performed in this run - verdicts withheld")
    if run_mismatch_any(bundle, gate):
        caveats.append("the gate file belongs to a different pit run (pit_run_id mismatch) - "
                       "verdicts are UNVERIFIED")
    if no_stats:
        caveats.append(f"{no_stats} shard families had no manifest stats and were skipped")
    if withheld:
        caveats.append(f"{withheld} restricted (licensed) families withheld from this response")
    if bundle.lite:
        caveats.append("free tier: summary + top families only; unlimited detail needs Pro")
    caveats.extend(_strip_public(k) for k in bundle.known_limitations)
    caveats.append("verdicts describe a family's cross-sectional power across the universe, "
                   "not a forecast for this name")

    return {
        "symbol": symbol,
        "as_of": cutoff,
        "headline": headline,
        "summary": _summary(bundle, counts, tested, sig, total, len(shown), no_point, withheld),
        "families": shown,
        "run": _run_block(bundle),
        "datasets": {k: v for k, v in (bundle.run_datasets or {}).items()
                     if k not in restricted and not v.get("restricted")},
        "gate": _gate_block(bundle, gate),
        "observation_floor": {"min_n_obs": MIN_N_OBS, "min_cross_section": MIN_CROSS_SECTION,
                              "rolling_window_weeks": ROLLING_WINDOW, "hac_lag_rule": HAC_LAG_RULE},
        "methodology": bundle.methodology,
        "how_to_read": HOW_TO_READ,
        "data_source": bundle.data_source,
        "caveats": caveats,
    }


def run_mismatch_any(bundle: RunBundle, gate: dict) -> bool:
    return any(_run_mismatch(bundle, sur) for sur in gate.get("survivors", {}).values())


def _headline(symbol: str, bundle: RunBundle, gate: dict, counts: dict, tested: int,
              withheld: int) -> str:
    sig = counts[SIGNIFICANT]
    if gate.get("unreliable"):
        return (f"{symbol}: the run's shuffled-date placebo over-performed, so no significance "
                f"verdict is offered for {tested} family-horizon tests (UNVERIFIED).")
    if gate.get("pit_run_id") and bundle.run_id and str(gate.get("pit_run_id")) != str(bundle.run_id):
        return (f"{symbol}: the gate file belongs to a different pit run, so no significance verdict "
                f"is offered for the {tested} family-horizon tests on file (UNVERIFIED).")
    if not gate["available"]:
        return (f"{symbol}: {tested} family-horizon tests on file but NO L1 gate artifact is "
                f"available, so no result can be called significant (UNVERIFIED).")
    withheld_note = f" ({withheld} restricted families withheld)" if withheld else ""
    if sig:
        return (f"{symbol}: {sig} of {tested} family-horizon tests are SIGNIFICANT under the L1 gate "
                f"(rolling IR > 0, BH q <= {BH_Q_MAX}); the other {tested - sig} are not significant "
                f"or not judged.{withheld_note}")
    return (f"{symbol}: 0 of {tested} family-horizon tests are significant under the L1 gate - "
            f"nothing here is supported as a signal.{withheld_note}")


def _summary(bundle: RunBundle, counts: dict, tested: int, sig: int, total: int, shown: int,
             no_point: int, withheld: int) -> dict:
    lite = bundle.lite and bundle.lite_counts is not None
    return {
        "families_with_results": bundle.lite_counts.get("families_with_results", total) if lite else total,
        "family_horizon_tests": tested,
        "significant": counts.get(SIGNIFICANT, 0),
        "not_significant": counts.get(NOT_SIGNIFICANT, 0),
        "not_judged": counts.get(NOT_JUDGED, 0),
        "insufficient_observations": counts.get(INSUFFICIENT, 0),
        "unverified": counts.get(UNVERIFIED, 0),
        "families_shown": shown,
        "families_omitted": (bundle.lite_counts.get("families_with_results", total)
                             if lite else total) - shown,
        "families_without_release_by_as_of": no_point,
        "restricted_withheld": withheld,
        "lite": bundle.lite,
    }


def _run_block(bundle: RunBundle) -> dict:
    return {
        "run_id": bundle.run_id,
        "generated_at_utc": bundle.generated_at_utc,
        "source": bundle.source,
        "timeframe": bundle.timeframe,
        "horizons_sessions": bundle.horizons,
        "data_source": bundle.data_source,
        "reuse_rule": "LATEST is reloaded; with no new release the stored per-family stats are reused",
        "schema": SCHEMA,
    }


def _gate_block(bundle: RunBundle, gate: dict) -> dict:
    return {
        "rule": (f"rolling {ROLLING_WINDOW}-week IR > 0 AND Benjamini-Hochberg q <= {BH_Q_MAX} "
                 f"(with harmonic-mean p and the licence class); Newey-West {HAC_LAG_RULE} lags"),
        "available": gate["available"],
        "source": gate["source"],
        "source_run": gate["source_run"],
        "pit_run_id": gate.get("pit_run_id"),
        "families_tested": gate["families_tested"],
        "survivors": len(gate["survivors"]),
        "placebo_survivors": gate.get("placebo_survivors"),
        "unreliable": gate.get("unreliable"),
    }


def _strip_public(text: str) -> str:
    return str(text).replace("benzinga", "licensed vendor").replace("Benzinga", "licensed vendor") \
        .replace("alpaca", "the news provider").replace("Alpaca", "The news provider")


def _parse_utc_public(text) -> dt.datetime | None:
    return _parse_utc(text)
