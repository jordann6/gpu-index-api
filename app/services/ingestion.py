"""Concurrent ingestion.

The fan-out is bounded by a semaphore so a wide provider list cannot exhaust
the connection pool. docs/query-tuning.md records the async vs sequential
measurement produced by scripts/bench_ingest.py.
"""

import asyncio
from collections.abc import Sequence
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.models import GpuModel, Provider
from app.schemas.models import PricePointIn


async def _resolve_lookup(session: AsyncSession) -> tuple[dict, dict]:
    gpus = {row.model_name: row.id for row in (await session.execute(select(GpuModel))).scalars()}
    providers = {row.name: row.id for row in (await session.execute(select(Provider))).scalars()}
    return gpus, providers


async def ingest_prices(
    session: AsyncSession, payload: Sequence[PricePointIn]
) -> tuple[int, int, list[str]]:
    """Upsert observations, skipping conflicts on the natural key."""
    if not payload:
        return 0, 0, []

    gpus, providers = await _resolve_lookup(session)

    rows: list[dict[str, Any]] = []
    errors: list[str] = []

    for item in payload:
        gpu_id = gpus.get(item.gpu_model_name)
        provider_id = providers.get(item.provider_name)
        if gpu_id is None:
            errors.append(f"unknown gpu_model_name: {item.gpu_model_name}")
            continue
        if provider_id is None:
            errors.append(f"unknown provider_name: {item.provider_name}")
            continue
        rows.append(
            {
                "gpu_model_id": gpu_id,
                "provider_id": provider_id,
                "region": item.region,
                "instance_type": item.instance_type,
                "usd_per_hour": item.usd_per_hour,
                "availability": item.availability,
                "observed_at": item.observed_at,
            }
        )

    if not rows:
        return 0, len(errors), errors

    from app.db.models import PricePoint

    stmt = insert(PricePoint).values(rows).on_conflict_do_nothing(constraint="uq_price_observation")
    await session.execute(stmt)
    await session.commit()

    return len(rows), len(errors), errors


async def fan_out_fetch(fetchers: Sequence[Any], concurrency: int | None = None) -> list[Any]:
    """Run provider fetches concurrently under a bounded semaphore.

    Exceptions are returned alongside successes so one bad feed does not sink
    the whole ingestion run.
    """
    limit = concurrency or get_settings().ingest_concurrency
    sem = asyncio.Semaphore(limit)

    async def guarded(fn):
        async with sem:
            return await fn()

    return await asyncio.gather(*(guarded(f) for f in fetchers), return_exceptions=True)


async def table_counts(session: AsyncSession) -> dict[str, int]:
    # Column names avoid `t`/`c`: single letters collide with SQLAlchemy's
    # deprecated Row tuple accessors. Read via .mappings() to be explicit.
    result = await session.execute(
        text(
            """
            SELECT 'gpu_models' AS table_name, count(*) AS row_count FROM gpu_models
            UNION ALL SELECT 'providers', count(*) FROM providers
            UNION ALL SELECT 'price_points', count(*) FROM price_points
            UNION ALL SELECT 'benchmark_runs', count(*) FROM benchmark_runs
            """
        )
    )
    return {row["table_name"]: row["row_count"] for row in result.mappings()}
