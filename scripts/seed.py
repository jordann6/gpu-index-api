"""Seed synthetic pricing and benchmark data.

Volume is the point. A few thousand rows would make every plan look fine and
prove nothing about indexing. Default target is 5M price points.

Usage: python scripts/seed.py [--rows 5000000]
"""

import argparse
import asyncio
import random
from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from app.db.models import Base
from app.db.session import dispose_engine, get_engine, get_sessionmaker

random.seed(1337)

GPUS = [
    ("NVIDIA", "H100 SXM", 80, "Hopper", 700),
    ("NVIDIA", "H100 PCIe", 80, "Hopper", 350),
    ("NVIDIA", "H200 SXM", 141, "Hopper", 700),
    ("NVIDIA", "A100 SXM 80GB", 80, "Ampere", 400),
    ("NVIDIA", "A100 PCIe 40GB", 40, "Ampere", 250),
    ("NVIDIA", "L40S", 48, "Ada Lovelace", 350),
    ("NVIDIA", "L4", 24, "Ada Lovelace", 72),
    ("NVIDIA", "A10G", 24, "Ampere", 150),
    ("NVIDIA", "RTX 4090", 24, "Ada Lovelace", 450),
    ("NVIDIA", "RTX 6000 Ada", 48, "Ada Lovelace", 300),
    ("NVIDIA", "V100 32GB", 32, "Volta", 300),
    ("NVIDIA", "T4", 16, "Turing", 70),
    ("NVIDIA", "B200", 192, "Blackwell", 1000),
    ("NVIDIA", "GB200 NVL2", 384, "Blackwell", 1200),
    ("AMD", "MI300X", 192, "CDNA3", 750),
    ("AMD", "MI250X", 128, "CDNA2", 560),
    ("AMD", "MI210", 64, "CDNA2", 300),
    ("AMD", "MI325X", 256, "CDNA3", 1000),
    ("Intel", "Gaudi 2", 96, "Gaudi", 600),
    ("Intel", "Gaudi 3", 128, "Gaudi", 900),
    ("Intel", "Max 1550", 128, "Xe-HPC", 600),
    ("Google", "TPU v5e", 16, "TPU", 200),
    ("Google", "TPU v5p", 95, "TPU", 450),
    ("Google", "TPU v6e", 32, "TPU", 300),
    ("AWS", "Trainium2", 96, "Trainium", 500),
    ("AWS", "Inferentia2", 32, "Inferentia", 175),
]

PROVIDERS = [
    ("AWS", "global"),
    ("Google Cloud", "global"),
    ("Microsoft Azure", "global"),
    ("CoreWeave", "us-eu"),
    ("Lambda Labs", "us"),
    ("RunPod", "global"),
    ("Vast.ai", "global"),
    ("Paperspace", "us-eu"),
    ("Oracle Cloud", "global"),
    ("Crusoe", "us"),
]

REGIONS = [
    "us-east-1",
    "us-east-2",
    "us-west-1",
    "us-west-2",
    "us-central-1",
    "ca-central-1",
    "eu-west-1",
    "eu-west-2",
    "eu-central-1",
    "eu-north-1",
    "ap-northeast-1",
    "ap-northeast-2",
    "ap-south-1",
    "ap-southeast-1",
    "ap-southeast-2",
    "sa-east-1",
    "me-central-1",
    "af-south-1",
    "eu-south-1",
    "us-gov-west-1",
]

WORKLOADS = [
    "llm-inference-7b",
    "llm-inference-70b",
    "llm-training-7b",
    "stable-diffusion-xl",
    "resnet50-training",
    "bert-large-inference",
    "whisper-transcription",
    "embedding-generation",
]
PRECISIONS = ["fp32", "fp16", "bf16", "fp8", "int8"]
AVAILABILITY = ["available"] * 7 + ["constrained"] * 2 + ["unavailable"]

# Rough anchor prices so the index output is not nonsense.
BASE_PRICE = {
    "H100 SXM": 3.20,
    "H100 PCIe": 2.40,
    "H200 SXM": 4.10,
    "B200": 6.50,
    "GB200 NVL2": 9.80,
    "A100 SXM 80GB": 1.90,
    "A100 PCIe 40GB": 1.30,
    "L40S": 1.10,
    "L4": 0.45,
    "A10G": 0.60,
    "RTX 4090": 0.55,
    "RTX 6000 Ada": 0.95,
    "V100 32GB": 0.65,
    "T4": 0.28,
    "MI300X": 2.80,
    "MI250X": 1.60,
    "MI210": 0.90,
    "MI325X": 3.40,
    "Gaudi 2": 1.40,
    "Gaudi 3": 2.20,
    "Max 1550": 1.20,
    "TPU v5e": 1.05,
    "TPU v5p": 3.60,
    "TPU v6e": 2.10,
    "Trainium2": 1.75,
    "Inferentia2": 0.55,
}


async def create_schema() -> None:
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    print("schema created")


async def seed_dimensions() -> tuple[dict, dict]:
    async with get_sessionmaker()() as session:
        for vendor, name, vram, arch, tdp in GPUS:
            await session.execute(
                text(
                    "INSERT INTO gpu_models (vendor, model_name, vram_gb, arch, tdp_w) "
                    "VALUES (:v, :n, :vr, :a, :t)"
                ),
                {"v": vendor, "n": name, "vr": vram, "a": arch, "t": tdp},
            )
        for name, scope in PROVIDERS:
            await session.execute(
                text("INSERT INTO providers (name, region_scope) VALUES (:n, :s)"),
                {"n": name, "s": scope},
            )
        await session.commit()

        gpus = {
            r.model_name: r.id
            for r in await session.execute(text("SELECT id, model_name FROM gpu_models"))
        }
        providers = {
            r.name: r.id for r in await session.execute(text("SELECT id, name FROM providers"))
        }
    print(f"seeded {len(gpus)} gpu models, {len(providers)} providers")
    return gpus, providers


async def seed_benchmarks(gpus: dict) -> None:
    """One run per gpu/workload/precision, throughput scaled off vram and tdp."""
    rows = []
    now = datetime.now(UTC)
    for _vendor, name, vram, _arch, tdp in GPUS:
        for workload in WORKLOADS:
            for precision in PRECISIONS:
                scale = {"fp32": 1.0, "fp16": 2.1, "bf16": 2.0, "fp8": 3.8, "int8": 4.2}
                base = (vram * 0.9 + tdp * 0.15) * scale[precision]
                throughput = round(base * random.uniform(0.85, 1.15), 3)
                rows.append(
                    {
                        "g": gpus[name],
                        "w": workload,
                        "p": precision,
                        "t": throughput,
                        "l": round(1000.0 / max(throughput, 0.1) * random.uniform(0.8, 1.2), 3),
                        "r": now - timedelta(days=random.randint(0, 29)),
                    }
                )

    async with get_sessionmaker()() as session:
        await session.execute(
            text(
                "INSERT INTO benchmark_runs "
                "(gpu_model_id, workload, precision, throughput, latency_p95_ms, run_at) "
                "VALUES (:g, :w, :p, :t, :l, :r)"
            ),
            rows,
        )
        await session.commit()
    print(f"seeded {len(rows)} benchmark runs")


async def seed_prices(gpus: dict, providers: dict, target_rows: int) -> None:
    """Bulk insert via COPY-style executemany batches."""
    now = datetime.now(UTC)
    gpu_names = [g[1] for g in GPUS]
    provider_names = [p[0] for p in PROVIDERS]

    batch_size = 25_000
    written = 0
    seen: set[tuple] = set()

    async with get_sessionmaker()() as session:
        while written < target_rows:
            batch = []
            while len(batch) < batch_size and written + len(batch) < target_rows:
                gname = random.choice(gpu_names)
                pname = random.choice(provider_names)
                region = random.choice(REGIONS)
                itype = (
                    f"{pname[:3].lower()}-{gname.split()[0].lower()}-{random.choice([1, 2, 4, 8])}x"
                )
                hours_ago = random.randint(0, 30 * 24)
                observed = now - timedelta(hours=hours_ago)

                natural = (gpus[gname], providers[pname], region, itype, observed)
                if natural in seen:
                    continue
                seen.add(natural)

                base = BASE_PRICE[gname]
                # Provider spread plus a mild time drift so medians are meaningful.
                drift = 1.0 + (hours_ago / (30 * 24)) * random.uniform(-0.12, 0.18)
                price = round(base * random.uniform(0.72, 1.45) * drift, 4)

                batch.append(
                    {
                        "g": gpus[gname],
                        "pr": providers[pname],
                        "re": region,
                        "it": itype,
                        "u": max(price, 0.01),
                        "av": random.choice(AVAILABILITY),
                        "ob": observed,
                    }
                )

            if not batch:
                break

            await session.execute(
                text(
                    "INSERT INTO price_points "
                    "(gpu_model_id, provider_id, region, instance_type, usd_per_hour, "
                    "availability, observed_at) "
                    "VALUES (:g, :pr, :re, :it, :u, :av, :ob) "
                    "ON CONFLICT ON CONSTRAINT uq_price_observation DO NOTHING"
                ),
                batch,
            )
            await session.commit()
            written += len(batch)
            if written % 250_000 == 0 or written >= target_rows:
                print(f"  {written:,} / {target_rows:,} price points")

            # Cap memory on the dedupe set for very large runs.
            if len(seen) > 2_000_000:
                seen.clear()

    async with get_sessionmaker()() as session:
        await session.execute(text("ANALYZE price_points"))
        await session.execute(text("ANALYZE benchmark_runs"))
        await session.commit()
    print("analyzed")


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rows", type=int, default=5_000_000)
    args = parser.parse_args()

    await create_schema()
    gpus, providers = await seed_dimensions()
    await seed_benchmarks(gpus)
    await seed_prices(gpus, providers, args.rows)
    await dispose_engine()
    print("seed complete")


if __name__ == "__main__":
    asyncio.run(main())
