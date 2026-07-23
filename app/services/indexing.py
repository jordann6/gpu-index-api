"""Composite index: dollars per unit of throughput, ranked across providers.

This is the expensive derived query and the main reason the cache exists.
"""

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

INDEX_SQL = """
WITH best_price AS (
    SELECT DISTINCT ON (pp.gpu_model_id)
           pp.gpu_model_id,
           pp.provider_id,
           pp.region,
           pp.usd_per_hour
    FROM price_points pp
    WHERE pp.observed_at >= :since
      AND pp.availability = 'available'
    ORDER BY pp.gpu_model_id, pp.usd_per_hour ASC
),
best_bench AS (
    SELECT DISTINCT ON (br.gpu_model_id)
           br.gpu_model_id,
           br.throughput,
           br.precision
    FROM benchmark_runs br
    WHERE br.workload = :workload
      -- Explicit cast: asyncpg cannot infer a type for a bare NULL parameter.
      AND ((:precision)::text IS NULL OR br.precision = (:precision)::text)
    ORDER BY br.gpu_model_id, br.throughput DESC
)
SELECT g.model_name AS gpu_model,
       g.vendor,
       :workload AS workload,
       bb.precision,
       bp.usd_per_hour::float8 AS best_usd_per_hour,
       bb.throughput::float8 AS throughput,
       ((bp.usd_per_hour / NULLIF(bb.throughput, 0)) * 1000000)::float8
           AS usd_per_million_units,
       pr.name AS provider,
       bp.region
FROM best_bench bb
JOIN best_price bp ON bp.gpu_model_id = bb.gpu_model_id
JOIN gpu_models g ON g.id = bb.gpu_model_id
JOIN providers pr ON pr.id = bp.provider_id
WHERE bb.throughput > 0
ORDER BY usd_per_million_units ASC
LIMIT :limit
"""


async def compute_index(
    session: AsyncSession,
    workload: str,
    precision: str | None = None,
    window_days: int = 30,
    limit: int = 50,
) -> list[dict[str, Any]]:
    since = datetime.now(UTC) - timedelta(days=window_days)
    result = await session.execute(
        text(INDEX_SQL),
        {
            "workload": workload,
            "precision": precision,
            "since": since,
            "limit": limit,
        },
    )
    return [dict(row) for row in result.mappings()]
