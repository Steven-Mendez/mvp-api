"""Aurora DSQL through SQLAlchemy + asyncpg.

DSQL has no passwords: each new connection presents a short-lived IAM auth token signed
locally with the Lambda role's credentials. Connections are reused across invocations of
one execution environment and recycled before DSQL's one-hour connection limit.

In local mode the same code talks to a plain Postgres at `DATABASE_URL` instead.
"""

import re
from collections.abc import AsyncIterator
from functools import cache
from typing import TYPE_CHECKING, Any

import boto3
from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.infrastructure.config import Settings, get_settings

if TYPE_CHECKING:
    from mypy_boto3_dsql import AuroraDSQLClient


def make_engine(endpoint: str, region: str, **options: Any) -> AsyncEngine:
    engine = create_async_engine(
        f"postgresql+asyncpg://admin@{endpoint}:5432/postgres",
        connect_args={"ssl": "require"},
        pool_pre_ping=True,
        pool_recycle=45 * 60,
        **options,
    )

    @event.listens_for(engine.sync_engine, "do_connect")
    def _iam_token(  # pyright: ignore[reportUnusedFunction]
        _dialect: object, _record: object, _cargs: object, cparams: dict[str, Any]
    ) -> None:
        # Injected into the client by botocore (signers.py), so absent from the stubs.
        dsql: Any = _dsql(region)
        cparams["password"] = dsql.generate_db_connect_admin_auth_token(
            Hostname=endpoint, Region=region
        )

    return engine


def engine_for(settings: Settings, **options: Any) -> AsyncEngine:
    """DSQL, or the local Postgres in local mode."""
    if settings.database_url:
        return create_async_engine(settings.database_url, pool_pre_ping=True, **options)
    return make_engine(settings.dsql_endpoint, settings.aws_region, **options)


_ASYNC_INDEX = re.compile(r"\bINDEX\s+ASYNC\b", re.IGNORECASE)


def postgres_ddl(statement: str) -> str:
    """A DSQL statement as plain Postgres takes it: `CREATE [UNIQUE] INDEX ASYNC` is
    DSQL's own syntax, and a plain index is its local equivalent."""
    return _ASYNC_INDEX.sub("INDEX", statement)


@cache
def _dsql(region: str) -> AuroraDSQLClient:
    return boto3.client("dsql", region_name=region)


@cache
def _sessions() -> async_sessionmaker[AsyncSession]:
    settings = get_settings()
    # A Lambda execution environment serves one request at a time: a tiny pool is plenty.
    # Locally one uvicorn serves the web and mobile apps at once: the default pool.
    pool = {} if settings.is_local else {"pool_size": 1, "max_overflow": 2}
    engine = engine_for(settings, **pool)
    return async_sessionmaker(engine, expire_on_commit=False)


def new_session() -> AsyncSession:
    return _sessions()()


async def get_session() -> AsyncIterator[AsyncSession]:
    async with new_session() as session:
        yield session
