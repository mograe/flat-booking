FROM ghcr.io/astral-sh/uv:0.13.8 AS uv
FROM python:3.13-slim-trixie AS dependencies

COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

FROM python:3.13-slim-trixie
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 10001 flat \
    && useradd --system --uid 10001 --gid flat --no-create-home flat \
    && install -d -o flat -g flat -m 0750 /app/data \
    && test -r /usr/share/zoneinfo/UTC

COPY --from=dependencies /app/.venv /app/.venv
COPY app ./app
COPY migrations ./migrations
COPY scripts ./scripts
COPY alembic.ini LICENSE ./
# Seed the named volume with a directory that preserves the application owner.
COPY --chown=10001:10001 data/.gitkeep /app/data/.gitkeep
USER 10001:10001
CMD ["sh", "-c", "umask 027 && alembic upgrade head && exec python -m app.main"]
