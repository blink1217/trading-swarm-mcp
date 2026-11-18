"""Remote streamable-HTTP MCP servers (Cloud Run).

Exposes the three swarm MCP servers over Streamable HTTP for remote agents:
  /mcp/data    -> swarm-data-mcp   (bars, enrichment, feature building)
  /mcp/warden  -> swarm-warden-mcp (invariant + leakage auditing)
  /mcp/gym     -> swarm-gym-mcp    (regime fragility probing)

Every request must carry `Authorization: Bearer <site token>` (issued at
https://1.21initiative.com/mcp/). The middleware validates it against the site's
verify endpoint (300s in-process cache), parses the enriched entitlement
(plan/status/features/quota), and scopes both to the request so the hosted data
relay meters per-prospect. Fail closed: no token or a rejected token gets a 401
(per the MCP auth spec this also triggers OAuth discovery).

Plan enforcement here is REAL (this endpoint is ours, unlike the open-source
stdio surface): a tools/call for a Pro tool on a non-paid entitlement is refused
with HTTP 402 and a JSON-RPC error carrying the upgrade_url. Pro tools still
LIST so each attempt is a conversion event, not a dead end.

Hosted COMPUTE is metered (swarm_mcp.metering): before a Pro compute tool runs
the middleware charges the site's /api/mcp/meter in credits (1 per simulated
gym episode, 1 per single-shot tool). A refusal (empty/expired pool, burst) is
a JSON-RPC -32003 with the structured reason; an unreachable meter refuses too.

  /internal/tournament/run  site -> runner dispatch for the Shadow Tournament
                            (X-Swarm-Internal-Key shared secret, 202 + background
                            scoring, callback to /api/mcp/tournament/complete).

Run: python -m swarm_mcp.servers.http_server  (PORT env, default 8080)
"""
from __future__ import annotations

import asyncio
import base64
import binascii
from collections import OrderedDict, deque
import hashlib
import inspect
import json
import logging
import math
import os
import threading
import time
from urllib.parse import parse_qs, urlparse

from starlette.responses import JSONResponse

from swarm_mcp import access, request_context, server_meta
from swarm_mcp.mcp_compat import MCPServer
from swarm_mcp.servers import data_server, gym_server, unified_server, warden_server
from swarm_mcp.tools import audit_tools

log = logging.getLogger("swarm_mcp.http")

# M-01 + v1.1.0 transport isolation: local-only tools (process-global switches,
# local SQLite cache warm/stats, and local-filesystem path audit.* tools) are omitted
# from remote HTTP tools/list and hard-refused on remote HTTP tools/call.
HOSTED_LOCAL_ONLY_TOOLS = frozenset({
    "cache.warm",
    "cache.offline",
    "cache.stats",
    "audit.backtest",
    "audit.preflight",
    "audit.quote",
    "audit.run",
    "audit.report",
    "audit.compare",
})

HEAVY_TOOL_PREFIXES = ("market.", "gym.")
HEAVY_EXACT_TOOLS = frozenset({
    "pit.analysis",
    "features.build",
    "volume.forecast",
    "tournament.submit",
})

DEFAULT_TOKEN_RPM = 60
DEFAULT_HEAVY_RPM = 20
DEFAULT_DISCOVERY_RPM = 120
RATE_WINDOW_S = 60.0

UNAUTHENTICATED_DISCOVERY_METHODS = frozenset({
    "initialize",
    "notifications/initialized",
    "notifications/cancelled",
    "ping",
    "tools/list",
    "resources/list",
    "prompts/list",
})


def _is_heavy_tool(name: str) -> bool:
    return name in HEAVY_EXACT_TOOLS or name.startswith(HEAVY_TOOL_PREFIXES)


class SlidingWindowRateLimiter:
    """Bounded in-process sliding-window rate limiter (keyed by hashed token or client IP)."""

    def __init__(self, *, window_s: float = RATE_WINDOW_S, max_keys: int = 4096) -> None:
        self._window_s = window_s
        self._max_keys = max_keys
        self._buckets: OrderedDict[str, deque[float]] = OrderedDict()
        self._lock = threading.Lock()

    def check_and_consume(
        self,
        key: str,
        *,
        limit: int,
        weight: int = 1,
        now: float | None = None,
    ) -> tuple[bool, int]:
        """Return `(allowed, retry_after_seconds)`. `limit <= 0` disables enforcement."""
        if limit <= 0 or weight <= 0:
            return True, 0
        ts = time.monotonic() if now is None else now
        cutoff = ts - self._window_s
        with self._lock:
            dq = self._buckets.get(key)
            if dq is not None:
                while dq and dq[0] <= cutoff:
                    dq.popleft()
                self._buckets.move_to_end(key)
            else:
                if len(self._buckets) >= self._max_keys:
                    self._buckets.popitem(last=False)
                dq = deque()
                self._buckets[key] = dq

            if len(dq) + weight > limit:
                oldest = dq[0] if dq else ts
                retry_s = max(1, int(math.ceil((oldest + self._window_s) - ts)))
                return False, retry_s

            for _ in range(weight):
                dq.append(ts)
            return True, 0


def _env_rpm(var_name: str, default: int) -> int:
    raw = os.environ.get(var_name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _token_bucket_key(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


def _client_ip_key(scope: dict, headers: dict[bytes, bytes]) -> str:
    xff = headers.get(b"x-forwarded-for", b"").decode("utf-8", "replace").strip()
    if xff:
        first_ip = xff.split(",")[0].strip()
        if first_ip:
            return first_ip[:64]
    client = scope.get("client")
    if isinstance(client, (list, tuple)) and client:
        return str(client[0])[:64]
    return "unknown"


def _clone_remote_safe_server(
    source_mcp: MCPServer,
    omitted_names: frozenset[str],
    *,
    server_key: str,
    remote_instructions: str,
) -> MCPServer:
    """Clone an MCPServer omitting local-only tools while attaching server-scoped prompts, resources, and remote instructions."""
    srv = MCPServer(
        name=source_mcp.name,
        title=getattr(source_mcp, "title", None) or source_mcp.name,
        description=getattr(source_mcp, "description", None) or "",
        instructions=remote_instructions,
        website_url=server_meta.SITE_URL,
        icons=[{"src": server_meta.ICON_URL, "mimeType": "image/png", "sizes": ["192x192"]}],
        version=server_meta.PACKAGE_VERSION,
    )
    spec_by_name = {spec[0]: spec for spec in unified_server._ALL_TOOL_SPECS}
    source_names = [t.name for t in getattr(source_mcp, "_tool_manager").list_tools()]
    for name in source_names:
        if name in omitted_names:
            continue
        _, title, ro, idem, ow, fn = spec_by_name[name]
        srv.tool(
            name=name,
            title=title,
            annotations=server_meta.annotations(read_only=ro, idempotent=idem, open_world=ow),
            structured_output=True,
        )(fn)
    unified_server.attach_server_prompts_and_resources(srv, server_key)
    return srv


SERVERS = {
    "/mcp": unified_server.remote_mcp,
    "/mcp/data": _clone_remote_safe_server(
        data_server.mcp,
        HOSTED_LOCAL_ONLY_TOOLS,
        server_key="data",
        remote_instructions=data_server.REMOTE_INSTRUCTIONS,
    ),
    "/mcp/warden": _clone_remote_safe_server(
        warden_server.mcp,
        HOSTED_LOCAL_ONLY_TOOLS,
        server_key="warden",
        remote_instructions=warden_server.REMOTE_INSTRUCTIONS,
    ),
    "/mcp/gym": _clone_remote_safe_server(
        gym_server.mcp,
        HOSTED_LOCAL_ONLY_TOOLS,
        server_key="gym",
        remote_instructions=gym_server.REMOTE_INSTRUCTIONS,
    ),
}

PUBLIC_PATHS = {"/", "/health", "/healthz"}
INTERNAL_PREFIX = "/internal/"

PROTECTED_RESOURCE = ".well-known/oauth-protected-resource"
TOURNAMENT_RUN_PATH = "/internal/tournament/run"


async def _read_body(receive) -> bytes:
    body = b""
    while True:
        msg = await receive()
        if msg.get("type") == "http.disconnect":
            break
        body += msg.get("body", b"")
        if not msg.get("more_body"):
            break
    return body


def _replay_receive(body: bytes):
    state = {"sent": False}

    async def receive():
        if not state["sent"]:
            state["sent"] = True
            return {"type": "http.request", "body": body, "more_body": False}
        await asyncio.Future()  # pragma: no cover — body is single-shot

    return receive


def _tool_calls(body: bytes) -> list[tuple[str, dict]]:
    """Every (tool name, arguments) pair this JSON-RPC payload tries to call.
    Handles single messages and batches."""
    try:
        payload = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return []
    messages = payload if isinstance(payload, list) else [payload]
    calls: list[tuple[str, dict]] = []
    for msg in messages:
        if not isinstance(msg, dict) or msg.get("method") != "tools/call":
            continue
        params = msg.get("params")
        name = params.get("name") if isinstance(params, dict) else None
        args = params.get("arguments") if isinstance(params, dict) else None
        if isinstance(name, str):
            calls.append((name, args if isinstance(args, dict) else {}))
    return calls


def _gated_tool_name(body: bytes, ent: access.Entitlement) -> str | None:
    """Return the Pro tool name this JSON-RPC payload tries to call, if any.

    Handles single messages and batches; a batch is refused as soon as any
    member targets a gated tool (the batch does not execute).
    """
    for name, _args in _tool_calls(body):
        if not ent.allows_tool(name):
            return name
    return None


def _local_only_tool_name(body: bytes) -> str | None:
    """First local-only tool this payload tries to call on the hosted endpoint."""
    for name, _args in _tool_calls(body):
        if name in HOSTED_LOCAL_ONLY_TOOLS:
            return name
    return None


def _compute_units(body: bytes) -> tuple[int, str | None, str | None]:
    """Total hosted-compute credits this payload costs (per plans.COMPUTE_RATES),
    the first metered tool name, and the first cap violation (refused BEFORE
    any charge — step 28) — 0/None when nothing here is compute-metered."""
    from swarm_mcp import metering

    total = 0
    first: str | None = None
    invalid: str | None = None
    for name, args in _tool_calls(body):
        try:
            metering.assert_within_caps(name, args)
        except ValueError as e:
            invalid = invalid or f"{name}: {e}"
            continue
        units = metering.compute_units(name, args)
        if units > 0:
            total += units
            first = first or name
    return total, first, invalid


def _const_eq(a: str, b: str) -> bool:
    import hmac

    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def _token_from_query(query_string: bytes) -> str | None:
    """Extract the access token from the query string.

    Accepts, in order of preference:
      * ``apiToken=<token>``   (documented remote form)
      * ``api_token=<token>``  (snake_case alias)
      * ``config=<base64 JSON>`` — Smithery's remote gateway forwards the
        connection config as a base64-encoded JSON payload, e.g.
        ``{"apiToken": "..."}``.

    Values arrive percent-encoded; parse_qs decodes them, so tokens with
    reserved characters survive intact.
    """
    params = parse_qs(query_string.decode("utf-8", "replace"))
    for key in ("apiToken", "api_token"):
        values = params.get(key)
        if values and values[0].strip():
            return values[0].strip()
    for raw in params.get("config", []):
        try:
            padded = raw + "=" * (-len(raw) % 4)
            payload = json.loads(base64.b64decode(padded).decode("utf-8"))
        except (ValueError, UnicodeDecodeError, binascii.Error):
            continue
        if not isinstance(payload, dict):
            continue
        tok = payload.get("apiToken") or payload.get("api_token")
        if isinstance(tok, str) and tok.strip():
            return tok.strip()
    return None


def _is_discovery_only_payload(body: bytes) -> bool:
    """True when a JSON-RPC body contains exclusively read-only metadata discovery
    methods (`initialize`, `ping`, `tools/list`, `resources/list`, `prompts/list`),
    allowing directory crawlers to index tools without requiring an access token."""
    if not body:
        return False
    try:
        payload = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return False
    messages = payload if isinstance(payload, list) else [payload]
    if not messages:
        return False
    for msg in messages:
        if not isinstance(msg, dict):
            return False
        if msg.get("method") not in UNAUTHENTICATED_DISCOVERY_METHODS:
            return False
    return True


def _has_explicit_credential_attempt(auth_header: bytes, query_string: bytes) -> bool:
    if auth_header.strip():
        return True
    params = parse_qs(query_string.decode("utf-8", "replace"))
    return any(k in params for k in ("apiToken", "api_token", "config"))


class BearerAuthMiddleware:
    """Validate the per-request token, parse its entitlement, and scope both
    to the request.

    The token may arrive either as `Authorization: Bearer <token>` or via the
    query string (`?apiToken=`, `?api_token=`, or Smithery's base64 `config`
    payload — how Smithery's gateway forwards the apiToken connection
    parameter). Both forms are validated against the site. tools/call
    requests for Pro tools on a non-paid entitlement get a 402 JSON-RPC error
    with the upgrade_url instead of reaching the child server.
    """

    def __init__(self, app):
        self.app = app
        self._token_limiter = SlidingWindowRateLimiter()
        self._heavy_limiter = SlidingWindowRateLimiter()
        self._zero_credit_limiter = SlidingWindowRateLimiter()
        self._discovery_limiter = SlidingWindowRateLimiter()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        path = scope.get("path", "")
        if (path in PUBLIC_PATHS
                or path == f"/{PROTECTED_RESOURCE}"
                or path.startswith(f"/{PROTECTED_RESOURCE}/")
                or path == "/.well-known/oauth-authorization-server"
                or path.startswith("/.well-known/oauth-authorization-server/")
                or path.startswith(INTERNAL_PREFIX)):
            # Internal routes authenticate with the shared secret inside _app.
            return await self.app(scope, receive, send)

        headers = dict(scope.get("headers") or [])
        auth = headers.get(b"authorization", b"")
        qs = scope.get("query_string", b"")
        token = None
        if auth.lower().startswith(b"bearer "):
            token = auth[7:].decode("utf-8", "replace").strip()
        if not token:
            token = _token_from_query(qs)

        has_cred_attempt = _has_explicit_credential_attempt(auth, qs)
        body: bytes | None = None

        # Unauthenticated metadata discovery (initialize, ping, tools/list,
        # resources/list, prompts/list) passes through when no credential was
        # presented so MCP directory crawlers index all tools cleanly.
        if not has_cred_attempt and scope.get("method") == "POST" and path in SERVERS:
            body = await _read_body(receive)
            if _is_discovery_only_payload(body):
                disc_rpm = _env_rpm("SWARM_MCP_DISCOVERY_RPM", DEFAULT_DISCOVERY_RPM)
                allowed, retry_s = self._discovery_limiter.check_and_consume(
                    f"ip:{_client_ip_key(scope, headers)}",
                    limit=disc_rpm,
                )
                if not allowed:
                    return await self._rate_limited(send, "discovery", disc_rpm, retry_s)
                return await self.app(scope, _replay_receive(body), send)
            return await self._deny(send, scope)

        # M-05: async verification (worker thread + negative cache) — the old
        # sync httpx.post here stalled the whole event loop per request.
        ent = await access.verify_entitlement_async(token) if token else None
        if ent is None:
            return await self._deny(send, scope)

        if scope.get("method") == "POST" and path in SERVERS:
            if body is None:
                body = await _read_body(receive)
            local_only = _local_only_tool_name(body)
            if local_only:
                return await self._local_only_refused(send, local_only)
            gated = _gated_tool_name(body, ent)
            if gated:
                return await self._upgrade_required(send, gated, ent)

            # Per-token burst limits run BEFORE credit metering so a throttled
            # request never deducts hosted compute credits. Heavy-tool sub-limits
            # check first so a rejected heavy burst does not consume general RPM.
            calls: list[tuple[str, dict]] = []
            if token and not _is_discovery_only_payload(body):
                calls = _tool_calls(body)
                tok_key = _token_bucket_key(token)
                heavy_count = sum(1 for t_name, _ in calls if _is_heavy_tool(t_name))
                if heavy_count > 0:
                    heavy_rpm = _env_rpm("SWARM_MCP_HEAVY_RPM", DEFAULT_HEAVY_RPM)
                    ok_heavy, retry_heavy = self._heavy_limiter.check_and_consume(
                        f"heavy:{tok_key}",
                        limit=heavy_rpm,
                        weight=heavy_count,
                    )
                    if not ok_heavy:
                        return await self._rate_limited(send, "heavy_tools", heavy_rpm, retry_heavy)

                tok_rpm = _env_rpm("SWARM_MCP_TOKEN_RPM", DEFAULT_TOKEN_RPM)
                ok_tok, retry_tok = self._token_limiter.check_and_consume(
                    f"tok:{tok_key}",
                    limit=tok_rpm,
                    weight=max(1, len(calls)),
                )
                if not ok_tok:
                    return await self._rate_limited(send, "token_requests", tok_rpm, retry_tok)
            # Hosted compute is metered BEFORE it runs: gym replays cost 1 credit
            # per simulated episode, single-shot Pro tools 1 credit. The site
            # decrements the pool and answers with the same structured 402s the
            # relay uses; unreachable meter => refuse (never serve unmetered).
            # Step 28: arguments over the tool caps are refused BEFORE charging
            # so a rejected call never bills credits.
            units, metered_tool, invalid = _compute_units(body)
            if invalid:
                return await self._invalid_args(send, invalid)
            if units > 0 and metered_tool:
                from swarm_mcp import metering

                try:
                    await asyncio.to_thread(metering.charge, token, metered_tool, units)
                except metering.MeterRefused as e:
                    return await self._meter_refused(send, metered_tool, units, e)
            elif token and calls:
                from swarm_mcp import metering

                zero_tools = [t_name for t_name, _ in calls if t_name in metering.ZERO_CREDIT_HOSTED_TOOLS]
                if zero_tools:
                    zero_rpm = _env_rpm("SWARM_MCP_ZERO_CREDIT_RPM", 60)
                    ok_zero, retry_zero = self._zero_credit_limiter.check_and_consume(
                        f"zero:{_token_bucket_key(token)}",
                        limit=zero_rpm,
                        weight=len(zero_tools),
                    )
                    if not ok_zero:
                        return await self._rate_limited(send, "zero_credit_tools", zero_rpm, retry_zero)
                    try:
                        await asyncio.to_thread(metering.record_zero_credit, token, zero_tools[0])
                    except metering.MeterRefused as e:
                        return await self._meter_refused(send, zero_tools[0], 0, e)
            receive = _replay_receive(body)

        tok_ctx = request_context.current_token.set(token)
        ent_ctx = request_context.current_entitlement.set(ent)
        try:
            await self.app(scope, receive, send)
        finally:
            request_context.current_entitlement.reset(ent_ctx)
            request_context.current_token.reset(tok_ctx)

    @staticmethod
    async def _deny(send, scope=None):
        # RFC 6750 challenge carrying the resource metadata so OAuth-capable
        # clients (Claude/ChatGPT connectors, MCP Inspector) discover the AS.
        metadata_url = f"{remote_base_url()}/.well-known/oauth-protected-resource"
        body = json.dumps({
            "jsonrpc": "2.0",
            "error": {
                "code": -32001,
                "message": f"unauthorized - request an access token at {access.SITE_URL}",
            },
        }).encode()
        headers = [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
        ]
        if scope is not None and scope.get("method", "POST") == "POST" and scope.get("path") in SERVERS:
            headers.append((b"www-authenticate",
                            f'Bearer resource_metadata="{metadata_url}"'.encode()))
        await send({
            "type": "http.response.start",
            "status": 401,
            "headers": headers,
        })
        await send({"type": "http.response.body", "body": body})

    @staticmethod
    async def _local_only_refused(send, tool: str):
        body = json.dumps({
            "jsonrpc": "2.0",
            "id": None,
            "error": {
                "code": -32004,
                "message": (f"'{tool}' is a local-only tool — run quant-swarm locally "
                            "(stdio) to use it; the hosted endpoint does not expose it"),
                "data": {"reason": "local_only_tool", "tool": tool},
            },
        }).encode()
        await send({
            "type": "http.response.start",
            "status": 200,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        })
        await send({"type": "http.response.body", "body": body})

    @staticmethod
    async def _invalid_args(send, detail: str):
        body = json.dumps({
            "jsonrpc": "2.0",
            "id": None,
            "error": {
                "code": -32602,
                "message": f"hosted compute arguments out of bounds — {detail}",
                "data": {"reason": "args_exceed_caps", "detail": detail},
            },
        }).encode()
        await send({
            "type": "http.response.start",
            "status": 200,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        })
        await send({"type": "http.response.body", "body": body})

    @staticmethod
    async def _meter_refused(send, tool: str, units: int, err):
        body = json.dumps({
            "jsonrpc": "2.0",
            "id": None,
            "error": {
                "code": -32003,
                "message": (f"'{tool}' needs {units} credits of hosted compute — {err} "
                            f"(manage credits at {err.upgrade_url or access.SITE_URL})"),
                "data": {
                    "reason": err.reason or "compute_refused",
                    "tool": tool,
                    "units": units,
                    "upgrade_url": err.upgrade_url or access.SITE_URL,
                    "quota": err.quota,
                },
            },
        }).encode()
        await send({
            "type": "http.response.start",
            "status": err.status_code,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        })
        await send({"type": "http.response.body", "body": body})

    @staticmethod
    async def _upgrade_required(send, tool: str, ent: access.Entitlement):
        body = json.dumps({
            "jsonrpc": "2.0",
            "id": None,
            "error": {
                "code": -32002,
                "message": (f"'{tool}' requires the Pro plan (current plan: {ent.plan}) — "
                            f"upgrade at {ent.upgrade_url}"),
                "data": {
                    "reason": "plan_required",
                    "tool": tool,
                    "plan": ent.plan,
                    "status": ent.status,
                    "upgrade_url": ent.upgrade_url,
                    "quota": ent.quota,
                },
            },
        }).encode()
        await send({
            "type": "http.response.start",
            "status": 402,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        })
        await send({"type": "http.response.body", "body": body})


    @staticmethod
    async def _rate_limited(send, bucket_scope: str, limit_rpm: int, retry_after_s: int):
        body = json.dumps({
            "jsonrpc": "2.0",
            "id": None,
            "error": {
                "code": -32005,
                "message": (
                    f"rate limit exceeded ({bucket_scope}: max {limit_rpm}/min) — "
                    f"retry after {retry_after_s}s"
                ),
                "data": {
                    "reason": "rate_limited",
                    "scope": bucket_scope,
                    "limit_per_minute": limit_rpm,
                    "retry_after_s": retry_after_s,
                },
            },
        }).encode()
        await send({
            "type": "http.response.start",
            "status": 429,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
                (b"retry-after", str(retry_after_s).encode()),
            ],
        })
        await send({"type": "http.response.body", "body": body})


# Hosts that legitimately reach the service (DNS-rebinding protection stays
# on). The Cloud Run hostname and the Smithery gateway host are included so a
# clean deploy with no env overrides still accepts remote clients.
# SWARM_MCP_ALLOWED_HOSTS EXTENDS this list (it never replaces it), and
# SWARM_MCP_REMOTE_URL adds its host too.
DEFAULT_ALLOWED_HOSTS = [
    "mcp.1.21initiative.com",
    "1.21initiative.com",
    server_meta.LEGACY_CLOUD_RUN_HOST,
    "localhost",
    "127.0.0.1",
    "testserver",
    urlparse(server_meta.REMOTE_BASE_URL).hostname or "swarm-mcp",
    "server.smithery.ai",
]


def remote_base_url() -> str:
    """Hosted endpoint base this instance advertises (env override first)."""
    return os.environ.get("SWARM_MCP_REMOTE_URL", server_meta.REMOTE_BASE_URL).rstrip("/")


def oauth_issuer_url() -> str:
    """Canonical OAuth 2.1 authorization-server issuer URL (origin without path)."""
    return os.environ.get("SWARM_MCP_OAUTH_ISSUER", server_meta.OAUTH_ISSUER).rstrip("/")


def _transport_security():
    """Allow the hosts that legitimately reach the service (DNS-rebinding
    protection stays on). testserver is the starlette TestClient default.
    The env var is additive so operators can never accidentally drop the
    Cloud Run host (which would 421 every remote client)."""
    from mcp.server.transport_security import TransportSecuritySettings

    extra = [h.strip() for h in os.environ.get("SWARM_MCP_ALLOWED_HOSTS", "").split(",") if h.strip()]
    remote_host = urlparse(remote_base_url()).hostname
    if remote_host and remote_host not in extra:
        extra.append(remote_host)
    hosts = list(dict.fromkeys(DEFAULT_ALLOWED_HOSTS + extra))
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True, allowed_hosts=hosts)


def build_app(*, require_approval_secret: bool | None = None) -> BearerAuthMiddleware:
    """Compose the streamable-HTTP apps behind the Bearer auth gate.

    Each child keeps its absolute path (no prefix stripping); the ASGI lifespan
    stream is fanned out to every child so their session managers start and
    stop correctly.
    """
    enforce_secret = (
        require_approval_secret
        if require_approval_secret is not None
        else (os.environ.get("SWARM_MCP_TRANSPORT", "").strip().lower() == "http")
    )
    if enforce_secret:
        audit_tools.get_approval_secret(require_configured=True)
    if os.environ.get("SWARM_MCP_BYOK", "").strip() == "1":
        log.error(
            "SWARM_MCP_BYOK=1 is ignored on the hosted transport; all data goes through the metered relay"
        )

    transport_security = _transport_security()

    children = {}
    for path, srv in SERVERS.items():
        kwargs = {
            "streamable_http_path": path,
            "stateless_http": True,
            "json_response": True,
        }
        sig = inspect.signature(srv.streamable_http_app)
        if "transport_security" in sig.parameters:
            kwargs["transport_security"] = transport_security
        children[path] = srv.streamable_http_app(**kwargs)

    lifespan_tasks: dict = {}

    async def _start_children():
        for key, child in children.items():
            shutdown_event = asyncio.Event()
            started = asyncio.Event()

            async def receive(sent=[False], shutdown=shutdown_event, started=started):
                if not sent[0]:
                    sent[0] = True
                    return {"type": "lifespan.startup"}
                await shutdown.wait()
                return {"type": "lifespan.shutdown"}

            async def send(msg, started=started):
                if msg.get("type") == "lifespan.startup.complete":
                    started.set()

            async def run_child(child=child, receive=receive, send=send):
                await child({"type": "lifespan", "asgi": {"version": "3.0"}, "state": {}},
                            receive, send)

            task = asyncio.create_task(run_child())
            await asyncio.wait_for(started.wait(), timeout=15)
            lifespan_tasks[key] = (task, shutdown_event)

    async def _stop_children():
        for key, (task, shutdown_event) in list(lifespan_tasks.items()):
            shutdown_event.set()
            await task
            lifespan_tasks.pop(key, None)

    async def _handle_lifespan(receive, send):
        while True:
            try:
                msg = await receive()
            except Exception:
                break
            if msg is None:
                break
            if msg.get("type") == "lifespan.startup":
                await _start_children()
                await send({"type": "lifespan.startup.complete"})
            elif msg.get("type") == "lifespan.shutdown":
                await _stop_children()
                await send({"type": "lifespan.shutdown.complete"})
                break

    background: set = set()

    async def _internal_tournament_run(scope, receive, send):
        """Site -> runner dispatch. Shared secret, 202 immediately, score in the
        background and call the site back (see swarm_mcp.tournament_runner)."""
        from swarm_mcp import tournament_runner

        headers = dict(scope.get("headers") or [])
        presented = headers.get(b"x-swarm-internal-key", b"").decode("utf-8", "replace")
        expected = tournament_runner.internal_key()
        if scope.get("method") != "POST":
            return await JSONResponse({"error": "method not allowed"}, status_code=405)(scope, receive, send)
        if not expected or not presented or not _const_eq(presented, expected):
            return await JSONResponse({"error": "unauthorized"}, status_code=401)(scope, receive, send)
        body = await _read_body(receive)
        try:
            job = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            job = None
        if not isinstance(job, dict) or not isinstance(job.get("job_id"), str) or not isinstance(job.get("genome"), dict):
            return await JSONResponse({"error": "job_id and genome required"}, status_code=400)(scope, receive, send)
        task = asyncio.create_task(tournament_runner.run_job(job))
        background.add(task)
        task.add_done_callback(background.discard)
        return await JSONResponse({"accepted": True, "job_id": job["job_id"]}, status_code=202)(
            scope, receive, send)

    async def _app(scope, receive, send):
        if scope["type"] == "lifespan":
            return await _handle_lifespan(receive, send)
        if scope["type"] != "http":
            return

        path = scope.get("path", "")
        if path == TOURNAMENT_RUN_PATH:
            return await _internal_tournament_run(scope, receive, send)
        if path.startswith(INTERNAL_PREFIX):
            return await JSONResponse({"error": "not found"}, status_code=404)(scope, receive, send)
        if path in ("/health", "/healthz"):
            return await JSONResponse({"status": "ok"})(scope, receive, send)
        if path == f"/{PROTECTED_RESOURCE}" or path.startswith(f"/{PROTECTED_RESOURCE}/"):
            # RFC 9728 Protected Resource Metadata: identifies this MCP host as the
            # protected resource and delegates authorization to the site issuer.
            issuer = oauth_issuer_url()
            return await JSONResponse({
                "resource": remote_base_url(),
                "resource_documentation": server_meta.SITE_URL,
                "authorization_servers": [issuer],
                "scopes_supported": ["mcp:tools"],
                "bearer_methods_supported": ["header"],
            })(scope, receive, send)
        if (path == "/.well-known/oauth-authorization-server"
                or path.startswith("/.well-known/oauth-authorization-server/")):
            # RFC 8414 Authorization Server Metadata: mirrors 1.21initiative.com's
            # OAuth 2.1 PKCE metadata so MCP clients probing the MCP host directly
            # receive valid AS endpoints instead of a protected-resource document.
            issuer = oauth_issuer_url()
            return await JSONResponse({
                "issuer": issuer,
                "authorization_endpoint": f"{issuer}/api/mcp/oauth/authorize",
                "token_endpoint": f"{issuer}/api/mcp/oauth/token",
                "revocation_endpoint": f"{issuer}/api/mcp/oauth/revoke",
                "registration_endpoint": f"{issuer}/api/mcp/oauth/register",
                "response_types_supported": ["code"],
                "grant_types_supported": ["authorization_code", "refresh_token"],
                "code_challenge_methods_supported": ["S256"],
                "token_endpoint_auth_methods_supported": ["none"],
                "scopes_supported": ["mcp:tools"],
                "service_documentation": server_meta.SITE_URL,
            })(scope, receive, send)
        if path == "/":
            return await JSONResponse({
                "service": "quant-swarm (remote)",
                "servers": [f"{remote_base_url()}{p}" for p in SERVERS],
                "request_access_at": server_meta.SITE_URL,
            })(scope, receive, send)
        child = children.get(path)
        if child is None:
            return await JSONResponse({"error": "not found"}, status_code=404)(
                scope, receive, send)
        await child(scope, receive, send)

    return BearerAuthMiddleware(_app)


app = build_app()


def main() -> None:
    import uvicorn

    os.environ.setdefault("SWARM_MCP_TRANSPORT", "http")
    audit_tools.get_approval_secret(require_configured=True)
    port = int(os.environ.get("PORT", "8080"))
    # access_log=False: Smithery forwards the token as ?apiToken=/?config=,
    # and uvicorn's default access log line includes the query string — a live
    # token would be written to our log stream. Cloud Run still logs request
    # URLs at the platform layer; those tokens stay revocable and metered, and
    # clients that can use Authorization: Bearer should.
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info", access_log=False)


if __name__ == "__main__":
    main()
