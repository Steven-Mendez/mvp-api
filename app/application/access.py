"""Access checks, run inside the unit of work of the use case they guard, so a permission
and the state it protects are read in the same transaction."""

from app.application.ports import UnitOfWork
from app.domain.access import (
    ONBOARDED,
    PERMISSION_DENIED,
    is_on_own_account,
    onboarding_needed_for,
)
from app.domain.errors import ForbiddenError, NotFoundError
from app.domain.user import existing
from app.domain.workspace import WORKSPACE_NOT_FOUND, Workspace


async def require(uow: UnitOfWork, user_id: str, action: str, resource: str) -> None:
    """Refuse unless `user_id` may perform `action` on `resource` (their own account id or
    a workspace id): 401/403 for an unfinished onboarding step, 403 otherwise."""
    existing(await uow.users.get(user_id)).ensure_onboarded(onboarding_needed_for(action))
    if not await permits(uow, user_id, action, resource):
        raise ForbiddenError(PERMISSION_DENIED)


async def permits(uow: UnitOfWork, user_id: str, action: str, resource: str) -> bool:
    if is_on_own_account(action):
        return resource == user_id
    workspace = await uow.workspaces.get(resource)
    member = await uow.members.get(resource, user_id) if workspace else None
    if workspace is None or member is None:
        return False
    granted = workspace.grants(user_id, action)
    if granted is not None:
        return granted
    return action in await uow.roles.permissions(member.role_id)


class AccessService:
    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    async def workspace(self, user_id: str, slug: str, action: str | None = None) -> Workspace:
        """The workspace at `slug` if the account belongs to it (404 otherwise), and may
        perform `action` there when one is given (403 otherwise)."""
        uow = self._uow
        existing(await uow.users.get(user_id)).ensure_onboarded(ONBOARDED)
        workspace = await uow.workspaces.by_slug(slug)
        if workspace is None or await uow.members.get(workspace.id, user_id) is None:
            raise NotFoundError(WORKSPACE_NOT_FOUND)
        if action is not None and not await permits(uow, user_id, action, workspace.id):
            raise ForbiddenError(PERMISSION_DENIED)
        return workspace
