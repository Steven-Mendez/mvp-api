"""Every guarded operation, done by a member who is not the owner but whose role holds
the permission it needs.

Tests with the owner cannot tell a right permission check from a wrong one (the owner
passes every check), and tests with someone refused cannot either (a misspelled action
refuses everybody). Mutation testing found both gaps; this closes them.
"""

from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from app.domain.access import CATALOG
from app.domain.workspace import Workspace
from tests.unit.conftest import World

type Operation = Callable[[World, Workspace, str], Awaitable[Any]]


async def _rename(w: World, ws: Workspace, me: str) -> None:
    await w.workspaces.update(ws.id, "New", "new", me)


async def _presign_logo(w: World, ws: Workspace, me: str) -> None:
    await w.workspaces.request_logo_upload(ws.id, me, "image/png")


async def _set_logo(w: World, ws: Workspace, me: str) -> None:
    await w.workspaces.set_logo(ws.id, w.media.upload(), me)


async def _create_role(w: World, ws: Workspace, me: str) -> None:
    await w.workspaces.save_role(ws.id, None, "editor", me)


async def _set_permissions(w: World, ws: Workspace, me: str) -> None:
    await w.workspaces.set_permissions(ws.id, await _custom_role(w, ws), [], me)


async def _delete_role(w: World, ws: Workspace, me: str) -> None:
    await w.workspaces.delete_role(ws.id, await _custom_role(w, ws), me)


async def _reassign(w: World, ws: Workspace, me: str) -> None:
    other = await w.join(ws, "other@example.com")
    await w.workspaces.set_member_role(ws.id, other, await w.role_id(ws, "viewer"), me)


async def _remove(w: World, ws: Workspace, me: str) -> None:
    await w.workspaces.remove_member(ws.id, await w.join(ws, "other@example.com"), me)


async def _invite_as_admin(w: World, ws: Workspace, me: str) -> None:
    await w.workspaces.invite(ws.id, "new@example.com", await w.role_id(ws, "admin"), me)


async def _revoke(w: World, ws: Workspace, me: str) -> None:
    invited = await w.workspaces.invite(
        ws.id, "new@example.com", await w.role_id(ws, "member"), ws.owner_id
    )
    await w.workspaces.revoke_invitation(ws.id, invited.view.invitation.id, me)


async def _custom_role(w: World, ws: Workspace) -> str:
    return (await w.workspaces.save_role(ws.id, None, "custom", ws.owner_id)).role.id


CASES: dict[str, tuple[str, Operation]] = {
    "read the workspace": ("viewer", lambda w, ws, me: w.workspaces.get(ws.id, me)),
    "list roles": ("viewer", lambda w, ws, me: w.workspaces.roles(ws.id, me)),
    "list members": ("viewer", lambda w, ws, me: w.workspaces.members(ws.id, me)),
    "list invitations": ("viewer", lambda w, ws, me: w.workspaces.invitations(ws.id, me)),
    "read permissions": ("viewer", lambda w, ws, me: w.workspaces.permission_catalog(ws.id, me)),
    "rename the workspace": ("admin", _rename),
    "ask for a logo upload": ("admin", _presign_logo),
    "set the logo": ("admin", _set_logo),
    "create a role": ("admin", _create_role),
    "set permissions": ("admin", _set_permissions),
    "delete a role": ("admin", _delete_role),
    "reassign a member": ("admin", _reassign),
    "remove a member": ("admin", _remove),
    "invite as admin": ("admin", _invite_as_admin),
    "revoke an invitation": ("admin", _revoke),
}


@pytest.mark.parametrize("name", CASES)
async def test_a_role_holding_the_permission_may(world: World, name: str) -> None:
    role, operation = CASES[name]
    acme = await world.workspace(await world.user("owner@example.com"))
    someone = await world.join(acme, "someone@example.com", role)

    await operation(world, acme, someone)  # no ForbiddenError


async def test_a_member_sees_the_permissions_of_the_role(world: World) -> None:
    acme = await world.workspace(await world.user("owner@example.com"))
    member = await world.join(acme, "member@example.com")

    view = await world.workspaces.get(acme.id, member)

    assert view.permissions == ["products.create", "products.delete", "products.update"]


async def test_the_permission_catalog_is_the_whole_catalog(world: World) -> None:
    acme = await world.workspace(await world.user("owner@example.com"))

    assert await world.workspaces.permission_catalog(acme.id, acme.owner_id) == CATALOG
