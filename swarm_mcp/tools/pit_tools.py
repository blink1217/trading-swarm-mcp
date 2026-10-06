"""swarm-data-mcp pit.analysis tool - explained PIT-correlation significance queried by
underlying. Data source: the local/mirrored lake (SWARM_MCP_PIT_LAKE_ROOT) when the operator has
one, otherwise the site relay (which never returns raw metric values or restricted families).
Verdicts stay consistent with the funnel's l1_survivors.json gate either way. Free plans get a
lite response (one symbol, top families, headline counts); paid plans get the full detail."""
from __future__ import annotations

import asyncio

from swarm_mcp import access, redaction, relay
from swarm_mcp.cache.db import get_db
from swarm_mcp.pit import analysis
from swarm_mcp.tool_runner import run_tool

MAX_SYMBOLS = 10
LITE_MAX_FAMILIES = 3


async def _fetch_bundle(symbols: list[str]) -> analysis.RunBundle:
    """Lake path when an operator lake root is set (file I/O in a worker); otherwise one relay
    POST for the whole call (async), then only CPU-bound pure code downstream."""
    root = analysis.lake_root()
    if root is not None:
        return await asyncio.to_thread(analysis.load_lake_run, root)
    return analysis.relay_source(await relay.fetch_pit(symbols))


def _explain(bundle: analysis.RunBundle, symbols: list[str], *, as_of, horizons,
             only_significant: bool, max_families: int) -> tuple[dict, dict]:
    out, unavailable = {}, {}
    for s in symbols:
        try:
            out[s] = analysis.explain_symbol(
                bundle, s, as_of=None if bundle.lite else as_of,
                horizons=horizons,
                only_significant=only_significant,
                max_families=LITE_MAX_FAMILIES if bundle.lite else max_families)
        except analysis.PitStoreError as e:
            unavailable[s] = str(e)
    return out, unavailable


async def pit_analysis(symbols: list[str], as_of: str | None = None,
                       horizons: list[str] | None = None,
                       only_significant: bool = False, max_families: int = 20) -> dict:
    redaction.reject_keylike_args({"symbols": symbols, "as_of": as_of, "horizons": horizons})

    async def _do():
        syms = sorted({s.strip().upper() for s in symbols or [] if s and s.strip()})
        if not syms:
            raise ValueError("provide at least one underlying symbol")
        if len(syms) > MAX_SYMBOLS:
            raise ValueError(f"at most {MAX_SYMBOLS} symbols per call (got {len(syms)})")
        ent = access.current_entitlement()
        lite = ent is None or not ent.is_funded
        # free plans: one symbol per call (+ relay-side truncation backstops it)
        request_syms = syms[:1] if lite else syms
        bundle = await _fetch_bundle(request_syms)
        # the tool's entitlement decision governs lite even on the lake path
        bundle.lite = bundle.lite or lite

        results, unavailable = await asyncio.to_thread(
            _explain, bundle, request_syms,
            **{"as_of": None if bundle.lite else as_of, "horizons": horizons,
               "only_significant": only_significant,
               "max_families": LITE_MAX_FAMILIES if bundle.lite else max_families})
        if not results and unavailable:
            # every symbol failed: surface the store-level problem as a tool error
            raise ValueError("; ".join(sorted(set(unavailable.values()))))
        get_db().log_provenance("pit.analysis", ",".join(syms),
                                "pit_analysis/v2 lake run (LATEST)",
                                "derived significance statistics only - no raw metric values returned")
        any_run = next(iter(results.values()))
        out = {
            "tool": "pit.analysis",
            "source": "pit_analysis/v2 (release-gated PIT-correlation store) + L1 funnel gate",
            "underlyings": results,
            "unavailable": unavailable,
            "run_id": any_run["run"]["run_id"],
            "how_to_read": analysis.HOW_TO_READ,
            "methodology": ("Spearman cross-sectional rank IC per (dataset|metric|horizon), "
                            "size/sector-neutralized; rolling 52-week IR; Newey-West two-sided p "
                            "(lag ceil(horizon/5)) because horizons overlap; a family is significant "
                            "only if it survives the funnel gate (IR > 0 and Benjamini-Hochberg "
                            "q <= 0.05). Point-in-time: release points after as_of are ignored."),
            "not_investment_advice": True,
            "learn_more": access.SITE_URL,
        }
        if bundle.lite:
            out["lite"] = True
            out["upgrade"] = {
                "what_pro_adds": ("up to 10 symbols per call, every family, full per-horizon detail "
                                  "and historical as_of"),
                "url": ent.upgrade_url if ent is not None else access.SITE_URL,
            }
        return out

    return await run_tool("pit.analysis", _do)
