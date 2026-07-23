# Query Tuning

Measurements taken against the seeded dataset. Reproduce with `python scripts/tune.py`.

- **Rows in `price_points`:** 2,000,000
- **Table size including indexes:** 785 MB
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
| Before indexing | **183.2 ms** |
| After indexing | **24.3 ms** |
| Improvement | **86.7%** |

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
| `OFFSET 50000` | 78.4 ms | 3.7 ms |
| Keyset `(observed_at, id) < (...)` | 47.0 ms | 1.0 ms |

`OFFSET` has to walk and discard every preceding row, so its cost grows linearly
with depth no matter what indexes exist. Keyset pagination seeks straight to the
cursor position. This is why the API exposes a cursor rather than a page number.

## Plan before

```
Limit  (cost=58380.38..58380.88 rows=200 width=210) (actual time=710.988..711.001 rows=200 loops=1)
  Buffers: shared hit=621 read=46911 written=3761
  InitPlan 1 (returns $0)
    ->  Aggregate  (cost=26984.25..26984.26 rows=1 width=8) (actual time=295.691..295.691 rows=1 loops=1)
          Buffers: shared hit=167 read=23602 written=1963
          ->  Bitmap Heap Scan on price_points  (cost=1605.41..26606.04 rows=75642 width=6) (actual time=20.184..280.442 rows=77451 loops=1)
                Recheck Cond: ((gpu_model_id = 7) AND (observed_at >= '2026-06-23 12:34:20.710811-05'::timestamp with time zone))
                Heap Blocks: exact=23007
                Buffers: shared hit=159 read=23602 written=1963
                ->  Bitmap Index Scan on uq_price_observation  (cost=0.00..1586.50 rows=75642 width=0) (actual time=18.172..18.172 rows=77451 loops=1)
                      Index Cond: ((gpu_model_id = 7) AND (observed_at >= '2026-06-23 12:34:20.710811-05'::timestamp with time zone))
                      Buffers: shared hit=127 read=627 written=81
  ->  Sort  (cost=31396.11..31396.61 rows=200 width=210) (actual time=710.988..710.992 rows=200 loops=1)
        Sort Key: pp.usd_per_hour, pp.region
        Sort Method: quicksort  Memory: 53kB
        Buffers: shared hit=621 read=46911 written=3761
        ->  Merge Join  (cost=30965.82..31388.46 rows=200 width=210) (actual time=706.859..710.905 rows=200 loops=1)
              Merge Cond: (pp.provider_id = pr.id)
              Buffers: shared hit=621 read=46911 written=3761
              ->  Unique  (cost=30965.68..31364.69 rows=200 width=52) (actual time=411.150..415.161 rows=200 loops=1)
                    Buffers: shared hit=452 read=23309 written=1798
                    ->  Sort  (cost=30965.68..31098.68 rows=53202 width=52) (actual time=411.150..412.947 rows=53959 loops=1)
                          Sort Key: pp.provider_id, pp.region, pp.observed_at DESC
                          Sort Method: quicksort  Memory: 7269kB
                          Buffers: shared hit=452 read=23309 written=1798
                          ->  Bitmap Heap Scan on price_points pp  (cost=1599.80..26789.53 rows=53202 width=52) (actual time=86.250..370.774 rows=53959 loops=1)
                                Recheck Cond: ((gpu_model_id = 7) AND (observed_at >= '2026-06-23 12:34:20.710811-05'::timestamp with time zone))
                                Filter: ((availability)::text = 'available'::text)
                                Rows Removed by Filter: 23492
                                Heap Blocks: exact=23007
                                Buffers: shared hit=452 read=23309 written=1798
                                ->  Bitmap Index Scan on uq_price_observation  (cost=0.00..1586.50 rows=75642 width=0) (actual time=84.228..84.228 rows=77451 loops=1)
                                      Index Cond: ((gpu_model_id = 7) AND (observed_at >= '2026-06-23 12:34:20.710811-05'::timestamp with time zone))
                                      Buffers: shared hit=160 read=594
              ->  Index Scan using providers_pkey on providers pr  (cost=0.15..18.00 rows=310 width=150) (actual time=0.010..0.013 rows=10 loops=1)
                    Buffers: shared hit=2
Planning:
  Buffers: shared hit=82 read=2
Planning Time: 0.647 ms
Execution Time: 711.049 ms
```

## Plan after

```
Limit  (cost=5184.41..5184.91 rows=200 width=210) (actual time=34.898..34.910 rows=200 loops=1)
  Buffers: shared hit=2 read=1198
  InitPlan 1 (returns $0)
    ->  Aggregate  (cost=2302.23..2302.24 rows=1 width=8) (actual time=18.579..18.580 rows=1 loops=1)
          Buffers: shared hit=1 read=384
          ->  Index Only Scan using ix_price_median on price_points  (cost=0.43..1924.37 rows=75572 width=6) (actual time=0.147..6.359 rows=77451 loops=1)
                Index Cond: ((gpu_model_id = 7) AND (observed_at >= '2026-06-23 12:34:20.710811-05'::timestamp with time zone))
                Heap Fetches: 0
                Buffers: shared hit=1 read=384
  ->  Sort  (cost=2882.16..2882.66 rows=200 width=210) (actual time=34.897..34.901 rows=200 loops=1)
        Sort Key: pp.usd_per_hour, pp.region
        Sort Method: quicksort  Memory: 53kB
        Buffers: shared hit=2 read=1198
        ->  Merge Join  (cost=0.70..2874.52 rows=200 width=210) (actual time=19.101..34.814 rows=200 loops=1)
              Merge Cond: (pp.provider_id = pr.id)
              Buffers: shared hit=2 read=1198
              ->  Result  (cost=0.55..2850.75 rows=200 width=52) (actual time=0.338..16.003 rows=200 loops=1)
                    Buffers: shared hit=1 read=812
                    ->  Unique  (cost=0.55..2850.75 rows=200 width=52) (actual time=0.337..15.981 rows=200 loops=1)
                          Buffers: shared hit=1 read=812
                          ->  Index Only Scan using ix_price_latest on price_points pp  (cost=0.55..2585.60 rows=53029 width=52) (actual time=0.337..13.856 rows=53959 loops=1)
                                Index Cond: ((gpu_model_id = 7) AND (observed_at >= '2026-06-23 12:34:20.710811-05'::timestamp with time zone))
                                Filter: ((availability)::text = 'available'::text)
                                Rows Removed by Filter: 23492
                                Heap Fetches: 0
                                Buffers: shared hit=1 read=812
              ->  Index Scan using providers_pkey on providers pr  (cost=0.15..18.00 rows=310 width=150) (actual time=0.180..0.184 rows=10 loops=1)
                    Buffers: shared read=2
Planning:
  Buffers: shared hit=35
Planning Time: 0.211 ms
Execution Time: 34.940 ms
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
