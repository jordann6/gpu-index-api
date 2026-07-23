# Multi-stage. Build deps stay out of the runtime image.
FROM python:3.12-slim AS builder

WORKDIR /build
RUN apt-get update && apt-get install -y --no-install-recommends gcc && \
    rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
RUN pip install --no-cache-dir --prefix=/install \
    "fastapi>=0.115.0" "uvicorn[standard]>=0.32.0" "pydantic>=2.9.0" \
    "pydantic-settings>=2.6.0" "sqlalchemy[asyncio]>=2.0.36" \
    "alembic>=1.14.0" "asyncpg>=0.30.0" "redis>=5.2.0" "httpx>=0.27.0"

FROM python:3.12-slim AS runtime

# Non-root. The task definition also sets a read-only root filesystem.
RUN useradd --create-home --uid 10001 appuser

WORKDIR /app

# Python puts the *script's* directory on sys.path, not the working directory,
# so `python scripts/seed.py` cannot import `app` without this. uvicorn adds
# cwd itself, which is why the API worked while the scripts did not.
ENV PYTHONPATH=/app

COPY --from=builder /install /usr/local
COPY app ./app
COPY scripts ./scripts
# Migrations ship in the image so seeding and schema upgrades can run as
# one-off ECS tasks inside the VPC, where RDS is not publicly reachable.
COPY migrations ./migrations
COPY alembic.ini ./alembic.ini

USER appuser
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz').status==200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
