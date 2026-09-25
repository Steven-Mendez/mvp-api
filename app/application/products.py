"""Product use cases. They orchestrate the aggregate, persistence and side effects; the
rules themselves live in `app.domain.product`. Callers have already checked access to the
workspace (the routes resolve it with the permission each one needs)."""

from datetime import datetime
from decimal import Decimal
from typing import Literal

from app.application.media import PRODUCT_IMAGE_TYPES
from app.application.ports import (
    ChangeEvent,
    DuplicateError,
    EventPublisher,
    MediaStorage,
    ProductFilter,
    StoredImage,
    UnitOfWork,
    UploadTicket,
)
from app.domain.errors import found
from app.domain.ids import now
from app.domain.product import (
    PRODUCT_NOT_FOUND,
    Product,
    ProductChanges,
    ProductImage,
    ProductQuotas,
    ProductStatus,
    ensure_room_for_image,
    parse_order,
)


class ProductService:
    def __init__(
        self,
        uow: UnitOfWork,
        media: MediaStorage,
        events: EventPublisher,
        quotas: ProductQuotas,
    ) -> None:
        self._uow = uow
        self._media = media
        self._events = events
        self._quotas = quotas

    @staticmethod
    def filter(
        search: str | None, status: ProductStatus | None, order_by: str | None
    ) -> ProductFilter:
        field, descending = parse_order(order_by)
        return ProductFilter(
            search=search or None, status=status, order_by=field, descending=descending
        )

    async def page(
        self, workspace_id: str, query: ProductFilter, page: int, size: int
    ) -> tuple[list[Product], int]:
        return await self._uow.products.page(workspace_id, query, page, size)

    async def after(
        self, workspace_id: str, query: ProductFilter, cursor: str | None, size: int
    ) -> tuple[list[Product], str | None]:
        return await self._uow.products.after(workspace_id, query, cursor, size)

    async def get(self, workspace_id: str, product_id: str) -> Product:
        return found(await self._uow.products.get(workspace_id, product_id), PRODUCT_NOT_FOUND)

    async def create(
        self,
        workspace_id: str,
        *,
        name: str,
        sku: str | None,
        price: Decimal,
        status: ProductStatus,
        description: str | None,
    ) -> Product:
        uow = self._uow

        async def create() -> Product:
            await self._lock_quota(workspace_id)
            self._quotas.ensure_room_for_product(await uow.products.count(workspace_id))
            product = Product.create(
                workspace_id=workspace_id,
                name=name,
                sku=sku,
                price=price,
                status=status,
                description=description,
            )
            uow.products.add(product)
            return product

        product = await uow.transaction(create)
        await self._notify("created", product.id, workspace_id)
        return product

    async def update(
        self,
        workspace_id: str,
        product_id: str,
        changes: ProductChanges,
        if_match: datetime | None,
    ) -> Product:
        async def update() -> Product:
            product = await self.get(workspace_id, product_id)
            product.ensure_unchanged_since(if_match)
            product.update(changes)
            return product

        product = await self._uow.transaction(update)
        await self._notify("updated", product_id, workspace_id)
        return product

    async def delete(self, workspace_id: str, product_id: str) -> None:
        async def delete() -> Product:
            product = await self.get(workspace_id, product_id)
            await self._uow.products.delete(product)
            return product

        product = await self._uow.transaction(delete)
        await self._notify("deleted", product_id, workspace_id)
        for key in product.storage_keys:
            await self._media.delete(key)

    async def request_image_upload(
        self, workspace_id: str, product_id: str, content_type: str
    ) -> UploadTicket:
        """A presigned POST for one more image of the product; nothing is stored yet."""
        uow = self._uow
        if not await uow.products.exists(workspace_id, product_id):
            found(None, PRODUCT_NOT_FOUND)
        ensure_room_for_image(await uow.products.count_images(product_id))
        return self._media.presign(content_type, PRODUCT_IMAGE_TYPES)

    async def confirm_image(
        self, workspace_id: str, product_id: str, upload_key: str
    ) -> ProductImage:
        """Turn a pending upload into an image of the product.

        Idempotent per `upload_key`: confirming the same key again returns the image it
        already produced. The checks run first, the slow image work outside any transaction,
        and the insert last, in a transaction that may be retried.
        """
        uow = self._uow
        if not await uow.products.exists(workspace_id, product_id):
            found(None, PRODUCT_NOT_FOUND)
        existing = await uow.products.image_by_upload_key(product_id, upload_key)
        if existing is not None:
            return existing
        size = await self._media.pending_size(upload_key)
        self._quotas.ensure_room_for_bytes(await uow.products.storage_bytes(workspace_id), size)
        ensure_room_for_image(await uow.products.count_images(product_id))

        stored = await self._media.store_image(upload_key)
        try:
            image = await self._attach(workspace_id, product_id, stored, upload_key)
        except DuplicateError:
            # Two confirms of the same upload raced past the lookup above; the other won.
            await self._discard(stored)
            winner = await uow.products.image_by_upload_key(product_id, upload_key)
            if winner is None:
                raise
            return winner
        except BaseException:
            await self._discard(stored)
            raise
        await self._notify("updated", product_id, workspace_id)
        return image

    async def remove_image(self, workspace_id: str, product_id: str, image_id: str) -> None:
        async def remove() -> ProductImage:
            product = await self.get(workspace_id, product_id)
            return product.remove_image(image_id)

        image = await self._uow.transaction(remove)
        await self._notify("updated", product_id, workspace_id)
        for key in image.storage_keys:
            await self._media.delete(key)

    async def reorder_images(
        self, workspace_id: str, product_id: str, image_ids: list[str]
    ) -> Product:
        async def reorder() -> Product:
            product = await self.get(workspace_id, product_id)
            product.reorder_images(image_ids)
            return product

        product = await self._uow.transaction(reorder)
        await self._notify("updated", product_id, workspace_id)
        return product

    async def _attach(
        self, workspace_id: str, product_id: str, stored: StoredImage, upload_key: str
    ) -> ProductImage:
        uow = self._uow

        async def attach() -> ProductImage:
            await self._lock_quota(workspace_id)
            product = await self.get(workspace_id, product_id)
            product.ensure_room_for_image()
            used = await uow.products.storage_bytes(workspace_id)
            self._quotas.ensure_room_for_bytes(used, stored.size_bytes)
            return product.add_image(
                key=stored.key,
                thumb_key=stored.thumb_key,
                upload_key=upload_key,
                content_type=stored.content_type,
                size_bytes=stored.size_bytes,
            )

        return await uow.transaction(attach)

    async def _lock_quota(self, workspace_id: str) -> None:
        """Quota checks of one workspace run one at a time: they lock its row."""
        await self._uow.workspaces.lock(workspace_id)

    async def _discard(self, stored: StoredImage) -> None:
        await self._media.delete(stored.key)
        await self._media.delete(stored.thumb_key)

    async def _notify(
        self, action: Literal["created", "updated", "deleted"], product_id: str, workspace_id: str
    ) -> None:
        recipients = await self._uow.members.user_ids(workspace_id)
        event = ChangeEvent(
            resource="product", action=action, id=product_id, owner_id=workspace_id, at=now()
        )
        await self._events.publish(event, recipients)
