"""Request and response bodies. Their names are OpenAPI component names, which the
generated clients use as type names: keep them stable."""

from collections.abc import Callable
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field, ValidationInfo, field_validator

from app.domain.product import (
    MAX_IMAGES_PER_PRODUCT,
    Product,
    ProductChanges,
    ProductImage,
    ProductStatus,
)

UrlFor = Callable[[str], str]


class ProductBase(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    sku: str | None = Field(default=None, max_length=64)
    price: Decimal = Field(default=Decimal(0), max_digits=10, decimal_places=2, ge=0)
    status: ProductStatus = ProductStatus.draft
    description: str | None = Field(default=None, max_length=2000)


class ProductCreate(ProductBase):
    pass


class ProductUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    sku: str | None = Field(default=None, max_length=64)
    price: Decimal | None = Field(default=None, max_digits=10, decimal_places=2, ge=0)
    status: ProductStatus | None = None
    description: str | None = Field(default=None, max_length=2000)

    @field_validator("name", "price", "status", mode="before")
    @classmethod
    def required_columns_cannot_be_cleared(cls, value: object, info: ValidationInfo) -> object:
        if value is None:
            raise ValueError(f"{info.field_name} cannot be null")
        return value

    def changes(self) -> ProductChanges:
        """The fields the client sent; `name`, `price` and `status` are never null here."""
        sent = self.model_fields_set
        changes = ProductChanges()
        if "name" in sent and self.name is not None:
            changes["name"] = self.name
        if "sku" in sent:
            changes["sku"] = self.sku
        if "price" in sent and self.price is not None:
            changes["price"] = self.price
        if "status" in sent and self.status is not None:
            changes["status"] = self.status
        if "description" in sent:
            changes["description"] = self.description
        return changes


class ProductImageRead(BaseModel):
    id: str
    url: str
    thumbnail_url: str | None
    content_type: str
    size_bytes: int
    position: int
    created_at: datetime

    @classmethod
    def of(cls, image: ProductImage, url_for: UrlFor) -> ProductImageRead:
        return cls(
            id=image.id,
            url=url_for(image.key),
            thumbnail_url=url_for(image.thumb_key) if image.thumb_key else None,
            content_type=image.content_type,
            size_bytes=image.size_bytes,
            position=image.position,
            created_at=image.created_at,
        )


class ProductImageOrder(BaseModel):
    image_ids: list[str] = Field(min_length=1, max_length=MAX_IMAGES_PER_PRODUCT)


class ProductRead(ProductBase):
    id: str
    created_at: datetime
    updated_at: datetime
    # Required on read (no defaults), unlike the create/update shapes they inherit from.
    sku: str | None = Field()
    price: Decimal = Field()
    status: ProductStatus = Field()
    description: str | None = Field()
    images: list[ProductImageRead]

    @classmethod
    def of(cls, product: Product, url_for: UrlFor) -> ProductRead:
        return cls(
            id=product.id,
            name=product.name,
            sku=product.sku,
            price=product.price,
            status=product.status,
            description=product.description,
            created_at=product.created_at,
            updated_at=product.updated_at,
            images=[ProductImageRead.of(image, url_for) for image in product.images],
        )


class ProductCursorPage(BaseModel):
    items: list[ProductRead]
    next_cursor: str | None = Field(
        default=None,
        description="Pass back as `?cursor=` for the next page; absent once there is no more data.",
    )


class Page[T](BaseModel):
    """A numbered page; its OpenAPI name (`Page_ProductRead_`) is part of the contract."""

    items: list[T]
    total: int = Field(ge=0)
    page: int = Field(ge=1)
    size: int = Field(ge=1)
    pages: int = Field(ge=0)
