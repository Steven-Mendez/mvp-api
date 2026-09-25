"""What the use cases hand back when an entity alone is not the whole answer."""

from dataclasses import dataclass

from app.domain.user import User
from app.domain.workspace import Invitation, Member, Role, Workspace


@dataclass(frozen=True)
class ProfileView:
    user: User
    joined_workspace: bool


@dataclass(frozen=True)
class WorkspaceView:
    """A workspace as one member sees it: their role and what it allows."""

    workspace: Workspace
    role_id: str
    permissions: list[str]


@dataclass(frozen=True)
class RoleView:
    role: Role
    permissions: list[str]
    member_count: int


@dataclass(frozen=True)
class MemberView:
    member: Member
    user: User
    role_name: str


@dataclass(frozen=True)
class InvitationView:
    invitation: Invitation
    accepted_by: User | None


@dataclass(frozen=True)
class CreatedInvitation:
    view: InvitationView
    url: str


@dataclass(frozen=True)
class InvitationPreview:
    workspace: Workspace
    invitation: Invitation
    role_name: str
