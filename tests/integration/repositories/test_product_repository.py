"""Products as stored.

- every read, count and delete is scoped to its workspace: nothing leaks across tenants
- a product comes back with its images in position order; dropping one from the list
  deletes its row
- search matches name or SKU, ignoring case, and treats `%`, `_` and `\\` literally
- numbered and cursor pages visit every row exactly once, ties included, both ways
- a batch delete takes at most `limit` products with their images and reports the files
- one upload key makes one image per product
"""

from decimal import Decimal

import pytest

from app.application.ports import DuplicateError, ProductFilter, UnitOfWork
from app.domain.errors import InvalidError
from app.domain.product import Product, ProductStatus
from tests import factories
from tests.integration.repositories.conftest import Seed, Tenant


async def _tenant(seed: Seed, slug: str = "acme") -> Tenant:
    return await seed.tenant(slug, await seed.user(f"{slug}@example.com"))


async def _add(uow: UnitOfWork, tenant: Tenant, images: int = 0, **fields: object) -> Product:
    product = factories.product_with_images(images, workspace_id=tenant.workspace.id, **fields)
    uow.products.add(product)
    await uow.flush()
    return product


def _query(
    order_by: str = "name",
    *,
    descending: bool = False,
    search: str | None = None,
    status: ProductStatus | None = None,
) -> ProductFilter:
    return ProductFilter(search=search, status=status, order_by=order_by, descending=descending)


class TestTenantIsolation:
    async def test_reads_do_not_cross_workspaces(self, any_uow: UnitOfWork, seed: Seed) -> None:
        acme, other = await _tenant(seed), await _tenant(seed, "other")
        product = await _add(any_uow, acme, images=2)

        assert await any_uow.products.get(other.workspace.id, product.id) is None
        assert not await any_uow.products.exists(other.workspace.id, product.id)
        assert await any_uow.products.count(other.workspace.id) == 0
        assert await any_uow.products.storage_bytes(other.workspace.id) == 0
        assert await any_uow.products.page(other.workspace.id, _query(), 1, 10) == ([], 0)
        assert await any_uow.products.after(other.workspace.id, _query(), None, 10) == ([], None)

    async def test_the_owning_workspace_sees_it(self, any_uow: UnitOfWork, seed: Seed) -> None:
        acme = await _tenant(seed)
        product = await _add(any_uow, acme, images=2)

        assert await any_uow.products.get(acme.workspace.id, product.id) is product
        assert await any_uow.products.exists(acme.workspace.id, product.id)
        assert await any_uow.products.count(acme.workspace.id) == 1
        assert await any_uow.products.storage_bytes(acme.workspace.id) == 200

    async def test_a_batch_delete_stays_in_its_workspace(
        self, any_uow: UnitOfWork, seed: Seed
    ) -> None:
        acme, other = await _tenant(seed), await _tenant(seed, "other")
        await _add(any_uow, acme)
        kept = await _add(any_uow, other, images=1)

        await any_uow.products.delete_batch(acme.workspace.id, 10)

        assert await any_uow.products.count(acme.workspace.id) == 0
        assert await any_uow.products.get(other.workspace.id, kept.id) is kept
        assert await any_uow.products.count_images(kept.id) == 1


class TestImages:
    async def test_images_come_back_in_position_order(
        self, any_uow: UnitOfWork, seed: Seed
    ) -> None:
        acme = await _tenant(seed)
        product = await _add(any_uow, acme, images=3)
        first, second, third = (image.id for image in product.images)
        product.reorder_images([third, first, second])
        await any_uow.flush()

        stored = await any_uow.products.get(acme.workspace.id, product.id)

        assert stored is not None
        assert [image.id for image in stored.images] == [third, first, second]

    async def test_a_removed_image_is_deleted(self, any_uow: UnitOfWork, seed: Seed) -> None:
        acme = await _tenant(seed)
        product = await _add(any_uow, acme, images=2)

        product.remove_image(product.images[0].id)
        await any_uow.flush()

        assert await any_uow.products.count_images(product.id) == 1
        assert await any_uow.products.storage_bytes(acme.workspace.id) == 100

    async def test_found_by_upload_key_within_the_product(
        self, any_uow: UnitOfWork, seed: Seed
    ) -> None:
        acme = await _tenant(seed)
        product, other = await _add(any_uow, acme, images=1), await _add(any_uow, acme)

        found = await any_uow.products.image_by_upload_key(product.id, "u0")

        assert found is product.images[0]
        assert await any_uow.products.image_by_upload_key(other.id, "u0") is None

    async def test_one_upload_key_makes_one_image(self, any_uow: UnitOfWork, seed: Seed) -> None:
        acme = await _tenant(seed)
        product = await _add(any_uow, acme, images=1)
        product.add_image(
            key="k", thumb_key=None, upload_key="u0", content_type="image/webp", size_bytes=1
        )

        with pytest.raises(DuplicateError):
            await any_uow.flush()


class TestSearch:
    @pytest.mark.parametrize(
        ("search", "expected"),
        [
            ("mug", ["Blue Mug", "Red MUG"]),  # name, any case
            ("sku-7", ["Plate"]),  # SKU
            ("100%", ["100% cotton"]),
            ("_", ["snake_case"]),
            ("\\", ["back\\slash"]),
        ],
    )
    async def test_matches_name_or_sku_literally(
        self, any_uow: UnitOfWork, seed: Seed, search: str, expected: list[str]
    ) -> None:
        acme = await _tenant(seed)
        for name, sku in [
            ("Blue Mug", None),
            ("Red MUG", None),
            ("Plate", "SKU-7"),
            ("100% cotton", None),
            ("1000 cotton", None),
            ("snake_case", None),
            ("snakeXcase", None),
            ("back\\slash", None),
        ]:
            await _add(any_uow, acme, name=name, sku=sku)

        items, total = await any_uow.products.page(acme.workspace.id, _query(search=search), 1, 50)

        assert ([p.name for p in items], total) == (expected, len(expected))

    async def test_filters_by_status(self, any_uow: UnitOfWork, seed: Seed) -> None:
        acme = await _tenant(seed)
        await _add(any_uow, acme, name="Draft")
        await _add(any_uow, acme, name="Live", status=ProductStatus.active)

        items, _ = await any_uow.products.page(
            acme.workspace.id, _query(status=ProductStatus.active), 1, 50
        )

        assert [p.name for p in items] == ["Live"]


class TestPagination:
    @pytest.fixture
    async def catalog(self, any_uow: UnitOfWork, seed: Seed) -> tuple[Tenant, list[Product]]:
        """Seven products, with ties on name and on price."""
        acme = await _tenant(seed)
        rows = [("A", "3"), ("B", "1"), ("B", "10"), ("B", "2"), ("C", "2"), ("D", "0"), ("E", "2")]
        products = [
            await _add(any_uow, acme, name=name, price=Decimal(price)) for name, price in rows
        ]
        return acme, products

    @pytest.mark.parametrize("order_by", ["name", "price", "created_at", "status"])
    @pytest.mark.parametrize("descending", [False, True], ids=["asc", "desc"])
    async def test_cursor_pages_visit_every_row_once_in_order(
        self,
        any_uow: UnitOfWork,
        catalog: tuple[Tenant, list[Product]],
        order_by: str,
        descending: bool,
    ) -> None:
        acme, products = catalog
        query = _query(order_by, descending=descending)
        expected = sorted(products, key=lambda p: (getattr(p, order_by), p.id), reverse=descending)

        seen: list[Product] = []
        cursor: str | None = None
        while True:
            items, cursor = await any_uow.products.after(acme.workspace.id, query, cursor, 3)
            seen += items
            if cursor is None:
                break

        assert [p.id for p in seen] == [p.id for p in expected]

    async def test_numbered_pages_count_every_row(
        self, any_uow: UnitOfWork, catalog: tuple[Tenant, list[Product]]
    ) -> None:
        acme, products = catalog
        query = _query("price", descending=True)

        pages = [await any_uow.products.page(acme.workspace.id, query, n, 3) for n in (1, 2, 3)]

        assert [total for _, total in pages] == [7, 7, 7]
        assert [len(items) for items, _ in pages] == [3, 3, 1]
        assert [p.price for items, _ in pages for p in items] == sorted(
            (p.price for p in products), reverse=True
        )

    async def test_a_numbered_page_past_the_end_is_empty_but_keeps_the_total(
        self, any_uow: UnitOfWork, catalog: tuple[Tenant, list[Product]]
    ) -> None:
        acme, products = catalog
        query = _query("price", descending=True)

        boundary = await any_uow.products.page(acme.workspace.id, query, 7, 1)
        just_past = await any_uow.products.page(acme.workspace.id, query, 8, 1)
        # The route puts no ceiling on `page`: an offset past bigint must not reach Postgres.
        far_past = await any_uow.products.page(acme.workspace.id, query, 2**63, 100)

        assert [p.price for p in boundary[0]] == [min(p.price for p in products)]
        assert (just_past, far_past) == (([], 7), ([], 7))
        assert boundary[1] == 7

    async def test_a_last_full_page_has_no_next_cursor(
        self, any_uow: UnitOfWork, catalog: tuple[Tenant, list[Product]]
    ) -> None:
        acme, _ = catalog

        items, cursor = await any_uow.products.after(acme.workspace.id, _query(), None, 7)

        assert (len(items), cursor) == (7, None)

    async def test_a_forged_cursor_is_invalid(
        self, any_uow: UnitOfWork, catalog: tuple[Tenant, list[Product]]
    ) -> None:
        acme, _ = catalog

        with pytest.raises(InvalidError):
            await any_uow.products.after(acme.workspace.id, _query(), "forged", 3)


class TestBatchDelete:
    async def test_takes_at_most_the_limit_and_reports_the_files(
        self, any_uow: UnitOfWork, seed: Seed
    ) -> None:
        acme = await _tenant(seed)
        for _ in range(3):
            await _add(any_uow, acme, images=2)

        deleted, keys = await any_uow.products.delete_batch(acme.workspace.id, 2)

        assert deleted == 2
        assert len(keys) == 8  # two images each, original and thumbnail
        assert all(key.startswith("media/") for key in keys)
        assert await any_uow.products.count(acme.workspace.id) == 1

    async def test_an_empty_workspace_deletes_nothing(
        self, any_uow: UnitOfWork, seed: Seed
    ) -> None:
        acme = await _tenant(seed)

        assert await any_uow.products.delete_batch(acme.workspace.id, 2) == (0, [])
