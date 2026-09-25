"""SQLAlchemy implementations of the repository ports. Queries filter through the table
columns (`tables.users.c.id`), which keeps them typed; the ORM loads and tracks the
domain objects, so a change to one is written at commit without a `save` call.

Bulk UPDATEs and DELETEs of mapped rows name the entity (`update(Invitation)`) and
synchronize the session: objects it already holds see the change, instead of staying
stale until the next request."""

import base64
import binascii
import json
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    ColumnElement,
    Select,
    and_,
    case,
    delete,
    exists,
    func,
    literal,
    or_,
    select,
    tuple_,
    update,
)
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.ports import ProductFilter
from app.domain.access import OWNER_ROLE
from app.domain.errors import InvalidError
from app.domain.ids import now
from app.domain.product import Product, ProductImage
from app.domain.user import User
from app.domain.workspace import Invitation, Member, Role, Workspace, hash_token
from app.infrastructure.db.tables import (
    invitations,
    members,
    product_images,
    products,
    role_permissions,
    roles,
    users,
    workspaces,
)

# Bulk statements update or drop the objects the session already holds (module docstring).
_SYNCED = {"synchronize_session": "fetch"}


class _Repository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def _exists(self, condition: ColumnElement[bool]) -> bool:
        return bool(await self._session.scalar(select(exists().where(condition))))

    async def _count(self, statement: Select[Any]) -> int:
        count = await self._session.scalar(select(func.count()).select_from(statement.subquery()))
        return count or 0


class SqlUserRepository(_Repository):
    async def get(self, user_id: str) -> User | None:
        return await self._session.get(User, user_id)

    def add(self, user: User) -> None:
        self._session.add(user)

    async def add_unless_taken(self, user: User) -> None:
        await self._session.execute(
            insert(users)
            .values(
                id=user.id,
                email=user.email,
                display_name=user.display_name,
                avatar_url=user.avatar_url,
                avatar_key=user.avatar_key,
                onboarding_status=user.onboarding_status,
                created_at=user.created_at,
            )
            .on_conflict_do_nothing()
        )

    async def delete(self, user: User) -> None:
        await self._session.delete(user)


class SqlWorkspaceRepository(_Repository):
    _live = workspaces.c.deleted_at.is_(None)

    async def get(self, workspace_id: str) -> Workspace | None:
        return await self._one(and_(workspaces.c.id == workspace_id, self._live))

    async def get_deleted(self, workspace_id: str) -> Workspace | None:
        return await self._one(
            and_(workspaces.c.id == workspace_id, workspaces.c.deleted_at.is_not(None))
        )

    async def by_slug(self, slug: str) -> Workspace | None:
        return await self._one(and_(workspaces.c.slug == slug, self._live))

    async def lock(self, workspace_id: str) -> Workspace | None:
        statement = (
            select(Workspace)
            .where(workspaces.c.id == workspace_id, self._live)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return await self._session.scalar(statement)

    async def of_member(self, user_id: str) -> list[Workspace]:
        statement = (
            select(Workspace)
            .join(members, members.c.workspace_id == workspaces.c.id)
            .where(members.c.user_id == user_id, self._live)
            .order_by(workspaces.c.name)
        )
        return list(await self._session.scalars(statement))

    async def slug_exists(self, slug: str) -> bool:
        return await self._exists(workspaces.c.slug == slug)

    async def any_owned_by(self, user_id: str) -> bool:
        return await self._exists(and_(workspaces.c.owner_id == user_id, self._live))

    async def deleted_ids(self, *, owned_by: str | None = None) -> list[str]:
        statement = (
            select(workspaces.c.id)
            .where(workspaces.c.deleted_at.is_not(None))
            .order_by(workspaces.c.deleted_at)
        )
        if owned_by is not None:
            statement = statement.where(workspaces.c.owner_id == owned_by)
        return list(await self._session.scalars(statement))

    def add(self, workspace: Workspace) -> None:
        self._session.add(workspace)

    async def purge(self, workspace: Workspace) -> None:
        role_ids = select(roles.c.id).where(roles.c.workspace_id == workspace.id)
        session = self._session
        await session.execute(
            delete(role_permissions).where(role_permissions.c.role_id.in_(role_ids))
        )
        await session.execute(delete(roles).where(roles.c.workspace_id == workspace.id))
        await session.execute(delete(members).where(members.c.workspace_id == workspace.id))
        await session.execute(delete(invitations).where(invitations.c.workspace_id == workspace.id))
        await session.delete(workspace)

    async def _one(self, condition: ColumnElement[bool]) -> Workspace | None:
        return await self._session.scalar(select(Workspace).where(condition))


class SqlRoleRepository(_Repository):
    async def get(self, role_id: str) -> Role | None:
        return await self._session.get(Role, role_id)

    async def by_name(self, workspace_id: str, name: str) -> Role | None:
        statement = select(Role).where(roles.c.workspace_id == workspace_id, roles.c.name == name)
        return await self._session.scalar(statement)

    async def of_workspace(self, workspace_id: str) -> list[Role]:
        owner_first = case((roles.c.name == OWNER_ROLE, 0), else_=1)
        statement = (
            select(Role)
            .where(roles.c.workspace_id == workspace_id)
            .order_by(owner_first, roles.c.name)
        )
        return list(await self._session.scalars(statement))

    async def permissions(self, role_id: str) -> list[str]:
        statement = (
            select(role_permissions.c.permission)
            .where(role_permissions.c.role_id == role_id)
            .order_by(role_permissions.c.permission)
        )
        return list(await self._session.scalars(statement))

    async def member_count(self, role_id: str) -> int:
        return await self._count(select(members.c.user_id).where(members.c.role_id == role_id))

    async def is_in_use(self, role_id: str) -> bool:
        pending = and_(
            invitations.c.role_id == role_id,
            invitations.c.status == "pending",
            invitations.c.expires_at > now(),
        )
        return await self._exists(members.c.role_id == role_id) or await self._exists(pending)

    async def add(self, role: Role, permissions: list[str]) -> None:
        self._session.add(role)
        await self._grant(role.id, permissions)

    async def replace_permissions(self, role_id: str, permissions: set[str]) -> None:
        await self._session.execute(
            delete(role_permissions).where(role_permissions.c.role_id == role_id)
        )
        await self._grant(role_id, sorted(permissions))

    async def delete(self, role: Role) -> None:
        await self._session.execute(
            delete(role_permissions).where(role_permissions.c.role_id == role.id)
        )
        await self._session.delete(role)

    async def _grant(self, role_id: str, permissions: list[str]) -> None:
        if permissions:
            await self._session.execute(
                insert(role_permissions),
                [{"role_id": role_id, "permission": permission} for permission in permissions],
            )


class SqlMemberRepository(_Repository):
    async def get(self, workspace_id: str, user_id: str) -> Member | None:
        return await self._session.get(Member, (workspace_id, user_id))

    async def of_workspace(self, workspace_id: str) -> list[tuple[Member, User, Role]]:
        statement = (
            select(Member, User, Role)
            .join(users, users.c.id == members.c.user_id)
            .join(roles, roles.c.id == members.c.role_id)
            .where(members.c.workspace_id == workspace_id)
            .order_by(users.c.display_name, users.c.email)
        )
        return [(row[0], row[1], row[2]) for row in await self._session.execute(statement)]

    async def user_ids(self, workspace_id: str) -> list[str]:
        statement = select(members.c.user_id).where(members.c.workspace_id == workspace_id)
        return list(await self._session.scalars(statement))

    async def owner_ids(self, workspace_id: str) -> list[str]:
        statement = (
            select(members.c.user_id)
            .join(roles, roles.c.id == members.c.role_id)
            .where(members.c.workspace_id == workspace_id, roles.c.is_protected.is_(True))
            .order_by(members.c.user_id)
        )
        return list(await self._session.scalars(statement))

    async def belongs_to_any(self, user_id: str) -> bool:
        return await self._exists(members.c.user_id == user_id)

    async def has_email(self, workspace_id: str, email: str) -> bool:
        return await self._exists(
            and_(
                members.c.workspace_id == workspace_id,
                members.c.user_id == users.c.id,
                users.c.email == email.lower(),
            )
        )

    def add(self, member: Member) -> None:
        self._session.add(member)

    async def delete(self, member: Member) -> None:
        await self._session.delete(member)

    async def remove_all(self, workspace_id: str) -> None:
        await self._session.execute(
            delete(Member).where(members.c.workspace_id == workspace_id),
            execution_options=_SYNCED,
        )

    async def remove_user(self, user_id: str) -> None:
        await self._session.execute(
            delete(Member).where(members.c.user_id == user_id), execution_options=_SYNCED
        )


class SqlInvitationRepository(_Repository):
    async def get(self, invitation_id: str) -> Invitation | None:
        return await self._session.get(Invitation, invitation_id)

    async def by_token(self, token: str) -> Invitation | None:
        statement = select(Invitation).where(invitations.c.token_hash == hash_token(token))
        return await self._session.scalar(statement)

    async def of_workspace(self, workspace_id: str) -> list[tuple[Invitation, User | None]]:
        statement = (
            select(Invitation, User)
            .outerjoin(users, users.c.id == invitations.c.accepted_by)
            .where(invitations.c.workspace_id == workspace_id)
            .order_by(invitations.c.created_at.desc())
        )
        return [(row[0], row[1]) for row in await self._session.execute(statement)]

    async def has_pending(self, workspace_id: str, email: str) -> bool:
        return await self._exists(
            and_(
                invitations.c.workspace_id == workspace_id,
                invitations.c.email == email.lower(),
                invitations.c.status == "pending",
                invitations.c.expires_at > now(),
            )
        )

    def add(self, invitation: Invitation) -> None:
        self._session.add(invitation)

    async def revoke_pending(self, workspace_id: str) -> None:
        await self._session.execute(
            update(Invitation)
            .where(invitations.c.workspace_id == workspace_id, invitations.c.status == "pending")
            .values(status="revoked"),
            execution_options=_SYNCED,
        )

    async def delete_for_role(self, role_id: str) -> None:
        await self._session.execute(
            delete(Invitation).where(invitations.c.role_id == role_id), execution_options=_SYNCED
        )

    async def delete_sent_by(self, user_id: str) -> None:
        await self._session.execute(
            delete(Invitation).where(invitations.c.invited_by == user_id),
            execution_options=_SYNCED,
        )


class SqlProductRepository(_Repository):
    async def get(self, workspace_id: str, product_id: str) -> Product | None:
        statement = (
            select(Product)
            .where(products.c.id == product_id, products.c.workspace_id == workspace_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return await self._session.scalar(statement)

    async def exists(self, workspace_id: str, product_id: str) -> bool:
        return await self._exists(
            and_(products.c.id == product_id, products.c.workspace_id == workspace_id)
        )

    async def count(self, workspace_id: str) -> int:
        return await self._count(
            select(products.c.id).where(products.c.workspace_id == workspace_id)
        )

    async def count_images(self, product_id: str) -> int:
        return await self._count(
            select(product_images.c.id).where(product_images.c.product_id == product_id)
        )

    async def storage_bytes(self, workspace_id: str) -> int:
        total = await self._session.scalar(
            select(func.coalesce(func.sum(product_images.c.size_bytes), 0)).where(
                product_images.c.workspace_id == workspace_id
            )
        )
        return int(total or 0)

    async def image_by_upload_key(self, product_id: str, upload_key: str) -> ProductImage | None:
        statement = select(ProductImage).where(
            product_images.c.product_id == product_id, product_images.c.upload_key == upload_key
        )
        return await self._session.scalar(statement)

    async def page(
        self, workspace_id: str, query: ProductFilter, page: int, size: int
    ) -> tuple[list[Product], int]:
        filtered = self._filtered(workspace_id, query)
        total = await self._count(filtered.with_only_columns(products.c.id))
        offset = (page - 1) * size
        if offset >= total:
            # Past the last row there is nothing to fetch, and `page` has no ceiling:
            # an offset beyond bigint would make asyncpg reject the query.
            return [], total
        items = await self._session.scalars(
            filtered.order_by(*self._order(query)).offset(offset).limit(size)
        )
        return list(items), total

    async def after(
        self, workspace_id: str, query: ProductFilter, cursor: str | None, size: int
    ) -> tuple[list[Product], str | None]:
        statement = self._filtered(workspace_id, query)
        column = products.c[query.order_by]
        if cursor:
            value, last_id = decode_cursor(cursor, query.order_by)
            position = tuple_(column, products.c.id)
            boundary = tuple_(literal(value), literal(last_id))
            statement = statement.where(
                position < boundary if query.descending else position > boundary
            )
        rows = list(
            await self._session.scalars(statement.order_by(*self._order(query)).limit(size + 1))
        )
        if len(rows) <= size:
            return rows, None
        last = rows[size - 1]
        return rows[:size], encode_cursor(getattr(last, query.order_by), last.id)

    def add(self, product: Product) -> None:
        self._session.add(product)

    async def delete(self, product: Product) -> None:
        await self._session.delete(product)

    async def delete_batch(self, workspace_id: str, limit: int) -> tuple[int, list[str]]:
        session = self._session
        ids = list(
            await session.scalars(
                select(products.c.id).where(products.c.workspace_id == workspace_id).limit(limit)
            )
        )
        if not ids:
            return 0, []
        images = await session.execute(
            select(product_images.c.key, product_images.c.thumb_key).where(
                product_images.c.product_id.in_(ids)
            )
        )
        keys = [key for row in images for key in row if key]
        await session.execute(delete(product_images).where(product_images.c.product_id.in_(ids)))
        await session.execute(delete(products).where(products.c.id.in_(ids)))
        return len(ids), keys

    @staticmethod
    def _filtered(workspace_id: str, query: ProductFilter) -> Select[tuple[Product]]:
        statement = select(Product).where(products.c.workspace_id == workspace_id)
        if query.status is not None:
            statement = statement.where(products.c.status == query.status)
        if query.search:
            escaped = query.search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            pattern = f"%{escaped}%"
            statement = statement.where(
                or_(
                    products.c.name.ilike(pattern, escape="\\"),
                    products.c.sku.ilike(pattern, escape="\\"),
                )
            )
        return statement

    @staticmethod
    def _order(query: ProductFilter) -> list[ColumnElement[Any]]:
        column, tie_breaker = products.c[query.order_by], products.c.id
        if query.descending:
            return [column.desc(), tie_breaker.desc()]
        return [column.asc(), tie_breaker.asc()]


# A cursor is the last row's sort value and id, opaque to clients.


def encode_cursor(value: object, row_id: str) -> str:
    raw = json.dumps([str(value) if not isinstance(value, datetime) else value.isoformat(), row_id])
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_cursor(cursor: str, field: str) -> tuple[object, str]:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        value, row_id = json.loads(raw)
        parsed: object = (
            datetime.fromisoformat(value)
            if field == "created_at"
            else Decimal(value)
            if field == "price"
            else str(value)
        )
    except (binascii.Error, ValueError, TypeError, ArithmeticError) as exc:
        raise InvalidError("Invalid cursor") from exc
    return parsed, str(row_id)
