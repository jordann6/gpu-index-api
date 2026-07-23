from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class GpuModel(Base):
    __tablename__ = "gpu_models"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    vendor: Mapped[str] = mapped_column(String(32), index=True)
    model_name: Mapped[str] = mapped_column(String(64), unique=True)
    vram_gb: Mapped[int] = mapped_column(Integer)
    arch: Mapped[str] = mapped_column(String(32))
    tdp_w: Mapped[int] = mapped_column(Integer)

    price_points: Mapped[list["PricePoint"]] = relationship(back_populates="gpu_model")
    benchmark_runs: Mapped[list["BenchmarkRun"]] = relationship(back_populates="gpu_model")


class Provider(Base):
    __tablename__ = "providers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    region_scope: Mapped[str] = mapped_column(String(32))

    price_points: Mapped[list["PricePoint"]] = relationship(back_populates="provider")


class PricePoint(Base):
    """Observed hourly rate for a GPU model at a provider and region.

    This is the high-cardinality table. Indexing decisions here are documented
    in docs/query-tuning.md.
    """

    __tablename__ = "price_points"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    gpu_model_id: Mapped[int] = mapped_column(ForeignKey("gpu_models.id"))
    provider_id: Mapped[int] = mapped_column(ForeignKey("providers.id"))
    region: Mapped[str] = mapped_column(String(32))
    instance_type: Mapped[str] = mapped_column(String(64))
    usd_per_hour: Mapped[float] = mapped_column(Numeric(10, 4))
    availability: Mapped[str] = mapped_column(String(16))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    gpu_model: Mapped[GpuModel] = relationship(back_populates="price_points")
    provider: Mapped[Provider] = relationship(back_populates="price_points")

    __table_args__ = (
        UniqueConstraint(
            "gpu_model_id",
            "provider_id",
            "region",
            "instance_type",
            "observed_at",
            name="uq_price_observation",
        ),
    )


class BenchmarkRun(Base):
    __tablename__ = "benchmark_runs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    gpu_model_id: Mapped[int] = mapped_column(ForeignKey("gpu_models.id"))
    workload: Mapped[str] = mapped_column(String(48))
    precision: Mapped[str] = mapped_column(String(16))
    throughput: Mapped[float] = mapped_column(Numeric(12, 3))
    latency_p95_ms: Mapped[float] = mapped_column(Numeric(10, 3))
    run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    gpu_model: Mapped[GpuModel] = relationship(back_populates="benchmark_runs")

    __table_args__ = (Index("ix_benchmark_lookup", "gpu_model_id", "workload", "precision"),)
