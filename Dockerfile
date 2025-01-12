# syntax=docker/dockerfile:1
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    COST_FIREWALL_CONFIG=/config/config.toml

WORKDIR /app

# Dependencies first so application edits do not invalidate this layer.
COPY pyproject.toml README.md ./
COPY src ./src
RUN python -m pip install --no-cache-dir .

# Unprivileged runtime user; the ledger lives in a volume it owns.
RUN useradd --create-home --uid 10001 app \
    && mkdir -p /data \
    && chown -R app:app /data
USER app

EXPOSE 8787
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8787/healthz', timeout=4).status == 200 else 1)"

ENTRYPOINT ["cost-firewall"]
CMD ["serve", "--config", "/config/config.toml", "--host", "0.0.0.0", "--port", "8787"]
