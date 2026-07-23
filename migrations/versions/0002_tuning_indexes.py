"""Performance indexes for the hot query, index ranking, and keyset pagination.

Kept as its own revision so the performance work is a reviewable change rather
than being buried in the initial schema. The DDL lives in app/db/indexes.py so
this migration and scripts/tune.py cannot drift apart.

Measured effect on 2M rows: hot query 166.6ms to 23.8ms (85.5%), heap blocks
read 43,215 to 1,198. See docs/query-tuning.md.

Revision ID: 0002
Revises: 0001
"""

from alembic import op

from app.db.indexes import TABLES_INDEXED, create_statements, drop_statements

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for statement in create_statements():
        op.execute(statement)

    # Index-only scans need an up-to-date visibility map, otherwise Postgres
    # falls back to heap fetches and the indexes underdeliver. ANALYZE also
    # refreshes the stats the planner uses to choose them at all.
    for table in TABLES_INDEXED:
        op.execute(f"ANALYZE {table}")


def downgrade() -> None:
    for statement in drop_statements():
        op.execute(statement)
