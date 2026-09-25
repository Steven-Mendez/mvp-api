"""ProductService.

- creating one tells every member, and counts the quota under the workspace lock
- quotas are per workspace
- a product is invisible from any other workspace, whatever the operation
- a stale `If-Match` changes nothing; the current one applies
- deleting a product deletes its files
- uploads: images only, and none for a product that is full
- confirming an upload is idempotent, respects the storage quota (an exact fit passes)
  and leaves no orphan file when it fails or is retried after a lost race
- removing an image deletes its files; reordering picks the cover
"""

from collections.abc import Awaitable, Callable
from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest

from app.application.products import ProductService
from app.domain.errors import (
    ConflictError,
    NotFoundError,
    PreconditionFailedError,
    UnsupportedMediaTypeError,
)
from app.domain.product import MAX_IMAGES_PER_PRODUCT, Product, ProductStatus
from app.domain.workspace import Workspace
from tests import factories
from tests.unit.conftest import World


async def _acme(world: World) -> Workspace:
    return await world.workspace(await world.user("owner@example.com"))


async def _product(world: World, workspace: Workspace, name: str = "Mug") -> Product:
    return await world.products.create(
        workspace.id,
        name=name,
        sku=None,
        price=Decimal("9.99"),
        status=ProductStatus.draft,
        description=None,
    )


async def _image(world: World, workspace: Workspace, product: Product, size: int = 1000) -> str:
    image = await world.products.confirm_image(workspace.id, product.id, world.media.upload(size))
    return image.id


class TestCreate:
    async def test_every_field_is_stored(self, world: World) -> None:
        acme = await _acme(world)

        created = await world.products.create(
            acme.id,
            name="Mug",
            sku="MUG-1",
            price=Decimal("9.99"),
            status=ProductStatus.active,
            description="Ceramic",
        )

        stored = await world.products.get(acme.id, created.id)
        assert (stored.sku, stored.price, stored.status, stored.description) == (
            "MUG-1",
            Decimal("9.99"),
            ProductStatus.active,
            "Ceramic",
        )

    async def test_every_member_hears_of_it(self, world: World) -> None:
        acme = await _acme(world)
        member = await world.join(acme, "member@example.com")

        product = await _product(world, acme)

        event, recipients = world.events.published[-1]
        assert (event.action, event.id, event.owner_id) == ("created", product.id, acme.id)
        assert recipients == sorted([acme.owner_id, member])

    async def test_the_quota_is_counted_under_the_workspace_lock(self, world: World) -> None:
        acme = await _acme(world)
        world.uow.locks.clear()

        await _product(world, acme)

        assert world.uow.locks == [acme.id]

    async def test_a_workspace_at_its_quota_takes_no_more(self, world: World) -> None:
        acme = await _acme(world)
        for n in range(world.quotas.max_products):
            await _product(world, acme, f"P{n}")

        with pytest.raises(ConflictError, match="limit of 3 products"):
            await _product(world, acme, "One too many")

        assert await world.uow.products.count(acme.id) == world.quotas.max_products

    async def test_quotas_are_per_workspace(self, world: World) -> None:
        acme = await _acme(world)
        other = await world.workspace(acme.owner_id, "other")
        for n in range(world.quotas.max_products):
            await _product(world, acme, f"P{n}")

        await _product(world, other)

        assert await world.uow.products.count(other.id) == 1


_FOREIGN_OPERATIONS: dict[str, Callable[[World, str, str], Awaitable[Any]]] = {
    "get": lambda w, ws, p: w.products.get(ws, p),
    "update": lambda w, ws, p: w.products.update(ws, p, {"name": "Stolen"}, None),
    "delete": lambda w, ws, p: w.products.delete(ws, p),
    "presign": lambda w, ws, p: w.products.request_image_upload(ws, p, "image/png"),
    "confirm": lambda w, ws, p: w.products.confirm_image(ws, p, w.media.upload()),
    "reorder": lambda w, ws, p: w.products.reorder_images(ws, p, []),
    "remove image": lambda w, ws, p: w.products.remove_image(ws, p, "any"),
}


@pytest.mark.parametrize("operation", _FOREIGN_OPERATIONS)
async def test_a_product_is_not_found_from_another_workspace(world: World, operation: str) -> None:
    acme = await _acme(world)
    other = await world.workspace(acme.owner_id, "other")
    product = await _product(world, acme)

    with pytest.raises(NotFoundError):
        await _FOREIGN_OPERATIONS[operation](world, other.id, product.id)

    assert (await world.products.get(acme.id, product.id)).name == "Mug"


_CHANGES: dict[str, tuple[str, Callable[[World, Workspace, Product], Awaitable[Any]]]] = {
    "update": ("updated", lambda w, ws, p: w.products.update(ws.id, p.id, {"name": "Cup"}, None)),
    "delete": ("deleted", lambda w, ws, p: w.products.delete(ws.id, p.id)),
    "confirm": ("updated", lambda w, ws, p: _image(w, ws, p)),
    "reorder": ("updated", lambda w, ws, p: w.products.reorder_images(ws.id, p.id, [])),
}


@pytest.mark.parametrize("operation", _CHANGES)
async def test_every_change_tells_every_member(world: World, operation: str) -> None:
    acme = await _acme(world)
    member = await world.join(acme, "member@example.com")
    product = await _product(world, acme)
    action, change = _CHANGES[operation]

    await change(world, acme, product)

    assert world.events.last() == (
        "product",
        action,
        product.id,
        acme.id,
        sorted([acme.owner_id, member]),
    )
    assert world.events.published[-1][0].at.tzinfo is not None


async def test_removing_an_image_tells_every_member(world: World) -> None:
    acme = await _acme(world)
    product = await _product(world, acme)
    image = await _image(world, acme, product)

    await world.products.remove_image(acme.id, product.id, image)

    assert world.events.last() == ("product", "updated", product.id, acme.id, [acme.owner_id])


class TestUpdate:
    async def test_a_stale_version_changes_nothing(self, world: World) -> None:
        acme = await _acme(world)
        product = await _product(world, acme)
        stale = product.updated_at - timedelta(seconds=1)

        with pytest.raises(PreconditionFailedError):
            await world.products.update(acme.id, product.id, {"name": "Cup"}, stale)

        assert (await world.products.get(acme.id, product.id)).name == "Mug"
        assert world.events.actions() == ["created"]

    async def test_the_current_version_applies_and_is_announced(self, world: World) -> None:
        acme = await _acme(world)
        product = await _product(world, acme)

        await world.products.update(acme.id, product.id, {"name": "Cup"}, product.updated_at)

        assert (await world.products.get(acme.id, product.id)).name == "Cup"
        assert world.events.actions() == ["created", "updated"]


async def test_deleting_a_product_deletes_its_files(world: World) -> None:
    acme = await _acme(world)
    product = await _product(world, acme)
    await _image(world, acme, product)

    await world.products.delete(acme.id, product.id)

    assert not await world.uow.products.exists(acme.id, product.id)
    assert world.media.objects == set()
    assert world.events.actions()[-1] == "deleted"


class TestUploads:
    async def test_an_image_type_gets_a_ticket(self, world: World) -> None:
        acme = await _acme(world)
        product = await _product(world, acme)

        ticket = await world.products.request_image_upload(acme.id, product.id, "image/webp")

        assert ticket.max_bytes == world.media.max_bytes

    async def test_other_types_are_refused(self, world: World) -> None:
        acme = await _acme(world)
        product = await _product(world, acme)

        with pytest.raises(UnsupportedMediaTypeError):
            await world.products.request_image_upload(acme.id, product.id, "application/pdf")

    async def test_a_full_product_takes_no_more(self, world: World) -> None:
        world.quotas = factories.quotas(max_storage_bytes=10**9)
        acme = await _acme(world)
        product = await _product(world, acme)
        for _ in range(MAX_IMAGES_PER_PRODUCT):
            await _image(world, acme, product)

        key = world.media.upload()

        with pytest.raises(ConflictError):
            await world.products.request_image_upload(acme.id, product.id, "image/png")
        with pytest.raises(ConflictError):
            await world.products.confirm_image(acme.id, product.id, key)

        assert key in world.media.pending  # refused before any image work


class TestConfirm:
    async def test_confirming_twice_returns_the_same_image(self, world: World) -> None:
        acme = await _acme(world)
        product = await _product(world, acme)
        key = world.media.upload()
        first = await world.products.confirm_image(acme.id, product.id, key)

        again = await world.products.confirm_image(acme.id, product.id, key)

        assert again.id == first.id
        assert len((await world.products.get(acme.id, product.id)).images) == 1

    async def test_the_storage_quota_refuses_before_any_work(self, world: World) -> None:
        acme = await _acme(world)
        product = await _product(world, acme)
        await _image(world, acme, product, size=6000)
        key = world.media.upload(size=4001)  # 10,001 > 10,000 bytes

        with pytest.raises(ConflictError, match="storage limit"):
            await world.products.confirm_image(acme.id, product.id, key)

        assert key in world.media.pending  # never processed
        assert len(world.media.objects) == 2  # the first image and its thumbnail

    async def test_an_exact_fit_is_accepted(self, world: World) -> None:
        acme = await _acme(world)
        product = await _product(world, acme)
        await _image(world, acme, product, size=6000)

        await _image(world, acme, product, size=4000)

        assert await world.uow.products.storage_bytes(acme.id) == world.quotas.max_storage_bytes

    async def test_the_attach_is_counted_under_the_workspace_lock(self, world: World) -> None:
        acme = await _acme(world)
        product = await _product(world, acme)
        world.uow.locks.clear()

        image = await world.products.confirm_image(acme.id, product.id, world.media.upload())

        assert world.uow.locks == [acme.id]
        assert (image.content_type, image.size_bytes) == ("image/webp", 1000)

    async def test_an_upload_that_filled_the_quota_meanwhile_wins(self, world: World) -> None:
        acme = await _acme(world)
        product = await _product(world, acme)
        other = await _product(world, acme, "Other")
        rival = world.media.upload(size=6000)
        world.media.while_processing = lambda: world.products.confirm_image(
            acme.id, other.id, rival
        )

        with pytest.raises(ConflictError, match="storage limit"):
            await _image(world, acme, product, size=6000)

        assert await world.uow.products.storage_bytes(acme.id) == 6000
        assert (await world.products.get(acme.id, product.id)).images == []
        assert len(world.media.objects) == 2  # the rival's image and thumbnail only

    async def test_a_concurrent_confirm_of_the_same_upload_returns_its_image(
        self, world: World
    ) -> None:
        acme = await _acme(world)
        product = await _product(world, acme)
        key = world.media.upload()
        first = await world.products.confirm_image(acme.id, product.id, key)
        world.media.pending[key] = 1000  # both requests found the upload pending
        world.uow.products.stale_upload_key_reads = 1  # and no image for it yet

        second = await world.products.confirm_image(acme.id, product.id, key)

        assert second.id == first.id
        assert world.media.objects == set(
            (await world.products.get(acme.id, product.id)).storage_keys
        )

    async def test_a_failed_attach_leaves_no_orphan_file(self, world: World) -> None:
        acme = await _acme(world)
        product = await _product(world, acme)
        world.uow.fail_next_commit = RuntimeError("database went away")

        with pytest.raises(RuntimeError):
            await _image(world, acme, product)

        assert world.media.objects == set()
        assert (await world.products.get(acme.id, product.id)).images == []

    async def test_a_lost_race_is_retried_without_duplicating(self, world: World) -> None:
        acme = await _acme(world)
        product = await _product(world, acme)
        world.uow.conflicts = 1

        await _image(world, acme, product)

        stored = await world.products.get(acme.id, product.id)
        assert len(stored.images) == 1
        assert set(stored.storage_keys) == world.media.objects

    async def test_confirming_announces_the_change(self, world: World) -> None:
        acme = await _acme(world)
        product = await _product(world, acme)

        await _image(world, acme, product)

        assert world.events.actions() == ["created", "updated"]


class TestArrange:
    async def test_removing_an_image_deletes_its_files(self, world: World) -> None:
        acme = await _acme(world)
        product = await _product(world, acme)
        first = await _image(world, acme, product)
        second = await _image(world, acme, product)

        await world.products.remove_image(acme.id, product.id, first)

        remaining = (await world.products.get(acme.id, product.id)).images
        assert [(image.id, image.position) for image in remaining] == [(second, 0)]
        assert world.media.objects == set(remaining[0].storage_keys)
        assert world.events.actions()[-1] == "updated"

    async def test_reordering_picks_the_cover(self, world: World) -> None:
        acme = await _acme(world)
        product = await _product(world, acme)
        first = await _image(world, acme, product)
        second = await _image(world, acme, product)

        reordered = await world.products.reorder_images(acme.id, product.id, [second, first])

        assert [image.id for image in reordered.images] == [second, first]
        assert world.events.actions()[-1] == "updated"


class TestListing:
    def test_an_empty_search_is_no_search(self) -> None:
        query = ProductService.filter("", None, "-name")

        assert (query.search, query.order_by, query.descending) == (None, "name", True)

    def test_a_search_and_status_pass_through(self) -> None:
        query = ProductService.filter("mug", ProductStatus.active, None)

        assert (query.search, query.status, query.order_by) == (
            "mug",
            ProductStatus.active,
            "created_at",
        )

    async def test_numbered_pages(self, world: World) -> None:
        acme = await _acme(world)
        for name in ("A", "B", "C"):
            await _product(world, acme, name)

        items, total = await world.products.page(
            acme.id, world.products.filter(None, None, "name"), 2, 2
        )

        assert ([p.name for p in items], total) == (["C"], 3)

    async def test_cursor_pages(self, world: World) -> None:
        acme = await _acme(world)
        for name in ("A", "B", "C"):
            await _product(world, acme, name)
        query = world.products.filter(None, None, "name")
        _, cursor = await world.products.after(acme.id, query, None, 2)

        items, last = await world.products.after(acme.id, query, cursor, 2)

        assert ([p.name for p in items], last) == (["C"], None)
