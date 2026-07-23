"""Closed-loop load test. Reports p50/p95/p99 and the cache hit ratio.

No external load tool required, so it runs the same locally and in CI.

Usage:
  python tests/load/loadtest.py --url http://localhost:8000 --key local-dev-key \
      --concurrency 20 --duration 20
"""

import argparse
import asyncio
import random
import statistics
import time

import httpx

random.seed(11)


async def worker(
    client: httpx.AsyncClient,
    paths: list[str],
    deadline: float,
    latencies: list[float],
    errors: list[int],
) -> None:
    while time.perf_counter() < deadline:
        path = random.choice(paths)
        start = time.perf_counter()
        try:
            response = await client.get(path)
            latencies.append((time.perf_counter() - start) * 1000)
            if response.status_code >= 400:
                errors.append(response.status_code)
        except Exception:
            errors.append(0)


async def cache_stats(client: httpx.AsyncClient) -> dict[str, float]:
    body = (await client.get("/metrics")).text
    stats = {}
    for line in body.splitlines():
        if line.startswith("cache_") and " " in line:
            name, _, value = line.partition(" ")
            stats[name] = float(value)
    return stats


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://localhost:8000")
    parser.add_argument("--key", default="local-dev-key")
    parser.add_argument("--concurrency", type=int, default=20)
    parser.add_argument("--duration", type=float, default=20.0)
    args = parser.parse_args()

    limits = httpx.Limits(
        max_connections=args.concurrency * 2, max_keepalive_connections=args.concurrency
    )
    async with httpx.AsyncClient(
        base_url=args.url,
        headers={"X-API-Key": args.key},
        limits=limits,
        timeout=30.0,
    ) as client:
        gpus = (await client.get("/v1/gpus", params={"limit": 30})).json()
        ids = [item["id"] for item in gpus["items"]]
        if not ids:
            raise SystemExit("no GPU models found, seed the database first")

        # Weighted toward the hot path, with the expensive index query mixed in.
        paths = (
            [f"/v1/gpus/{i}/prices" for i in ids] * 6
            + [f"/v1/gpus/{i}/prices?availability=available" for i in ids] * 3
            + ["/v1/index/llm-inference-7b", "/v1/index/stable-diffusion-xl"] * 2
            + [f"/v1/gpus/{i}/benchmarks" for i in ids]
            + ["/v1/gpus?limit=50"]
        )

        before = await cache_stats(client)

        print(f"warming up for 3s at concurrency {args.concurrency}...")
        warm_deadline = time.perf_counter() + 3
        await asyncio.gather(
            *(worker(client, paths, warm_deadline, [], []) for _ in range(args.concurrency))
        )

        print(f"running for {args.duration:.0f}s...")
        latencies: list[float] = []
        errors: list[int] = []
        deadline = time.perf_counter() + args.duration
        start = time.perf_counter()

        await asyncio.gather(
            *(worker(client, paths, deadline, latencies, errors) for _ in range(args.concurrency))
        )

        elapsed = time.perf_counter() - start
        after = await cache_stats(client)

    latencies.sort()
    hits = after.get("cache_hits_total", 0) - before.get("cache_hits_total", 0)
    misses = after.get("cache_misses_total", 0) - before.get("cache_misses_total", 0)
    ratio = hits / (hits + misses) if (hits + misses) else 0.0

    def pct(p: float) -> float:
        return latencies[min(int(len(latencies) * p), len(latencies) - 1)]

    error_rate = len(errors) / len(latencies) if latencies else 1.0
    throttled = sum(1 for e in errors if e == 429)

    print(f"""
requests        {len(latencies):,}
duration        {elapsed:.1f} s
throughput      {len(latencies) / elapsed:.1f} req/s
errors          {len(errors)} ({error_rate:.1%}), of which {throttled} were 429

latency p50     {statistics.median(latencies):.1f} ms
latency p95     {pct(0.95):.1f} ms
latency p99     {pct(0.99):.1f} ms
latency max     {latencies[-1]:.1f} ms

cache hits      {hits:.0f}
cache misses    {misses:.0f}
cache hit ratio {ratio:.1%}
""")

    # Latency percentiles computed over mostly-rejected requests are worthless.
    # Fail loudly rather than publish a flattering but meaningless number.
    if error_rate > 0.01:
        raise SystemExit(
            f"FAILED: {error_rate:.1%} error rate invalidates these percentiles. "
            f"{throttled} requests were rate limited; raise RATE_LIMIT_REQUESTS "
            f"on the server or lower --concurrency."
        )


if __name__ == "__main__":
    asyncio.run(main())
