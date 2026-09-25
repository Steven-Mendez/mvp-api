"""The product aggregate.

- a product needs a name and a price that is not negative (zero is fine)
- an update only moves the version when a value really changes, and is re-validated
- `If-Match` refuses a stale version; no header means no check
- images keep contiguous positions 0..n-1 when one is removed or they are reordered
- a reorder must name every image exactly once
- a product holds at most MAX_IMAGES_PER_PRODUCT images
- quotas: products up to the limit, bytes up to and including the limit
- sizes read in the largest binary unit they reach
- lists order by a known field, `-` meaning descending
"""

from datetime import timedelta
from decimal import Decimal

import pytest

from app.domain.errors import ConflictError, InvalidError, NotFoundError, PreconditionFailedError
from app.domain.product import (
    MAX_IMAGES_PER_PRODUCT,
    ProductStatus,
    ensure_room_for_image,
    parse_order,
    readable_size,
)
from tests import factories


class TestInvariants:
    def test_a_new_product_starts_as_an_unchanged_draft(self) -> None:
        product = factories.product()

        assert product.status is ProductStatus.draft
        assert product.updated_at == product.created_at
        assert product.images == []

    def test_every_field_given_is_kept(self) -> None:
        product = factories.product(sku="MUG-1", description="Ceramic", status=ProductStatus.active)

        assert (product.sku, product.description, product.status) == (
            "MUG-1",
            "Ceramic",
            ProductStatus.active,
        )
        assert product.created_at.tzinfo is not None

    def test_a_price_of_zero_is_allowed(self) -> None:
        assert factories.product(price=Decimal(0)).price == 0

    def test_a_negative_price_is_refused(self) -> None:
        with pytest.raises(InvalidError, match="negative"):
            factories.product(price=Decimal("-0.01"))

    def test_an_empty_name_is_refused(self) -> None:
        with pytest.raises(InvalidError, match="name"):
            factories.product(name="")


class TestUpdate:
    def test_setting_the_same_values_keeps_the_version(self) -> None:
        product = factories.product()
        version = product.updated_at

        product.update({"name": "Mug", "price": Decimal("9.99")})

        assert product.updated_at == version

    def test_a_real_change_moves_the_version(self) -> None:
        product = factories.product()
        version = product.updated_at

        product.update({"name": "Mug", "status": ProductStatus.active})

        assert (product.status, product.updated_at > version) == (ProductStatus.active, True)

    def test_a_change_that_breaks_an_invariant_is_refused(self) -> None:
        product = factories.product()

        with pytest.raises(InvalidError):
            product.update({"price": Decimal(-1)})

    def test_a_stale_version_is_refused(self) -> None:
        product = factories.product()

        with pytest.raises(PreconditionFailedError):
            product.ensure_unchanged_since(product.updated_at - timedelta(microseconds=1))

    def test_the_current_version_or_none_passes(self) -> None:
        product = factories.product()

        product.ensure_unchanged_since(product.updated_at)
        product.ensure_unchanged_since(None)


class TestImages:
    def test_a_new_image_goes_last_and_belongs_to_the_product(self) -> None:
        product = factories.product_with_images(2)

        image = product.images[-1]

        assert (image.position, image.product_id, image.workspace_id) == (1, product.id, "w1")
        assert (image.content_type, image.size_bytes, image.upload_key) == ("image/webp", 100, "u1")
        assert image.created_at.tzinfo is not None

    def test_removing_one_closes_the_gap(self) -> None:
        product = factories.product_with_images(3)
        middle = product.images[1]

        removed = product.remove_image(middle.id)

        assert removed is middle
        assert [image.position for image in product.images] == [0, 1]

    def test_removing_an_unknown_image_is_not_found(self) -> None:
        with pytest.raises(NotFoundError):
            factories.product_with_images(1).remove_image("missing")

    def test_reordering_sets_positions_in_the_given_order(self) -> None:
        product = factories.product_with_images(3)
        first, second, third = (image.id for image in product.images)

        product.reorder_images([third, first, second])

        assert [(image.id, image.position) for image in product.images] == [
            (third, 0),
            (first, 1),
            (second, 2),
        ]

    @pytest.mark.parametrize("pick", [[0], [0, 0], [0, 1, 1], []], ids=str)
    def test_a_reorder_must_name_every_image_exactly_once(self, pick: list[int]) -> None:
        product = factories.product_with_images(2)
        ids = [product.images[n].id for n in pick]

        with pytest.raises(InvalidError):
            product.reorder_images(ids)

    def test_the_last_image_fits_and_one_more_does_not(self) -> None:
        product = factories.product_with_images(MAX_IMAGES_PER_PRODUCT - 1)
        product.ensure_room_for_image()

        product = factories.product_with_images(MAX_IMAGES_PER_PRODUCT)

        with pytest.raises(ConflictError, match=f"at most {MAX_IMAGES_PER_PRODUCT} images"):
            product.ensure_room_for_image()

    def test_storage_keys_cover_originals_and_thumbnails(self) -> None:
        product = factories.product_with_images(2)
        product.images[1].thumb_key = None

        assert product.storage_keys == ["media/0.webp", "media/0_thumb.webp", "media/1.webp"]

    def test_the_image_limit_counts_what_the_database_holds(self) -> None:
        ensure_room_for_image(MAX_IMAGES_PER_PRODUCT - 1)

        with pytest.raises(ConflictError):
            ensure_room_for_image(MAX_IMAGES_PER_PRODUCT)


class TestQuotas:
    def test_products_below_the_limit_fit(self) -> None:
        factories.quotas(max_products=2).ensure_room_for_product(1)

    @pytest.mark.parametrize(
        ("limit", "message"), [(1, "limit of 1 product$"), (2, "limit of 2 products$")]
    )
    def test_a_workspace_at_its_limit_takes_no_more(self, limit: int, message: str) -> None:
        with pytest.raises(ConflictError, match=message):
            factories.quotas(max_products=limit).ensure_room_for_product(limit)

    def test_bytes_up_to_the_limit_fit(self) -> None:
        factories.quotas(max_storage_bytes=1000).ensure_room_for_bytes(600, 400)

    def test_one_byte_over_the_limit_is_refused(self) -> None:
        quotas = factories.quotas(max_storage_bytes=1024)

        with pytest.raises(ConflictError, match=r"storage limit of 1 KB$"):
            quotas.ensure_room_for_bytes(600, 425)


@pytest.mark.parametrize(
    ("size", "expected"),
    [
        (0, "0 bytes"),
        (1, "1 byte"),
        (1023, "1023 bytes"),
        (1024, "1 KB"),
        (1100, "1.1 KB"),  # one decimal
        (1536, "1.5 KB"),
        (1024**2, "1 MB"),
        (1024**3 - 1, "1024 MB"),
        (1024**3, "1 GB"),
        (10 * 1024**3, "10 GB"),
    ],
)
def test_readable_size(size: int, expected: str) -> None:
    assert readable_size(size) == expected


class TestOrder:
    def test_newest_first_by_default(self) -> None:
        assert parse_order(None) == ("created_at", True)

    @pytest.mark.parametrize(
        ("value", "expected"), [("name", ("name", False)), ("-price", ("price", True))]
    )
    def test_a_field_with_an_optional_minus(self, value: str, expected: tuple[str, bool]) -> None:
        assert parse_order(value) == expected

    @pytest.mark.parametrize("value", ["sku", "-sku", "--name", "name-"])
    def test_other_fields_are_refused(self, value: str) -> None:
        with pytest.raises(InvalidError, match="one of created_at, name, price, status"):
            parse_order(value)
