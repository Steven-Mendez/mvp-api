"""Scheduled jobs: composition root of the `jobs` Lambda (the `jobs` target of the
Dockerfile, a plain Lambda handler with no web server).

EventBridge Scheduler invokes `handler` asynchronously with `{"job": "<name>"}`
(infra/lambda.tf). A job that fails raises, so the invocation fails: Lambda counts it in
`Errors`, retries it twice, then parks the event in the failed-jobs queue (and an alarm
emails about both; infra/monitoring.tf).
"""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.pool import NullPool

from app.application.purge import WorkspacePurge
from app.infrastructure.aws.storage import S3MediaStorage
from app.infrastructure.config import get_settings
from app.infrastructure.db.session import make_engine
from app.infrastructure.db.unit_of_work import SqlUnitOfWork
from app.infrastructure.logging import setup_logging

setup_logging(get_settings().log_level)


async def purge_deleted_workspaces() -> dict[str, Any]:
    settings = get_settings()
    media = S3MediaStorage(
        settings.media_bucket,
        settings.media_base_url,
        settings.media_max_upload_bytes,
        settings.media_thumbnail_size,
    )
    # A new event loop runs every invocation, and connections belong to the loop that
    # opened them: this engine lives for one run only.
    engine = make_engine(settings.dsql_endpoint, settings.aws_region, poolclass=NullPool)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            purged = await WorkspacePurge(SqlUnitOfWork(session), media).purge_all()
    finally:
        await engine.dispose()
    return {"purged_workspaces": purged}


JOBS: dict[str, Callable[[], Awaitable[dict[str, Any]]]] = {
    "purge-deleted-workspaces": purge_deleted_workspaces,
}


def handler(event: dict[str, Any], _context: object) -> dict[str, Any]:
    name = event.get("job")
    job = JOBS.get(name) if isinstance(name, str) else None
    if job is None:
        raise ValueError(f"Unknown job {name!r}; known jobs: {', '.join(JOBS)}")
    logger.info("Job {} started", name)
    result = asyncio.run(job())
    logger.info("Job {} finished: {}", name, result)
    return result
