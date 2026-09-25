"""WorkspacePurge.

- only soft-deleted workspaces go, with their roles, members, invitations and products
- products go in batches, their files and the logo with them
- live workspaces and their files stay
"""

from decimal import Decimal

import pytest

from app.domain.product import ProductStatus
from app.domain.workspace import Workspace
from tests import factories
from tests.unit.conftest import World


async def _with_products(world: World, workspace: Workspace, count: int) -> list[str]:
    keys: list[str] = []
    for n in range(count):
        product = await world.products.create(
            workspace.id,
            name=f"P{n}",
            sku=None,
            price=Decimal(1),
            status=ProductStatus.active,
            description=None,
        )
        await world.products.confirm_image(workspace.id, product.id, world.media.upload())
        keys += (await world.products.get(workspace.id, product.id)).storage_keys
    return keys


async def test_a_deleted_workspace_goes_with_everything_in_it(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.application.purge.PRODUCT_BATCH", 2)
    world.quotas = factories.quotas(max_products=10, max_storage_bytes=10**9)
    owner = await world.user("owner@example.com")
    doomed = await world.workspace(owner, "doomed")
    await world.join(doomed, "member@example.com")
    await world.workspaces.set_logo(doomed.id, world.media.upload(), owner)
    await _with_products(world, doomed, 5)  # three batches of two
    await world.workspaces.delete(doomed.id, owner)

    purged = await world.purge.purge_all()

    assert purged == 1
    assert world.uow.products.batches == [2, 2, 1]  # DSQL caps rows per transaction
    assert await world.uow.workspaces.get_deleted(doomed.id) is None
    assert await world.uow.roles.of_workspace(doomed.id) == []
    assert await world.uow.products.count(doomed.id) == 0
    assert world.media.objects == set()


async def test_live_workspaces_and_their_files_stay(world: World) -> None:
    owner = await world.user("owner@example.com")
    doomed = await world.workspace(owner, "doomed")
    kept = await world.workspace(owner, "kept")
    kept_keys = await _with_products(world, kept, 1)
    await world.workspaces.delete(doomed.id, owner)

    await world.purge.purge_all()

    assert await world.uow.products.count(kept.id) == 1
    assert len(await world.uow.roles.of_workspace(kept.id)) == 4
    assert world.media.objects == set(kept_keys)


async def test_nothing_to_purge_is_a_no_op(world: World) -> None:
    await world.workspace(await world.user("owner@example.com"))

    assert await world.purge.purge_all() == 0
