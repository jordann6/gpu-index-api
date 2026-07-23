"""Measure the hot query before and after indexing, and write docs/query-tuning.md.

Order matters: run the query naive, capture the plan, add indexes, capture again.
The generated document is the artifact, not this script.

Usage: python scripts/tune.py
"""

import asyncio
import os
import statistics
import time
from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from app.db.session import dispose_engine, get_engine, get_sessionmaker
from app.services.pricing import LATEST_PRICES_SQL

INDEXES = [
    (
        "ix_price_latest",
        "CREATE INDEX ix_price_latest ON price_points "
        "(gpu_model_id, provider_id, region, observed_at DESC) "
        "INCLUDE (usd_per_hour, instance_type, availability)",
        "Covering index for the hot path, doing two things at once. The column "
        "order matches `DISTINCT ON (provider_id, region) ORDER BY ..., "
        "observed_at DESC` exactly, so Postgres consumes index order and skips "
        "the sort entirely. INCLUDE carries every selected column, so the scan "
        "is index-only and never touches the heap.\n\n"
        "Deliberately **not** a partial index on `availability = 'available'`. "
        "That version measured worse: because the API passes availability as a "
        "bind parameter rather than a literal, the planner cannot prove the "
        "partial predicate is satisfied and falls back to a bitmap heap scan "
        "plus an external merge sort. Carrying `availability` in INCLUDE lets "
        "the filter be applied index-only instead.",
    ),
    (
        "ix_price_median",
        "CREATE INDEX ix_price_median ON price_points "
        "(gpu_model_id, observed_at) INCLUDE (usd_per_hour)",
        "Covers the rolling-median CTE, which reads every observation in the "
        "window regardless of availability. Without INCLUDE this was the single "
        "most expensive branch of the plan, fetching ~23k heap blocks.",
    ),
    (
        "ix_price_keyset",
        "CREATE INDEX ix_price_keyset ON price_points (gpu_model_id, observed_at DESC, id DESC)",
        "Supports keyset pagination so deep pages seek instead of counting off.",
    ),
    (
        "ix_price_cheapest",
        "CREATE INDEX ix_price_cheapest ON price_points "
        "(gpu_model_id, usd_per_hour) INCLUDE (provider_id, region, observed_at) "
        "WHERE availability = 'available'",
        "Serves the `/v1/index/{workload}` ranking, whose `best_price` CTE takes "
        "`DISTINCT ON (gpu_model_id) ... ORDER BY gpu_model_id, usd_per_hour` "
        "across every model at once. Without this the endpoint scanned the whole "
        "table on a cache miss and produced multi-second outliers under load. "
        "A partial index is correct here because this query filters on the "
        "literal `'available'` rather than a bind parameter.",
    ),
    (
        "ix_benchmark_best",
        "CREATE INDEX ix_benchmark_best ON benchmark_runs "
        "(workload, gpu_model_id, throughput DESC) INCLUDE (precision)",
        "Matches the `best_bench` CTE ordering so the top throughput per model "
        "is read straight off the index instead of sorted per request.",
    ),
]

DROP_ALL = "; ".join(f"DROP INDEX IF EXISTS {name}" for name, _, _ in INDEXES)


async def vacuum_analyze() -> None:
    """VACUUM cannot run inside a transaction block, so it gets its own
    autocommit connection."""
    engine = get_engine()
    async with engine.connect() as conn:
        await conn.execution_options(isolation_level="AUTOCOMMIT")
        await conn.execute(text("VACUUM ANALYZE price_points"))
        await conn.execute(text("VACUUM ANALYZE benchmark_runs"))


async def apply_planner_settings(session) -> None:
    """Storage-accurate planner costs.

    The defaults assume a spinning disk (random_page_cost 4.0). On SSD or EBS
    gp3, random reads cost roughly what sequential reads do, and leaving the
    default makes the planner reject index scans that are actually cheaper.
    work_mem is raised so the remaining sorts stay in memory instead of
    spilling to an external merge on disk.
    """
    await session.execute(text("SET random_page_cost = 1.1"))
    await session.execute(text("SET effective_cache_size = '2GB'"))
    await session.execute(text("SET work_mem = '32MB'"))


async def explain(session, sql: str, params: dict) -> str:
    result = await session.execute(text(f"EXPLAIN (ANALYZE, BUFFERS, TIMING) {sql}"), params)
    return "\n".join(row[0] for row in result)


async def timed(session, sql: str, params: dict, runs: int = 5) -> float:
    """Median wall time in ms. Median avoids a cold first run skewing it."""
    times = []
    for _ in range(runs):
        start = time.perf_counter()
        await session.execute(text(sql), params)
        times.append((time.perf_counter() - start) * 1000)
    return statistics.median(times)


PAGINATION_DEPTH = 50_000


async def offset_vs_keyset(session, gpu_id: int, depth: int = PAGINATION_DEPTH) -> tuple:
    """Deep pagination is where OFFSET falls apart."""
    # depth is coerced to int before interpolation, so no caller-controlled text
    # reaches the statement. Interpolated rather than bound because the point of
    # the measurement is to vary OFFSET across runs.
    depth = int(depth)

    base = "SELECT id, observed_at FROM price_points WHERE gpu_model_id = :g ORDER BY observed_at DESC, id DESC"

    offset_sql = f"{base} LIMIT 50 OFFSET {depth}"  # noqa: S608
    off_ms = await timed(session, offset_sql, {"g": gpu_id}, runs=3)

    cursor_sql = f"{base} LIMIT 1 OFFSET {depth}"  # noqa: S608
    row = (await session.execute(text(cursor_sql), {"g": gpu_id})).first()
    # base selects (id, observed_at) in that order.
    cursor_id, cursor_ts = (row[0], row[1]) if row else (None, None)

    if row is None:
        return off_ms, None

    keyset_sql = (
        "SELECT id, observed_at FROM price_points WHERE gpu_model_id = :g "
        "AND (observed_at, id) < (:ts, :id) "
        "ORDER BY observed_at DESC, id DESC LIMIT 50"
    )
    key_ms = await timed(
        session, keyset_sql, {"g": gpu_id, "ts": cursor_ts, "id": cursor_id}, runs=3
    )
    return off_ms, key_ms


async def main() -> None:
    maker = get_sessionmaker()
    since = datetime.now(UTC) - timedelta(days=30)

    async with maker() as session:
        total = (await session.execute(text("SELECT count(*) FROM price_points"))).scalar_one()
        gpu_id, gpu_name = (
            await session.execute(
                text(
                    "SELECT pp.gpu_model_id, g.model_name FROM price_points pp "
                    "JOIN gpu_models g ON g.id = pp.gpu_model_id "
                    "GROUP BY 1, 2 ORDER BY count(*) DESC LIMIT 1"
                )
            )
        ).first()

        sql = LATEST_PRICES_SQL.format(availability_clause="AND pp.availability = :availability")
        params = {
            "gpu_model_id": gpu_id,
            "since": since,
            "limit": 200,
            "availability": "available",
        }

        # Applied to BOTH runs so the reported delta isolates the indexes
        # rather than mixing in the planner-cost change.
        await apply_planner_settings(session)

        # Guarantee a clean baseline even on a rerun.
        for stmt in DROP_ALL.split("; "):
            await session.execute(text(stmt))
        await session.commit()
        await session.execute(text("ANALYZE price_points"))
        await session.commit()

        print("measuring baseline...")
        before_plan = await explain(session, sql, params)
        before_ms = await timed(session, sql, params)
        before_off, before_key = await offset_vs_keyset(session, gpu_id)
        print(f"  baseline median {before_ms:.1f} ms")

        print("creating indexes...")
        for name, ddl, _ in INDEXES:
            start = time.perf_counter()
            await session.execute(text(ddl))
            await session.commit()
            print(f"  {name} built in {time.perf_counter() - start:.1f}s")

    # VACUUM updates the visibility map, without which Postgres cannot use an
    # index-only scan and falls back to heap fetches. It cannot run inside a
    # transaction, so it needs its own autocommit connection.
    await vacuum_analyze()

    async with maker() as session:
        await apply_planner_settings(session)
        print("measuring tuned...")
        after_plan = await explain(session, sql, params)
        after_ms = await timed(session, sql, params)
        after_off, after_key = await offset_vs_keyset(session, gpu_id)
        print(f"  tuned median {after_ms:.1f} ms")

        sizes = {
            row[0]: row[1]
            for row in await session.execute(
                text(
                    "SELECT indexrelname, pg_size_pretty(pg_relation_size(indexrelid)) "
                    "FROM pg_stat_user_indexes WHERE relname = 'price_points'"
                )
            )
        }
        table_size = (
            await session.execute(
                text("SELECT pg_size_pretty(pg_total_relation_size('price_points'))")
            )
        ).scalar_one()

    improvement = ((before_ms - after_ms) / before_ms * 100) if before_ms else 0.0
    doc = render(
        total,
        gpu_name,
        table_size,
        before_ms,
        after_ms,
        improvement,
        before_plan,
        after_plan,
        sizes,
        before_off,
        before_key,
        after_off,
        after_key,
    )

    # The container image ships app/ and scripts/ but not docs/, so this runs
    # fine as a one-off ECS task to apply indexes without a writable docs dir.
    os.makedirs("docs", exist_ok=True)
    with open("docs/query-tuning.md", "w") as fh:
        fh.write(doc)

    print(f"\nwrote docs/query-tuning.md ({improvement:.1f}% improvement)")
    await dispose_engine()


def render(
    total,
    gpu_name,
    table_size,
    before_ms,
    after_ms,
    improvement,
    before_plan,
    after_plan,
    sizes,
    before_off,
    before_key,
    after_off,
    after_key,
) -> str:
    index_docs = "\n".join(
        f"**`{name}`** {sizes.get(name, 'n/a')}\n\n```sql\n{ddl}\n```\n\n{why}\n"
        for name, ddl, why in INDEXES
    )

    pagination = ""
    if before_key and after_key:
        pagination = f"""
## Keyset vs OFFSET pagination

At depth {PAGINATION_DEPTH:,} rows:

| Strategy | Before indexes | After indexes |
|---|---|---|
| `OFFSET {PAGINATION_DEPTH}` | {before_off:.1f} ms | {after_off:.1f} ms |
| Keyset `(observed_at, id) < (...)` | {before_key:.1f} ms | {after_key:.1f} ms |

`OFFSET` has to walk and discard every preceding row, so its cost grows linearly
with depth no matter what indexes exist. Keyset pagination seeks straight to the
cursor position. This is why the API exposes a cursor rather than a page number.
"""

    return f"""# Query Tuning

Measurements taken against the seeded dataset. Reproduce with `python scripts/tune.py`.

- **Rows in `price_points`:** {total:,}
- **Table size including indexes:** {table_size}
- **Query under test:** latest price per provider and region for `{gpu_name}`,
  with a 30 day rolling median, filtered to available capacity, sorted by price
- **Method:** median of 5 runs, `EXPLAIN (ANALYZE, BUFFERS)`

Planner settings (`random_page_cost = 1.1`, `effective_cache_size = 2GB`,
`work_mem = 32MB`) are applied to **both** runs, so the numbers below isolate
the effect of the indexes rather than mixing in the cost-model change. Those
settings matter on their own: with the shipped default `random_page_cost = 4.0`
the planner assumes spinning-disk seeks, prices the index-only scan above a
bitmap heap scan, and picks the slower plan even once the index exists.

## Result

| | Median latency |
|---|---|
| Before indexing | **{before_ms:.1f} ms** |
| After indexing | **{after_ms:.1f} ms** |
| Improvement | **{improvement:.1f}%** |

## Indexes added

{index_docs}
{pagination}
## Plan before

```
{before_plan}
```

## Plan after

```
{after_plan}
```

## Notes

The `DISTINCT ON` formulation is deliberate. The obvious alternative, a window
function with `ROW_NUMBER() OVER (PARTITION BY ...)`, forces a sort of the full
filtered set before it can discard rows. `DISTINCT ON` consumes the index order
directly and stops at the first row per group.

Write cost is the tradeoff. Three indexes on a high-ingest table means three
additional B-tree updates per insert. The partial index limits that somewhat by
excluding non-available rows. For an ingest-heavy production deployment the next
step would be time-based partitioning on `observed_at`, so index maintenance
stays confined to the current partition.
"""


if __name__ == "__main__":
    asyncio.run(main())
