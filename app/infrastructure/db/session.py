"""Aurora DSQL through SQLAlchemy + asyncpg.

DSQL has no passwords: each new connection presents a short-lived IAM auth token signed
locally with the Lambda role's credentials. Connections are reused across invocations of
one execution environment and recycled before DSQL's one-hour connection limit.
"""

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

from app.infrastructure.config import get_settings

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


@cache
def _dsql(region: str) -> AuroraDSQLClient:
    return boto3.client("dsql", region_name=region)


@cache
def _sessions() -> async_sessionmaker[AsyncSession]:
    settings = get_settings()
    # A Lambda execution environment serves one request at a time: a tiny pool is plenty.
    engine = make_engine(settings.dsql_endpoint, settings.aws_region, pool_size=1, max_overflow=2)
    return async_sessionmaker(engine, expire_on_commit=False)


def new_session() -> AsyncSession:
    return _sessions()()


async def get_session() -> AsyncIterator[AsyncSession]:
    async with new_session() as session:
        yield session
