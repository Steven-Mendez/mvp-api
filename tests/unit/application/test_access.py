import pytest

from app.application.access import AccessService, permits, require
from app.domain.errors import (
    ForbiddenError,
    NotFoundError,
    OnboardingIncompleteError,
    UnauthenticatedError,
)
from tests.unit.conftest import World


async def test_an_unknown_account_is_unauthenticated(world: World) -> None:
    with pytest.raises(UnauthenticatedError):
        await require(world.uow, "nobody", "profile.read", "nobody")


async def test_each_action_waits_for_its_onboarding_step(world: World) -> None:
    ana = await world.user("ana@example.com", name="")
    await require(world.uow, ana, "profile.update", ana)  # the step that is pending
    with pytest.raises(OnboardingIncompleteError) as refused:
        await require(world.uow, ana, "workspaces.create", ana)
    assert refused.value.onboarding_status == "profile_pending"


async def test_own_account_actions_only_reach_the_own_account(world: World) -> None:
    ana = await world.user("ana@example.com")
    bea = await world.user("bea@example.com")
    assert await permits(world.uow, ana, "profile.update", ana)
    assert not await permits(world.uow, ana, "profile.update", bea)


async def test_outsiders_may_do_nothing_in_a_workspace(world: World) -> None:
    owner = await world.user("owner@example.com")
    outsider = await world.user("out@example.com")
    await world.workspace(outsider, "elsewhere")  # onboarded, just not a member
    acme = await world.workspace(owner)
    assert not await permits(world.uow, outsider, "workspace.read", acme.id)
    with pytest.raises(ForbiddenError):
        await require(world.uow, outsider, "products.read", acme.id)


@pytest.mark.parametrize(
    ("role", "action", "allowed"),
    [
        ("viewer", "products.read", True),  # every member reads
        ("viewer", "products.create", False),
        ("member", "products.create", True),
        ("member", "members.invite", False),
        ("admin", "roles.manage", True),
        ("admin", "workspace.delete", False),  # owner actions stay the owner's
        ("admin", "workspace.transfer", False),
    ],
)
async def test_a_member_may_do_what_the_role_grants(
    world: World, role: str, action: str, allowed: bool
) -> None:
    owner = await world.user("owner@example.com")
    acme = await world.workspace(owner)
    someone = await world.join(acme, "someone@example.com", role)
    assert await permits(world.uow, someone, action, acme.id) is allowed


async def test_the_owner_may_do_anything(world: World) -> None:
    owner = await world.user("owner@example.com")
    acme = await world.workspace(owner)
    for action in ("workspace.delete", "workspace.transfer", "roles.manage"):
        assert await permits(world.uow, owner, action, acme.id)


async def test_nobody_acts_in_a_deleted_workspace(world: World) -> None:
    owner = await world.user("owner@example.com")
    acme = await world.workspace(owner)
    await world.workspaces.delete(acme.id, owner)
    assert not await permits(world.uow, owner, "workspace.read", acme.id)


class TestWorkspaceBySlug:
    async def test_a_workspace_one_does_not_belong_to_does_not_exist(self, world: World) -> None:
        owner = await world.user("owner@example.com")
        outsider = await world.user("out@example.com")
        await world.workspace(outsider, "elsewhere")
        await world.workspace(owner)
        with pytest.raises(NotFoundError):  # 404, not 403: the slug's existence stays hidden
            await AccessService(world.uow).workspace(outsider, "acme")

    async def test_members_get_it_and_the_action_is_checked(self, world: World) -> None:
        owner = await world.user("owner@example.com")
        acme = await world.workspace(owner)
        viewer = await world.join(acme, "viewer@example.com", "viewer")
        access = AccessService(world.uow)
        assert (await access.workspace(viewer, "acme")).id == acme.id
        assert (await access.workspace(viewer, "acme", "products.read")).id == acme.id
        with pytest.raises(ForbiddenError):
            await access.workspace(viewer, "acme", "products.delete")

    async def test_it_needs_a_finished_onboarding(self, world: World) -> None:
        pending = await world.user("pending@example.com", name="")
        with pytest.raises(OnboardingIncompleteError):
            await AccessService(world.uow).workspace(pending, "acme")
