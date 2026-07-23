"""Edge paths: empty results, filters, limits, and ops endpoints."""

from datetime import UTC, datetime

from sqlalchemy import text


async def test_prices_for_gpu_with_no_observations(client, session):
    await session.execute(
        text(
            "INSERT INTO gpu_models (id, vendor, model_name, vram_gb, arch, tdp_w) "
            "VALUES (77, 'NVIDIA', 'Unobserved GPU', 24, 'Ada', 150) "
            "ON CONFLICT DO NOTHING"
        )
    )
    await session.commit()

    body = (await client.get("/v1/gpus/77/prices")).json()

    assert body["points"] == []
    assert body["region_count"] == 0
    assert body["cheapest_usd_per_hour"] == 0.0
    assert body["median_30d_usd_per_hour"] is None


async def test_gpus_min_vram_filter(client):
    body = (await client.get("/v1/gpus", params={"min_vram_gb": 100})).json()
    assert all(item["vram_gb"] >= 100 for item in body["items"])


async def test_gpus_limit_is_bounded(client):
    assert (await client.get("/v1/gpus", params={"limit": 9999})).status_code == 422
    assert (await client.get("/v1/gpus", params={"limit": 0})).status_code == 422


async def test_benchmarks_precision_filter(client):
    body = (
        await client.get(
            "/v1/gpus/1/benchmarks", params={"workload": "llm-inference-7b", "precision": "fp16"}
        )
    ).json()
    assert all(i["precision"] == "fp16" for i in body["items"])


async def test_benchmarks_unknown_precision_is_empty(client):
    body = (await client.get("/v1/gpus/1/benchmarks", params={"precision": "fp4"})).json()
    assert body["count"] == 0


async def test_index_with_explicit_precision(client):
    body = (await client.get("/v1/index/llm-inference-7b", params={"precision": "fp16"})).json()
    assert body["count"] >= 1
    assert all(i["precision"] == "fp16" for i in body["items"])


async def test_index_unknown_workload_is_empty(client):
    body = (await client.get("/v1/index/not-a-real-workload")).json()
    assert body["count"] == 0
    assert body["items"] == []


async def test_ingest_rejects_oversized_batch(client):
    payload = [
        {
            "gpu_model_name": "H100 SXM",
            "provider_name": "AWS",
            "region": "us-east-1",
            "instance_type": "x",
            "usd_per_hour": 1.0,
            "availability": "available",
            "observed_at": datetime.now(UTC).isoformat(),
        }
    ] * 5001

    assert (await client.post("/v1/ingest/prices", json=payload)).status_code == 413


async def test_history_pagination_ends_with_null_cursor(client):
    body = (await client.get("/v1/gpus/1/prices/history", params={"limit": 500})).json()
    assert body["next_cursor"] is None, "a short final page carries no cursor"


async def test_history_for_unknown_gpu_404s(client):
    assert (await client.get("/v1/gpus/4242/prices/history")).status_code == 404


async def test_benchmarks_for_unknown_gpu_404s(client):
    assert (await client.get("/v1/gpus/4242/benchmarks")).status_code == 404


async def test_readyz_reports_dependency_checks(client):
    """Runs against the real engine and a real Redis ping, so it may degrade."""
    response = await client.get("/readyz")

    assert response.status_code in (200, 503)
    body = response.json()
    assert set(body["checks"]) == {"postgres", "redis"}
    assert body["status"] in ("ready", "degraded")
