import os
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.cache import cache_dependency
from app.core.config import get_settings
from app.db.models import Base
from app.db.session import session_dependency
from app.main import app

TEST_DB = os.getenv(
    "TEST_DATABASE_URL", "postgresql+asyncpg://gpu:gpu@localhost:5432/gpuindex_test"
)


class FakeRedis:
    """In-memory stand-in. Keeps unit tests off the network."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.counters: dict[str, int] = {}

    async def get(self, key):
        return self.store.get(key)

    async def set(self, key, value, ex=None):
        self.store[key] = value
        return True

    async def delete(self, key):
        return int(self.store.pop(key, None) is not None)

    async def ping(self):
        return True

    def pipeline(self):
        return FakePipeline(self)

    async def scan_iter(self, match="*", count=100):
        prefix = match.rstrip("*")
        for key in list(self.store):
            if key.startswith(prefix):
                yield key


class FakePipeline:
    def __init__(self, client: FakeRedis) -> None:
        self.client = client
        self.ops: list = []

    def incr(self, key, amount=1):
        self.ops.append(("incr", key, amount))
        return self

    def expire(self, key, seconds):
        self.ops.append(("expire", key, seconds))
        return self

    async def execute(self):
        results = []
        for op in self.ops:
            if op[0] == "incr":
                self.client.counters[op[1]] = self.client.counters.get(op[1], 0) + op[2]
                results.append(self.client.counters[op[1]])
            else:
                results.append(True)
        self.ops.clear()
        return results


@pytest_asyncio.fixture(scope="session")
async def engine():
    # NullPool: every checkout is a fresh connection, so nothing is held across
    # tests and the schema teardown cannot deadlock on a pooled connection.
    eng = create_async_engine(TEST_DB, poolclass=NullPool)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture(loop_scope="session")
async def session(engine) -> AsyncIterator[AsyncSession]:
    """Request-scoped session, rolled back after each test."""
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
        await s.rollback()


@pytest.fixture
def fake_cache() -> FakeRedis:
    return FakeRedis()


@pytest_asyncio.fixture
async def client(session, fake_cache) -> AsyncIterator[AsyncClient]:
    """DI overrides swap the real session and cache for test doubles."""
    app.dependency_overrides[session_dependency] = lambda: session
    app.dependency_overrides[cache_dependency] = lambda: fake_cache

    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"X-API-Key": get_settings().api_key_set.copy().pop()},
    ) as c:
        yield c

    app.dependency_overrides.clear()
