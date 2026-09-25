"""Entities with sensible defaults: a test names only what it is about.

`product(price=Decimal(0))` says the test is about the price; everything else is noise
it does not have to write.
"""

from datetime import timedelta
from decimal import Decimal
from typing import Any

from app.domain.product import Product, ProductQuotas
from app.domain.user import User
from app.domain.workspace import Invitation, Workspace


def product(**overrides: Any) -> Product:
    fields: dict[str, Any] = {"workspace_id": "w1", "name": "Mug", "price": Decimal("9.99")}
    return Product.create(**(fields | overrides))


def product_with_images(count: int, **overrides: Any) -> Product:
    created = product(**overrides)
    for n in range(count):
        created.add_image(
            key=f"media/{n}.webp",
            thumb_key=f"media/{n}_thumb.webp",
            upload_key=f"u{n}",
            content_type="image/webp",
            size_bytes=100,
        )
    return created


def quotas(**overrides: Any) -> ProductQuotas:
    fields: dict[str, Any] = {"max_products": 3, "max_storage_bytes": 10_000}
    return ProductQuotas(**(fields | overrides))


def user(**overrides: Any) -> User:
    fields: dict[str, Any] = {"user_id": "u1", "email": "ana@example.com"}
    return User.new(**(fields | overrides))


def onboarded_user(**overrides: Any) -> User:
    created = user(**overrides)
    created.rename("Ana", belongs_to_a_workspace=True)
    return created


def workspace(**overrides: Any) -> Workspace:
    fields: dict[str, Any] = {"name": "Acme", "slug": "acme", "owner_id": "owner"}
    return Workspace.create(**(fields | overrides))


def invitation(*, expires_in: timedelta | None = None, **overrides: Any) -> Invitation:
    fields: dict[str, Any] = {
        "workspace_id": "w1",
        "email": "ana@example.com",
        "role_id": "r1",
        "invited_by": "owner",
    }
    issued, _ = Invitation.issue(**(fields | overrides))
    if expires_in is not None:
        issued.expires_at = issued.created_at + expires_in
    return issued
