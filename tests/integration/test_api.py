"""Integration tests against real Postgres, with the cache swapped via DI."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text


@pytest.fixture(autouse=True)
async def seed_minimal(session):
    """Small deterministic fixture. Volume lives in scripts/seed.py, not here."""
    await session.execute(text("DELETE FROM price_points"))
    await session.execute(text("DELETE FROM benchmark_runs"))
    await session.execute(text("DELETE FROM gpu_models"))
    await session.execute(text("DELETE FROM providers"))

    await session.execute(
        text(
            "INSERT INTO gpu_models (id, vendor, model_name, vram_gb, arch, tdp_w) "
            "VALUES (1, 'NVIDIA', 'H100 SXM', 80, 'Hopper', 700), "
            "(2, 'AMD', 'MI300X', 192, 'CDNA3', 750)"
        )
    )
    await session.execute(
        text(
            "INSERT INTO providers (id, name, region_scope) "
            "VALUES (1, 'AWS', 'global'), (2, 'CoreWeave', 'us-eu')"
        )
    )

    now = datetime.now(UTC)
    rows = [
        (1, 1, "us-east-1", "aws-h100-8x", 3.10, "available", now - timedelta(hours=1)),
        (1, 1, "us-east-1", "aws-h100-8x", 3.90, "available", now - timedelta(hours=5)),
        (1, 2, "us-west-2", "cor-h100-8x", 2.40, "available", now - timedelta(hours=2)),
        (1, 2, "eu-west-1", "cor-h100-4x", 2.90, "constrained", now - timedelta(hours=3)),
        (2, 1, "us-east-1", "aws-mi300x-8x", 2.75, "available", now - timedelta(hours=1)),
    ]
    for g, p, region, itype, price, avail, observed in rows:
        await session.execute(
            text(
                "INSERT INTO price_points (gpu_model_id, provider_id, region, "
                "instance_type, usd_per_hour, availability, observed_at) "
                "VALUES (:g, :p, :r, :i, :u, :a, :o)"
            ),
            {"g": g, "p": p, "r": region, "i": itype, "u": price, "a": avail, "o": observed},
        )

    await session.execute(
        text(
            "INSERT INTO benchmark_runs (gpu_model_id, workload, precision, "
            "throughput, latency_p95_ms, run_at) VALUES "
            "(1, 'llm-inference-7b', 'fp16', 2400.0, 12.5, :o), "
            "(2, 'llm-inference-7b', 'fp16', 1900.0, 15.1, :o)"
        ),
        {"o": now},
    )
    await session.commit()


async def test_unauthenticated_is_rejected(client):
    response = await client.get("/v1/gpus", headers={"X-API-Key": ""})
    assert response.status_code == 401


async def test_list_gpus(client):
    response = await client.get("/v1/gpus")
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 2
    assert {item["model_name"] for item in body["items"]} == {"H100 SXM", "MI300X"}


async def test_list_gpus_filters_by_vendor(client):
    response = await client.get("/v1/gpus", params={"vendor": "AMD"})
    assert response.json()["count"] == 1


async def test_latest_price_wins_over_older_observation(client):
    """The 3.10 observation is newer than 3.90 for the same provider/region."""
    response = await client.get("/v1/gpus/1/prices")
    assert response.status_code == 200
    body = response.json()

    aws_east = [p for p in body["points"] if p["provider"] == "AWS" and p["region"] == "us-east-1"]
    assert len(aws_east) == 1, "DISTINCT ON should collapse to one row per provider/region"
    assert aws_east[0]["usd_per_hour"] == 3.10


async def test_price_summary_aggregates(client):
    body = (await client.get("/v1/gpus/1/prices")).json()
    assert body["gpu_model"] == "H100 SXM"
    assert body["cheapest_usd_per_hour"] == 2.40
    assert body["provider_count"] == 2
    assert body["median_30d_usd_per_hour"] is not None


async def test_availability_filter_excludes_constrained(client):
    body = (await client.get("/v1/gpus/1/prices", params={"availability": "available"})).json()
    assert all(p["availability"] == "available" for p in body["points"])
    assert not any(p["region"] == "eu-west-1" for p in body["points"])


async def test_second_read_is_cached(client, fake_cache):
    await client.get("/v1/gpus/1/prices")
    assert len(fake_cache.store) == 1
    await client.get("/v1/gpus/1/prices")
    assert len(fake_cache.store) == 1, "same key should be reused, not duplicated"


async def test_unknown_gpu_returns_404(client):
    assert (await client.get("/v1/gpus/9999/prices")).status_code == 404


async def test_keyset_pagination_walks_without_overlap(client):
    first = (await client.get("/v1/gpus/1/prices/history", params={"limit": 2})).json()
    assert first["count"] == 2
    assert first["next_cursor"]

    second = (
        await client.get(
            "/v1/gpus/1/prices/history",
            params={"limit": 2, "cursor": first["next_cursor"]},
        )
    ).json()

    first_keys = {(i["observed_at"], i["usd_per_hour"]) for i in first["items"]}
    second_keys = {(i["observed_at"], i["usd_per_hour"]) for i in second["items"]}
    assert not (first_keys & second_keys), "pages must not overlap"


async def test_workload_index_ranks_by_price_per_throughput(client):
    body = (await client.get("/v1/index/llm-inference-7b")).json()
    assert body["count"] == 2

    # H100: 2.40/2400 = 0.001, MI300X: 2.75/1900 = 0.00145. H100 ranks first.
    assert body["items"][0]["gpu_model"] == "H100 SXM"
    assert body["items"][0]["usd_per_million_units"] < body["items"][1]["usd_per_million_units"]


async def test_benchmarks_endpoint(client):
    body = (await client.get("/v1/gpus/1/benchmarks")).json()
    assert body["count"] == 1
    assert body["items"][0]["workload"] == "llm-inference-7b"


async def test_ingest_accepts_and_invalidates_cache(client, fake_cache):
    await client.get("/v1/gpus/1/prices")
    assert len(fake_cache.store) == 1

    payload = [
        {
            "gpu_model_name": "H100 SXM",
            "provider_name": "AWS",
            "region": "ap-south-1",
            "instance_type": "aws-h100-2x",
            "usd_per_hour": 2.05,
            "availability": "available",
            "observed_at": datetime.now(UTC).isoformat(),
        }
    ]
    response = await client.post("/v1/ingest/prices", json=payload)

    assert response.status_code == 202
    assert response.json()["accepted"] == 1
    assert len(fake_cache.store) == 0, "ingest must invalidate cached price reads"


async def test_ingest_reports_unknown_names_without_failing(client):
    payload = [
        {
            "gpu_model_name": "Nonexistent GPU",
            "provider_name": "AWS",
            "region": "us-east-1",
            "instance_type": "x",
            "usd_per_hour": 1.0,
            "availability": "available",
            "observed_at": datetime.now(UTC).isoformat(),
        }
    ]
    body = (await client.post("/v1/ingest/prices", json=payload)).json()
    assert body["accepted"] == 0
    assert body["rejected"] == 1
    assert "Nonexistent GPU" in body["errors"][0]


async def test_validation_error_uses_stable_envelope(client):
    payload = [
        {
            "gpu_model_name": "H100 SXM",
            "provider_name": "AWS",
            "region": "us-east-1",
            "instance_type": "x",
            "usd_per_hour": -5,  # violates gt=0
            "availability": "made-up",  # violates pattern
            "observed_at": datetime.now(UTC).isoformat(),
        }
    ]
    response = await client.post("/v1/ingest/prices", json=payload)

    assert response.status_code == 422
    body = response.json()
    assert body["error"] == "validation_error"
    assert {d["field"].split(".")[-1] for d in body["detail"]} >= {
        "usd_per_hour",
        "availability",
    }


async def test_healthz_needs_no_dependencies(client):
    assert (await client.get("/healthz")).json() == {"status": "ok"}


async def test_metrics_exposes_hit_ratio(client):
    await client.get("/v1/gpus/1/prices")
    body = (await client.get("/metrics")).text
    assert "cache_hit_ratio" in body
    assert "cache_hits_total" in body
