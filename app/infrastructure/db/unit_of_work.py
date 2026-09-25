"""The unit of work over one SQLAlchemy session.

Aurora DSQL runs every transaction optimistically: one that lost a race fails at commit
with SQLSTATE 40001. `transaction()` reruns the work from the top in that case, which is
why the use cases keep storage and HTTP calls outside it.
"""

import asyncio
import random
from collections.abc import Awaitable, Callable

from loguru import logger
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.ports import ConcurrencyConflictError, DuplicateError
from app.infrastructure.db.repositories import (
    SqlInvitationRepository,
    SqlMemberRepository,
    SqlProductRepository,
    SqlRoleRepository,
    SqlUserRepository,
    SqlWorkspaceRepository,
)

ATTEMPTS = 3
_BASE_DELAY_SECONDS = 0.05
_SERIALIZATION_FAILURE = "40001"


def _sqlstate(exc: DBAPIError) -> str | None:
    orig = exc.orig
    return getattr(orig, "sqlstate", None) or getattr(
        getattr(orig, "__cause__", None), "sqlstate", None
    )


class SqlUnitOfWork:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self.users = SqlUserRepository(session)
        self.workspaces = SqlWorkspaceRepository(session)
        self.roles = SqlRoleRepository(session)
        self.members = SqlMemberRepository(session)
        self.invitations = SqlInvitationRepository(session)
        self.products = SqlProductRepository(session)

    async def flush(self) -> None:
        try:
            await self._session.flush()
        except IntegrityError as exc:
            raise DuplicateError(str(exc.orig)) from exc

    async def transaction[T](self, work: Callable[[], Awaitable[T]]) -> T:
        for attempt in range(1, ATTEMPTS + 1):
            try:
                result = await work()
                await self._session.commit()
            except IntegrityError as exc:
                await self._session.rollback()
                raise DuplicateError(str(exc.orig)) from exc
            except DuplicateError:
                await self._session.rollback()
                raise
            except DBAPIError as exc:
                await self._session.rollback()
                if _sqlstate(exc) != _SERIALIZATION_FAILURE:
                    raise
                if attempt == ATTEMPTS:
                    raise ConcurrencyConflictError from exc
                delay = _BASE_DELAY_SECONDS * attempt + random.uniform(0, _BASE_DELAY_SECONDS)  # noqa: S311
                logger.info("Serialization conflict ({}/{}), retrying", attempt, ATTEMPTS)
                await asyncio.sleep(delay)
            except BaseException:
                await self._session.rollback()
                raise
            else:
                return result
        raise AssertionError("unreachable")
