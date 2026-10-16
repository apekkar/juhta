# AgentCore Runtime container for Strategist (/invocations + /ping on 8080, arm64).

FROM --platform=linux/arm64 python:3.14-slim

WORKDIR /app

RUN apt-get update && apt-get install -y \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv
# GitHub MCP server, pinned: tool output shapes change between versions.
COPY --from=ghcr.io/github/github-mcp-server:v2.0.2 /server/github-mcp-server /usr/local/bin/github-mcp-server
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-default-groups --no-cache
ENV PATH="/app/.venv/bin:$PATH"

COPY strategist/ /app/strategist/

ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app

EXPOSE 8080

# ADOT: exports OpenTelemetry spans to CloudWatch.
CMD ["opentelemetry-instrument", "python", "-m", "strategist.server"]
