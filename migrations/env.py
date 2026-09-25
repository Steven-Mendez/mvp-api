"""Alembic against Aurora DSQL.

DSQL allows one DDL statement per transaction and no DDL next to DML, so migrations run
in AUTOCOMMIT: every statement, including Alembic's version bookkeeping, commits alone.
Write each migration as a list of single statements, and create indexes with
`CREATE INDEX ASYNC` on tables that may already hold data.

In local mode (`ENVIRONMENT=local`, `make up`) they run against the Postgres at
`DATABASE_URL` instead, which has no `ASYNC` indexes: there the keyword is dropped on the
way out. Only that flag picks Postgres, so a stray `DATABASE_URL` in a shell never
diverts a production migration.
"""

import asyncio
import os
from typing import Any

from alembic import context
from sqlalchemy import Connection, event
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.infrastructure.db.session import make_engine, postgres_ddl
from app.infrastructure.db.tables import metadata


def _migrate(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=metadata)
    with context.begin_transaction():
        context.run_migrations()


def _postgres(url: str) -> AsyncEngine:
    engine = create_async_engine(url, isolation_level="AUTOCOMMIT")

    @event.listens_for(engine.sync_engine, "before_cursor_execute", retval=True)
    def _drop_async(  # pyright: ignore[reportUnusedFunction]
        _conn: object, _cursor: object, statement: str, parameters: Any, *_: object
    ) -> tuple[str, Any]:
        return postgres_ddl(statement), parameters

    return engine


async def _online() -> None:
    if os.environ.get("ENVIRONMENT") == "local":
        engine = _postgres(os.environ["DATABASE_URL"])
    else:
        engine = make_engine(
            os.environ["DSQL_ENDPOINT"], os.environ["AWS_REGION"], isolation_level="AUTOCOMMIT"
        )
    async with engine.connect() as connection:
        await connection.run_sync(_migrate)
    await engine.dispose()


if context.is_offline_mode():
    raise SystemExit("Offline migrations are not supported: run them against the cluster")
asyncio.run(_online())
