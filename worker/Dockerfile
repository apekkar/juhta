# AgentCore Runtime container for the Worker, with git, uv and Node for the repo's checks.

FROM --platform=linux/arm64 python:3.14-slim

WORKDIR /app

RUN apt-get update && apt-get install -y \
    build-essential \
    git \
    && rm -rf /var/lib/apt/lists/*

# Node: dashboard checks and CDK's jsii.
COPY --from=node:24-slim /usr/local/bin/node /usr/local/bin/node
COPY --from=node:24-slim /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx

COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv
# GitHub MCP server, pinned: tool output shapes change between versions.
COPY --from=ghcr.io/github/github-mcp-server:v2.0.2 /server/github-mcp-server /usr/local/bin/github-mcp-server
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-default-groups --no-cache
ENV PATH="/app/.venv/bin:$PATH"

COPY worker/ /app/worker/

ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app

EXPOSE 8080

# ADOT: exports OpenTelemetry spans to CloudWatch.
CMD ["opentelemetry-instrument", "uvicorn", "worker.app:app", "--host", "0.0.0.0", "--port", "8080"]
