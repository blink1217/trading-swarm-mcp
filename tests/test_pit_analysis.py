"""pit.analysis: explains pit_analysis/v2 results per underlying and states explicitly
whether each (dataset|metric|horizon) is significant. A family is SIGNIFICANT only when it
survives the funnel's L1 gate (IR > 0, BH q <= 0.05) OF THE SAME RUN; null p below the
observation floor, stale and diagnostic families are never judged; restricted (licensed)
families are withheld; raw metric values never leave the store."""
from __future__ import annotations

import gzip
import hashlib
import json
import os

import pytest

from helpers import run_async
from swarm_mcp.pit import analysis
from swarm_mcp.tools import pit_tools

RUN = "pit_20260101T000000000"
GATE_RUN_DIR = "zoo_20260101_000000"


def _stat(ir, p, n, ro=False, t=None, lag=1):
    return {"rolling_ir": ir, "p": p, "n_obs": n, "research_only": ro,
            "t_hac": t if t is not None else (ir * n ** 0.5 if p is not None else None),
            "hac_lag": lag}


def _manifest(stale_ids=(), with_licence=True):
    metrics = [
        {"metric_id": "wiki|wiki.pageviews", "dataset": "wiki", "metric": "wiki.pageviews",
         "licence": "open", "stale": "wiki|wiki.pageviews" in stale_ids, "diagnostic": False,
         "latest": {"1d": _stat(0.9, 0.0004, 52), "1w": _stat(0.3, 0.03, 40),
                    "1m": _stat(-0.2, None, 52), "6m": _stat("NaN", None, 10)}},
        {"metric_id": "edgar|edgar.rev", "dataset": "edgar", "metric": "edgar.rev",
         "licence": "open", "stale": "edgar|edgar.rev" in stale_ids, "diagnostic": False,
         "latest": {"1d": _stat(1.1, 0.00001, 52, True), "1w": _stat(0.0, None, 0),
                    "1m": _stat(0.1, 0.5, 30), "6m": _stat(0.2, 0.2, 30)}},
        {"metric_id": "edgar|edgar.assets", "dataset": "edgar", "metric": "edgar.assets",
         "licence": "open", "stale": False, "diagnostic": True,
         "latest": {"1d": _stat(2.0, None, 52)}},
        {"metric_id": "news_alpaca|news.count_1d", "dataset": "news_alpaca",
         "metric": "news.count_1d",
         "licence": "restricted" if with_licence else "open", "stale": False, "diagnostic": False,
         "latest": {"1d": _stat(1.5, 0.0001, 52)}},
    ]
    return {
        "schema_version": "pit_analysis/v2",
        "generated_at_utc": "2026-01-01T00:00:00.1234567Z",
        "run_id": RUN, "source": "local", "timeframe": "1Day",
        "horizons": {"1d": 1, "1w": 5, "1m": 21, "6m": 126},
        "known_limitations": ["reuse caveat"],
        "methodology": {"ic": "spearman", "neutralization": "log_dv20+sector",
                        "inference": "newey_west_bartlett", "hac_lag_rule": "ceil(horizon_sessions / 5)",
                        "rolling_window": 52, "min_n_obs": 26, "bh_q": 0.05,
                        "staleness_caps": {"wiki": 5, "edgar": 95}},
        "metrics": metrics,
        "shards": [{"symbol": "AAPL",
                    "path": f"pit_analysis/v2/run={RUN}/underlyings/AAPL.json.gz",
                    "sha256": None}],  # filled in _write_lake
    }


def _gate_rows(pit_run_id=RUN):
    return [
        {"dataset": "wiki", "metric": "wiki.pageviews", "horizon": "1d", "rolling_ir": 0.9,
         "bh_q": 0.004, "harmonic_mean_p": 0.01, "research_only": False, "pit_run_id": pit_run_id},
        {"dataset": "edgar", "metric": "edgar.rev", "horizon": "1d", "rolling_ir": 1.1,
         "bh_q": 0.0001, "harmonic_mean_p": 0.01, "research_only": True, "pit_run_id": pit_run_id},
    ]


def _write_lake(root, *, gate="json", shard_future=True, manifest=None, pit_run_id=RUN,
                gate_unreliable=False):
    manifest = manifest or _manifest()
    run_dir = root / "pit_analysis" / "v2" / f"run={RUN}"
    (run_dir / "underlyings").mkdir(parents=True)
    shard_doc = {"AAPL": {
        "wiki|wiki.pageviews": {
            "2025-12-01": {"1d": "0.04", "1w": None, "1m": None, "6m": None, "value": 7777771.5},
            "2025-12-15": {"1d": "0.0004", "1w": "0.03", "1m": None, "6m": None, "value": 8888882.5},
            **({"2099-01-01": {"1d": "0.9", "1w": None, "1m": None, "6m": None, "value": 1.0}}
               if shard_future else {})},
        "edgar|edgar.rev": {
            "2025-11-01": {"1d": "0.00001", "1w": None, "1m": "0.5", "6m": "0.2",
                           "value": 6666663.5}},
        "edgar|edgar.assets": {"2025-11-01": {"1d": None, "1w": None, "1m": None, "6m": None,
                                              "value": 111.0}},
        "news_alpaca|news.count_1d": {"2025-12-01": {"1d": "0.0001", "1w": None, "1m": None,
                                                     "6m": None, "value": 5.0}},
    }}
    raw = json.dumps(shard_doc).encode()
    (run_dir / "underlyings" / "AAPL.json.gz").write_bytes(gzip.compress(raw))
    manifest["shards"][0]["sha256"] = hashlib.sha256(raw).hexdigest().upper()
    (run_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (root / "pit_analysis" / "v2" / "LATEST").write_text(json.dumps({
        "schema_version": "pit_analysis/v2", "run_id": RUN,
        "manifest_path": f"pit_analysis/v2/run={RUN}/manifest.json",
        "generated_at_utc": "2026-01-01T00:00:00Z"}), encoding="utf-8")
    if gate == "json":
        g = root / "search_runs" / "zoo" / GATE_RUN_DIR
        g.mkdir(parents=True)
        (g / "l1_survivors.json").write_text(json.dumps(_gate_rows(pit_run_id)), encoding="utf-8")
        (g / "l1_counts.json").write_text(json.dumps({
            "l1_metrics_tested": 8, "l1_placebo_survivors": 0,
            **({"l1_gate_unreliable": 1} if gate_unreliable else {})}), encoding="utf-8")
    elif gate == "csv":
        (run_dir / "survivors.csv").write_text(
            "dataset,metric,horizon,ir,p,q,n_obs,research_only\n"
            "wiki,wiki.pageviews,1d,0.9,0.0004,0.004,52,False\n", encoding="utf-8")


@pytest.fixture
def lake(tmp_path, monkeypatch):
    monkeypatch.setenv(analysis.LAKE_ROOT_ENV, str(tmp_path))
    analysis.reset_cache()
    yield tmp_path
    analysis.reset_cache()


def _fam(out, fid):
    return next(f for f in out["families"] if f["family_id"] == fid)


def _h(fam, h):
    return next(r for r in fam["horizons"] if r["horizon"] == h)


def _explain(lake_root, **kw):
    b = analysis.load_lake_run(analysis.lake_root())
    return analysis.explain_symbol(b, kw.pop("symbol", "AAPL"), **kw)


def test_significant_only_when_l1_survivor_of_same_run(lake):
    _write_lake(lake)
    out = _explain(lake, as_of="2026-06-01")
    d1 = _h(_fam(out, "wiki|wiki.pageviews"), "1d")
    assert d1["status"] == "SIGNIFICANT" and d1["significant"] is True
    assert d1["rolling_ir"] == 0.9 and d1["p_value_two_sided"] == 0.0004 and d1["n_obs"] == 52
    assert d1["t_hac"] is not None and d1["hac_lag"] == 1
    assert d1["bh_q"] == 0.004 and d1["research_only"] is False
    assert "SIGNIFICANT" in d1["explanation"] and "BH q" in d1["explanation"]
    assert "Newey-West" in d1["explanation"]
    # nominal p=0.03 but not an L1 survivor -> explicitly NOT significant, says why
    w1 = _h(_fam(out, "wiki|wiki.pageviews"), "1w")
    assert w1["status"] == "NOT_SIGNIFICANT" and w1["significant"] is False
    assert "NOT SIGNIFICANT" in w1["explanation"] and "Benjamini-Hochberg" in w1["explanation"]
    # negative IR
    m1 = _h(_fam(out, "wiki|wiki.pageviews"), "1m")
    assert m1["status"] == "NOT_SIGNIFICANT" and "not positive" in m1["explanation"]


def test_below_floor_is_not_judged(lake):
    _write_lake(lake)
    out = _explain(lake, as_of="2026-06-01")
    s6 = _h(_fam(out, "wiki|wiki.pageviews"), "6m")
    assert s6["status"] == "INSUFFICIENT_OBSERVATIONS" and s6["significant"] is None
    assert s6["rolling_ir"] is None and s6["p_value_two_sided"] is None
    assert "NOT JUDGED" in s6["explanation"] and "floor 26" in s6["explanation"]
    z = _h(_fam(out, "edgar|edgar.rev"), "1w")
    assert z["n_obs"] == 0 and z["status"] == "INSUFFICIENT_OBSERVATIONS"


def test_stale_family_never_judged(lake):
    _write_lake(lake, manifest=_manifest(stale_ids=("wiki|wiki.pageviews",)))
    out = _explain(lake, as_of="2026-06-01")
    d1 = _h(_fam(out, "wiki|wiki.pageviews"), "1d")
    assert d1["status"] == "NOT_JUDGED" and "stale" in d1["explanation"]
    assert d1["significant"] is None
    assert out["summary"]["significant"] == 1  # only edgar 1d survives


def test_diagnostic_family_never_judged(lake):
    _write_lake(lake)
    out = _explain(lake, as_of="2026-06-01")
    d1 = _h(_fam(out, "edgar|edgar.assets"), "1d")
    assert d1["status"] == "NOT_JUDGED" and "diagnostic" in d1["explanation"]


def test_research_only_flag(lake):
    _write_lake(lake)
    out = _explain(lake, as_of="2026-06-01")
    e1 = _h(_fam(out, "edgar|edgar.rev"), "1d")
    assert e1["status"] == "SIGNIFICANT" and e1["research_only"] is True
    assert "RESEARCH-ONLY" in e1["explanation"]


def test_restricted_families_withheld_and_vendor_never_leaks(lake):
    _write_lake(lake)
    out = _explain(lake, as_of="2026-06-01")
    assert "news_alpaca|news.count_1d" not in [f["family_id"] for f in out["families"]]
    assert out["summary"]["restricted_withheld"] == 1  # one restricted family
    blob = json.dumps(out)
    assert "benzinga" not in blob.lower()
    assert "news_alpaca" not in blob  # dataset names of restricted families stay out of rows
    assert "7777771" not in blob and "8888882" not in blob and "6666663" not in blob


def test_point_in_time_cutoff_and_derived_only(lake):
    _write_lake(lake)
    out = _explain(lake, as_of="2025-12-10")
    wiki = _fam(out, "wiki|wiki.pageviews")
    assert wiki["release_point"]["date"] == "2025-12-01"
    assert _h(wiki, "1d")["p_value_at_release"] == 0.04
    late = _explain(lake, as_of="2026-06-01")
    assert _fam(late, "wiki|wiki.pageviews")["release_point"]["date"] == "2025-12-15"
    early = _explain(lake, as_of="2025-10-01")
    assert early["summary"]["families_without_release_by_as_of"] == 3


def test_no_gate_means_unverified_never_significant(lake):
    _write_lake(lake, gate=None)
    out = _explain(lake, as_of="2026-06-01")
    d1 = _h(_fam(out, "wiki|wiki.pageviews"), "1d")
    assert d1["status"] == "UNVERIFIED" and d1["significant"] is None
    assert out["summary"]["significant"] == 0 and out["gate"]["available"] is False


def test_csv_gate_fallback_is_labelled(lake):
    _write_lake(lake, gate="csv")
    out = _explain(lake, as_of="2026-06-01")
    assert out["gate"]["source"] == "pit_analysis survivors.csv"
    assert _h(_fam(out, "wiki|wiki.pageviews"), "1d")["status"] == "SIGNIFICANT"
    assert any("survivors.csv" in c for c in out["caveats"])


def test_gate_from_other_run_never_upgrades(lake):
    _write_lake(lake, pit_run_id="pit_OTHER")
    out = _explain(lake, as_of="2026-06-01")
    d1 = _h(_fam(out, "wiki|wiki.pageviews"), "1d")
    assert d1["status"] == "UNVERIFIED"
    assert out["summary"]["significant"] == 0
    assert any("different pit run" in c for c in out["caveats"])


def test_placebo_unreliable_withholds_all(lake):
    _write_lake(lake, gate_unreliable=True)
    out = _explain(lake, as_of="2026-06-01")
    assert out["summary"]["significant"] == 0
    assert all(h["status"] == "UNVERIFIED"
               for f in out["families"] for h in f["horizons"])
    assert any("placebo" in c for c in out["caveats"])


def test_summary_counts_ranking_and_only_significant(lake):
    _write_lake(lake)
    out = _explain(lake, as_of="2026-06-01")
    s = out["summary"]
    assert s["significant"] == 2 and s["not_judged"] >= 1 and s["insufficient_observations"] >= 2
    assert s["significant"] + s["not_significant"] + s["not_judged"] + \
        s["insufficient_observations"] + s["unverified"] == s["family_horizon_tests"]
    assert out["families"][0]["horizons"][0]["status"] == "SIGNIFICANT"  # ranked first
    only = _explain(lake, as_of="2026-06-01", only_significant=True, max_families=1)
    assert only["summary"]["families_shown"] == 1
    assert out["run"]["run_id"] == RUN and out["gate"]["families_tested"] == 8
    assert out["methodology"]["neutralization"] == "log_dv20+sector"
    assert out["datasets"]["wiki"]["families"] == 1


def test_horizon_subset_and_validation(lake):
    _write_lake(lake)
    out = _explain(lake, as_of="2026-06-01", horizons=["1d"])
    assert all(len(f["horizons"]) == 1 for f in out["families"])
    with pytest.raises(analysis.PitStoreError):
        _explain(lake, horizons=["2y"])
    with pytest.raises(analysis.PitStoreError):
        _explain(lake, as_of="yesterday")


def test_v1_store_refused(lake, capsys):
    lake.joinpath("pit_analysis", "v1", "LATEST").parent.mkdir(parents=True)
    lake.joinpath("pit_analysis", "v1", "LATEST").write_text("{}")
    with pytest.raises(analysis.PitStoreError) as e:
        analysis.load_lake_run(analysis.lake_root())
    assert "pit-correlations" in str(e.value)


def test_tool_envelope_unknown_symbol_and_missing_lake(lake, monkeypatch):
    _write_lake(lake)
    out = run_async(pit_tools.pit_analysis(symbols=["AAPL", "ZZZZ"], as_of="2026-06-01"))
    assert out["tool"] == "pit.analysis" and out["not_investment_advice"] is True
    assert "AAPL" in out["underlyings"] and "ZZZZ" in out["unavailable"]
    assert "how_to_read" in out and out["run_id"] == RUN
    only_bad = run_async(pit_tools.pit_analysis(symbols=["ZZZZ"]))
    assert "no shard" in only_bad["error"]
    assert "provide at least one" in run_async(pit_tools.pit_analysis(symbols=[]))["error"]
    monkeypatch.delenv(analysis.LAKE_ROOT_ENV)
    analysis.reset_cache()
    err = run_async(pit_tools.pit_analysis(symbols=["AAPL"]))["error"]
    assert ("relay" in err.lower() or "SWARM_MCP_PIT_LAKE_ROOT" in err
            or "token" in err.lower() or "unreachable" in err.lower())


def test_free_plan_gets_lite(monkeypatch, lake):
    _write_lake(lake)
    from swarm_mcp import access
    monkeypatch.setattr(access, "current_entitlement", lambda: None)
    analysis.reset_cache()
    out = run_async(pit_tools.pit_analysis(symbols=["AAPL", "MSFT"], as_of="2026-06-01"))
    assert out.get("lite") is True and "upgrade" in out
    assert list(out["underlyings"]) == ["AAPL"]
    assert out["underlyings"]["AAPL"]["summary"]["families_shown"] <= 3
    assert out["upgrade"]["what_pro_adds"]


def test_paid_plan_full(monkeypatch, lake):
    _write_lake(lake)
    from swarm_mcp import access
    ent = access.Entitlement(plan="pro", status="active")
    monkeypatch.setattr(access, "current_entitlement", lambda: ent)
    analysis.reset_cache()
    out = run_async(pit_tools.pit_analysis(symbols=["AAPL", "ZZZZ"], as_of="2026-06-01"))
    assert out.get("lite") is None and "upgrade" not in out
    assert out["underlyings"]["AAPL"]["summary"]["families_shown"] >= 3


def test_relay_source_lite_counts(monkeypatch):
    body = {
        "ok": True,
        "run": {"run_id": RUN, "generated_at_utc": "2026-01-01T00:00:00Z", "timeframe": "1Day",
                "horizons": {"1d": 1}, "methodology": {"neutralization": "log_dv20+sector"},
                "known_limitations": ["reuse caveat"]},
        "run_datasets": {"wiki": {"dataset": "wiki", "families": 1, "tested": 1}},
        "families": {"wiki|wiki.pageviews": {
            "metric_id": "wiki|wiki.pageviews", "dataset": "wiki", "metric": "wiki.pageviews",
            "licence": "open", "stale": False, "diagnostic": False,
            "latest": {"1d": _stat(0.9, 0.0004, 52)}}},
        "shards": {"AAPL": {"wiki|wiki.pageviews": {"2025-12-15": {"1d": "0.0004"}}}},
        "gate": {"available": True, "source": "l1_survivors.json", "source_run": GATE_RUN_DIR,
                 "pit_run_id": RUN, "families_tested": 8, "placebo_survivors": 0,
                 "unreliable": False, "survivors": _gate_rows(RUN)},
        "counts": {"significant": 4, "not_significant": 2, "unverified": 0, "not_judged": 1,
                   "insufficient_observations": 1, "family_horizon_tests": 8,
                   "families_with_results": 2},
        "lite": True,
        "restricted": {"families_withheld": 1, "reason": "licensed paid source"},
    }
    b = analysis.relay_source(body)
    out = analysis.explain_symbol(b, "AAPL", as_of="2026-06-01")
    assert b.data_source == "relay" and b.lite is True
    assert out["summary"]["significant"] == 4  # relay counts over ALL families, verbatim
    assert out["summary"]["lite"] is True
    d1 = _h(_fam(out, "wiki|wiki.pageviews"), "1d")
    assert d1["status"] == "SIGNIFICANT"
    assert any("relay" in c for c in out["caveats"])


def test_relay_refusal_maps_to_error(monkeypatch):
    import asyncio
    from swarm_mcp import relay as relay_mod

    class _Resp:
        status_code = 402
        def json(self):
            return {"ok": False, "error": "plan required", "reason": "plan_required",
                    "upgrade_url": "https://x/upgrade"}

    async def _fail(symbols):
        raise relay_mod.RelayError("data relay refused (plan_required)", reason="plan_required",
                                   upgrade_url="https://x/upgrade")

    monkeypatch.setattr(relay_mod, "fetch_pit", _fail)
    monkeypatch.delenv(analysis.LAKE_ROOT_ENV, raising=False)
    analysis.reset_cache()
    out = run_async(pit_tools.pit_analysis(symbols=["AAPL"]))
    assert "error" in out


def test_shard_hash_mismatch_is_flagged(lake):
    _write_lake(lake)
    p = lake / "pit_analysis" / "v2" / f"run={RUN}" / "manifest.json"
    m = json.loads(p.read_text())
    m["shards"][0]["sha256"] = "00"
    p.write_text(json.dumps(m))
    analysis.reset_cache()
    out = _explain(lake, as_of="2026-06-01")
    assert any("sha256" in c for c in out["caveats"])


def test_path_traversal_refused(lake):
    _write_lake(lake)
    p = lake / "pit_analysis" / "v2" / f"run={RUN}" / "manifest.json"
    m = json.loads(p.read_text())
    m["shards"][0]["path"] = "../../../../etc/passwd"
    p.write_text(json.dumps(m))
    analysis.reset_cache()
    with pytest.raises(analysis.PitStoreError):
        _explain(lake)


def test_latest_reused_until_new_run(lake):
    _write_lake(lake)
    a = analysis.load_lake_run(analysis.lake_root())
    assert analysis.load_lake_run(analysis.lake_root()) is a  # no new release => reuse


@pytest.mark.skipif(not os.environ.get("PIT_REAL_LAKE"), reason="set PIT_REAL_LAKE to a real lake root")
def test_real_lake_smoke(monkeypatch):
    monkeypatch.setenv(analysis.LAKE_ROOT_ENV, os.environ["PIT_REAL_LAKE"])
    analysis.reset_cache()
    b = analysis.load_lake_run(analysis.lake_root())
    out = analysis.explain_symbol(b, os.environ.get("PIT_REAL_SYMBOL", "AA"))
    assert out["summary"]["family_horizon_tests"] > 0
