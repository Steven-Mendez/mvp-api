"""Database fixtures for the integration and end-to-end tiers (a pytest plugin, loaded by
tests/conftest.py; nothing starts until a test asks for them).

A throwaway Postgres 16 (what Aurora DSQL speaks), migrated with the real Alembic
migrations, one per test session. Needs Docker.

Each test runs inside a transaction that is rolled back afterwards: the code under test
commits to a SAVEPOINT, so it behaves as in production and leaves nothing behind. Tests
about concurrency need real commits across connections: they use `committed`, which
empties the tables afterwards instead.
"""

import os
import subprocess
import sys
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from testcontainers.community.postgres import PostgresContainer

from app.infrastructure.db.tables import metadata
from app.infrastructure.db.unit_of_work import SqlUnitOfWork

ROOT = Path(__file__).parents[1]


@pytest.fixture(scope="session")
def database_url() -> Iterator[str]:
    with PostgresContainer("postgres:16-alpine", driver="asyncpg") as postgres:
        url = postgres.get_connection_url()
        env = os.environ | {"ENVIRONMENT": "local", "DATABASE_URL": url}
        subprocess.run(  # our own interpreter running our own migrations
            [sys.executable, "-m", "alembic", "upgrade", "head"], cwd=ROOT, env=env, check=True
        )
        yield url


@pytest.fixture(scope="session")
async def engine(database_url: str) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(database_url)
    yield engine
    await engine.dispose()


@pytest.fixture
async def session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    async with engine.connect() as connection:
        outer = await connection.begin()
        session = AsyncSession(
            bind=connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
        )
        yield session
        await session.close()
        await outer.rollback()


@pytest.fixture
def uow(session: AsyncSession) -> SqlUnitOfWork:
    return SqlUnitOfWork(session)


@pytest.fixture
async def committed(engine: AsyncEngine) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Sessions that really commit, for tests that need several connections at once."""
    yield async_sessionmaker(engine, expire_on_commit=False)
    tables = ", ".join(table.name for table in metadata.sorted_tables)
    async with engine.begin() as connection:
        await connection.execute(text(f"TRUNCATE {tables}"))
