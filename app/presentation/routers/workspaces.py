from fastapi import APIRouter, status

from app.presentation.dependencies import (
    CurrentUserDep,
    CurrentWorkspaceDep,
    WorkspaceServiceDep,
)
from app.presentation.errors import (
    ONBOARDED,
    WORKSPACE_SCOPED,
    error_responses,
)
from app.presentation.schemas.uploads import PresignedUploadRead, UploadConfirm, UploadRequest
from app.presentation.schemas.users import (
    AcceptInvitationInput,
    InvitationCreated,
    InvitationInput,
    InvitationPreview,
    InvitationRead,
    MemberRead,
    MemberRoleInput,
    PermissionRead,
    PermissionsInput,
    RoleInput,
    RoleRead,
    SlugAvailability,
    TransferInput,
    WorkspaceInput,
    WorkspaceRead,
)

router = APIRouter(tags=["workspaces"])
# The routes under one workspace; they take the "workspaces" tag from `router` when included.
scoped = APIRouter(
    prefix="/api/workspaces/{slug}",
    responses=error_responses(*WORKSPACE_SCOPED),
)


@router.get("/api/workspaces", responses=error_responses(*ONBOARDED))
async def workspaces(user: CurrentUserDep, service: WorkspaceServiceDep) -> list[WorkspaceRead]:
    return [WorkspaceRead.of(view) for view in await service.list_mine(user.id)]


@router.post(
    "/api/workspaces",
    status_code=201,
    responses=error_responses(*ONBOARDED, status.HTTP_409_CONFLICT),
)
async def create_workspace(
    data: WorkspaceInput, user: CurrentUserDep, service: WorkspaceServiceDep
) -> WorkspaceRead:
    return WorkspaceRead.of(await service.create(data.name, data.slug, user.id))


@router.get("/api/workspaces/availability/{slug}", responses=error_responses(*ONBOARDED))
async def slug_availability(
    slug: str, user: CurrentUserDep, service: WorkspaceServiceDep
) -> SlugAvailability:
    return SlugAvailability(available=await service.slug_available(slug, user.id))


@scoped.get("")
async def get_workspace(
    workspace: CurrentWorkspaceDep, user: CurrentUserDep, service: WorkspaceServiceDep
) -> WorkspaceRead:
    return WorkspaceRead.of(await service.get(workspace.id, user.id))


@scoped.patch("", responses=error_responses(status.HTTP_409_CONFLICT))
async def update_workspace(
    workspace: CurrentWorkspaceDep,
    data: WorkspaceInput,
    user: CurrentUserDep,
    service: WorkspaceServiceDep,
) -> WorkspaceRead:
    return WorkspaceRead.of(await service.update(workspace.id, data.name, data.slug, user.id))


@scoped.post(
    "/logo/uploads",
    responses=error_responses(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE),
    description=(
        "A presigned POST for a new workspace logo (JPEG, PNG or WebP, up to `max_bytes`). "
        "Upload straight to `url` with `fields` plus the file, then call `confirm`."
    ),
)
async def presign_logo(
    workspace: CurrentWorkspaceDep,
    data: UploadRequest,
    user: CurrentUserDep,
    service: WorkspaceServiceDep,
) -> PresignedUploadRead:
    ticket = await service.request_logo_upload(workspace.id, user.id, data.content_type)
    return PresignedUploadRead.of(ticket)


@scoped.post(
    "/logo/confirm",
    responses=error_responses(
        status.HTTP_413_CONTENT_TOO_LARGE,
        status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
        status.HTTP_422_UNPROCESSABLE_CONTENT,
    ),
    description=(
        "Make the uploaded object the workspace logo: it is validated, cropped to a 256px "
        "WebP square and stored; the pending object is deleted, so a second confirm of the "
        "same key answers `404`."
    ),
)
async def confirm_logo(
    workspace: CurrentWorkspaceDep,
    data: UploadConfirm,
    user: CurrentUserDep,
    service: WorkspaceServiceDep,
) -> WorkspaceRead:
    return WorkspaceRead.of(await service.set_logo(workspace.id, data.key, user.id))


@scoped.delete("/logo")
async def delete_logo(
    workspace: CurrentWorkspaceDep, user: CurrentUserDep, service: WorkspaceServiceDep
) -> WorkspaceRead:
    return WorkspaceRead.of(await service.set_logo(workspace.id, None, user.id))


@scoped.delete("", status_code=204)
async def delete_workspace(
    workspace: CurrentWorkspaceDep, user: CurrentUserDep, service: WorkspaceServiceDep
) -> None:
    await service.delete(workspace.id, user.id)


@scoped.get("/roles")
async def roles(
    workspace: CurrentWorkspaceDep, user: CurrentUserDep, service: WorkspaceServiceDep
) -> list[RoleRead]:
    return [RoleRead.of(view) for view in await service.roles(workspace.id, user.id)]


@scoped.post(
    "/roles",
    status_code=201,
    responses=error_responses(status.HTTP_409_CONFLICT),
)
async def create_role(
    workspace: CurrentWorkspaceDep,
    data: RoleInput,
    user: CurrentUserDep,
    service: WorkspaceServiceDep,
) -> RoleRead:
    return RoleRead.of(await service.save_role(workspace.id, None, data.name, user.id))


@scoped.patch(
    "/roles/{role_id}",
    responses=error_responses(status.HTTP_409_CONFLICT),
)
async def update_role(
    workspace: CurrentWorkspaceDep,
    role_id: str,
    data: RoleInput,
    user: CurrentUserDep,
    service: WorkspaceServiceDep,
) -> RoleRead:
    return RoleRead.of(await service.save_role(workspace.id, role_id, data.name, user.id))


@scoped.delete(
    "/roles/{role_id}",
    status_code=204,
    responses=error_responses(status.HTTP_409_CONFLICT),
)
async def delete_role(
    workspace: CurrentWorkspaceDep,
    role_id: str,
    user: CurrentUserDep,
    service: WorkspaceServiceDep,
) -> None:
    await service.delete_role(workspace.id, role_id, user.id)


@scoped.put(
    "/roles/{role_id}/permissions",
    status_code=204,
)
async def set_permissions(
    workspace: CurrentWorkspaceDep,
    role_id: str,
    data: PermissionsInput,
    user: CurrentUserDep,
    service: WorkspaceServiceDep,
) -> None:
    await service.set_permissions(workspace.id, role_id, data.permissions, user.id)


@scoped.get("/permissions")
async def permissions(
    workspace: CurrentWorkspaceDep, user: CurrentUserDep, service: WorkspaceServiceDep
) -> list[PermissionRead]:
    return PermissionRead.catalog(await service.permission_catalog(workspace.id, user.id))


@scoped.get("/members")
async def members(
    workspace: CurrentWorkspaceDep, user: CurrentUserDep, service: WorkspaceServiceDep
) -> list[MemberRead]:
    return [MemberRead.of_member(view) for view in await service.members(workspace.id, user.id)]


@scoped.patch(
    "/members/{member_id}",
    status_code=204,
    responses=error_responses(status.HTTP_409_CONFLICT),
)
async def set_member_role(
    workspace: CurrentWorkspaceDep,
    member_id: str,
    data: MemberRoleInput,
    user: CurrentUserDep,
    service: WorkspaceServiceDep,
) -> None:
    await service.set_member_role(workspace.id, member_id, data.role_id, user.id)


@scoped.delete(
    "/members/{member_id}",
    status_code=204,
    responses=error_responses(status.HTTP_409_CONFLICT),
)
async def remove_member(
    workspace: CurrentWorkspaceDep,
    member_id: str,
    user: CurrentUserDep,
    service: WorkspaceServiceDep,
) -> None:
    await service.remove_member(workspace.id, member_id, user.id)


@scoped.post(
    "/transfer-ownership",
    status_code=204,
    responses=error_responses(status.HTTP_409_CONFLICT),
)
async def transfer_ownership(
    workspace: CurrentWorkspaceDep,
    data: TransferInput,
    user: CurrentUserDep,
    service: WorkspaceServiceDep,
) -> None:
    await service.transfer(workspace.id, data.user_id, user.id)


@scoped.get("/invitations")
async def invitations(
    workspace: CurrentWorkspaceDep, user: CurrentUserDep, service: WorkspaceServiceDep
) -> list[InvitationRead]:
    return [InvitationRead.of(view) for view in await service.invitations(workspace.id, user.id)]


@scoped.post(
    "/invitations",
    status_code=201,
    responses=error_responses(status.HTTP_409_CONFLICT),
)
async def invite(
    workspace: CurrentWorkspaceDep,
    data: InvitationInput,
    user: CurrentUserDep,
    service: WorkspaceServiceDep,
) -> InvitationCreated:
    created = await service.invite(workspace.id, str(data.email), data.role_id, user.id)
    return InvitationCreated.created(created.view, created.url)


@scoped.delete(
    "/invitations/{invitation_id}",
    status_code=204,
    responses=error_responses(status.HTTP_409_CONFLICT),
)
async def revoke_invitation(
    workspace: CurrentWorkspaceDep,
    invitation_id: str,
    user: CurrentUserDep,
    service: WorkspaceServiceDep,
) -> None:
    await service.revoke_invitation(workspace.id, invitation_id, user.id)


# Included before the invitation routes so the operations keep their order.
router.include_router(scoped)


@router.get("/api/invitations/{token}", responses=error_responses(status.HTTP_410_GONE))
async def invitation_preview(token: str, service: WorkspaceServiceDep) -> InvitationPreview:
    return InvitationPreview.of(await service.preview_invitation(token))


@router.post(
    "/api/invitations/accept",
    responses=error_responses(
        *ONBOARDED, status.HTTP_404_NOT_FOUND, status.HTTP_409_CONFLICT, status.HTTP_410_GONE
    ),
)
async def accept_invitation(
    data: AcceptInvitationInput, user: CurrentUserDep, service: WorkspaceServiceDep
) -> WorkspaceRead:
    return WorkspaceRead.of(await service.accept_invitation(data.token, user.id))
