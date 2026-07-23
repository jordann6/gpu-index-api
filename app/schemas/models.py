from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class GpuModelOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    vendor: str
    model_name: str
    vram_gb: int
    arch: str
    tdp_w: int


class PricePointOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    provider: str
    region: str
    instance_type: str
    usd_per_hour: float
    availability: str
    observed_at: datetime


class PricePointIn(BaseModel):
    """Ingest payload. Deliberately separate from the read model."""

    gpu_model_name: str = Field(min_length=1, max_length=64)
    provider_name: str = Field(min_length=1, max_length=64)
    region: str = Field(min_length=1, max_length=32)
    instance_type: str = Field(min_length=1, max_length=64)
    usd_per_hour: float = Field(gt=0, le=1000)
    availability: str = Field(pattern="^(available|constrained|unavailable)$")
    observed_at: datetime


class PriceSummaryOut(BaseModel):
    gpu_model: str
    region_count: int
    provider_count: int
    cheapest_usd_per_hour: float
    median_30d_usd_per_hour: float | None
    points: list[PricePointOut]


class BenchmarkOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    workload: str
    precision: str
    throughput: float
    latency_p95_ms: float
    run_at: datetime


class IndexEntryOut(BaseModel):
    """Price-per-unit-throughput ranking. The derived metric worth caching."""

    gpu_model: str
    vendor: str
    workload: str
    precision: str
    best_usd_per_hour: float
    throughput: float
    usd_per_million_units: float
    provider: str
    region: str


class Page[T](BaseModel):
    items: list[T]
    count: int
    next_cursor: str | None = None


class IngestResult(BaseModel):
    accepted: int
    rejected: int
    cache_entries_invalidated: int
    errors: list[str] = Field(default_factory=list)


class ErrorEnvelope(BaseModel):
    error: str
    detail: str | list[dict] | None = None
