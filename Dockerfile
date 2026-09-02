FROM python:3.12-slim

WORKDIR /app
COPY . .

RUN pip install --no-cache-dir ".[remote]"

ENV PYTHONUNBUFFERED=1
ENV SWARM_MCP_CACHE_DB=/tmp/swarm-mcp-cache/cache.db
# DNS-rebinding protection validates the Host header. The defaults already
# allow the Cloud Run hostname, 1.21initiative.com, and the Smithery gateway;
# SWARM_MCP_ALLOWED_HOSTS (comma-separated) EXTENDS the list for a custom
# domain or gateway — it never replaces the defaults. SWARM_MCP_REMOTE_URL
# overrides the advertised endpoint base, and SWARM_MCP_TOKEN_VERIFY_URL the
# verify endpoint.

EXPOSE 8080
CMD ["python", "-m", "swarm_mcp.servers.http_server"]
