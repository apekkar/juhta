# AgentCore Runtime container for Coordinator (HTTP /invocations + /ping on 8080, linux/arm64).
# Local test commands: see the root README.

FROM --platform=linux/arm64 python:3.14-slim

WORKDIR /app

RUN apt-get update && apt-get install -y \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv
# GitHub's official MCP server (single Go binary), run by Coordinator over stdio.
# Pinned: v2 changed tool output shapes (issue_read wraps the issue); bump deliberately.
COPY --from=ghcr.io/github/github-mcp-server:v2.0.2 /server/github-mcp-server /usr/local/bin/github-mcp-server
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-default-groups --no-cache
ENV PATH="/app/.venv/bin:$PATH"

COPY coordinator/ /app/coordinator/

ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app

EXPOSE 8080

# ADOT launcher: exports Strands' OpenTelemetry spans (incl. token usage) to CloudWatch.
CMD ["opentelemetry-instrument", "python", "-m", "coordinator.server"]
