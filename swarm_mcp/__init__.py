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

__version__ = "0.3.0"
