"""Workspace use cases: the workspace itself, its roles, members and invitations.

Every change runs in one transaction and checks access (`require`) after locking the
workspace row, so a permission and the state it guards are read together.
"""

from loguru import logger

from app.application.access import permits, require
from app.application.dto import (
    CreatedInvitation,
    InvitationPreview,
    InvitationView,
    MemberView,
    RoleView,
    WorkspaceView,
)
from app.application.media import SQUARE_IMAGE_TYPES
from app.application.ports import (
    DuplicateError,
    Mailer,
    MediaStorage,
    UnitOfWork,
    UploadTicket,
)
from app.domain.access import ADMIN_PERMISSIONS, ADMIN_ROLE, CATALOG, OWNER_ROLE, SYSTEM_ROLES
from app.domain.errors import (
    ConflictError,
    ForbiddenError,
    GoneError,
    InvalidError,
    NotFoundError,
    found,
)
from app.domain.user import existing
from app.domain.workspace import (
    INVITATION_NOT_FOUND,
    INVITATION_UNUSABLE,
    MEMBER_NOT_FOUND,
    OWNER_IS_PROTECTED,
    ROLE_NOT_FOUND,
    SLUG_TAKEN,
    WORKSPACE_NOT_FOUND,
    Invitation,
    Member,
    Role,
    Workspace,
    ensure_assignable_role_name,
    is_valid_slug,
    validate_permissions,
)


async def workspace_view(uow: UnitOfWork, workspace: Workspace, user_id: str) -> WorkspaceView:
    member = await uow.members.get(workspace.id, user_id)
    if member is None:
        raise ForbiddenError("Workspace membership required")
    role_permissions = (
        [] if workspace.owner_id == user_id else await uow.roles.permissions(member.role_id)
    )
    return WorkspaceView(
        workspace=workspace,
        role_id=member.role_id,
        permissions=workspace.permissions_for(user_id, role_permissions),
    )


async def configurable_role(uow: UnitOfWork, workspace_id: str, role_id: str) -> Role:
    role = await uow.roles.get(role_id)
    if role is None or role.workspace_id != workspace_id:
        raise NotFoundError(ROLE_NOT_FOUND)
    role.ensure_configurable()
    return role


async def usable_invitation(uow: UnitOfWork, token: str) -> Invitation:
    invitation = await uow.invitations.by_token(token)
    if invitation is None:
        raise GoneError(INVITATION_UNUSABLE)
    invitation.ensure_usable()
    return invitation


async def join_with_invitation(uow: UnitOfWork, token: str, user_id: str) -> WorkspaceView:
    """Accept an invitation inside the caller's transaction."""
    await require(uow, user_id, "invitations.accept", user_id)
    invitation = await usable_invitation(uow, token)
    user = existing(await uow.users.get(user_id))
    invitation.ensure_addressed_to(user.email)
    workspace = found(await uow.workspaces.lock(invitation.workspace_id), WORKSPACE_NOT_FOUND)
    await configurable_role(uow, workspace.id, invitation.role_id)
    if await uow.members.get(workspace.id, user_id) is not None:
        raise ConflictError("You already belong to this workspace")
    # Accept before adding the member: an invitation that expires in between must fail
    # before anything changes, or a caller that swallows the error would commit half of it.
    invitation.accept(user_id, user.email)
    uow.members.add(Member.join(workspace.id, user_id, invitation.role_id))
    user.joined_a_workspace()
    await uow.flush()
    return await workspace_view(uow, workspace, user_id)


class WorkspaceService:
    def __init__(
        self, uow: UnitOfWork, media: MediaStorage, mailer: Mailer, web_base_url: str
    ) -> None:
        self._uow = uow
        self._media = media
        self._mailer = mailer
        self._web_base_url = web_base_url

    # --- workspaces -------------------------------------------------------------------

    async def list_mine(self, user_id: str) -> list[WorkspaceView]:
        uow = self._uow
        await require(uow, user_id, "workspaces.list", user_id)
        return [
            await workspace_view(uow, workspace, user_id)
            for workspace in await uow.workspaces.of_member(user_id)
        ]

    async def slug_available(self, slug: str, user_id: str) -> bool:
        await require(self._uow, user_id, "workspaces.availability", user_id)
        if not is_valid_slug(slug):
            raise InvalidError("Invalid workspace slug")
        return not await self._uow.workspaces.slug_exists(slug)

    async def get(self, workspace_id: str, user_id: str) -> WorkspaceView:
        uow = self._uow
        await require(uow, user_id, "workspace.read", workspace_id)
        workspace = found(await uow.workspaces.get(workspace_id), WORKSPACE_NOT_FOUND)
        return await workspace_view(uow, workspace, user_id)

    async def create(self, name: str, slug: str, user_id: str) -> WorkspaceView:
        """A new workspace with the four system roles and its creator as the owner."""
        uow = self._uow

        async def create() -> WorkspaceView:
            await require(uow, user_id, "workspaces.create", user_id)
            user = existing(await uow.users.get(user_id))
            workspace = Workspace.create(name=name, slug=slug, owner_id=user_id)
            uow.workspaces.add(workspace)
            for role_name, permissions in SYSTEM_ROLES.items():
                role = Role.system(workspace.id, role_name)
                await uow.roles.add(role, permissions)
                if role_name == OWNER_ROLE:
                    uow.members.add(Member.join(workspace.id, user_id, role.id))
            user.created_a_workspace()
            await uow.flush()
            workspace.ensure_single_owner(await uow.members.owner_ids(workspace.id))
            return await workspace_view(uow, workspace, user_id)

        try:
            return await uow.transaction(create)
        except DuplicateError as exc:
            raise ConflictError(SLUG_TAKEN) from exc

    async def update(self, workspace_id: str, name: str, slug: str, user_id: str) -> WorkspaceView:
        uow = self._uow

        async def update() -> WorkspaceView:
            workspace = await self._locked(workspace_id)
            await require(uow, user_id, "workspace.update", workspace_id)
            workspace.rename(name, slug)
            await uow.flush()
            return await workspace_view(uow, workspace, user_id)

        try:
            return await uow.transaction(update)
        except DuplicateError as exc:
            raise ConflictError(SLUG_TAKEN) from exc

    async def request_logo_upload(
        self, workspace_id: str, user_id: str, content_type: str
    ) -> UploadTicket:
        """A presigned POST for a new logo; nothing changes until it is confirmed."""
        await require(self._uow, user_id, "workspace.update", workspace_id)
        return self._media.presign(content_type, SQUARE_IMAGE_TYPES)

    async def set_logo(
        self, workspace_id: str, upload_key: str | None, user_id: str
    ) -> WorkspaceView:
        """Point the workspace at a freshly processed logo, or at none. The image work runs
        before the transaction and is undone if the change fails."""
        uow = self._uow
        await require(uow, user_id, "workspace.update", workspace_id)
        new_key = await self._media.store_square(upload_key) if upload_key else None

        async def replace() -> tuple[WorkspaceView, str | None]:
            workspace = await self._locked(workspace_id)
            await require(uow, user_id, "workspace.update", workspace_id)
            url = self._media.url_for(new_key) if new_key else None
            previous = workspace.replace_logo(new_key, url)
            return await workspace_view(uow, workspace, user_id), previous

        try:
            view, previous = await uow.transaction(replace)
        except BaseException:
            if new_key:
                await self._media.delete(new_key)
            raise
        if previous:
            await self._media.delete(previous)
        return view

    async def delete(self, workspace_id: str, user_id: str) -> None:
        """Soft delete: members lose access now; the scheduled purge removes the rows."""
        uow = self._uow

        async def delete() -> None:
            workspace = await self._locked(workspace_id)
            await require(uow, user_id, "workspace.delete", workspace_id)
            member_ids = await uow.members.user_ids(workspace_id)
            await uow.members.remove_all(workspace_id)
            await uow.invitations.revoke_pending(workspace_id)
            workspace.soft_delete()
            await uow.flush()
            await self._return_to_workspace_step(member_ids)

        await uow.transaction(delete)

    # --- roles ------------------------------------------------------------------------

    async def roles(self, workspace_id: str, user_id: str) -> list[RoleView]:
        await require(self._uow, user_id, "workspace.read", workspace_id)
        return [
            await self._role_view(role) for role in await self._uow.roles.of_workspace(workspace_id)
        ]

    async def save_role(
        self, workspace_id: str, role_id: str | None, name: str, user_id: str
    ) -> RoleView:
        """Create a custom role (`role_id` None) or rename a configurable one."""
        uow = self._uow

        async def save() -> RoleView:
            await self._locked(workspace_id)
            await require(uow, user_id, "roles.manage", workspace_id)
            ensure_assignable_role_name(name)
            if role_id is None:
                role = Role.custom(workspace_id, name)
                await uow.roles.add(role, [])
            else:
                role = await configurable_role(uow, workspace_id, role_id)
                role.rename(name)
            await uow.flush()
            return await self._role_view(role)

        try:
            return await uow.transaction(save)
        except DuplicateError as exc:
            raise ConflictError("A role already uses this name") from exc

    async def delete_role(self, workspace_id: str, role_id: str, user_id: str) -> None:
        uow = self._uow

        async def delete() -> None:
            await self._locked(workspace_id)
            await require(uow, user_id, "roles.manage", workspace_id)
            role = await configurable_role(uow, workspace_id, role_id)
            if await uow.roles.is_in_use(role_id):
                raise ConflictError(
                    "Reassign members and revoke pending invitations before deleting this role"
                )
            await uow.invitations.delete_for_role(role_id)
            await uow.roles.delete(role)

        await uow.transaction(delete)

    async def set_permissions(
        self, workspace_id: str, role_id: str, permissions: list[str], user_id: str
    ) -> None:
        uow = self._uow

        async def replace() -> None:
            await self._locked(workspace_id)
            await require(uow, user_id, "roles.manage", workspace_id)
            await configurable_role(uow, workspace_id, role_id)
            await uow.roles.replace_permissions(role_id, validate_permissions(permissions))

        await uow.transaction(replace)

    async def permission_catalog(
        self, workspace_id: str, user_id: str
    ) -> dict[str, tuple[str, str, str]]:
        await require(self._uow, user_id, "workspace.read", workspace_id)
        return CATALOG

    # --- members ----------------------------------------------------------------------

    async def members(self, workspace_id: str, user_id: str) -> list[MemberView]:
        await require(self._uow, user_id, "workspace.read", workspace_id)
        return [
            MemberView(member=member, user=user, role_name=role.name)
            for member, user, role in await self._uow.members.of_workspace(workspace_id)
        ]

    async def set_member_role(
        self, workspace_id: str, member_id: str, role_id: str, user_id: str
    ) -> None:
        uow = self._uow

        async def assign() -> None:
            workspace = await self._locked(workspace_id)
            await require(uow, user_id, "roles.manage", workspace_id)
            role = await configurable_role(uow, workspace_id, role_id)
            workspace.ensure_not_the_owner(member_id, OWNER_IS_PROTECTED)
            member = found(await uow.members.get(workspace_id, member_id), MEMBER_NOT_FOUND)
            member.role_id = role.id
            await uow.flush()
            workspace.ensure_single_owner(await uow.members.owner_ids(workspace_id))

        await uow.transaction(assign)

    async def remove_member(self, workspace_id: str, member_id: str, user_id: str) -> None:
        uow = self._uow

        async def remove() -> None:
            workspace = await self._locked(workspace_id)
            await require(uow, user_id, "members.remove", workspace_id)
            workspace.ensure_not_the_owner(
                member_id, "Transfer ownership before removing the owner"
            )
            member = found(await uow.members.get(workspace_id, member_id), MEMBER_NOT_FOUND)
            await uow.members.delete(member)
            await uow.flush()
            workspace.ensure_single_owner(await uow.members.owner_ids(workspace_id))
            await self._return_to_workspace_step([member_id])

        await uow.transaction(remove)

    async def transfer(self, workspace_id: str, target_id: str, user_id: str) -> None:
        """The target takes the owner role; the previous owner becomes an admin."""
        uow = self._uow

        async def transfer() -> None:
            workspace = await self._locked(workspace_id)
            await require(uow, user_id, "workspace.transfer", workspace_id)
            if target_id == user_id:
                raise InvalidError("Select a different member")
            target = await uow.members.get(workspace_id, target_id)
            previous = await uow.members.get(workspace_id, user_id)
            if target is None or previous is None:
                raise NotFoundError(MEMBER_NOT_FOUND)
            admin = await uow.roles.by_name(workspace_id, ADMIN_ROLE)
            if admin is None:
                admin = Role.system(workspace_id, ADMIN_ROLE)
                await uow.roles.add(admin, ADMIN_PERMISSIONS)
            target.role_id, previous.role_id = previous.role_id, admin.id
            workspace.owner_id = target_id
            await uow.flush()
            workspace.ensure_single_owner(await uow.members.owner_ids(workspace_id))

        await uow.transaction(transfer)

    # --- invitations ------------------------------------------------------------------

    async def invitations(self, workspace_id: str, user_id: str) -> list[InvitationView]:
        await require(self._uow, user_id, "workspace.read", workspace_id)
        return [
            InvitationView(invitation=invitation, accepted_by=accepted_by)
            for invitation, accepted_by in await self._uow.invitations.of_workspace(workspace_id)
        ]

    async def invite(
        self, workspace_id: str, email: str, role_id: str, user_id: str
    ) -> CreatedInvitation:
        uow = self._uow
        email = email.lower()

        async def issue() -> tuple[Workspace, Invitation, str]:
            workspace = await self._locked(workspace_id)
            await require(uow, user_id, "members.invite", workspace_id)
            role = await configurable_role(uow, workspace_id, role_id)
            role.ensure_invitable(
                may_manage_roles=await permits(uow, user_id, "roles.manage", workspace_id)
            )
            if await uow.members.has_email(
                workspace_id, email
            ) or await uow.invitations.has_pending(workspace_id, email):
                raise ConflictError("This email is already a member or has a pending invitation")
            invitation, token = Invitation.issue(
                workspace_id=workspace_id, email=email, role_id=role_id, invited_by=user_id
            )
            uow.invitations.add(invitation)
            return workspace, invitation, token

        workspace, invitation, token = await uow.transaction(issue)
        url = f"{self._web_base_url}/invite?workspace={workspace.slug}&token={token}"
        try:
            await self._mailer.send_invitation(email, workspace.name, url)
        except Exception:
            # The invitation is committed: failing now would lose the only copy of its link.
            # The inviter gets the link in the answer to share it instead.
            logger.opt(exception=True).warning(
                "Invitation {} created, but its mail could not be sent", invitation.id
            )
        return CreatedInvitation(view=InvitationView(invitation, accepted_by=None), url=url)

    async def revoke_invitation(self, workspace_id: str, invitation_id: str, user_id: str) -> None:
        uow = self._uow

        async def revoke() -> None:
            await self._locked(workspace_id)
            await require(uow, user_id, "invitations.revoke", workspace_id)
            invitation = await uow.invitations.get(invitation_id)
            if invitation is None or invitation.workspace_id != workspace_id:
                raise NotFoundError(INVITATION_NOT_FOUND)
            invitation.revoke()

        await uow.transaction(revoke)

    async def preview_invitation(self, token: str) -> InvitationPreview:
        uow = self._uow
        invitation = await usable_invitation(uow, token)
        # Deleting a workspace revokes its pending invitations in the same transaction,
        # so a usable invitation always has a live workspace.
        workspace = found(await uow.workspaces.get(invitation.workspace_id), WORKSPACE_NOT_FOUND)
        role = await configurable_role(uow, invitation.workspace_id, invitation.role_id)
        return InvitationPreview(workspace=workspace, invitation=invitation, role_name=role.name)

    async def accept_invitation(self, token: str, user_id: str) -> WorkspaceView:
        uow = self._uow

        async def accept() -> WorkspaceView:
            return await join_with_invitation(uow, token, user_id)

        return await uow.transaction(accept)

    # --- helpers ----------------------------------------------------------------------

    async def _locked(self, workspace_id: str) -> Workspace:
        return found(await self._uow.workspaces.lock(workspace_id), WORKSPACE_NOT_FOUND)

    async def _role_view(self, role: Role) -> RoleView:
        return RoleView(
            role=role,
            permissions=await self._uow.roles.permissions(role.id),
            member_count=await self._uow.roles.member_count(role.id),
        )

    async def _return_to_workspace_step(self, user_ids: list[str]) -> None:
        """People who no longer belong to any workspace go back to that onboarding step."""
        for member_id in user_ids:
            user = await self._uow.users.get(member_id)
            if user is not None and not await self._uow.members.belongs_to_any(member_id):
                user.left_every_workspace()
