FROM python:3.12-slim

WORKDIR /app
COPY . .

RUN pip install --no-cache-dir ".[remote]"

ENV PYTHONUNBUFFERED=1
ENV SWARM_MCP_CACHE_DB=/tmp/swarm-mcp-cache/cache.db

EXPOSE 8080
CMD ["python", "-m", "swarm_mcp.servers.http_server"]
