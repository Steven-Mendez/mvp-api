"""What the use cases need from the outside world. `app.infrastructure` implements these;
the use cases never import it."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol

from app.domain.product import Product, ProductImage, ProductStatus
from app.domain.user import User
from app.domain.workspace import Invitation, Member, Role, Workspace

# --- persistence ------------------------------------------------------------------------


class UserRepository(Protocol):
    async def get(self, user_id: str) -> User | None: ...
    def add(self, user: User) -> None: ...
    async def add_unless_taken(self, user: User) -> None:
        """Insert unless the id or the email already exists (concurrent first requests)."""
        ...

    async def delete(self, user: User) -> None: ...


class WorkspaceRepository(Protocol):
    async def get(self, workspace_id: str) -> Workspace | None:
        """A live workspace."""
        ...

    async def get_deleted(self, workspace_id: str) -> Workspace | None: ...
    async def by_slug(self, slug: str) -> Workspace | None: ...
    async def lock(self, workspace_id: str) -> Workspace | None:
        """A live workspace, locked until the transaction ends."""
        ...

    async def of_member(self, user_id: str) -> list[Workspace]: ...
    async def slug_exists(self, slug: str) -> bool: ...
    async def any_owned_by(self, user_id: str) -> bool: ...
    async def deleted_ids(self, *, owned_by: str | None = None) -> list[str]: ...
    def add(self, workspace: Workspace) -> None: ...
    async def purge(self, workspace: Workspace) -> None:
        """Delete the workspace row with its members, roles and invitations."""
        ...


class RoleRepository(Protocol):
    async def get(self, role_id: str) -> Role | None: ...
    async def by_name(self, workspace_id: str, name: str) -> Role | None: ...
    async def of_workspace(self, workspace_id: str) -> list[Role]:
        """Owner first, then by name."""
        ...

    async def permissions(self, role_id: str) -> list[str]: ...
    async def member_count(self, role_id: str) -> int: ...
    async def is_in_use(self, role_id: str) -> bool:
        """Assigned to a member or to a pending, unexpired invitation."""
        ...

    async def add(self, role: Role, permissions: list[str]) -> None: ...
    async def replace_permissions(self, role_id: str, permissions: set[str]) -> None: ...
    async def delete(self, role: Role) -> None: ...


class MemberRepository(Protocol):
    async def get(self, workspace_id: str, user_id: str) -> Member | None: ...
    async def of_workspace(self, workspace_id: str) -> list[tuple[Member, User, Role]]:
        """Members with their account and role, by display name."""
        ...

    async def user_ids(self, workspace_id: str) -> list[str]: ...
    async def owner_ids(self, workspace_id: str) -> list[str]:
        """Members holding the protected owner role."""
        ...

    async def belongs_to_any(self, user_id: str) -> bool: ...
    async def has_email(self, workspace_id: str, email: str) -> bool: ...
    def add(self, member: Member) -> None: ...
    async def delete(self, member: Member) -> None: ...
    async def remove_all(self, workspace_id: str) -> None: ...
    async def remove_user(self, user_id: str) -> None: ...


class InvitationRepository(Protocol):
    async def get(self, invitation_id: str) -> Invitation | None: ...
    async def by_token(self, token: str) -> Invitation | None: ...
    async def of_workspace(self, workspace_id: str) -> list[tuple[Invitation, User | None]]:
        """Newest first, each with the account that accepted it."""
        ...

    async def has_pending(self, workspace_id: str, email: str) -> bool: ...
    def add(self, invitation: Invitation) -> None: ...
    async def revoke_pending(self, workspace_id: str) -> None: ...
    async def delete_for_role(self, role_id: str) -> None: ...
    async def delete_sent_by(self, user_id: str) -> None: ...


@dataclass(frozen=True)
class ProductFilter:
    search: str | None
    status: ProductStatus | None
    order_by: str
    descending: bool


class ProductRepository(Protocol):
    async def get(self, workspace_id: str, product_id: str) -> Product | None:
        """The product with its images, locked until the transaction ends."""
        ...

    async def exists(self, workspace_id: str, product_id: str) -> bool: ...
    async def count(self, workspace_id: str) -> int: ...
    async def count_images(self, product_id: str) -> int: ...
    async def storage_bytes(self, workspace_id: str) -> int: ...
    async def image_by_upload_key(
        self, product_id: str, upload_key: str
    ) -> ProductImage | None: ...
    async def page(
        self, workspace_id: str, query: ProductFilter, page: int, size: int
    ) -> tuple[list[Product], int]:
        """One numbered page and the total count."""
        ...

    async def after(
        self, workspace_id: str, query: ProductFilter, cursor: str | None, size: int
    ) -> tuple[list[Product], str | None]:
        """The page after an opaque cursor (`None`: the first) and the next cursor."""
        ...

    def add(self, product: Product) -> None: ...
    async def delete(self, product: Product) -> None: ...
    async def delete_batch(self, workspace_id: str, limit: int) -> tuple[int, list[str]]:
        """Delete up to `limit` products with their images; how many went, and the storage
        keys their images pointed at."""
        ...


class ConcurrencyConflictError(Exception):
    """A concurrent transaction won and retrying did not help."""


class DuplicateError(Exception):
    """A write broke a uniqueness constraint the database enforces."""


class UnitOfWork(Protocol):
    """One database session and the repositories that work in it.

    `await uow.transaction(work)` runs `work` and commits, rerunning it when the database
    reports a lost race, so `work` must only touch the database. A unique violation
    surfaces as `DuplicateError`. Reads outside `transaction` need no commit.
    """

    @property
    def users(self) -> UserRepository: ...

    @property
    def workspaces(self) -> WorkspaceRepository: ...

    @property
    def roles(self) -> RoleRepository: ...

    @property
    def members(self) -> MemberRepository: ...

    @property
    def invitations(self) -> InvitationRepository: ...

    @property
    def products(self) -> ProductRepository: ...

    async def transaction[T](self, work: Callable[[], Awaitable[T]]) -> T: ...
    async def flush(self) -> None:
        """Send pending changes so the queries that follow in this transaction see them."""
        ...


# --- outside services -------------------------------------------------------------------


class EmailTakenError(Exception):
    """The identity provider already has an account with this email."""


class PasswordRejectedError(Exception):
    """The identity provider's password policy refused the password; the message says why."""


class AccountLookupError(Exception):
    """The identity provider could not describe the account behind a valid token."""


class IdentityProvider(Protocol):
    """The user pool: it owns passwords and sessions; the API only creates and deletes."""

    async def create_account(self, email: str, password: str) -> str:
        """Create a confirmed account and return its subject."""
        ...

    async def email_of(self, access_token: str) -> tuple[str, str]:
        """`(sub, email)` of the account an access token belongs to."""
        ...

    async def delete_account(self, sub: str) -> None: ...


@dataclass(frozen=True)
class UploadTicket:
    """A presigned POST straight to the bucket: send `fields` plus the file to `url`, then
    confirm `key`."""

    url: str
    fields: dict[str, str]
    key: str
    max_bytes: int
    expires_in: int


@dataclass(frozen=True)
class StoredImage:
    key: str
    thumb_key: str
    content_type: str
    size_bytes: int


class MediaStorage(Protocol):
    def presign(self, content_type: str, allowed: frozenset[str]) -> UploadTicket:
        """A presigned POST for one upload; `UnsupportedMediaTypeError` outside `allowed`."""
        ...

    async def pending_size(self, upload_key: str) -> int:
        """Size of a pending upload; refuses one that is missing or too large."""
        ...

    async def store_image(self, upload_key: str) -> StoredImage:
        """Validate and re-encode a pending upload with a thumbnail, then drop it."""
        ...

    async def store_square(self, upload_key: str) -> str:
        """Validate a pending upload, crop it to a square and return the stored key."""
        ...

    def url_for(self, key: str) -> str: ...
    async def delete(self, key: str) -> None:
        """Best effort: a failure only leaves an orphan object behind."""
        ...


class Mailer(Protocol):
    async def send_invitation(self, email: str, workspace_name: str, url: str) -> None: ...


@dataclass(frozen=True)
class ChangeEvent:
    """Tells clients a resource changed so they refetch it; carries no data."""

    resource: Literal["product"]
    action: Literal["created", "updated", "deleted"]
    id: str
    owner_id: str  # the workspace
    at: datetime


class EventPublisher(Protocol):
    async def publish(self, event: ChangeEvent, recipients: list[str]) -> None:
        """Deliver to each recipient's own channel; best effort."""
        ...
