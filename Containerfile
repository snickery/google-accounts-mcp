FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim

# Pull in Debian security updates the base image lags behind on
# (rebuild with --pull --no-cache to refresh this layer).
RUN apt-get update && apt-get -y upgrade \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# README.md + LICENSE are build-time inputs: pyproject declares
# readme/license-files, so hatchling refuses to build the wheel without them.
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN uv sync --frozen --no-dev

COPY src/ src/

VOLUME /data
EXPOSE 8321

ENV UV_PROJECT_ENVIRONMENT=/app/.venv
CMD ["uv", "run", "google-accounts-mcp"]
