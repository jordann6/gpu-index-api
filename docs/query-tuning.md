# Query Tuning

Measurements taken against the seeded dataset. Reproduce with `python scripts/tune.py`.

- **Rows in `price_points`:** 2,000,000
- **Table size including indexes:** 824 MB
- **Query under test:** latest price per provider and region for `L4`,
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
| Before indexing | **165.6 ms** |
| After indexing | **27.4 ms** |
| Improvement | **83.4%** |

## Indexes added

**`ix_price_latest`** 166 MB

```sql
CREATE INDEX ix_price_latest ON price_points (gpu_model_id, provider_id, region, observed_at DESC) INCLUDE (usd_per_hour, instance_type, availability)
```

Covering index for the hot path, doing two things at once. The column order matches `DISTINCT ON (provider_id, region) ORDER BY ..., observed_at DESC` exactly, so Postgres consumes index order and skips the sort entirely. INCLUDE carries every selected column, so the scan is index-only and never touches the heap.

Deliberately **not** a partial index on `availability = 'available'`. That version measured worse: because the API passes availability as a bind parameter rather than a literal, the planner cannot prove the partial predicate is satisfied and falls back to a bitmap heap scan plus an external merge sort. Carrying `availability` in INCLUDE lets the filter be applied index-only instead.

**`ix_price_median`** 77 MB

```sql
CREATE INDEX ix_price_median ON price_points (gpu_model_id, observed_at) INCLUDE (usd_per_hour)
```

Covers the rolling-median CTE, which reads every observation in the window regardless of availability. Without INCLUDE this was the single most expensive branch of the plan, fetching ~23k heap blocks.

**`ix_price_keyset`** 77 MB

```sql
CREATE INDEX ix_price_keyset ON price_points (gpu_model_id, observed_at DESC, id DESC)
```

Supports keyset pagination so deep pages seek instead of counting off.

**`ix_price_cheapest`** 79 MB

```sql
CREATE INDEX ix_price_cheapest ON price_points (gpu_model_id, usd_per_hour) INCLUDE (provider_id, region, observed_at) WHERE availability = 'available'
```

Serves the `/v1/index/{workload}` ranking, whose `best_price` CTE takes `DISTINCT ON (gpu_model_id) ... ORDER BY gpu_model_id, usd_per_hour` across every model at once. Without this the endpoint scanned the whole table on a cache miss and produced multi-second outliers under load. A partial index is correct here because this query filters on the literal `'available'` rather than a bind parameter.

**`ix_benchmark_best`** n/a

```sql
CREATE INDEX ix_benchmark_best ON benchmark_runs (workload, gpu_model_id, throughput DESC) INCLUDE (precision)
```

Matches the `best_bench` CTE ordering so the top throughput per model is read straight off the index instead of sorted per request.


## Keyset vs OFFSET pagination

At depth 50,000 rows:

| Strategy | Before indexes | After indexes |
|---|---|---|
| `OFFSET 50000` | 74.3 ms | 12.9 ms |
| Keyset `(observed_at, id) < (...)` | 45.7 ms | 0.6 ms |

`OFFSET` has to walk and discard every preceding row, so its cost grows linearly
with depth no matter what indexes exist. Keyset pagination seeks straight to the
cursor position. This is why the API exposes a cursor rather than a page number.

## Plan before

```
Limit  (cost=56662.04..56662.54 rows=200 width=210) (actual time=484.688..484.719 rows=200 loops=1)
  Buffers: shared hit=4476 read=40277 written=12
  InitPlan 1 (returns $0)
    ->  Aggregate  (cost=26419.25..26419.26 rows=1 width=8) (actual time=350.919..350.920 rows=1 loops=1)
          Buffers: shared hit=174 read=22991 written=8
          ->  Bitmap Heap Scan on price_points  (cost=976.66..26025.10 rows=78829 width=6) (actual time=7.877..324.096 rows=77451 loops=1)
                Recheck Cond: ((gpu_model_id = 7) AND (observed_at >= '2026-06-23 12:00:06.4474-05'::timestamp with time zone))
                Heap Blocks: exact=23007
                Buffers: shared hit=166 read=22991 written=8
                ->  Bitmap Index Scan on ix_price_lookup  (cost=0.00..956.96 rows=78829 width=0) (actual time=5.882..5.883 rows=77451 loops=1)
                      Index Cond: ((gpu_model_id = 7) AND (observed_at >= '2026-06-23 12:00:06.4474-05'::timestamp with time zone))
                      Buffers: shared read=150
  ->  Sort  (cost=30242.78..30243.28 rows=200 width=210) (actual time=484.687..484.698 rows=200 loops=1)
        Sort Key: pp.usd_per_hour, pp.region
        Sort Method: quicksort  Memory: 53kB
        Buffers: shared hit=4476 read=40277 written=12
        ->  Merge Join  (cost=29797.66..30235.13 rows=200 width=210) (actual time=474.484..484.415 rows=200 loops=1)
              Merge Cond: (pp.provider_id = pr.id)
              Buffers: shared hit=4476 read=40277 written=12
              ->  Unique  (cost=29797.51..30211.36 rows=200 width=52) (actual time=123.533..133.376 rows=200 loops=1)
                    Buffers: shared hit=4300 read=17286 written=4
                    ->  Sort  (cost=29797.51..29935.46 rows=55180 width=52) (actual time=123.532..127.819 rows=53959 loops=1)
                          Sort Key: pp.provider_id, pp.region, pp.observed_at DESC
                          Sort Method: quicksort  Memory: 7269kB
                          Buffers: shared hit=4300 read=17286 written=4
                          ->  Bitmap Heap Scan on price_points pp  (cost=619.92..25451.57 rows=55180 width=52) (actual time=5.738..81.306 rows=53959 loops=1)
                                Recheck Cond: ((gpu_model_id = 7) AND (observed_at >= '2026-06-23 12:00:06.4474-05'::timestamp with time zone) AND ((availability)::text = 'available'::text))
                                Heap Blocks: exact=21536
                                Buffers: shared hit=4300 read=17286 written=4
                                ->  Bitmap Index Scan on ix_price_available  (cost=0.00..606.13 rows=55180 width=0) (actual time=3.781..3.781 rows=53959 loops=1)
                                      Index Cond: ((gpu_model_id = 7) AND (observed_at >= '2026-06-23 12:00:06.4474-05'::timestamp with time zone))
                                      Buffers: shared read=50
              ->  Index Scan using providers_pkey on providers pr  (cost=0.15..18.00 rows=310 width=150) (actual time=0.020..0.026 rows=10 loops=1)
                    Buffers: shared hit=2
Planning:
  Buffers: shared hit=90
Planning Time: 0.367 ms
Execution Time: 484.803 ms
```

## Plan after

```
Limit  (cost=5194.98..5195.48 rows=200 width=210) (actual time=39.707..39.719 rows=200 loops=1)
  Buffers: shared hit=2 read=1198
  InitPlan 1 (returns $0)
    ->  Aggregate  (cost=2306.88..2306.89 rows=1 width=8) (actual time=20.169..20.170 rows=1 loops=1)
          Buffers: shared hit=1 read=384
          ->  Index Only Scan using ix_price_median on price_points  (cost=0.43..1928.31 rows=75714 width=6) (actual time=0.231..7.182 rows=77451 loops=1)
                Index Cond: ((gpu_model_id = 7) AND (observed_at >= '2026-06-23 12:00:06.4474-05'::timestamp with time zone))
                Heap Fetches: 0
                Buffers: shared hit=1 read=384
  ->  Sort  (cost=2888.08..2888.58 rows=200 width=210) (actual time=39.707..39.711 rows=200 loops=1)
        Sort Key: pp.usd_per_hour, pp.region
        Sort Method: quicksort  Memory: 53kB
        Buffers: shared hit=2 read=1198
        ->  Merge Join  (cost=0.70..2880.44 rows=200 width=210) (actual time=20.731..39.614 rows=200 loops=1)
              Merge Cond: (pp.provider_id = pr.id)
              Buffers: shared hit=2 read=1198
              ->  Result  (cost=0.55..2856.67 rows=200 width=52) (actual time=0.480..19.309 rows=200 loops=1)
                    Buffers: shared hit=1 read=812
                    ->  Unique  (cost=0.55..2856.67 rows=200 width=52) (actual time=0.479..19.287 rows=200 loops=1)
                          Buffers: shared hit=1 read=812
                          ->  Index Only Scan using ix_price_latest on price_points pp  (cost=0.55..2591.58 rows=53018 width=52) (actual time=0.479..17.081 rows=53959 loops=1)
                                Index Cond: ((gpu_model_id = 7) AND (observed_at >= '2026-06-23 12:00:06.4474-05'::timestamp with time zone))
                                Filter: ((availability)::text = 'available'::text)
                                Rows Removed by Filter: 23492
                                Heap Fetches: 0
                                Buffers: shared hit=1 read=812
              ->  Index Scan using providers_pkey on providers pr  (cost=0.15..18.00 rows=310 width=150) (actual time=0.077..0.084 rows=10 loops=1)
                    Buffers: shared read=2
Planning:
  Buffers: shared hit=43
Planning Time: 0.347 ms
Execution Time: 39.776 ms
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
