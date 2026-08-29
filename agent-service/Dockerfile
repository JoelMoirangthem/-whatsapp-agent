# Agent service — Python 3.12 + uv, non-root
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.8 /uv /usr/local/bin/uv

WORKDIR /srv/app

# Dependency layer first (cached until lockfile changes)
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY app ./app
RUN uv sync --frozen --no-dev

RUN useradd --uid 10001 --system --home-dir /nonexistent agent \
    && mkdir -p /srv/app/data \
    && chown -R agent:agent /srv/app

ENV AGENT_DB_PATH=/srv/app/data/agent.db \
    AGENT_TOKEN_FILE=/srv/app/data/.agent-token

USER agent
EXPOSE 8100

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s \
    CMD ["python", "-c", "import socket;s=socket.create_connection(('127.0.0.1',8100),3);s.close()"]

CMD ["/srv/app/.venv/bin/uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8100"]
