"""quant-swarm — local stdio MCP servers from the trading swarm.

Three servers:
- swarm-data-mcp   point-in-time bars + enrichment cache with as_of provenance
- swarm-warden-mcp pre-trade invariant + leakage gate (the live-capital checkers)
- swarm-gym-mcp    regime fragility probe; structurally unable to promote locally

Local-first and BYO-key: your Alpaca/Finnhub keys are read from env only, and
your symbols, genomes, and alpha never leave the machine. Promotion verdicts
are never issued locally — statistically undecidable outputs route to the
hosted tournament / Strategy Validation Audit.
"""

try:
    from importlib.metadata import version as _pkg_version

    __version__ = _pkg_version("quant-swarm")
except Exception:  # running from source without installation
    __version__ = "0.4.1"
