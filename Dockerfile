# Build stage. uv resolves from the lockfile into a virtualenv at /app/.venv.
FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependencies in a layer of their own, so editing the application does not
# reinstall them on every build.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY wima/ ./wima/
RUN uv sync --frozen --no-dev

# Runtime stage. No uv and no build tooling, just the interpreter and the venv.
FROM python:3.14-slim-bookworm

# One mount holds everything that is not in this image: the database, the
# accounts file, and the private key. Backing up /data backs up the whole
# service, and restoring it restores the whole service.
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    WIMA_HOST=0.0.0.0 \
    WIMA_PORT=8081 \
    WIMA_DB=/data/balances.db \
    WIMA_ACCOUNTS=/data/accounts.toml \
    EB_KEY_PATH=/data/private-key.pem

RUN useradd --create-home --uid 1000 wima \
    && mkdir -p /data \
    && chown wima:wima /data

WORKDIR /app
COPY --from=builder --chown=wima:wima /app /app

USER wima
EXPOSE 8081

# urllib rather than curl, which a slim image does not carry.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8081/api/status').read()" \
    || exit 1

# Serves the page and hosts the collector loop. One process.
CMD ["python", "-m", "wima.web"]
