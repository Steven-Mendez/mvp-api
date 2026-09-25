"""The product aggregate: a catalog entry of a workspace with up to
`MAX_IMAGES_PER_PRODUCT` ordered images. Every rule about products lives here."""

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import TypedDict

from app.domain.errors import ConflictError, InvalidError, NotFoundError, PreconditionFailedError
from app.domain.ids import new_id, now

MAX_IMAGES_PER_PRODUCT = 10

PRODUCT_NOT_FOUND = "Product not found"
IMAGE_NOT_FOUND = "Image not found"

# The fields a list can be ordered by; `-field` is descending.
SORTABLE_FIELDS = ("created_at", "name", "price", "status")
DEFAULT_ORDER = "-created_at"


class ProductStatus(StrEnum):
    # The docstring is the enum's OpenAPI description; clients see it.
    """Lives in code only: the column is text plus a CHECK, never a Postgres enum type."""

    active = "active"
    draft = "draft"
    archived = "archived"


class ProductChanges(TypedDict, total=False):
    name: str
    sku: str | None
    price: Decimal
    status: ProductStatus
    description: str | None


@dataclass(frozen=True)
class ProductQuotas:
    """What one workspace may hold; the limits come from configuration."""

    max_products: int
    max_storage_bytes: int

    def ensure_room_for_product(self, owned: int) -> None:
        if owned >= self.max_products:
            noun = "product" if self.max_products == 1 else "products"
            raise ConflictError(
                f"This workspace has reached its limit of {self.max_products} {noun}"
            )

    def ensure_room_for_bytes(self, used: int, incoming: int) -> None:
        if used + incoming > self.max_storage_bytes:
            raise ConflictError(
                "This workspace has reached its storage limit of "
                f"{readable_size(self.max_storage_bytes)}"
            )


def readable_size(size: int) -> str:
    """`size` bytes in the largest binary unit it reaches: "10 GB", "1.5 KB", "1 byte"."""
    for unit, factor in (("GB", 1024**3), ("MB", 1024**2), ("KB", 1024)):
        if size >= factor:
            return f"{round(size / factor, 1):g} {unit}"
    return "1 byte" if size == 1 else f"{size} bytes"


def parse_order(order_by: str | None) -> tuple[str, bool]:
    """`(field, descending)` for an `order_by` value."""
    value = order_by or DEFAULT_ORDER
    name = value.removeprefix("-")
    if name not in SORTABLE_FIELDS:
        allowed = ", ".join(SORTABLE_FIELDS)
        raise InvalidError(f"order_by must be one of {allowed}, optionally prefixed with -")
    return name, value.startswith("-")


def ensure_room_for_image_count(image_count: int) -> None:
    if image_count >= MAX_IMAGES_PER_PRODUCT:
        raise ConflictError(f"A product can have at most {MAX_IMAGES_PER_PRODUCT} images")


@dataclass
class ProductImage:
    id: str
    product_id: str
    workspace_id: str
    key: str
    thumb_key: str | None
    upload_key: str | None
    content_type: str
    size_bytes: int
    position: int
    created_at: datetime

    @property
    def storage_keys(self) -> list[str]:
        return [key for key in (self.key, self.thumb_key) if key]


@dataclass
class Product:
    id: str
    workspace_id: str
    name: str
    sku: str | None
    price: Decimal
    status: ProductStatus
    description: str | None
    created_at: datetime
    updated_at: datetime
    images: list[ProductImage] = field(default_factory=list[ProductImage])

    @classmethod
    def create(
        cls,
        *,
        workspace_id: str,
        name: str,
        sku: str | None = None,
        price: Decimal = Decimal(0),
        status: ProductStatus = ProductStatus.draft,
        description: str | None = None,
    ) -> Product:
        created = now()
        product = cls(
            id=new_id(),
            workspace_id=workspace_id,
            name=name,
            sku=sku,
            price=price,
            status=status,
            description=description,
            created_at=created,
            updated_at=created,
        )
        product._check_invariants()
        return product

    @property
    def storage_keys(self) -> list[str]:
        return [key for image in self.images for key in image.storage_keys]

    def ensure_unchanged_since(self, version: datetime | None) -> None:
        """Optimistic concurrency for clients that send `If-Match`: `updated_at` is the version."""
        if version is not None and version != self.updated_at:
            raise PreconditionFailedError("Product was modified since you last loaded it")

    def update(self, changes: ProductChanges) -> None:
        """Apply a partial update; `updated_at` only moves when a value actually changes."""
        changed = False
        for name, value in changes.items():
            if getattr(self, name) != value:
                setattr(self, name, value)
                changed = True
        if changed:
            self._check_invariants()
            self.updated_at = now()

    def ensure_room_for_image(self) -> None:
        ensure_room_for_image_count(len(self.images))

    def add_image(
        self,
        *,
        key: str,
        thumb_key: str | None,
        upload_key: str | None,
        content_type: str,
        size_bytes: int,
    ) -> ProductImage:
        self.ensure_room_for_image()
        image = ProductImage(
            id=new_id(),
            product_id=self.id,
            workspace_id=self.workspace_id,
            key=key,
            thumb_key=thumb_key,
            upload_key=upload_key,
            content_type=content_type,
            size_bytes=size_bytes,
            position=len(self.images),
            created_at=now(),
        )
        self.images.append(image)
        return image

    def remove_image(self, image_id: str) -> ProductImage:
        """Drop one image and close the gap, so positions stay 0..n-1."""
        image = next((image for image in self.images if image.id == image_id), None)
        if image is None:
            raise NotFoundError(IMAGE_NOT_FOUND)
        self.images.remove(image)
        for position, remaining in enumerate(self.images):
            remaining.position = position
        return image

    def reorder_images(self, image_ids: list[str]) -> None:
        """Set the display order; the first image is the cover."""
        by_id = {image.id: image for image in self.images}
        if sorted(image_ids) != sorted(by_id):
            raise InvalidError("image_ids must list every image of this product exactly once")
        self.images = [by_id[image_id] for image_id in image_ids]
        for position, image in enumerate(self.images):
            image.position = position

    def _check_invariants(self) -> None:
        if not self.name:
            raise InvalidError("A product needs a name")
        if self.price < 0:
            raise InvalidError("A price cannot be negative")
