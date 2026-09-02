"""Remote streamable-HTTP MCP servers (Cloud Run).

Exposes the three swarm MCP servers over Streamable HTTP for remote agents:
  /mcp/data    -> swarm-data-mcp   (bars, enrichment, feature building)
  /mcp/warden  -> swarm-warden-mcp (invariant + leakage auditing)
  /mcp/gym     -> swarm-gym-mcp    (regime fragility probing)

Every request must carry `Authorization: Bearer <site token>` (issued at
https://1.21initiative.com/). The middleware validates it against the site's
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
import inspect
import json
import os
from urllib.parse import parse_qs, urlparse

from starlette.responses import JSONResponse

from swarm_mcp import access, request_context, server_meta
from swarm_mcp.servers import data_server, gym_server, warden_server

SERVERS = {
    "/mcp/data": data_server.mcp,
    "/mcp/warden": warden_server.mcp,
    "/mcp/gym": gym_server.mcp,
}

PUBLIC_PATHS = {"/", "/health"}
INTERNAL_PREFIX = "/internal/"
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


def _compute_units(body: bytes) -> tuple[int, str | None]:
    """Total hosted-compute credits this payload costs (per plans.COMPUTE_RATES)
    and the first metered tool name — 0 when nothing here is compute-metered."""
    from swarm_mcp import metering

    total = 0
    first: str | None = None
    for name, args in _tool_calls(body):
        units = metering.compute_units(name, args)
        if units > 0:
            total += units
            first = first or name
    return total, first


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

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        path = scope.get("path", "")
        if path in PUBLIC_PATHS or path.startswith(INTERNAL_PREFIX):
            # Internal routes authenticate with the shared secret inside _app.
            return await self.app(scope, receive, send)

        headers = dict(scope.get("headers") or [])
        auth = headers.get(b"authorization", b"")
        token = None
        if auth.lower().startswith(b"bearer "):
            token = auth[7:].decode("utf-8", "replace").strip()
        if not token:
            token = _token_from_query(scope.get("query_string", b""))
        ent = access.verify_entitlement(token) if token else None
        if ent is None:
            return await self._deny(send)

        if scope.get("method") == "POST" and path in SERVERS:
            body = await _read_body(receive)
            gated = _gated_tool_name(body, ent)
            if gated:
                return await self._upgrade_required(send, gated, ent)
            # Hosted compute is metered BEFORE it runs: gym replays cost 1 credit
            # per simulated episode, single-shot Pro tools 1 credit. The site
            # decrements the pool and answers with the same structured 402s the
            # relay uses; unreachable meter => refuse (never serve unmetered).
            units, metered_tool = _compute_units(body)
            if units > 0 and metered_tool:
                from swarm_mcp import metering

                try:
                    await asyncio.to_thread(metering.charge, token, metered_tool, units)
                except metering.MeterRefused as e:
                    return await self._meter_refused(send, metered_tool, units, e)
            receive = _replay_receive(body)

        tok_ctx = request_context.current_token.set(token)
        ent_ctx = request_context.current_entitlement.set(ent)
        try:
            await self.app(scope, receive, send)
        finally:
            request_context.current_entitlement.reset(ent_ctx)
            request_context.current_token.reset(tok_ctx)

    @staticmethod
    async def _deny(send):
        body = json.dumps({
            "jsonrpc": "2.0",
            "error": {
                "code": -32001,
                "message": f"unauthorized — request an access token at {access.SITE_URL}",
            },
        }).encode()
        await send({
            "type": "http.response.start",
            "status": 401,
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


# Hosts that legitimately reach the service (DNS-rebinding protection stays
# on). The Cloud Run hostname and the Smithery gateway host are included so a
# clean deploy with no env overrides still accepts remote clients.
# SWARM_MCP_ALLOWED_HOSTS EXTENDS this list (it never replaces it), and
# SWARM_MCP_REMOTE_URL adds its host too.
DEFAULT_ALLOWED_HOSTS = [
    "1.21initiative.com",
    "localhost",
    "127.0.0.1",
    "testserver",
    urlparse(server_meta.REMOTE_BASE_URL).hostname or "swarm-mcp",
    "server.smithery.ai",
]


def remote_base_url() -> str:
    """Hosted endpoint base this instance advertises (env override first)."""
    return os.environ.get("SWARM_MCP_REMOTE_URL", server_meta.REMOTE_BASE_URL).rstrip("/")


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


def build_app() -> BearerAuthMiddleware:
    """Compose the three streamable-HTTP apps behind the Bearer auth gate.

    Each child keeps its absolute path (no prefix stripping); the ASGI lifespan
    stream is fanned out to every child so their session managers start and
    stop correctly.
    """
    transport_security = _transport_security()

    children = {}
    for path, srv in SERVERS.items():
        kwargs = {"streamable_http_path": path}
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
        if path == "/health":
            return await JSONResponse({"status": "ok"})(scope, receive, send)
        if path == "/":
            return await JSONResponse({
                "service": "quant-swarm (remote)",
                "servers": [f"{remote_base_url()}{p}" for p in SERVERS],
                "request_access_at": access.SITE_URL,
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

    port = int(os.environ.get("PORT", "8080"))
    # access_log=False: Smithery forwards the token as ?apiToken=/?config=,
    # and uvicorn's default access log line includes the query string — a live
    # token would be written to our log stream. Cloud Run still logs request
    # URLs at the platform layer; those tokens stay revocable and metered, and
    # clients that can use Authorization: Bearer should.
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info", access_log=False)


if __name__ == "__main__":
    main()
