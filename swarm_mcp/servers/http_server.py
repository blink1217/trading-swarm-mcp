"""Remote streamable-HTTP MCP servers (Cloud Run).

Exposes the three swarm MCP servers over Streamable HTTP for remote agents:
  /mcp/data    -> swarm-data-mcp   (bars, enrichment, feature building)
  /mcp/warden  -> swarm-warden-mcp (invariant + leakage auditing)
  /mcp/gym     -> swarm-gym-mcp    (regime fragility probing)

Every request must carry `Authorization: Bearer <site token>` (issued at
https://1.21initiative.com/). The middleware validates it against the site's
verify endpoint (300s in-process cache) and scopes the token to the request so
the hosted data relay meters per-prospect. Fail closed: no token or a rejected
token gets a 401 (per the MCP auth spec this also triggers OAuth discovery).

Run: python -m swarm_mcp.servers.http_server  (PORT env, default 8080)
"""
from __future__ import annotations

import inspect
import json
import os

from starlette.responses import JSONResponse

from swarm_mcp import access, request_context
from swarm_mcp.servers import data_server, gym_server, warden_server

SERVERS = {
    "/mcp/data": data_server.mcp,
    "/mcp/warden": warden_server.mcp,
    "/mcp/gym": gym_server.mcp,
}

PUBLIC_PATHS = {"/", "/health"}


class BearerAuthMiddleware:
    """Validate the per-request token and scope it to the request.

    The token may arrive either as `Authorization: Bearer <token>` or as the
    `?apiToken=<token>` query parameter (how Smithery's gateway forwards the
    apiToken connection parameter). Both are validated against the site.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        path = scope.get("path", "")
        if path in PUBLIC_PATHS:
            return await self.app(scope, receive, send)

        headers = dict(scope.get("headers") or [])
        auth = headers.get(b"authorization", b"")
        token = None
        if auth.lower().startswith(b"bearer "):
            token = auth[7:].decode("utf-8", "replace").strip()
        if not token:
            query = scope.get("query_string", b"").decode("utf-8", "replace")
            params = dict(
                part.split("=", 1) for part in query.split("&") if "=" in part
            )
            token = params.get("apiToken", "").strip()
        if not token or not access.validate_token(token):
            return await self._deny(send)

        ctx = request_context.current_token.set(token)
        try:
            await self.app(scope, receive, send)
        finally:
            request_context.current_token.reset(ctx)

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


def _transport_security():
    """Allow the hosts that legitimately reach the service (DNS-rebinding
    protection stays on). testserver is the starlette TestClient default."""
    from mcp.server.transport_security import TransportSecuritySettings

    raw = os.environ.get(
        "SWARM_MCP_ALLOWED_HOSTS",
        "1.21initiative.com,localhost,127.0.0.1,testserver",
    )
    hosts = [h.strip() for h in raw.split(",") if h.strip()]
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True, allowed_hosts=hosts)


def build_app() -> BearerAuthMiddleware:
    """Compose the three streamable-HTTP apps behind the Bearer auth gate.

    Each child keeps its absolute path (no prefix stripping); the ASGI lifespan
    stream is fanned out to every child so their session managers start and
    stop correctly.
    """
    import asyncio

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

    async def _app(scope, receive, send):
        if scope["type"] == "lifespan":
            return await _handle_lifespan(receive, send)
        if scope["type"] != "http":
            return

        path = scope.get("path", "")
        if path == "/health":
            return await JSONResponse({"status": "ok"})(scope, receive, send)
        if path == "/":
            return await JSONResponse({
                "service": "trading-swarm-mcp (remote)",
                "servers": [f"https://1.21initiative.com{p}" for p in SERVERS],
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
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")


if __name__ == "__main__":
    main()
