"""Measure concurrent vs sequential provider fan-out.

The claim "the ingestion path is async" is only worth making with a number
attached. This simulates N provider feeds with realistic per-feed latency and
compares a bounded-concurrency gather against a sequential loop.

Usage: python scripts/bench_ingest.py [--feeds 50] [--latency-ms 80]
"""

import argparse
import asyncio
import random
import time

from app.services.ingestion import fan_out_fetch

random.seed(7)


def make_feed(latency_s: float):
    async def fetch():
        # Jitter, because real provider APIs do not respond in lockstep.
        await asyncio.sleep(latency_s * random.uniform(0.6, 1.4))
        return {"rows": random.randint(50, 200)}

    return fetch


async def sequential(feeds) -> float:
    start = time.perf_counter()
    for feed in feeds:
        await feed()
    return (time.perf_counter() - start) * 1000


async def concurrent(feeds, limit: int) -> float:
    start = time.perf_counter()
    await fan_out_fetch(feeds, concurrency=limit)
    return (time.perf_counter() - start) * 1000


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--feeds", type=int, default=50)
    parser.add_argument("--latency-ms", type=float, default=80.0)
    args = parser.parse_args()

    latency_s = args.latency_ms / 1000.0
    feeds = [make_feed(latency_s) for _ in range(args.feeds)]

    print(f"{args.feeds} feeds, ~{args.latency_ms:.0f}ms each\n")

    seq_ms = await sequential(feeds)
    print(f"sequential            {seq_ms:9.1f} ms")

    results = {}
    for limit in (4, 8, 16, 32):
        ms = await concurrent(feeds, limit)
        results[limit] = ms
        print(f"concurrent (limit {limit:2d}) {ms:9.1f} ms   {seq_ms / ms:5.1f}x faster")

    best_limit = min(results, key=results.get)
    print(
        f"\nbest: concurrency {best_limit} at {results[best_limit]:.1f} ms, "
        f"{seq_ms / results[best_limit]:.1f}x faster than sequential"
    )
    print(
        "\nThe semaphore matters: unbounded gather over hundreds of feeds "
        "exhausts sockets and DB connections. Bounded fan-out keeps the "
        "speedup without the blast radius."
    )


if __name__ == "__main__":
    asyncio.run(main())
