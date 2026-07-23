"""Price query service.

The queries here are the ones measured in docs/query-tuning.md. Keep the SQL
explicit rather than ORM-generated so the plans stay legible and the tuning
work is reproducible.
"""

import base64
import json
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# Latest observation per provider/region for one GPU model, with a 30 day
# rolling median. DISTINCT ON is the Postgres-idiomatic latest-per-group and
# maps directly onto the (gpu_model_id, region, observed_at DESC) index.
LATEST_PRICES_SQL = """
WITH latest AS (
    SELECT DISTINCT ON (pp.provider_id, pp.region)
           pp.provider_id,
           pp.region,
           pp.instance_type,
           pp.usd_per_hour,
           pp.availability,
           pp.observed_at
    FROM price_points pp
    WHERE pp.gpu_model_id = :gpu_model_id
      AND pp.observed_at >= :since
      {availability_clause}
    ORDER BY pp.provider_id, pp.region, pp.observed_at DESC
),
median AS (
    SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY usd_per_hour) AS med
    FROM price_points
    WHERE gpu_model_id = :gpu_model_id
      AND observed_at >= :since
)
SELECT pr.name AS provider,
       l.region,
       l.instance_type,
       l.usd_per_hour::float8 AS usd_per_hour,
       l.availability,
       l.observed_at,
       (SELECT med FROM median)::float8 AS median_30d
FROM latest l
JOIN providers pr ON pr.id = l.provider_id
ORDER BY l.usd_per_hour ASC, l.region ASC
LIMIT :limit
"""

# Keyset pagination. Ordering on (observed_at, id) keeps the sort stable and
# lets the index seek directly to the cursor instead of counting rows off.
KEYSET_PAGE_SQL = """
SELECT pp.id,
       pr.name AS provider,
       pp.region,
       pp.instance_type,
       pp.usd_per_hour::float8 AS usd_per_hour,
       pp.availability,
       pp.observed_at
FROM price_points pp
JOIN providers pr ON pr.id = pp.provider_id
WHERE pp.gpu_model_id = :gpu_model_id
  {cursor_clause}
ORDER BY pp.observed_at DESC, pp.id DESC
LIMIT :limit
"""


def encode_cursor(observed_at: datetime, row_id: int) -> str:
    raw = json.dumps({"o": observed_at.isoformat(), "i": row_id})
    return base64.urlsafe_b64encode(raw.encode()).decode()


def decode_cursor(cursor: str) -> tuple[datetime, int]:
    raw = json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
    return datetime.fromisoformat(raw["o"]), int(raw["i"])


async def latest_prices(
    session: AsyncSession,
    gpu_model_id: int,
    window_days: int = 30,
    availability: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    since = datetime.now(UTC) - timedelta(days=window_days)
    clause = "AND pp.availability = :availability" if availability else ""
    sql = LATEST_PRICES_SQL.format(availability_clause=clause)

    params: dict[str, Any] = {
        "gpu_model_id": gpu_model_id,
        "since": since,
        "limit": limit,
    }
    if availability:
        params["availability"] = availability

    result = await session.execute(text(sql), params)
    return [dict(row) for row in result.mappings()]


async def price_page(
    session: AsyncSession,
    gpu_model_id: int,
    limit: int,
    cursor: str | None = None,
) -> tuple[list[dict[str, Any]], str | None]:
    params: dict[str, Any] = {"gpu_model_id": gpu_model_id, "limit": limit}
    clause = ""

    if cursor:
        observed_at, row_id = decode_cursor(cursor)
        clause = "AND (pp.observed_at, pp.id) < (:cursor_ts, :cursor_id)"
        params["cursor_ts"] = observed_at
        params["cursor_id"] = row_id

    sql = KEYSET_PAGE_SQL.format(cursor_clause=clause)
    rows = [dict(r) for r in (await session.execute(text(sql), params)).mappings()]

    next_cursor = None
    if len(rows) == limit:
        next_cursor = encode_cursor(rows[-1]["observed_at"], rows[-1]["id"])

    return rows, next_cursor
