"""Guards against the drift that shipped the first deployment untuned.

The tuning indexes originally lived only in scripts/tune.py, so they existed in
every measurement and in no deployment. They now live in app/db/indexes.py and
are applied by migration 0002. These tests assert the definitions are real,
valid SQL and that nothing can quietly diverge.
"""

import re
from pathlib import Path

import pytest
from sqlalchemy import text

from app.db.indexes import (
    INDEX_NAMES,
    TABLES_INDEXED,
    TUNING_INDEXES,
    create_statements,
    drop_statements,
)

MIGRATION = Path(__file__).resolve().parents[2] / "migrations/versions/0002_tuning_indexes.py"


def test_migration_uses_the_shared_definitions():
    """The migration must import the DDL, not restate it.

    A copy-pasted CREATE INDEX in the migration would be free to drift from the
    one the tuning script measures, which is the exact failure being prevented.
    """
    source = MIGRATION.read_text()

    assert "from app.db.indexes import" in source
    assert "create_statements()" in source
    assert "drop_statements()" in source
    assert "CREATE INDEX" not in source, (
        "migration should not hardcode DDL; import it from app.db.indexes"
    )


def test_every_index_has_a_name_table_ddl_and_rationale():
    for name, table, ddl, why in TUNING_INDEXES:
        assert name.startswith("ix_"), f"{name} should follow the ix_ convention"
        assert f"ON {table}" in ddl, f"{name} DDL must target its declared table {table}"
        assert f"INDEX {name}" in ddl, f"{name} DDL must create the index it is keyed by"
        assert len(why) > 40, f"{name} needs a real rationale, not a stub"


def test_names_are_unique():
    assert len(INDEX_NAMES) == len(set(INDEX_NAMES))


def test_drop_statements_cover_every_index():
    dropped = {re.search(r"DROP INDEX IF EXISTS (\S+)", s).group(1) for s in drop_statements()}
    assert dropped == set(INDEX_NAMES), "tune.py's teardown must match what is created"


def test_tables_indexed_is_derived_not_hardcoded():
    assert set(TABLES_INDEXED) == {table for _, table, _, _ in TUNING_INDEXES}


@pytest.mark.usefixtures("engine")
async def test_indexes_apply_cleanly_and_are_visible(session):
    """Execute the real DDL against Postgres, then confirm the planner sees it.

    This is what catches a typo or an unsupported clause: the definitions are
    strings, so nothing else validates them as SQL.
    """
    for statement in drop_statements():
        await session.execute(text(statement))
    await session.commit()

    for statement in create_statements():
        await session.execute(text(statement))
    await session.commit()

    present = {
        row[0]
        for row in await session.execute(
            text("SELECT indexname FROM pg_indexes WHERE schemaname = 'public'")
        )
    }
    missing = set(INDEX_NAMES) - present
    assert not missing, f"indexes did not apply: {missing}"

    # Leave the schema as the tests found it.
    for statement in drop_statements():
        await session.execute(text(statement))
    await session.commit()
