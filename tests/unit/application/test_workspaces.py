"""WorkspaceService.

- creating one makes the creator its owner, with the four system roles
- slugs are unique and valid; updating needs `workspace.update`
- a new logo replaces the old one's file; a failed change leaves no orphan
- only the owner deletes it: members lose access, pending invitations are revoked,
  people left without a workspace go back a step, and the slug is freed
- custom roles: unique names, never `owner`, no owner-only permissions, kept while in use
- members: reassigned by role managers, never into or out of the owner role
- transfer: the target becomes owner, the previous owner an admin
- invitations: mailed (a failed mail still hands out the link), for the addressee only,
  once, one pending per person; inviting as anything but member takes `roles.manage`
"""

from datetime import timedelta

import pytest

from app.domain.access import ADMIN_PERMISSIONS, CATALOG
from app.domain.errors import (
    ConflictError,
    ForbiddenError,
    GoneError,
    InvalidError,
    NotFoundError,
)
from app.domain.workspace import Workspace
from tests.unit.conftest import WEB, World


async def _acme(world: World) -> tuple[Workspace, str]:
    owner = await world.user("owner@example.com")
    return await world.workspace(owner), owner


async def _status(world: World, user_id: str) -> str:
    user = await world.uow.users.get(user_id)
    assert user is not None
    return user.onboarding_status


class TestCreate:
    async def test_the_creator_owns_it_with_the_system_roles(self, world: World) -> None:
        owner = await world.user("owner@example.com")
        assert await _status(world, owner) == "workspace_pending"
        view = await world.workspaces.create("  Acme  ", "acme", owner)
        assert (view.workspace.name, view.workspace.owner_id) == ("Acme", owner)
        assert view.permissions == list(CATALOG)
        roles = await world.uow.roles.of_workspace(view.workspace.id)
        assert [role.name for role in roles] == ["owner", "admin", "member", "viewer"]
        assert view.role_id == roles[0].id
        assert await world.uow.roles.permissions(roles[1].id) == sorted(ADMIN_PERMISSIONS)
        assert await _status(world, owner) == "completed"

    async def test_a_taken_slug_is_a_conflict(self, world: World) -> None:
        acme, owner = await _acme(world)
        with pytest.raises(ConflictError, match="slug is already taken"):
            await world.workspaces.create("Acme 2", acme.slug, owner)
        assert len(await world.uow.workspaces.of_member(owner)) == 1

    async def test_slug_availability(self, world: World) -> None:
        acme, owner = await _acme(world)
        assert await world.workspaces.slug_available("fresh", owner) is True
        assert await world.workspaces.slug_available(acme.slug, owner) is False
        with pytest.raises(InvalidError):
            await world.workspaces.slug_available("api", owner)

    async def test_list_mine_names_each_role(self, world: World) -> None:
        acme, _ = await _acme(world)
        viewer = await world.join(acme, "viewer@example.com", "viewer")
        views = await world.workspaces.list_mine(viewer)
        assert [(v.workspace.slug, v.permissions) for v in views] == [("acme", [])]


class TestUpdate:
    async def test_needs_the_permission(self, world: World) -> None:
        acme, _ = await _acme(world)
        member = await world.join(acme, "member@example.com")
        with pytest.raises(ForbiddenError):
            await world.workspaces.update(acme.id, "Hacked", "hacked", member)
        assert (await world.workspaces.get(acme.id, member)).workspace.slug == "acme"

    async def test_an_admin_renames_it(self, world: World) -> None:
        acme, _ = await _acme(world)
        admin = await world.join(acme, "admin@example.com", "admin")
        view = await world.workspaces.update(acme.id, " Acme Inc ", "acme-inc", admin)
        assert (view.workspace.name, view.workspace.slug) == ("Acme Inc", "acme-inc")

    async def test_a_taken_slug_is_a_conflict(self, world: World) -> None:
        acme, owner = await _acme(world)
        await world.workspace(owner, "other")
        with pytest.raises(ConflictError):
            await world.workspaces.update(acme.id, "Acme", "other", owner)


class TestLogo:
    async def test_a_new_logo_replaces_the_old_file(self, world: World) -> None:
        acme, owner = await _acme(world)
        await world.workspaces.set_logo(acme.id, world.media.upload(), owner)

        view = await world.workspaces.set_logo(acme.id, world.media.upload(), owner)

        assert view.workspace.logo_url == world.media.url_for(view.workspace.logo_key or "")
        assert world.media.objects == {view.workspace.logo_key}

    async def test_clearing_the_logo_deletes_its_file(self, world: World) -> None:
        acme, owner = await _acme(world)
        await world.workspaces.set_logo(acme.id, world.media.upload(), owner)

        view = await world.workspaces.set_logo(acme.id, None, owner)

        assert (view.workspace.logo_key, view.workspace.logo_url) == (None, None)
        assert world.media.objects == set()

    async def test_a_failed_change_leaves_no_orphan(self, world: World) -> None:
        acme, owner = await _acme(world)
        world.uow.fail_next_commit = RuntimeError("database went away")

        with pytest.raises(RuntimeError):
            await world.workspaces.set_logo(acme.id, world.media.upload(), owner)

        assert world.media.objects == set()

    async def test_members_cannot_change_it(self, world: World) -> None:
        acme, _ = await _acme(world)
        member = await world.join(acme, "member@example.com")
        with pytest.raises(ForbiddenError):
            await world.workspaces.request_logo_upload(acme.id, member, "image/png")
        with pytest.raises(ForbiddenError):
            await world.workspaces.set_logo(acme.id, world.media.upload(), member)
        assert world.media.objects == set()


class TestDelete:
    async def test_only_the_owner_deletes_it(self, world: World) -> None:
        acme, _ = await _acme(world)
        admin = await world.join(acme, "admin@example.com", "admin")
        with pytest.raises(ForbiddenError):
            await world.workspaces.delete(acme.id, admin)

    async def test_members_lose_access_and_the_slug_is_freed(self, world: World) -> None:
        acme, owner = await _acme(world)
        member = await world.join(acme, "member@example.com")
        busy = await world.join(acme, "busy@example.com")
        await world.workspaces.create("Busy", "busy", busy)
        pending = await world.workspaces.invite(
            acme.id, "late@example.com", await world.role_id(acme, "member"), owner
        )

        await world.workspaces.delete(acme.id, owner)

        assert await world.uow.workspaces.get(acme.id) is None
        assert await world.uow.members.user_ids(acme.id) == []
        assert pending.view.invitation.current_status == "revoked"
        assert await _status(world, member) == "workspace_pending"
        assert await _status(world, owner) == "workspace_pending"
        assert await _status(world, busy) == "completed"  # still in another workspace
        assert await world.workspaces.slug_available("acme", owner)


class TestRoles:
    async def test_custom_roles_are_created_renamed_and_deleted(self, world: World) -> None:
        acme, owner = await _acme(world)
        role = (await world.workspaces.save_role(acme.id, None, " editor ", owner)).role
        assert (role.name, role.is_system) == ("editor", False)
        await world.workspaces.set_permissions(acme.id, role.id, ["products.update"], owner)
        renamed = await world.workspaces.save_role(acme.id, role.id, "writer", owner)
        assert (renamed.role.name, renamed.permissions) == ("writer", ["products.update"])
        await world.workspaces.delete_role(acme.id, role.id, owner)
        assert await world.uow.roles.get(role.id) is None

    async def test_role_names_are_unique_and_owner_is_reserved(self, world: World) -> None:
        acme, owner = await _acme(world)
        with pytest.raises(ConflictError):
            await world.workspaces.save_role(acme.id, None, "admin", owner)
        with pytest.raises(ForbiddenError):
            await world.workspaces.save_role(acme.id, None, "owner", owner)

    async def test_the_owner_role_is_not_configurable(self, world: World) -> None:
        acme, owner = await _acme(world)
        owner_role = await world.role_id(acme, "owner")
        with pytest.raises(ForbiddenError):
            await world.workspaces.set_permissions(acme.id, owner_role, [], owner)
        with pytest.raises(ForbiddenError):
            await world.workspaces.save_role(acme.id, owner_role, "boss", owner)
        with pytest.raises(ForbiddenError):
            await world.workspaces.delete_role(acme.id, owner_role, owner)

    async def test_a_role_in_use_is_kept(self, world: World) -> None:
        acme, owner = await _acme(world)
        await world.join(acme, "viewer@example.com", "viewer")
        viewer_role = await world.role_id(acme, "viewer")
        with pytest.raises(ConflictError):
            await world.workspaces.delete_role(acme.id, viewer_role, owner)

    async def test_a_pending_invitation_keeps_its_role(self, world: World) -> None:
        acme, owner = await _acme(world)
        editor = (await world.workspaces.save_role(acme.id, None, "editor", owner)).role
        invited = await world.workspaces.invite(acme.id, "e@example.com", editor.id, owner)
        with pytest.raises(ConflictError):
            await world.workspaces.delete_role(acme.id, editor.id, owner)
        invited.view.invitation.expires_at -= timedelta(days=8)  # expired: no longer holds it

        await world.workspaces.delete_role(acme.id, editor.id, owner)

        assert await world.uow.invitations.get(invited.view.invitation.id) is None

    async def test_permissions_are_validated(self, world: World) -> None:
        acme, owner = await _acme(world)
        member_role = await world.role_id(acme, "member")
        with pytest.raises(ForbiddenError):
            await world.workspaces.set_permissions(
                acme.id, member_role, ["workspace.delete"], owner
            )
        with pytest.raises(InvalidError):
            await world.workspaces.set_permissions(acme.id, member_role, ["root"], owner)

    async def test_roles_of_another_workspace_are_not_found(self, world: World) -> None:
        acme, owner = await _acme(world)
        other = await world.workspace(owner, "other")
        foreign = await world.role_id(other, "member")
        with pytest.raises(NotFoundError):
            await world.workspaces.set_permissions(acme.id, foreign, [], owner)

    async def test_managing_roles_takes_the_permission(self, world: World) -> None:
        acme, _ = await _acme(world)
        member = await world.join(acme, "member@example.com")
        with pytest.raises(ForbiddenError):
            await world.workspaces.save_role(acme.id, None, "editor", member)

    async def test_the_role_list_counts_members(self, world: World) -> None:
        acme, owner = await _acme(world)
        await world.join(acme, "a@example.com")
        await world.join(acme, "b@example.com")
        counts = {v.role.name: v.member_count for v in await world.workspaces.roles(acme.id, owner)}
        assert counts == {"owner": 1, "admin": 0, "member": 2, "viewer": 0}


class TestMembers:
    async def test_a_role_manager_reassigns_members(self, world: World) -> None:
        acme, owner = await _acme(world)
        member = await world.join(acme, "member@example.com")
        viewer_role = await world.role_id(acme, "viewer")
        await world.workspaces.set_member_role(acme.id, member, viewer_role, owner)
        assert (await world.uow.members.get(acme.id, member)).role_id == viewer_role  # type: ignore[union-attr]

    async def test_nobody_becomes_or_stops_being_owner_by_reassignment(self, world: World) -> None:
        acme, owner = await _acme(world)
        admin = await world.join(acme, "admin@example.com", "admin")
        owner_role = await world.role_id(acme, "owner")
        with pytest.raises(ForbiddenError):
            await world.workspaces.set_member_role(acme.id, admin, owner_role, owner)
        with pytest.raises(ForbiddenError):
            member_role = await world.role_id(acme, "member")
            await world.workspaces.set_member_role(acme.id, owner, member_role, admin)

    async def test_removing_a_member_sends_them_back_to_onboarding(self, world: World) -> None:
        acme, owner = await _acme(world)
        member = await world.join(acme, "member@example.com")
        await world.workspaces.remove_member(acme.id, member, owner)
        assert await world.uow.members.get(acme.id, member) is None
        assert await _status(world, member) == "workspace_pending"

    async def test_the_owner_cannot_be_removed(self, world: World) -> None:
        acme, owner = await _acme(world)
        with pytest.raises(ForbiddenError):
            await world.workspaces.remove_member(acme.id, owner, owner)

    async def test_removing_needs_the_permission(self, world: World) -> None:
        acme, _ = await _acme(world)
        a = await world.join(acme, "a@example.com")
        b = await world.join(acme, "b@example.com")
        with pytest.raises(ForbiddenError):
            await world.workspaces.remove_member(acme.id, b, a)
        with pytest.raises(NotFoundError):
            await world.workspaces.remove_member(acme.id, "ghost", acme.owner_id)

    async def test_members_are_listed_with_their_role(self, world: World) -> None:
        acme, owner = await _acme(world)
        await world.join(acme, "viewer@example.com", "viewer")
        listed = await world.workspaces.members(acme.id, owner)

        assert sorted((v.user.email, v.member.role_id, v.role_name) for v in listed) == sorted(
            [
                ("owner@example.com", await world.role_id(acme, "owner"), "owner"),
                ("viewer@example.com", await world.role_id(acme, "viewer"), "viewer"),
            ]
        )


class TestTransfer:
    async def test_the_target_owns_it_and_the_owner_becomes_admin(self, world: World) -> None:
        acme, owner = await _acme(world)
        member = await world.join(acme, "member@example.com")
        await world.workspaces.transfer(acme.id, member, owner)
        view = await world.workspaces.get(acme.id, member)
        assert view.workspace.owner_id == member
        assert view.role_id == await world.role_id(acme, "owner")
        assert (await world.workspaces.get(acme.id, owner)).role_id == await world.role_id(
            acme, "admin"
        )
        assert await world.uow.members.owner_ids(acme.id) == [member]

    async def test_a_deleted_admin_role_is_recreated(self, world: World) -> None:
        acme, owner = await _acme(world)
        member = await world.join(acme, "member@example.com")
        await world.workspaces.delete_role(acme.id, await world.role_id(acme, "admin"), owner)
        await world.workspaces.transfer(acme.id, member, owner)
        admin = await world.uow.roles.by_name(acme.id, "admin")
        assert admin is not None
        assert await world.uow.roles.permissions(admin.id) == sorted(ADMIN_PERMISSIONS)

    async def test_only_the_owner_transfers_to_another_member(self, world: World) -> None:
        acme, owner = await _acme(world)
        admin = await world.join(acme, "admin@example.com", "admin")
        with pytest.raises(ForbiddenError):
            await world.workspaces.transfer(acme.id, admin, admin)
        with pytest.raises(InvalidError):
            await world.workspaces.transfer(acme.id, owner, owner)
        with pytest.raises(NotFoundError):
            await world.workspaces.transfer(acme.id, "stranger", owner)
        assert (await world.workspaces.get(acme.id, owner)).workspace.owner_id == owner


class TestInvitations:
    async def test_an_invitation_is_mailed_with_its_link(self, world: World) -> None:
        acme, owner = await _acme(world)

        created = await world.workspaces.invite(
            acme.id, "New@Example.com", await world.role_id(acme, "member"), owner
        )

        assert created.view.invitation.email == "new@example.com"
        assert created.url.startswith(f"{WEB}/invite?workspace=acme&token=")
        assert world.mailer.sent == [("new@example.com", "Acme", created.url)]

    async def test_the_link_previews_the_workspace_and_role(self, world: World) -> None:
        acme, owner = await _acme(world)
        await world.workspaces.invite(
            acme.id, "new@example.com", await world.role_id(acme, "viewer"), owner
        )

        preview = await world.workspaces.preview_invitation(
            world.mailer.token_sent_to("new@example.com")
        )

        assert (preview.workspace.id, preview.role_name) == (acme.id, "viewer")
        assert preview.invitation.email == "new@example.com"

    async def test_accepting_joins_with_the_role_and_finishes_onboarding(
        self, world: World
    ) -> None:
        acme, owner = await _acme(world)
        viewer_role = await world.role_id(acme, "viewer")
        await world.workspaces.invite(acme.id, "new@example.com", viewer_role, owner)
        newcomer = await world.user("new@example.com")

        view = await world.workspaces.accept_invitation(
            world.mailer.token_sent_to("new@example.com"), newcomer
        )

        assert (view.workspace.id, view.role_id) == (acme.id, viewer_role)
        assert await _status(world, newcomer) == "completed"

    async def test_an_invitation_is_used_once(self, world: World) -> None:
        acme, _ = await _acme(world)
        await world.join(acme, "new@example.com")
        newcomer = await world.user("new@example.com")

        with pytest.raises(GoneError):
            await world.workspaces.accept_invitation(
                world.mailer.token_sent_to("new@example.com"), newcomer
            )

    async def test_a_failed_mail_still_hands_out_the_link(self, world: World) -> None:
        acme, owner = await _acme(world)
        world.mailer.fail = True
        created = await world.workspaces.invite(
            acme.id, "new@example.com", await world.role_id(acme, "member"), owner
        )
        assert await world.uow.invitations.get(created.view.invitation.id) is not None
        assert "token=" in created.url

    async def test_only_the_addressee_accepts(self, world: World) -> None:
        acme, owner = await _acme(world)
        await world.workspaces.invite(
            acme.id, "new@example.com", await world.role_id(acme, "member"), owner
        )
        intruder = await world.user("intruder@example.com")
        with pytest.raises(ForbiddenError):
            await world.workspaces.accept_invitation(
                world.mailer.token_sent_to("new@example.com"), intruder
            )
        assert await world.uow.members.get(acme.id, intruder) is None

    async def test_one_pending_invitation_per_person(self, world: World) -> None:
        acme, owner = await _acme(world)
        member_role = await world.role_id(acme, "member")
        await world.workspaces.invite(acme.id, "new@example.com", member_role, owner)
        with pytest.raises(ConflictError):
            await world.workspaces.invite(acme.id, "NEW@example.com", member_role, owner)
        with pytest.raises(ConflictError):
            await world.workspaces.invite(acme.id, "owner@example.com", member_role, owner)

    async def test_inviting_with_another_role_takes_managing_roles(self, world: World) -> None:
        acme, owner = await _acme(world)
        inviter = (await world.workspaces.save_role(acme.id, None, "inviter", owner)).role
        await world.workspaces.set_permissions(acme.id, inviter.id, ["members.invite"], owner)
        recruiter = await world.join(acme, "recruiter@example.com", "inviter")
        admin_role = await world.role_id(acme, "admin")
        with pytest.raises(ForbiddenError):
            await world.workspaces.invite(acme.id, "x@example.com", admin_role, recruiter)
        member_role = await world.role_id(acme, "member")
        await world.workspaces.invite(acme.id, "x@example.com", member_role, recruiter)

    async def test_expired_and_revoked_invitations_are_gone(self, world: World) -> None:
        acme, owner = await _acme(world)
        member_role = await world.role_id(acme, "member")
        expired = await world.workspaces.invite(acme.id, "a@example.com", member_role, owner)
        expired.view.invitation.expires_at = expired.view.invitation.created_at
        revoked = await world.workspaces.invite(acme.id, "b@example.com", member_role, owner)
        await world.workspaces.revoke_invitation(acme.id, revoked.view.invitation.id, owner)
        for email in ("a@example.com", "b@example.com"):
            with pytest.raises(GoneError):
                await world.workspaces.preview_invitation(world.mailer.token_sent_to(email))
        with pytest.raises(ConflictError):
            await world.workspaces.revoke_invitation(acme.id, revoked.view.invitation.id, owner)
        with pytest.raises(GoneError):
            await world.workspaces.preview_invitation("made-up")

    async def test_revoking_is_scoped_to_the_workspace(self, world: World) -> None:
        acme, owner = await _acme(world)
        other = await world.workspace(owner, "other")
        created = await world.workspaces.invite(
            acme.id, "a@example.com", await world.role_id(acme, "member"), owner
        )
        with pytest.raises(NotFoundError):
            await world.workspaces.revoke_invitation(other.id, created.view.invitation.id, owner)
        assert created.view.invitation.status == "pending"

    async def test_members_do_not_join_twice(self, world: World) -> None:
        acme, owner = await _acme(world)
        member = await world.join(acme, "member@example.com")
        viewer_role = await world.role_id(acme, "viewer")
        # Invited again under a different address, then accepted by the same account.
        user = await world.uow.users.get(member)
        assert user is not None
        await world.workspaces.invite(acme.id, "alias@example.com", viewer_role, owner)
        user.email = "alias@example.com"
        with pytest.raises(ConflictError):
            await world.workspaces.accept_invitation(
                world.mailer.token_sent_to("alias@example.com"), member
            )

    async def test_listing_shows_who_accepted(self, world: World) -> None:
        acme, owner = await _acme(world)
        member = await world.join(acme, "member@example.com")
        views = await world.workspaces.invitations(acme.id, owner)
        assert [(v.invitation.status, v.accepted_by and v.accepted_by.id) for v in views] == [
            ("accepted", member)
        ]
