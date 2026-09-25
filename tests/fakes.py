"""In-memory stand-ins for every port in `app.application.ports`.

The unit tests run the use cases against these; the end-to-end tests keep the real
database and swap only the AWS adapters for them. `tests/integration/test_fake_contract.py`
runs the same scenarios against this unit of work and the SQL one, so the two cannot
drift apart unnoticed.
"""

import copy
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from app.application.ports import (
    ChangeEvent,
    ConcurrencyConflictError,
    DuplicateError,
    EmailTakenError,
    PasswordRejectedError,
    ProductFilter,
    StoredImage,
    UploadTicket,
)
from app.domain.access import OWNER_ROLE
from app.domain.errors import InvalidError, NotFoundError, UnsupportedMediaTypeError
from app.domain.ids import now
from app.domain.product import Product, ProductImage
from app.domain.user import User
from app.domain.workspace import Invitation, Member, Role, Workspace, hash_token

# --- persistence ------------------------------------------------------------------------


@dataclass
class Tables:
    users: dict[str, User] = field(default_factory=dict[str, User])
    workspaces: dict[str, Workspace] = field(default_factory=dict[str, Workspace])
    roles: dict[str, Role] = field(default_factory=dict[str, Role])
    role_permissions: dict[str, set[str]] = field(default_factory=dict[str, set[str]])
    members: dict[tuple[str, str], Member] = field(default_factory=dict[tuple[str, str], Member])
    invitations: dict[str, Invitation] = field(default_factory=dict[str, Invitation])
    products: dict[str, Product] = field(default_factory=dict[str, Product])

    def check_unique(self) -> None:
        """The unique constraints of migrations/versions/0001_initial_schema.py."""
        _unique("users.email", [u.email for u in self.users.values()])
        _unique("workspaces.slug", [w.slug for w in self.workspaces.values()])
        _unique("roles.name", [(r.workspace_id, r.name) for r in self.roles.values()])
        _unique("invitations.token_hash", [i.token_hash for i in self.invitations.values()])
        _unique(
            "product_images.upload_key",
            [
                (image.product_id, image.upload_key)
                for product in self.products.values()
                for image in product.images
                if image.upload_key is not None
            ],
        )


def _unique(constraint: str, values: list[Any]) -> None:
    if len(values) != len(set(values)):
        raise DuplicateError(f"duplicate key value violates unique constraint {constraint}")


class _Repository:
    def __init__(self, uow: FakeUnitOfWork) -> None:
        self._uow = uow

    @property
    def _t(self) -> Tables:
        return self._uow.tables


class FakeUserRepository(_Repository):
    async def get(self, user_id: str) -> User | None:
        return self._t.users.get(user_id)

    def add(self, user: User) -> None:
        self._t.users[user.id] = user

    async def add_unless_taken(self, user: User) -> None:
        taken = user.id in self._t.users or any(
            u.email == user.email for u in self._t.users.values()
        )
        if not taken:
            self._t.users[user.id] = user

    async def delete(self, user: User) -> None:
        del self._t.users[user.id]


class FakeWorkspaceRepository(_Repository):
    async def get(self, workspace_id: str) -> Workspace | None:
        workspace = self._t.workspaces.get(workspace_id)
        return workspace if workspace and not workspace.is_deleted else None

    async def get_deleted(self, workspace_id: str) -> Workspace | None:
        workspace = self._t.workspaces.get(workspace_id)
        return workspace if workspace and workspace.is_deleted else None

    async def by_slug(self, slug: str) -> Workspace | None:
        return next((w for w in self._live() if w.slug == slug), None)

    async def lock(self, workspace_id: str) -> Workspace | None:
        self._uow.locks.append(workspace_id)
        return await self.get(workspace_id)

    async def of_member(self, user_id: str) -> list[Workspace]:
        ids = {workspace_id for workspace_id, member in self._t.members if member == user_id}
        return sorted((w for w in self._live() if w.id in ids), key=lambda w: w.name)

    async def slug_exists(self, slug: str) -> bool:
        return any(w.slug == slug for w in self._t.workspaces.values())

    async def any_owned_by(self, user_id: str) -> bool:
        return any(w.owner_id == user_id for w in self._live())

    async def deleted_ids(self, *, owned_by: str | None = None) -> list[str]:
        deleted = [
            w
            for w in self._t.workspaces.values()
            if w.deleted_at is not None and owned_by in (None, w.owner_id)
        ]
        return [w.id for w in sorted(deleted, key=lambda w: w.deleted_at or w.created_at)]

    def add(self, workspace: Workspace) -> None:
        self._t.workspaces[workspace.id] = workspace

    async def purge(self, workspace: Workspace) -> None:
        t = self._t
        for role_id in [r.id for r in t.roles.values() if r.workspace_id == workspace.id]:
            del t.roles[role_id]
            t.role_permissions.pop(role_id, None)
        for key in [key for key in t.members if key[0] == workspace.id]:
            del t.members[key]
        for key in [k for k, i in t.invitations.items() if i.workspace_id == workspace.id]:
            del t.invitations[key]
        del t.workspaces[workspace.id]

    def _live(self) -> list[Workspace]:
        return [w for w in self._t.workspaces.values() if not w.is_deleted]


class FakeRoleRepository(_Repository):
    async def get(self, role_id: str) -> Role | None:
        return self._t.roles.get(role_id)

    async def by_name(self, workspace_id: str, name: str) -> Role | None:
        return next(
            (r for r in self._t.roles.values() if (r.workspace_id, r.name) == (workspace_id, name)),
            None,
        )

    async def of_workspace(self, workspace_id: str) -> list[Role]:
        roles = [r for r in self._t.roles.values() if r.workspace_id == workspace_id]
        return sorted(roles, key=lambda r: (r.name != OWNER_ROLE, r.name))

    async def permissions(self, role_id: str) -> list[str]:
        return sorted(self._t.role_permissions.get(role_id, set()))

    async def member_count(self, role_id: str) -> int:
        return sum(1 for m in self._t.members.values() if m.role_id == role_id)

    async def is_in_use(self, role_id: str) -> bool:
        return any(m.role_id == role_id for m in self._t.members.values()) or any(
            i.role_id == role_id and i.status == "pending" and i.expires_at > now()
            for i in self._t.invitations.values()
        )

    async def add(self, role: Role, permissions: list[str]) -> None:
        self._t.roles[role.id] = role
        self._t.role_permissions[role.id] = set(permissions)

    async def replace_permissions(self, role_id: str, permissions: set[str]) -> None:
        self._t.role_permissions[role_id] = set(permissions)

    async def delete(self, role: Role) -> None:
        del self._t.roles[role.id]
        self._t.role_permissions.pop(role.id, None)


class FakeMemberRepository(_Repository):
    async def get(self, workspace_id: str, user_id: str) -> Member | None:
        return self._t.members.get((workspace_id, user_id))

    async def of_workspace(self, workspace_id: str) -> list[tuple[Member, User, Role]]:
        t = self._t
        rows = [
            (m, t.users[m.user_id], t.roles[m.role_id])
            for m in t.members.values()
            if m.workspace_id == workspace_id
        ]
        return sorted(rows, key=lambda row: (row[1].display_name, row[1].email))

    async def user_ids(self, workspace_id: str) -> list[str]:
        return [m.user_id for m in self._t.members.values() if m.workspace_id == workspace_id]

    async def owner_ids(self, workspace_id: str) -> list[str]:
        t = self._t
        return sorted(
            m.user_id
            for m in t.members.values()
            if m.workspace_id == workspace_id
            and m.role_id in t.roles
            and t.roles[m.role_id].is_protected
        )

    async def belongs_to_any(self, user_id: str) -> bool:
        return any(m.user_id == user_id for m in self._t.members.values())

    async def has_email(self, workspace_id: str, email: str) -> bool:
        t = self._t
        return any(
            m.workspace_id == workspace_id and t.users[m.user_id].email == email.lower()
            for m in t.members.values()
        )

    def add(self, member: Member) -> None:
        self._t.members[(member.workspace_id, member.user_id)] = member

    async def delete(self, member: Member) -> None:
        del self._t.members[(member.workspace_id, member.user_id)]

    async def remove_all(self, workspace_id: str) -> None:
        for key in [key for key in self._t.members if key[0] == workspace_id]:
            del self._t.members[key]

    async def remove_user(self, user_id: str) -> None:
        for key in [key for key in self._t.members if key[1] == user_id]:
            del self._t.members[key]


class FakeInvitationRepository(_Repository):
    async def get(self, invitation_id: str) -> Invitation | None:
        return self._t.invitations.get(invitation_id)

    async def by_token(self, token: str) -> Invitation | None:
        digest = hash_token(token)
        return next((i for i in self._t.invitations.values() if i.token_hash == digest), None)

    async def of_workspace(self, workspace_id: str) -> list[tuple[Invitation, User | None]]:
        t = self._t
        found = [i for i in t.invitations.values() if i.workspace_id == workspace_id]
        found.sort(key=lambda i: i.created_at, reverse=True)
        return [(i, t.users.get(i.accepted_by) if i.accepted_by else None) for i in found]

    async def has_pending(self, workspace_id: str, email: str) -> bool:
        return any(
            i.workspace_id == workspace_id
            and i.email == email.lower()
            and i.status == "pending"
            and i.expires_at > now()
            for i in self._t.invitations.values()
        )

    def add(self, invitation: Invitation) -> None:
        self._t.invitations[invitation.id] = invitation

    async def revoke_pending(self, workspace_id: str) -> None:
        for i in self._t.invitations.values():
            if i.workspace_id == workspace_id and i.status == "pending":
                i.status = "revoked"

    async def delete_for_role(self, role_id: str) -> None:
        self._delete_where(lambda i: i.role_id == role_id)

    async def delete_sent_by(self, user_id: str) -> None:
        self._delete_where(lambda i: i.invited_by == user_id)

    def _delete_where(self, condition: Callable[[Invitation], bool]) -> None:
        for key in [k for k, i in self._t.invitations.items() if condition(i)]:
            del self._t.invitations[key]


class FakeProductRepository(_Repository):
    def __init__(self, uow: FakeUnitOfWork) -> None:
        super().__init__(uow)
        self.batches: list[int] = []  # the size of each `delete_batch`
        # The next that many upload-key lookups miss, as a read taken just before a
        # concurrent confirm of the same upload committed does.
        self.stale_upload_key_reads = 0

    async def get(self, workspace_id: str, product_id: str) -> Product | None:
        product = self._t.products.get(product_id)
        return product if product and product.workspace_id == workspace_id else None

    async def exists(self, workspace_id: str, product_id: str) -> bool:
        return await self.get(workspace_id, product_id) is not None

    async def count(self, workspace_id: str) -> int:
        return len(self._of(workspace_id))

    async def count_images(self, product_id: str) -> int:
        product = self._t.products.get(product_id)
        return len(product.images) if product else 0

    async def storage_bytes(self, workspace_id: str) -> int:
        return sum(i.size_bytes for p in self._of(workspace_id) for i in p.images)

    async def image_by_upload_key(self, product_id: str, upload_key: str) -> ProductImage | None:
        if self.stale_upload_key_reads:
            self.stale_upload_key_reads -= 1
            return None
        product = self._t.products.get(product_id)
        images = product.images if product else []
        return next((i for i in images if i.upload_key == upload_key), None)

    async def page(
        self, workspace_id: str, query: ProductFilter, page: int, size: int
    ) -> tuple[list[Product], int]:
        rows = self._filtered(workspace_id, query)
        return rows[(page - 1) * size : page * size], len(rows)

    async def after(
        self, workspace_id: str, query: ProductFilter, cursor: str | None, size: int
    ) -> tuple[list[Product], str | None]:
        rows = self._filtered(workspace_id, query)
        if cursor:
            ids = [p.id for p in rows]
            if cursor not in ids:
                raise InvalidError("Invalid cursor")
            rows = rows[ids.index(cursor) + 1 :]
        if len(rows) <= size:
            return rows, None
        return rows[:size], rows[size - 1].id

    def add(self, product: Product) -> None:
        self._t.products[product.id] = product

    async def delete(self, product: Product) -> None:
        del self._t.products[product.id]

    async def delete_batch(self, workspace_id: str, limit: int) -> tuple[int, list[str]]:
        batch = self._of(workspace_id)[:limit]
        self.batches.append(len(batch))
        for product in batch:
            del self._t.products[product.id]
        return len(batch), [key for product in batch for key in product.storage_keys]

    def _of(self, workspace_id: str) -> list[Product]:
        return [p for p in self._t.products.values() if p.workspace_id == workspace_id]

    def _filtered(self, workspace_id: str, query: ProductFilter) -> list[Product]:
        rows = self._of(workspace_id)
        if query.status is not None:
            rows = [p for p in rows if p.status == query.status]
        if query.search:
            needle = query.search.lower()
            rows = [p for p in rows if needle in p.name.lower() or needle in (p.sku or "").lower()]
        rows.sort(key=lambda p: p.id, reverse=query.descending)
        rows.sort(key=lambda p: getattr(p, query.order_by), reverse=query.descending)
        return rows


class FakeUnitOfWork:
    """Transactions snapshot the tables and restore them on failure, like a rollback.

    `conflicts` makes the next that many commits fail as a lost race does on Aurora DSQL,
    so the work is rerun from the top: use cases must survive that. `fail_next_commit`
    makes the next commit raise it instead, as a database that went away does.
    """

    def __init__(self, tables: Tables | None = None) -> None:
        self.tables = tables or Tables()
        self.commits = 0
        self.conflicts = 0
        self.fail_next_commit: Exception | None = None
        self.locks: list[str] = []
        self.users = FakeUserRepository(self)
        self.workspaces = FakeWorkspaceRepository(self)
        self.roles = FakeRoleRepository(self)
        self.members = FakeMemberRepository(self)
        self.invitations = FakeInvitationRepository(self)
        self.products = FakeProductRepository(self)

    async def flush(self) -> None:
        self.tables.check_unique()

    async def transaction[T](self, work: Callable[[], Awaitable[T]]) -> T:
        for _ in range(3):
            snapshot = copy.deepcopy(self.tables)
            try:
                result = await work()
                self.tables.check_unique()
                if failure := self.fail_next_commit:
                    self.fail_next_commit = None
                    raise failure
            except BaseException:
                self._rollback(snapshot)
                raise
            if self.conflicts:
                self.conflicts -= 1
                self._rollback(snapshot)
                continue
            self.commits += 1
            return result
        raise ConcurrencyConflictError

    def _rollback(self, snapshot: Tables) -> None:
        """Back to `snapshot`, keeping the objects callers hold: the ORM's identity map
        also keeps them across a rollback and reloads their state."""
        for name, saved in vars(snapshot).items():
            rows: dict[object, object] = saved
            live: dict[object, object] = getattr(self.tables, name)
            for key in live.keys() - rows.keys():
                del live[key]
            for key, row in rows.items():
                if key in live and hasattr(row, "__dict__"):
                    vars(live[key]).update(vars(row))
                else:
                    live[key] = row


# --- outside services -------------------------------------------------------------------


class FakeIdentityProvider:
    """A user pool: `tokens` maps an access token to its `(sub, email)`."""

    def __init__(self) -> None:
        self.accounts: dict[str, str] = {}  # email -> sub
        self.tokens: dict[str, tuple[str, str]] = {}
        self.deleted: list[str] = []
        self.fail_deletes = False

    def sign_in(self, email: str) -> str:
        """An access token for the account with `email`, creating it if needed."""
        sub = self.accounts.setdefault(email.lower(), str(uuid.uuid4()))
        token = f"token-{uuid.uuid4().hex}"
        self.tokens[token] = (sub, email.lower())
        return token

    async def create_account(self, email: str, password: str) -> str:
        if email in self.accounts:
            raise EmailTakenError
        if len(password) < 8:
            raise PasswordRejectedError("Password did not conform with policy: too short")
        self.accounts[email] = str(uuid.uuid4())
        return self.accounts[email]

    async def email_of(self, access_token: str) -> tuple[str, str]:
        return self.tokens[access_token]

    async def delete_account(self, sub: str) -> None:
        if self.fail_deletes:
            raise RuntimeError("user pool unreachable")
        self.deleted.append(sub)


class FakeMediaStorage:
    """A bucket: `upload()` plays the client's presigned POST; `objects` is what is stored.

    `while_processing` runs in the middle of `store_image`, the slow step: what other
    requests do meanwhile.
    """

    def __init__(self, max_bytes: int = 5 * 1024 * 1024) -> None:
        self.max_bytes = max_bytes
        self.while_processing: Callable[[], Awaitable[object]] | None = None
        self.pending: dict[str, int] = {}
        self.objects: set[str] = set()
        self.deleted: list[str] = []

    def upload(self, size: int = 1000) -> str:
        key = uuid.uuid4().hex
        self.pending[key] = size
        return key

    def presign(self, content_type: str, allowed: frozenset[str]) -> UploadTicket:
        if content_type not in allowed:
            raise UnsupportedMediaTypeError("Unsupported image type")
        return UploadTicket(
            url="https://bucket.example.com",
            fields={"Content-Type": content_type},
            key=uuid.uuid4().hex,
            max_bytes=self.max_bytes,
            expires_in=600,
        )

    async def pending_size(self, upload_key: str) -> int:
        if upload_key not in self.pending:
            raise NotFoundError("Upload not found")
        return self.pending[upload_key]

    async def store_image(self, upload_key: str) -> StoredImage:
        size = await self.pending_size(upload_key)
        del self.pending[upload_key]
        if meanwhile := self.while_processing:
            self.while_processing = None
            await meanwhile()
        stem = f"media/{uuid.uuid4().hex}"
        stored = StoredImage(
            key=f"{stem}.webp",
            thumb_key=f"{stem}_thumb.webp",
            content_type="image/webp",
            size_bytes=size,
        )
        self.objects |= {stored.key, stored.thumb_key}
        return stored

    async def store_square(self, upload_key: str) -> str:
        await self.pending_size(upload_key)
        del self.pending[upload_key]
        key = f"media/{uuid.uuid4().hex}.webp"
        self.objects.add(key)
        return key

    def url_for(self, key: str) -> str:
        return f"https://cdn.example.com/{key}"

    async def delete(self, key: str) -> None:
        self.deleted.append(key)
        self.objects.discard(key)


class FakeMailer:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []
        self.fail = False

    async def send_invitation(self, email: str, workspace_name: str, url: str) -> None:
        if self.fail:
            raise RuntimeError("SES unreachable")
        self.sent.append((email, workspace_name, url))

    def token_sent_to(self, email: str) -> str:
        url = next(url for to, _, url in reversed(self.sent) if to == email)
        return url.rsplit("token=", 1)[1]


class FakeEventPublisher:
    def __init__(self) -> None:
        self.published: list[tuple[ChangeEvent, list[str]]] = []

    async def publish(self, event: ChangeEvent, recipients: list[str]) -> None:
        self.published.append((event, sorted(recipients)))

    def actions(self) -> list[str]:
        return [event.action for event, _ in self.published]

    def last(self) -> tuple[str, str, str, str, list[str]]:
        """`(resource, action, id, owner_id, recipients)` of the latest event."""
        event, recipients = self.published[-1]
        return event.resource, event.action, event.id, event.owner_id, recipients


class FakeRateLimiter:
    def __init__(self, retry_after: int | None = None) -> None:
        self.retry_after = retry_after
        self.hits: list[str] = []

    async def hit(self, key: str) -> int | None:
        self.hits.append(key)
        return self.retry_after
