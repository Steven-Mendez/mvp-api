from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.application.dto import (
    InvitationPreview as InvitationPreviewView,
)
from app.application.dto import (
    InvitationView,
    MemberView,
    ProfileView,
    RoleView,
    WorkspaceView,
)
from app.domain.workspace import RESERVED_SLUGS


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RegisterInput(StrictModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=256)
    display_name: str = Field(default="", max_length=100)
    invitation_token: str | None = Field(default=None, max_length=200)

    @field_validator("email", mode="after")
    @classmethod
    def normalized_email(cls, value: str) -> str:
        return value.lower()


class Registered(StrictModel):
    """Deliberately empty: the same answer for a new account and for a taken email."""

    registered: Literal[True] = True


class ProfileRead(StrictModel):
    id: str
    email: str
    display_name: str
    avatar_url: str | None
    onboarding_status: Literal["profile_pending", "workspace_pending", "completed"]
    required_profile_fields: list[str]
    missing_profile_fields: list[str]
    created_at: datetime
    # True once the account belongs to a workspace, e.g. after accepting an
    # invitation, so onboarding can skip workspace creation and invites.
    joined_workspace: bool = False

    @classmethod
    def of(cls, view: ProfileView) -> ProfileRead:
        return cls(**_profile_fields(view), joined_workspace=view.joined_workspace)


class ProfileUpdate(StrictModel):
    display_name: str = Field(min_length=1, max_length=100)

    @field_validator("display_name")
    @classmethod
    def nonempty_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Enter a name")
        return value.strip()


class WorkspaceInput(StrictModel):
    name: str = Field(min_length=1, max_length=100)
    slug: str = Field(min_length=3, max_length=50, pattern=r"^[a-z0-9][a-z0-9-]*[a-z0-9]$")

    @field_validator("slug")
    @classmethod
    def routable_slug(cls, value: str) -> str:
        if value in RESERVED_SLUGS:
            raise ValueError("This slug is reserved; choose another workspace address")
        return value

    @field_validator("name")
    @classmethod
    def nonempty_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Enter a workspace name")
        return value.strip()


class SlugAvailability(StrictModel):
    available: bool


class WorkspaceRead(StrictModel):
    id: str
    name: str
    slug: str
    owner_id: str
    logo_url: str | None
    role_id: str
    permissions: list[str]

    @classmethod
    def of(cls, view: WorkspaceView) -> WorkspaceRead:
        workspace = view.workspace
        return cls(
            id=workspace.id,
            name=workspace.name,
            slug=workspace.slug,
            owner_id=workspace.owner_id,
            logo_url=workspace.logo_url,
            role_id=view.role_id,
            permissions=view.permissions,
        )


class RoleInput(StrictModel):
    name: str = Field(min_length=2, max_length=50, pattern=r"^[a-z][a-z0-9 -]*$")


class RoleRead(StrictModel):
    id: str
    name: str
    is_system_role: bool
    is_protected: bool
    permissions: list[str]
    member_count: int

    @classmethod
    def of(cls, view: RoleView) -> RoleRead:
        return cls(
            id=view.role.id,
            name=view.role.name,
            is_system_role=view.role.is_system,
            is_protected=view.role.is_protected,
            permissions=view.permissions,
            member_count=view.member_count,
        )


class PermissionRead(StrictModel):
    id: str
    category: str
    label: str
    description: str

    @classmethod
    def catalog(cls, catalog: dict[str, tuple[str, str, str]]) -> list[PermissionRead]:
        return [
            cls(id=action, category=category, label=label, description=description)
            for action, (category, label, description) in catalog.items()
        ]


class PermissionsInput(StrictModel):
    permissions: list[str] = Field(max_length=100)


class MemberRead(ProfileRead):
    role_id: str
    role_name: str
    joined_at: datetime

    @classmethod
    def of_member(cls, view: MemberView) -> MemberRead:
        return cls(
            **_profile_fields(ProfileView(user=view.user, joined_workspace=True)),
            joined_workspace=True,
            role_id=view.member.role_id,
            role_name=view.role_name,
            joined_at=view.member.joined_at,
        )


class MemberRoleInput(StrictModel):
    role_id: str


class TransferInput(StrictModel):
    user_id: str


class InvitationInput(StrictModel):
    email: EmailStr
    role_id: str


class InvitationRead(StrictModel):
    id: str
    email: str
    role_id: str
    status: str
    expires_at: datetime
    avatar_url: str | None
    display_name: str | None

    @classmethod
    def of(cls, view: InvitationView) -> InvitationRead:
        return cls(**_invitation_fields(view))


class InvitationCreated(InvitationRead):
    invitation_url: str

    @classmethod
    def created(cls, view: InvitationView, url: str) -> InvitationCreated:
        return cls(**_invitation_fields(view), invitation_url=url)


class InvitationPreview(StrictModel):
    workspace_slug: str
    workspace_name: str
    email: str
    role_name: str
    expires_at: datetime

    @classmethod
    def of(cls, view: InvitationPreviewView) -> InvitationPreview:
        return cls(
            workspace_slug=view.workspace.slug,
            workspace_name=view.workspace.name,
            email=view.invitation.email,
            role_name=view.role_name,
            expires_at=view.invitation.expires_at,
        )


class AcceptInvitationInput(StrictModel):
    token: str = Field(max_length=200)


def _profile_fields(view: ProfileView) -> dict[str, Any]:
    user = view.user
    return {
        "id": user.id,
        "email": user.email,
        "display_name": user.display_name,
        "avatar_url": user.avatar_url,
        "onboarding_status": user.onboarding_status,
        "required_profile_fields": user.required_profile_fields,
        "missing_profile_fields": user.missing_profile_fields,
        "created_at": user.created_at,
    }


def _invitation_fields(view: InvitationView) -> dict[str, Any]:
    invitation, accepted_by = view.invitation, view.accepted_by
    return {
        "id": invitation.id,
        "email": invitation.email,
        "role_id": invitation.role_id,
        "status": invitation.current_status,
        "expires_at": invitation.expires_at,
        "avatar_url": accepted_by.avatar_url if accepted_by else None,
        "display_name": accepted_by.display_name if accepted_by else None,
    }
