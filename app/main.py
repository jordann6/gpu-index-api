import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlalchemy import text

from app.api.v1.routes import router as v1_router
from app.core import cache
from app.core.config import get_settings
from app.db.session import dispose_engine, get_sessionmaker

settings = get_settings()
logging.basicConfig(level=settings.log_level)
log = logging.getLogger(settings.app_name)


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("starting %s in %s", settings.app_name, settings.environment)
    yield
    await cache.close_client()
    await dispose_engine()


app = FastAPI(
    title="GPU Index API",
    description="Pricing intelligence and benchmark index for GPU compute.",
    version="1.0.0",
    lifespan=lifespan,
)

app.include_router(v1_router)


@app.exception_handler(RequestValidationError)
async def validation_handler(request: Request, exc: RequestValidationError):
    """Stable error envelope. Clients should not have to parse two shapes."""
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={
            "error": "validation_error",
            "detail": [
                {"field": ".".join(str(p) for p in e["loc"]), "message": e["msg"]}
                for e in exc.errors()
            ],
        },
    )


@app.get("/healthz", tags=["ops"])
async def healthz() -> dict[str, str]:
    """Liveness. Never touches dependencies."""
    return {"status": "ok"}


@app.get("/readyz", tags=["ops"])
async def readyz() -> JSONResponse:
    """Readiness. Fails the ALB health check if a dependency is down."""
    checks: dict[str, str] = {}
    healthy = True

    try:
        async with get_sessionmaker()() as session:
            await session.execute(text("SELECT 1"))
        checks["postgres"] = "ok"
    except Exception as exc:
        checks["postgres"] = f"error: {type(exc).__name__}"
        healthy = False

    try:
        await cache.get_client().ping()
        checks["redis"] = "ok"
    except Exception as exc:
        checks["redis"] = f"error: {type(exc).__name__}"
        healthy = False

    return JSONResponse(
        status_code=200 if healthy else 503,
        content={"status": "ready" if healthy else "degraded", "checks": checks},
    )


@app.get("/metrics", response_class=PlainTextResponse, tags=["ops"])
async def metrics() -> str:
    """Prometheus text format. Cache hit ratio is the number that matters."""
    hits = cache.STATS["hits"]
    misses = cache.STATS["misses"]
    total = hits + misses
    ratio = (hits / total) if total else 0.0

    return "\n".join(
        [
            "# HELP cache_hits_total Read-through cache hits.",
            "# TYPE cache_hits_total counter",
            f"cache_hits_total {hits}",
            "# HELP cache_misses_total Read-through cache misses.",
            "# TYPE cache_misses_total counter",
            f"cache_misses_total {misses}",
            "# HELP cache_errors_total Redis errors degraded to direct reads.",
            "# TYPE cache_errors_total counter",
            f"cache_errors_total {cache.STATS['errors']}",
            "# HELP cache_hit_ratio Hits over total lookups.",
            "# TYPE cache_hit_ratio gauge",
            f"cache_hit_ratio {ratio:.4f}",
            "",
        ]
    )
