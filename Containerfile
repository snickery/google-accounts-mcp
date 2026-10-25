FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim

# Pull in Debian security updates the base image lags behind on
# (rebuild with --pull --no-cache to refresh this layer).
RUN apt-get update && apt-get -y upgrade \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# README.md + LICENSE are build-time inputs: pyproject declares
# readme/license-files, so hatchling refuses to build the wheel without them.
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN uv sync --frozen --no-dev --no-install-project

# Install the project only once src/ is present. Syncing it before the COPY
# baked an empty install into the image, and `uv run` then rebuilt it at every
# container start.
COPY src/ src/
RUN uv sync --frozen --no-dev

VOLUME /data
EXPOSE 8321

ENV UV_PROJECT_ENVIRONMENT=/app/.venv
# The server itself is PID 1, not a resident `uv run` parent (~40 MiB).
CMD ["/app/.venv/bin/google-accounts-mcp"]
