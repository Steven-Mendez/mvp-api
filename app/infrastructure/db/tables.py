"""The schema, and the imperative mapping of the domain dataclasses onto it: the domain
never imports SQLAlchemy, and there are no persistence models to keep in sync.

Aurora DSQL enforces no foreign keys, so none are declared; the use cases keep
references consistent. `migrations/` creates these tables.
"""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Enum,
    Integer,
    MetaData,
    Numeric,
    String,
    Table,
    Text,
    and_,
)
from sqlalchemy.orm import foreign, registry, relationship

from app.domain.product import Product, ProductImage, ProductStatus
from app.domain.user import User
from app.domain.workspace import Invitation, Member, Role, Workspace

metadata = MetaData()


def _id(name: str = "id") -> Column[str]:
    return Column(name, String, primary_key=True)


def _time(name: str, *, nullable: bool = False) -> Column[datetime]:
    return Column(name, DateTime(timezone=True), nullable=nullable)


users = Table(
    "users",
    metadata,
    _id(),
    Column("email", Text, nullable=False),
    Column("display_name", Text, nullable=False),
    Column("avatar_url", Text),
    Column("avatar_key", Text),
    Column("onboarding_status", Text, nullable=False),
    _time("created_at"),
)

workspaces = Table(
    "workspaces",
    metadata,
    _id(),
    Column("name", Text, nullable=False),
    Column("slug", Text, nullable=False),
    Column("owner_id", String, nullable=False),
    Column("logo_url", Text),
    Column("logo_key", Text),
    _time("created_at"),
    _time("deleted_at", nullable=True),
)

roles = Table(
    "roles",
    metadata,
    _id(),
    Column("workspace_id", String, nullable=False),
    Column("name", Text, nullable=False),
    Column("is_system", Boolean, nullable=False),
    Column("is_protected", Boolean, nullable=False),
)

role_permissions = Table(
    "role_permissions",
    metadata,
    _id("role_id"),
    Column("permission", Text, primary_key=True),
)

members = Table(
    "members",
    metadata,
    _id("workspace_id"),
    _id("user_id"),
    Column("role_id", String, nullable=False),
    _time("joined_at"),
)

invitations = Table(
    "invitations",
    metadata,
    _id(),
    Column("workspace_id", String, nullable=False),
    Column("email", Text, nullable=False),
    Column("role_id", String, nullable=False),
    Column("token_hash", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("invited_by", String, nullable=False),
    Column("accepted_by", String),
    _time("expires_at"),
    _time("created_at"),
)

products = Table(
    "products",
    metadata,
    _id(),
    Column("workspace_id", String, nullable=False),
    Column("name", Text, nullable=False),
    Column("sku", Text),
    Column("price", Numeric(10, 2), nullable=False),
    Column(
        "status",
        Enum(ProductStatus, native_enum=False, create_constraint=False, length=16),
        nullable=False,
    ),
    Column("description", Text),
    _time("created_at"),
    _time("updated_at"),
)

product_images = Table(
    "product_images",
    metadata,
    _id(),
    Column("product_id", String, nullable=False),
    Column("workspace_id", String, nullable=False),
    Column("key", Text, nullable=False),
    Column("thumb_key", Text),
    Column("upload_key", Text),
    Column("content_type", Text, nullable=False),
    Column("size_bytes", BigInteger, nullable=False),
    Column("position", Integer, nullable=False),
    _time("created_at"),
)

mapper_registry = registry(metadata=metadata)
mapper_registry.map_imperatively(User, users)
mapper_registry.map_imperatively(Workspace, workspaces)
mapper_registry.map_imperatively(Role, roles)
mapper_registry.map_imperatively(Member, members)
mapper_registry.map_imperatively(Invitation, invitations)
mapper_registry.map_imperatively(ProductImage, product_images)
mapper_registry.map_imperatively(
    Product,
    products,
    properties={
        # Loaded with the product in one extra query; removing one from the list deletes it.
        "images": relationship(
            ProductImage,
            primaryjoin=and_(products.c.id == foreign(product_images.c.product_id)),
            order_by=product_images.c.position,
            cascade="all, delete-orphan",
            lazy="selectin",
        ),
    },
)
