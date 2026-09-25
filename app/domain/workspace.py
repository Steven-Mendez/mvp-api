"""Workspaces and the people in them: roles with permissions, members and invitations."""

import hashlib
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.domain.access import CATALOG, DEFAULT_MEMBER_ROLE, OWNER_ACTIONS, OWNER_ONLY, OWNER_ROLE
from app.domain.errors import ConflictError, ForbiddenError, GoneError, InvalidError
from app.domain.ids import new_id, now

RESERVED_SLUGS = frozenset(
    {
        "profile",
        "sessions",
        "settings",
        "onboarding",
        "login",
        "sign-in",
        "invite",
        "workspaces",
        "reset-password",
        "delete-account",
        "api",
        "assets",
        "availability",
        "media",
        "products",
    }
)
SLUG_PATTERN = r"^[a-z0-9][a-z0-9-]*[a-z0-9]$"
INVITATION_LIFETIME = timedelta(days=7)

WORKSPACE_NOT_FOUND = "Workspace not found"
MEMBER_NOT_FOUND = "Member not found"
ROLE_NOT_FOUND = "Role not found"
INVITATION_NOT_FOUND = "Invitation not found"
INVITATION_UNUSABLE = "This invitation is invalid, expired or already used"
OWNER_IS_PROTECTED = "Owner is protected. Use transfer ownership instead"
SLUG_TAKEN = "This workspace slug is already taken"


def is_valid_slug(slug: str) -> bool:
    """A slug is the workspace's address: routable, lowercase, not a reserved path."""
    return (
        3 <= len(slug) <= 50
        and re.fullmatch(SLUG_PATTERN, slug) is not None
        and slug not in RESERVED_SLUGS
    )


def validate_permissions(permissions: list[str]) -> set[str]:
    if OWNER_ONLY in permissions:
        raise ForbiddenError("Only the owner can delete a workspace")
    if not set(permissions).issubset(CATALOG):
        raise InvalidError("Unknown permission")
    return set(permissions)


def ensure_assignable_role_name(name: str) -> None:
    if name.strip() == OWNER_ROLE:
        raise ForbiddenError("Owner is a protected role")


@dataclass
class Workspace:
    id: str
    name: str
    slug: str
    owner_id: str
    logo_url: str | None
    logo_key: str | None
    created_at: datetime
    deleted_at: datetime | None

    @classmethod
    def create(cls, *, name: str, slug: str, owner_id: str) -> Workspace:
        workspace = cls(
            id=new_id(),
            name=name.strip(),
            slug=slug,
            owner_id=owner_id,
            logo_url=None,
            logo_key=None,
            created_at=now(),
            deleted_at=None,
        )
        if not workspace.name:
            raise InvalidError("Enter a workspace name")
        return workspace

    def rename(self, name: str, slug: str) -> None:
        self.name, self.slug = name.strip(), slug

    def replace_logo(self, key: str | None, url: str | None) -> str | None:
        """Point at a new logo (or none) and return the storage key it replaces."""
        previous = self.logo_key
        self.logo_key, self.logo_url = key, url
        return previous

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None

    def soft_delete(self) -> None:
        """Members lose access at once and the slug is freed; the rows go in the purge."""
        self.deleted_at = now()
        self.slug = f"deleted-{new_id()}"[:50]

    def grants(self, user_id: str, action: str) -> bool | None:
        """What a member may do regardless of their role: the owner anything, nobody else
        an owner action, everybody a read. `None` means the member's role decides."""
        if user_id == self.owner_id:
            return True
        if action in OWNER_ACTIONS:
            return False
        if action.endswith(".read"):
            return True
        return None

    def permissions_for(self, user_id: str, role_permissions: list[str]) -> list[str]:
        return list(CATALOG) if user_id == self.owner_id else role_permissions

    def ensure_single_owner(self, owner_ids: list[str]) -> None:
        """Exactly one member holds the protected owner role, and it is `owner_id`."""
        if owner_ids != [self.owner_id]:
            raise ConflictError("Workspace must have exactly one owner")

    def ensure_not_the_owner(self, user_id: str, message: str) -> None:
        if user_id == self.owner_id:
            raise ForbiddenError(message)


@dataclass
class Role:
    id: str
    workspace_id: str
    name: str
    is_system: bool
    is_protected: bool

    @classmethod
    def system(cls, workspace_id: str, name: str) -> Role:
        return cls(
            id=new_id(),
            workspace_id=workspace_id,
            name=name,
            is_system=True,
            is_protected=name == OWNER_ROLE,
        )

    @classmethod
    def custom(cls, workspace_id: str, name: str) -> Role:
        ensure_assignable_role_name(name)
        return cls(
            id=new_id(),
            workspace_id=workspace_id,
            name=name.strip(),
            is_system=False,
            is_protected=False,
        )

    def rename(self, name: str) -> None:
        ensure_assignable_role_name(name)
        self.name = name.strip()

    def ensure_configurable(self) -> None:
        if self.is_protected:
            raise ForbiddenError(OWNER_IS_PROTECTED)

    def ensure_invitable(self, *, may_manage_roles: bool) -> None:
        """`members.invite` alone hands out the system member role only; any other role is
        a role assignment and also takes `roles.manage`. A renamed member role fails closed."""
        is_default = self.is_system and self.name == DEFAULT_MEMBER_ROLE
        if not is_default and not may_manage_roles:
            raise ForbiddenError("Inviting with a role other than member requires managing roles")


@dataclass
class Member:
    workspace_id: str
    user_id: str
    role_id: str
    joined_at: datetime

    @classmethod
    def join(cls, workspace_id: str, user_id: str, role_id: str) -> Member:
        return cls(workspace_id=workspace_id, user_id=user_id, role_id=role_id, joined_at=now())


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass
class Invitation:
    id: str
    workspace_id: str
    email: str
    role_id: str
    token_hash: str
    status: str
    invited_by: str
    accepted_by: str | None
    expires_at: datetime
    created_at: datetime

    @classmethod
    def issue(
        cls, *, workspace_id: str, email: str, role_id: str, invited_by: str
    ) -> tuple[Invitation, str]:
        """A new invitation and its token; only the token's hash is kept."""
        token = secrets.token_urlsafe(32)
        created = now()
        invitation = cls(
            id=new_id(),
            workspace_id=workspace_id,
            email=email.lower(),
            role_id=role_id,
            token_hash=hash_token(token),
            status="pending",
            invited_by=invited_by,
            accepted_by=None,
            expires_at=created + INVITATION_LIFETIME,
            created_at=created,
        )
        return invitation, token

    @property
    def current_status(self) -> str:
        if self.status == "pending" and self.expires_at <= now():
            return "expired"
        return self.status

    def ensure_usable(self) -> None:
        if self.current_status != "pending":
            raise GoneError(INVITATION_UNUSABLE)

    def ensure_addressed_to(self, email: str) -> None:
        if email.lower() != self.email:
            raise ForbiddenError("Sign in with the email this invitation was sent to")

    def revoke(self) -> None:
        if self.current_status != "pending":
            raise ConflictError("This invitation is no longer pending")
        self.status = "revoked"

    def accept(self, user_id: str, email: str) -> None:
        self.ensure_usable()
        self.ensure_addressed_to(email)
        self.status, self.accepted_by = "accepted", user_id
