"""Tiered MCP test harness for the 26-tool suite (13 data / 6 warden / 7 gym).

Tier 0 (default, CI-gating, offline): committed synthetic deterministic
fixtures over the real stdio servers — contract snapshot + architecture
scenario chain + negative/fail-closed cases + golden simulator baselines.
Local JSONL run transcripts per scenario; no network, no credits, no broker.

Tier 1 (opt-in, HARNESS_AGENT=1): harness.agent_loop — scripted tool-trace
oracles over committed cassettes by default; the live LLM loop only with an
API key plus a hard spend cap (nightly, never merge-gating).

Tier 2 (opt-in, HARNESS_HOSTED=1 + token + --max-credits): harness.hosted —
relay / hosted / tournament round-trips under a credit budget cap. Never in
default CI.

See harness/README.md for the exact commands.
"""
