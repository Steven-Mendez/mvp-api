"""HTTP transport for products: parse, call one use case, shape the answer."""

import math
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query, status

from app.domain.product import DEFAULT_ORDER, ProductStatus
from app.presentation.dependencies import (
    CanCreateProducts,
    CanDeleteProducts,
    CanReadProducts,
    CanUpdateProducts,
    ProductServiceDep,
    current_user_id,
    media,
    rate_limit,
)
from app.presentation.errors import WORKSPACE_SCOPED, error_responses
from app.presentation.schemas.products import (
    Page,
    ProductCreate,
    ProductCursorPage,
    ProductImageOrder,
    ProductImageRead,
    ProductRead,
    ProductUpdate,
)
from app.presentation.schemas.uploads import PresignedUploadRead, UploadConfirm, UploadRequest

router = APIRouter(
    prefix="/api/workspaces/{slug}/products",
    tags=["products"],
    responses=error_responses(*WORKSPACE_SCOPED),
)

_image_rate_limit = Depends(rate_limit("products:add_image", current_user_id))


@router.get(
    "",
    response_model=Page[ProductRead] | ProductCursorPage,
    description=(
        "List the workspace's products. Web pages by number (`page`/`size`) and gets a "
        "`total` back. Mobile's infinite scroll opts in with `?cursor=` and gets `next_cursor` "
        "instead — never both: a cursor page runs no COUNT, so it has no `total` to report. "
        "`size` sets the page length in both modes; cursor mode ignores `page`."
    ),
)
async def list_products(
    scope: CanReadProducts,
    service: ProductServiceDep,
    page: Annotated[int, Query(ge=1, description="Page number")] = 1,
    size: Annotated[int, Query(ge=1, le=100, description="Page size")] = 50,
    search: Annotated[str | None, Query(max_length=100)] = None,
    status: ProductStatus | None = None,
    order_by: str | None = DEFAULT_ORDER,
    cursor: str | None = None,
) -> Page[ProductRead] | ProductCursorPage:
    query = service.filter(search, status, order_by)
    url_for = media().url_for
    if cursor is not None:
        items, next_cursor = await service.after(scope.id, query, cursor or None, size)
        return ProductCursorPage(
            items=[ProductRead.of(product, url_for) for product in items], next_cursor=next_cursor
        )
    items, total = await service.page(scope.id, query, page, size)
    return Page[ProductRead](
        items=[ProductRead.of(product, url_for) for product in items],
        total=total,
        page=page,
        size=size,
        pages=math.ceil(total / size),
    )


@router.post(
    "",
    response_model=ProductRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(rate_limit("products:create", current_user_id))],
    responses=error_responses(status.HTTP_409_CONFLICT, status.HTTP_429_TOO_MANY_REQUESTS),
)
async def create_product(
    payload: ProductCreate, scope: CanCreateProducts, service: ProductServiceDep
) -> ProductRead:
    product = await service.create(scope.id, **payload.model_dump())
    return ProductRead.of(product, media().url_for)


@router.get(
    "/{product_id}",
    response_model=ProductRead,
    responses=error_responses(status.HTTP_404_NOT_FOUND),
    description="Fetch one of the workspace's products; 404 if missing or in another workspace.",
)
async def get_product(
    product_id: str, scope: CanReadProducts, service: ProductServiceDep
) -> ProductRead:
    return ProductRead.of(await service.get(scope.id, product_id), media().url_for)


@router.patch(
    "/{product_id}",
    response_model=ProductRead,
    responses=error_responses(status.HTTP_404_NOT_FOUND, status.HTTP_412_PRECONDITION_FAILED),
    description=(
        "Partially update one of the workspace's products; 404 if missing or in another "
        "workspace. "
        "412 only when the caller sends `If-Match` and it no longer matches the row's "
        "`updated_at` — see the parameter above for why sending it is optional."
    ),
)
async def update_product(
    product_id: str,
    payload: ProductUpdate,
    scope: CanUpdateProducts,
    service: ProductServiceDep,
    if_match: Annotated[datetime | None, Header()] = None,
) -> ProductRead:
    product = await service.update(scope.id, product_id, payload.changes(), if_match)
    return ProductRead.of(product, media().url_for)


@router.delete(
    "/{product_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
    responses=error_responses(status.HTTP_404_NOT_FOUND),
    description="Delete one of the workspace's products; 404 if missing or in another workspace.",
)
async def delete_product(
    product_id: str, scope: CanDeleteProducts, service: ProductServiceDep
) -> None:
    await service.delete(scope.id, product_id)


@router.post(
    "/{product_id}/images/uploads",
    response_model=PresignedUploadRead,
    dependencies=[_image_rate_limit],
    responses=error_responses(
        status.HTTP_404_NOT_FOUND,
        status.HTTP_409_CONFLICT,
        status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
        status.HTTP_429_TOO_MANY_REQUESTS,
    ),
    description=(
        "Ask for a presigned POST to upload one image straight to the media bucket. The "
        "policy pins the object key, the content type and the size range "
        "(`content-length-range` up to `max_bytes`) and expires after `expires_in` seconds. "
        "Send `fields` plus the file (field name `file`, last) as multipart form data to "
        "`url`, then call `confirm` with `key`."
    ),
)
async def presign_product_image(
    product_id: str, data: UploadRequest, scope: CanUpdateProducts, service: ProductServiceDep
) -> PresignedUploadRead:
    ticket = await service.request_image_upload(scope.id, product_id, data.content_type)
    return PresignedUploadRead.of(ticket)


@router.post(
    "/{product_id}/images/confirm",
    response_model=ProductImageRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[_image_rate_limit],
    responses=error_responses(
        status.HTTP_404_NOT_FOUND,
        status.HTTP_409_CONFLICT,
        status.HTTP_413_CONTENT_TOO_LARGE,
        status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
        status.HTTP_429_TOO_MANY_REQUESTS,
    ),
    description=(
        "Attach an uploaded image to one of the workspace's products; the first one is "
        "the cover. The API validates and re-encodes the pending object, stores the original "
        "and a thumbnail under `media/` and deletes the pending object. Idempotent per `key`: "
        "confirming again returns the same image. `404` means the key was never uploaded, "
        "expired (pending uploads live one day) or was already rejected."
    ),
)
async def confirm_product_image(
    product_id: str, data: UploadConfirm, scope: CanUpdateProducts, service: ProductServiceDep
) -> ProductImageRead:
    image = await service.confirm_image(scope.id, product_id, data.key)
    return ProductImageRead.of(image, media().url_for)


@router.delete(
    "/{product_id}/images/{image_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
    responses=error_responses(status.HTTP_404_NOT_FOUND),
    description=(
        "Remove an image from one of the workspace's products; 404 `Product not found` or "
        "`Image not found` when either is missing."
    ),
)
async def delete_product_image(
    product_id: str, image_id: str, scope: CanUpdateProducts, service: ProductServiceDep
) -> None:
    await service.remove_image(scope.id, product_id, image_id)


@router.put(
    "/{product_id}/images/order",
    response_model=ProductRead,
    responses=error_responses(status.HTTP_404_NOT_FOUND),
    description="Set the display order of a product's images; the first id becomes the cover.",
)
async def reorder_product_images(
    product_id: str,
    payload: ProductImageOrder,
    scope: CanUpdateProducts,
    service: ProductServiceDep,
) -> ProductRead:
    product = await service.reorder_images(scope.id, product_id, payload.image_ids)
    return ProductRead.of(product, media().url_for)
