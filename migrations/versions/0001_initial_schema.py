"""Initial schema.

Revision ID: 0001
Revises:
"""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "gpu_models",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("vendor", sa.String(length=32), nullable=False),
        sa.Column("model_name", sa.String(length=64), nullable=False),
        sa.Column("vram_gb", sa.Integer(), nullable=False),
        sa.Column("arch", sa.String(length=32), nullable=False),
        sa.Column("tdp_w", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("model_name"),
    )
    op.create_index("ix_gpu_models_vendor", "gpu_models", ["vendor"])

    op.create_table(
        "providers",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("region_scope", sa.String(length=32), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )

    op.create_table(
        "price_points",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("gpu_model_id", sa.Integer(), nullable=False),
        sa.Column("provider_id", sa.Integer(), nullable=False),
        sa.Column("region", sa.String(length=32), nullable=False),
        sa.Column("instance_type", sa.String(length=64), nullable=False),
        sa.Column("usd_per_hour", sa.Numeric(precision=10, scale=4), nullable=False),
        sa.Column("availability", sa.String(length=16), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["gpu_model_id"], ["gpu_models.id"]),
        sa.ForeignKeyConstraint(["provider_id"], ["providers.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "gpu_model_id",
            "provider_id",
            "region",
            "instance_type",
            "observed_at",
            name="uq_price_observation",
        ),
    )

    op.create_table(
        "benchmark_runs",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("gpu_model_id", sa.Integer(), nullable=False),
        sa.Column("workload", sa.String(length=48), nullable=False),
        sa.Column("precision", sa.String(length=16), nullable=False),
        sa.Column("throughput", sa.Numeric(precision=12, scale=3), nullable=False),
        sa.Column("latency_p95_ms", sa.Numeric(precision=10, scale=3), nullable=False),
        sa.Column("run_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["gpu_model_id"], ["gpu_models.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_benchmark_lookup",
        "benchmark_runs",
        ["gpu_model_id", "workload", "precision"],
    )


def downgrade() -> None:
    op.drop_index("ix_benchmark_lookup", table_name="benchmark_runs")
    op.drop_table("benchmark_runs")
    op.drop_table("price_points")
    op.drop_table("providers")
    op.drop_index("ix_gpu_models_vendor", table_name="gpu_models")
    op.drop_table("gpu_models")
