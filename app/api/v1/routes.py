from typing import Annotated

import redis.asyncio as redis
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.cache import cache_dependency, invalidate_prefix, read_through
from app.core.config import Settings, get_settings
from app.core.security import Principal, rate_limit
from app.db.models import BenchmarkRun, GpuModel
from app.db.session import session_dependency
from app.schemas.models import (
    BenchmarkOut,
    GpuModelOut,
    IndexEntryOut,
    IngestResult,
    Page,
    PricePointIn,
    PricePointOut,
    PriceSummaryOut,
)
from app.services import indexing, ingestion, pricing

router = APIRouter(prefix="/v1")

SessionDep = Annotated[AsyncSession, Depends(session_dependency)]
CacheDep = Annotated[redis.Redis, Depends(cache_dependency)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
AuthDep = Annotated[Principal, Depends(rate_limit)]


@router.get("/gpus", response_model=Page[GpuModelOut], tags=["gpus"])
async def list_gpus(
    session: SessionDep,
    settings: SettingsDep,
    _: AuthDep,
    vendor: str | None = None,
    min_vram_gb: int | None = Query(default=None, ge=0),
    limit: int = Query(default=50, ge=1, le=500),
) -> Page[GpuModelOut]:
    stmt = select(GpuModel)
    if vendor:
        stmt = stmt.where(GpuModel.vendor == vendor)
    if min_vram_gb is not None:
        stmt = stmt.where(GpuModel.vram_gb >= min_vram_gb)

    rows = (await session.execute(stmt.order_by(GpuModel.model_name).limit(limit))).scalars().all()

    return Page(items=[GpuModelOut.model_validate(r) for r in rows], count=len(rows))


async def _load_gpu(session: AsyncSession, gpu_id: int) -> GpuModel:
    gpu = await session.get(GpuModel, gpu_id)
    if gpu is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"No GPU model {gpu_id}.")
    return gpu


@router.get("/gpus/{gpu_id}/prices", response_model=PriceSummaryOut, tags=["prices"])
async def gpu_prices(
    gpu_id: int,
    session: SessionDep,
    cache: CacheDep,
    settings: SettingsDep,
    _: AuthDep,
    availability: str | None = Query(default=None),
    window_days: int = Query(default=30, ge=1, le=365),
) -> PriceSummaryOut:
    """The hot path. Read-through cached, index-backed."""
    gpu = await _load_gpu(session, gpu_id)
    key = f"prices:{gpu_id}:{availability or 'all'}:{window_days}"

    async def produce():
        return await pricing.latest_prices(session, gpu_id, window_days, availability, limit=200)

    rows, _hit = await read_through(cache, key, produce)

    if not rows:
        return PriceSummaryOut(
            gpu_model=gpu.model_name,
            region_count=0,
            provider_count=0,
            cheapest_usd_per_hour=0.0,
            median_30d_usd_per_hour=None,
            points=[],
        )

    return PriceSummaryOut(
        gpu_model=gpu.model_name,
        region_count=len({r["region"] for r in rows}),
        provider_count=len({r["provider"] for r in rows}),
        cheapest_usd_per_hour=min(r["usd_per_hour"] for r in rows),
        median_30d_usd_per_hour=rows[0].get("median_30d"),
        points=[PricePointOut(**{k: r[k] for k in PricePointOut.model_fields}) for r in rows],
    )


@router.get("/gpus/{gpu_id}/prices/history", response_model=Page[PricePointOut], tags=["prices"])
async def gpu_price_history(
    gpu_id: int,
    session: SessionDep,
    settings: SettingsDep,
    _: AuthDep,
    cursor: str | None = None,
    limit: int = Query(default=50, ge=1, le=500),
) -> Page[PricePointOut]:
    """Keyset paginated. Depth does not degrade the plan."""
    await _load_gpu(session, gpu_id)
    rows, next_cursor = await pricing.price_page(session, gpu_id, limit, cursor)
    return Page(
        items=[PricePointOut(**{k: r[k] for k in PricePointOut.model_fields}) for r in rows],
        count=len(rows),
        next_cursor=next_cursor,
    )


@router.get("/gpus/{gpu_id}/benchmarks", response_model=Page[BenchmarkOut], tags=["benchmarks"])
async def gpu_benchmarks(
    gpu_id: int,
    session: SessionDep,
    _: AuthDep,
    workload: str | None = None,
    precision: str | None = None,
    limit: int = Query(default=50, ge=1, le=500),
) -> Page[BenchmarkOut]:
    await _load_gpu(session, gpu_id)
    stmt = select(BenchmarkRun).where(BenchmarkRun.gpu_model_id == gpu_id)
    if workload:
        stmt = stmt.where(BenchmarkRun.workload == workload)
    if precision:
        stmt = stmt.where(BenchmarkRun.precision == precision)

    rows = (
        (await session.execute(stmt.order_by(BenchmarkRun.run_at.desc()).limit(limit)))
        .scalars()
        .all()
    )

    return Page(items=[BenchmarkOut.model_validate(r) for r in rows], count=len(rows))


@router.get("/index/{workload}", response_model=Page[IndexEntryOut], tags=["index"])
async def workload_index(
    workload: str,
    session: SessionDep,
    cache: CacheDep,
    _: AuthDep,
    precision: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
) -> Page[IndexEntryOut]:
    """Price-per-throughput ranking. Expensive, so always cached."""
    key = f"index:{workload}:{precision or 'any'}:{limit}"

    async def produce():
        return await indexing.compute_index(session, workload, precision, limit=limit)

    rows, _hit = await read_through(cache, key, produce)
    return Page(items=[IndexEntryOut(**r) for r in rows], count=len(rows))


@router.post(
    "/ingest/prices",
    response_model=IngestResult,
    status_code=status.HTTP_202_ACCEPTED,
    tags=["ingest"],
)
async def ingest(
    payload: list[PricePointIn],
    session: SessionDep,
    cache: CacheDep,
    _: AuthDep,
) -> IngestResult:
    if len(payload) > 5000:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="Batch limit is 5000 observations.",
        )

    accepted, rejected, errors = await ingestion.ingest_prices(session, payload)
    invalidated = await invalidate_prefix(cache, "prices:")
    invalidated += await invalidate_prefix(cache, "index:")

    return IngestResult(
        accepted=accepted,
        rejected=rejected,
        cache_entries_invalidated=invalidated,
        errors=errors[:20],
    )
