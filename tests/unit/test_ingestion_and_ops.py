import asyncio

from app.services.ingestion import fan_out_fetch


async def test_fan_out_runs_concurrently():
    """Ten 50ms fetches under concurrency 10 should take ~50ms, not ~500ms."""

    async def slow():
        await asyncio.sleep(0.05)
        return "ok"

    start = asyncio.get_event_loop().time()
    results = await fan_out_fetch([slow] * 10, concurrency=10)
    elapsed = asyncio.get_event_loop().time() - start

    assert results == ["ok"] * 10
    assert elapsed < 0.25, f"expected concurrent execution, took {elapsed:.3f}s"


async def test_fan_out_respects_the_semaphore():
    """Concurrency 2 over four 50ms tasks must serialise into ~2 waves."""

    async def slow():
        await asyncio.sleep(0.05)
        return "ok"

    start = asyncio.get_event_loop().time()
    await fan_out_fetch([slow] * 4, concurrency=2)
    elapsed = asyncio.get_event_loop().time() - start

    assert elapsed >= 0.09, f"semaphore should have bounded the fan-out, took {elapsed:.3f}s"


async def test_fan_out_isolates_a_failing_feed():
    """One bad provider must not sink the whole ingestion run."""

    async def good():
        return "ok"

    async def bad():
        raise ValueError("provider returned garbage")

    results = await fan_out_fetch([good, bad, good])

    assert results[0] == "ok"
    assert isinstance(results[1], ValueError)
    assert results[2] == "ok"


async def test_ingest_empty_payload_is_a_noop(session):
    from app.services.ingestion import ingest_prices

    accepted, rejected, errors = await ingest_prices(session, [])
    assert (accepted, rejected, errors) == (0, 0, [])


async def test_table_counts_reports_every_table(session):
    from app.services.ingestion import table_counts

    counts = await table_counts(session)
    assert set(counts) == {
        "gpu_models",
        "providers",
        "price_points",
        "benchmark_runs",
    }
