"""Canonical definitions for the performance indexes.

Single source of truth, imported by both the Alembic migration that creates them
and by scripts/tune.py, which drops and rebuilds them to measure their effect.
Keeping one list means a tuned index can never exist in the measurement but be
missing from a real deployment, which is exactly the gap that shipped the first
version of this project untuned.

Each entry is (name, table, create_ddl, rationale).
"""

TUNING_INDEXES: list[tuple[str, str, str, str]] = [
    (
        "ix_price_latest",
        "price_points",
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
        "price_points",
        "CREATE INDEX ix_price_median ON price_points "
        "(gpu_model_id, observed_at) INCLUDE (usd_per_hour)",
        "Covers the rolling-median CTE, which reads every observation in the "
        "window regardless of availability. Without INCLUDE this was the single "
        "most expensive branch of the plan, fetching ~23k heap blocks.",
    ),
    (
        "ix_price_keyset",
        "price_points",
        "CREATE INDEX ix_price_keyset ON price_points (gpu_model_id, observed_at DESC, id DESC)",
        "Supports keyset pagination so deep pages seek instead of counting off.",
    ),
    (
        "ix_price_cheapest",
        "price_points",
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
        "benchmark_runs",
        "CREATE INDEX ix_benchmark_best ON benchmark_runs "
        "(workload, gpu_model_id, throughput DESC) INCLUDE (precision)",
        "Matches the `best_bench` CTE ordering so the top throughput per model "
        "is read straight off the index instead of sorted per request.",
    ),
]

INDEX_NAMES: list[str] = [name for name, _, _, _ in TUNING_INDEXES]

TABLES_INDEXED: list[str] = sorted({table for _, table, _, _ in TUNING_INDEXES})


def create_statements() -> list[str]:
    return [ddl for _, _, ddl, _ in TUNING_INDEXES]


def drop_statements() -> list[str]:
    return [f"DROP INDEX IF EXISTS {name}" for name in INDEX_NAMES]
