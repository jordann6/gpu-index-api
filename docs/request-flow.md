# Request Flow

## Read path

The hot path is `GET /v1/gpus/{id}/prices`. Auth and rate limiting run as
dependencies before the handler, so an unauthenticated or throttled request
never reaches Postgres.

```mermaid
sequenceDiagram
    participant C as Client
    participant A as ALB
    participant F as FastAPI
    participant R as Redis
    participant P as Postgres

    C->>A: GET /v1/gpus/7/prices
    A->>F: forward :8000
    F->>F: require_api_key (X-API-Key)
    F->>R: INCR rate bucket
    R-->>F: count
    Note over F: 401 if key invalid, 429 if over limit

    F->>R: GET prices:7:available:30
    alt cache hit
        R-->>F: cached JSON
    else cache miss
        R-->>F: nil
        F->>P: DISTINCT ON + rolling median
        Note over P: index-only scan,<br/>Heap Fetches: 0
        P-->>F: rows
        F->>R: SET key, TTL 60s
    end

    F-->>A: 200 PriceSummaryOut
    A-->>C: response
```

A Redis outage does not fail the request. `read_through` catches the error,
increments `cache_errors_total`, and falls through to a direct database read.
The rate limiter likewise fails open. Availability of the API is worth more than
cache hit ratio or perfect throttling.

## Ingest path

Provider feeds are fetched concurrently under a bounded semaphore, then upserted
in one statement. Cache invalidation happens after the write, not before, so a
failed write cannot leave the cache empty and the database stale.

```mermaid
flowchart LR
    subgraph Fan-out
        S[Semaphore<br/>limit 16]
        F1[Provider 1]
        F2[Provider 2]
        FN[Provider N]
    end

    I[POST /v1/ingest/prices] --> S
    S --> F1 & F2 & FN
    F1 & F2 & FN --> G[asyncio.gather<br/>return_exceptions=True]
    G --> V{Resolve<br/>names to IDs}
    V -->|unknown| E[Collect error,<br/>keep going]
    V -->|known| U[INSERT ... ON CONFLICT<br/>DO NOTHING]
    U --> INV[SCAN + DELETE<br/>prices:* and index:*]
    INV --> RES[202 IngestResult]
    E --> RES
```

`return_exceptions=True` is deliberate: one provider returning garbage should not
sink the whole ingestion run. Unknown GPU or provider names are collected as
errors and reported in the response rather than raised.

Measured fan-out, 50 feeds at ~80ms each:

| Mode | Wall time | Speedup |
|---|---|---|
| Sequential | 3,872 ms | 1.0x |
| Concurrent, limit 8 | 570 ms | 6.8x |
| Concurrent, limit 32 | 192 ms | 20.1x |

## Deploy and rollback

The previous task definition ARN is captured **before** the new revision is
registered, so rollback always has a known-good target.

```mermaid
flowchart TD
    L[lint<br/>ruff + mypy] --> T[test<br/>pytest, 85% gate]
    T --> S[security<br/>bandit, trivy, gitleaks]
    S --> CAP[Capture current<br/>task definition ARN]
    CAP --> B[Build + push<br/>ECR :sha]
    B --> D[Register revision<br/>update service]
    D --> W[Wait services-stable]
    W --> SM{Smoke tests<br/>healthz, readyz,<br/>401, authed read}
    SM -->|pass| OK[Deploy complete]
    SM -->|fail| RB[Redeploy captured ARN<br/>wait stable, exit 1]

    style RB fill:#c62828,color:#fff
    style OK fill:#2e7d32,color:#fff
```
