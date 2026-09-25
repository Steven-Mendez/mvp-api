"""Alembic against Aurora DSQL.

DSQL allows one DDL statement per transaction and no DDL next to DML, so migrations run
in AUTOCOMMIT: every statement, including Alembic's version bookkeeping, commits alone.
Write each migration as a list of single statements, and create indexes with
`CREATE INDEX ASYNC` on tables that may already hold data.
"""

import asyncio
import os

from alembic import context
from sqlalchemy import Connection

from app.infrastructure.db.session import make_engine
from app.infrastructure.db.tables import metadata


def _migrate(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=metadata)
    with context.begin_transaction():
        context.run_migrations()


async def _online() -> None:
    engine = make_engine(
        os.environ["DSQL_ENDPOINT"], os.environ["AWS_REGION"], isolation_level="AUTOCOMMIT"
    )
    async with engine.connect() as connection:
        await connection.run_sync(_migrate)
    await engine.dispose()


if context.is_offline_mode():
    raise SystemExit("Offline migrations are not supported: run them against the cluster")
asyncio.run(_online())
