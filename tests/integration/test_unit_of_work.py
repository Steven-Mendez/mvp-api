"""The SQL unit of work and the locks the use cases rely on, on a real Postgres.

- a unique violation surfaces as DuplicateError, at flush or at commit, and rolls back
- any failure rolls the whole transaction back
- a lost race (SQLSTATE 40001) reruns the work, and gives up after ATTEMPTS
- other database errors are not retried
- two concurrent creates at the product quota: the workspace lock lets only one through
- two edits sent with the same `If-Match`: the row lock lets only the first win
"""

import asyncio
import contextlib
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.application.ports import ConcurrencyConflictError, DuplicateError
from app.application.products import ProductService
from app.domain.product import ProductStatus
from app.domain.workspace import Workspace
from app.infrastructure.db.unit_of_work import ATTEMPTS, SqlUnitOfWork
from tests import factories
from tests.fakes import FakeEventPublisher, FakeMediaStorage


class _DriverError(Exception):
    sqlstate = "40001"


def _db_error(sqlstate: str) -> DBAPIError:
    orig = _DriverError()
    orig.sqlstate = sqlstate
    return DBAPIError("COMMIT", None, orig)


class TestTransaction:
    async def test_a_duplicate_is_reported_and_rolled_back(self, uow: SqlUnitOfWork) -> None:
        first = factories.workspace(slug="acme")
        await uow.transaction(lambda: _add(uow, first))

        with pytest.raises(DuplicateError):
            await uow.transaction(lambda: _add(uow, factories.workspace(slug="acme")))

        assert await uow.workspaces.by_slug("acme") is first

    async def test_a_duplicate_at_flush_is_reported(self, uow: SqlUnitOfWork) -> None:
        uow.workspaces.add(factories.workspace(slug="acme"))
        uow.workspaces.add(factories.workspace(slug="acme"))

        with pytest.raises(DuplicateError):
            await uow.flush()

    async def test_a_failure_rolls_everything_back(self, uow: SqlUnitOfWork) -> None:
        workspace = factories.workspace()

        async def work() -> None:
            uow.workspaces.add(workspace)
            await uow.flush()
            raise RuntimeError("halfway")

        with pytest.raises(RuntimeError):
            await uow.transaction(work)

        assert not await uow.workspaces.slug_exists(workspace.slug)

    async def test_a_lost_race_is_retried(
        self, uow: SqlUnitOfWork, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("app.infrastructure.db.unit_of_work._BASE_DELAY_SECONDS", 0)
        attempts = 0

        async def work() -> str:
            nonlocal attempts
            attempts += 1
            if attempts < ATTEMPTS:
                raise _db_error("40001")
            return "done"

        assert await uow.transaction(work) == "done"
        assert attempts == ATTEMPTS

    async def test_losing_every_race_gives_up(
        self, uow: SqlUnitOfWork, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("app.infrastructure.db.unit_of_work._BASE_DELAY_SECONDS", 0)
        attempts = 0

        async def work() -> None:
            nonlocal attempts
            attempts += 1
            raise _db_error("40001")

        with pytest.raises(ConcurrencyConflictError):
            await uow.transaction(work)
        assert attempts == ATTEMPTS

    async def test_other_database_errors_are_not_retried(self, uow: SqlUnitOfWork) -> None:
        attempts = 0

        async def work() -> None:
            nonlocal attempts
            attempts += 1
            raise _db_error("57014")  # query_canceled

        with pytest.raises(DBAPIError):
            await uow.transaction(work)
        assert attempts == 1


async def _add(uow: SqlUnitOfWork, workspace: Workspace) -> None:
    uow.workspaces.add(workspace)


class TestLocks:
    """Real commits on separate connections: `committed` empties the tables afterwards."""

    async def test_the_quota_lets_one_of_two_concurrent_creates_through(
        self, committed: async_sessionmaker[AsyncSession]
    ) -> None:
        workspace = await _committed_workspace(committed)
        both_counted = asyncio.Barrier(2)

        async def create(session: AsyncSession) -> Any:
            uow = SqlUnitOfWork(session)
            count = uow.products.count

            async def count_then_wait(workspace_id: str) -> int:
                # Hold the transaction open after counting, so without the lock both
                # would count zero; with it, the second is still waiting for the lock.
                counted = await count(workspace_id)
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(both_counted.wait(), timeout=0.5)
                return counted

            uow.products.count = count_then_wait  # type: ignore[method-assign]
            service = ProductService(
                uow, FakeMediaStorage(), FakeEventPublisher(), factories.quotas(max_products=1)
            )
            return await service.create(
                workspace.id,
                name="Mug",
                sku=None,
                price=Decimal(1),
                status=ProductStatus.draft,
                description=None,
            )

        async with committed() as a, committed() as b:
            results = await asyncio.gather(create(a), create(b), return_exceptions=True)

        async with committed() as session:
            stored = await SqlUnitOfWork(session).products.count(workspace.id)
        assert stored == 1
        assert sorted(type(r).__name__ for r in results) == ["ConflictError", "Product"]

    async def test_two_edits_from_the_same_version_do_not_both_win(
        self, committed: async_sessionmaker[AsyncSession]
    ) -> None:
        workspace = await _committed_workspace(committed)
        product = factories.product(workspace_id=workspace.id)
        async with committed() as session:
            SqlUnitOfWork(session).products.add(product)
            await session.commit()
        version = product.updated_at

        async def edit(session: AsyncSession, name: str) -> Any:
            uow = SqlUnitOfWork(session)
            get = uow.products.get

            async def get_then_wait(workspace_id: str, product_id: str) -> Any:
                loaded = await get(workspace_id, product_id)
                await asyncio.sleep(0.2)  # without the row lock, both check the same version
                return loaded

            uow.products.get = get_then_wait  # type: ignore[method-assign]
            service = ProductService(
                uow, FakeMediaStorage(), FakeEventPublisher(), factories.quotas()
            )
            return await service.update(workspace.id, product.id, {"name": name}, version)

        async with committed() as a, committed() as b:
            results = await asyncio.gather(edit(a, "Cup"), edit(b, "Bowl"), return_exceptions=True)

        assert sorted(type(r).__name__ for r in results) == [
            "PreconditionFailedError",
            "Product",
        ]


async def _committed_workspace(committed: async_sessionmaker[AsyncSession]) -> Workspace:
    workspace = factories.workspace()
    async with committed() as session:
        SqlUnitOfWork(session).workspaces.add(workspace)
        await session.commit()
    return workspace
