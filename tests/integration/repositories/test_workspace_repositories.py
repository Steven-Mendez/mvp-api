"""Users, workspaces, roles, members and invitations as stored.

- an account row is inserted once: a second one with the same id or email is ignored
- deleted workspaces are invisible to reads and locks, but keep their slug reserved
  until the purge; the purge removes that workspace's rows and nobody else's
- roles list owner first, then by name; a role is in use while a member or a pending,
  unexpired invitation holds it
- the owner is whoever holds the protected role; emails match whatever their case
- bulk member and invitation changes stay inside their workspace
"""

from datetime import timedelta

from app.application.ports import UnitOfWork
from app.domain.workspace import Invitation, Role
from tests import factories
from tests.integration.repositories.conftest import Seed


class TestUsers:
    async def test_a_second_row_for_the_same_account_is_ignored(
        self, any_uow: UnitOfWork, seed: Seed
    ) -> None:
        ana = await seed.user("ana@example.com", name="Ana")

        await any_uow.users.add_unless_taken(factories.user(user_id=ana.id, email="x@example.com"))
        await any_uow.users.add_unless_taken(factories.user(user_id="sub-2", email=ana.email))

        assert await any_uow.users.get("sub-2") is None
        stored = await any_uow.users.get(ana.id)
        assert stored is not None
        assert (stored.email, stored.display_name) == ("ana@example.com", "Ana")


class TestWorkspaces:
    async def test_deleted_workspaces_are_invisible(self, any_uow: UnitOfWork, seed: Seed) -> None:
        owner = await seed.user("owner@example.com")
        acme = await seed.tenant("acme", owner)
        slug = acme.workspace.slug

        acme.workspace.soft_delete()
        await any_uow.flush()

        assert await any_uow.workspaces.get(acme.workspace.id) is None
        assert await any_uow.workspaces.lock(acme.workspace.id) is None
        assert await any_uow.workspaces.by_slug(slug) is None
        assert await any_uow.workspaces.of_member(owner.id) == []
        assert not await any_uow.workspaces.any_owned_by(owner.id)
        assert await any_uow.workspaces.get_deleted(acme.workspace.id) is acme.workspace

    async def test_a_live_workspace_is_not_deleted(self, any_uow: UnitOfWork, seed: Seed) -> None:
        acme = await seed.tenant("acme", await seed.user("owner@example.com"))

        assert await any_uow.workspaces.get_deleted(acme.workspace.id) is None
        assert await any_uow.workspaces.deleted_ids() == []

    async def test_slugs_stay_reserved_until_the_purge(
        self, any_uow: UnitOfWork, seed: Seed
    ) -> None:
        acme = await seed.tenant("acme", await seed.user("owner@example.com"))
        acme.workspace.soft_delete()
        await any_uow.flush()

        assert await any_uow.workspaces.slug_exists(acme.workspace.slug)
        assert not await any_uow.workspaces.slug_exists("acme")

    async def test_member_workspaces_are_listed_by_name(
        self, any_uow: UnitOfWork, seed: Seed
    ) -> None:
        owner = await seed.user("owner@example.com")
        await seed.tenant("zeta", owner)
        await seed.tenant("alpha", owner)
        await seed.tenant("theirs", await seed.user("other@example.com"))

        listed = await any_uow.workspaces.of_member(owner.id)

        assert [w.slug for w in listed] == ["alpha", "zeta"]

    async def test_deleted_ids_can_be_limited_to_an_owner(
        self, any_uow: UnitOfWork, seed: Seed
    ) -> None:
        ana, bea = await seed.user("ana@example.com"), await seed.user("bea@example.com")
        mine, theirs = await seed.tenant("mine", ana), await seed.tenant("theirs", bea)
        mine.workspace.soft_delete()
        theirs.workspace.soft_delete()
        await any_uow.flush()

        assert await any_uow.workspaces.deleted_ids(owned_by=ana.id) == [mine.workspace.id]
        assert set(await any_uow.workspaces.deleted_ids()) == {
            mine.workspace.id,
            theirs.workspace.id,
        }

    async def test_the_purge_removes_only_that_workspace(
        self, any_uow: UnitOfWork, seed: Seed
    ) -> None:
        owner = await seed.user("owner@example.com")
        doomed, kept = await seed.tenant("doomed", owner), await seed.tenant("kept", owner)
        for tenant in (doomed, kept):
            invitation, _ = Invitation.issue(
                workspace_id=tenant.workspace.id,
                email="x@example.com",
                role_id=tenant.roles["member"].id,
                invited_by=owner.id,
            )
            any_uow.invitations.add(invitation)
        doomed.workspace.soft_delete()
        await any_uow.flush()

        await any_uow.workspaces.purge(doomed.workspace)
        await any_uow.flush()

        assert await any_uow.workspaces.get_deleted(doomed.workspace.id) is None
        assert await any_uow.roles.of_workspace(doomed.workspace.id) == []
        assert await any_uow.roles.permissions(doomed.roles["admin"].id) == []
        assert await any_uow.members.user_ids(doomed.workspace.id) == []
        assert await any_uow.invitations.of_workspace(doomed.workspace.id) == []
        assert len(await any_uow.roles.of_workspace(kept.workspace.id)) == 4
        assert await any_uow.members.user_ids(kept.workspace.id) == [owner.id]
        assert len(await any_uow.invitations.of_workspace(kept.workspace.id)) == 1


class TestRoles:
    async def test_owner_first_then_by_name(self, any_uow: UnitOfWork, seed: Seed) -> None:
        acme = await seed.tenant("acme", await seed.user("owner@example.com"))
        await any_uow.roles.add(Role.custom(acme.workspace.id, "editor"), [])

        roles = await any_uow.roles.of_workspace(acme.workspace.id)

        assert [r.name for r in roles] == ["owner", "admin", "editor", "member", "viewer"]

    async def test_permissions_are_replaced_and_sorted(
        self, any_uow: UnitOfWork, seed: Seed
    ) -> None:
        acme = await seed.tenant("acme", await seed.user("owner@example.com"))
        member = acme.roles["member"]

        await any_uow.roles.replace_permissions(member.id, {"products.update", "members.invite"})

        assert await any_uow.roles.permissions(member.id) == ["members.invite", "products.update"]
        assert await any_uow.roles.permissions(acme.roles["viewer"].id) == []

    async def test_found_by_name_within_the_workspace(
        self, any_uow: UnitOfWork, seed: Seed
    ) -> None:
        owner = await seed.user("owner@example.com")
        acme, other = await seed.tenant("acme", owner), await seed.tenant("other", owner)

        found = await any_uow.roles.by_name(acme.workspace.id, "admin")

        assert found is acme.roles["admin"]
        assert found is not other.roles["admin"]
        assert await any_uow.roles.by_name(acme.workspace.id, "nope") is None

    async def test_in_use_while_a_member_holds_it(self, any_uow: UnitOfWork, seed: Seed) -> None:
        acme = await seed.tenant("acme", await seed.user("owner@example.com"))
        await seed.member(acme, await seed.user("a@example.com"), "viewer")

        assert await any_uow.roles.is_in_use(acme.roles["viewer"].id)
        assert await any_uow.roles.member_count(acme.roles["viewer"].id) == 1
        assert not await any_uow.roles.is_in_use(acme.roles["admin"].id)
        assert await any_uow.roles.member_count(acme.roles["admin"].id) == 0

    async def test_in_use_while_a_live_invitation_holds_it(
        self, any_uow: UnitOfWork, seed: Seed
    ) -> None:
        owner = await seed.user("owner@example.com")
        acme = await seed.tenant("acme", owner)
        pending = factories.invitation(
            workspace_id=acme.workspace.id, role_id=acme.roles["admin"].id, invited_by=owner.id
        )
        expired = factories.invitation(
            workspace_id=acme.workspace.id,
            role_id=acme.roles["viewer"].id,
            invited_by=owner.id,
            expires_in=timedelta(0),
        )
        any_uow.invitations.add(pending)
        any_uow.invitations.add(expired)
        await any_uow.flush()

        assert await any_uow.roles.is_in_use(acme.roles["admin"].id)
        assert not await any_uow.roles.is_in_use(acme.roles["viewer"].id)

    async def test_a_deleted_role_takes_its_permissions(
        self, any_uow: UnitOfWork, seed: Seed
    ) -> None:
        acme = await seed.tenant("acme", await seed.user("owner@example.com"))
        admin = acme.roles["admin"]

        await any_uow.roles.delete(admin)
        await any_uow.flush()

        assert await any_uow.roles.get(admin.id) is None
        assert await any_uow.roles.permissions(admin.id) == []


class TestMembers:
    async def test_the_owner_is_whoever_holds_the_protected_role(
        self, any_uow: UnitOfWork, seed: Seed
    ) -> None:
        owner = await seed.user("owner@example.com")
        acme = await seed.tenant("acme", owner)
        await seed.member(acme, await seed.user("admin@example.com"), "admin")

        assert await any_uow.members.owner_ids(acme.workspace.id) == [owner.id]

    async def test_listed_by_display_name_with_account_and_role(
        self, any_uow: UnitOfWork, seed: Seed
    ) -> None:
        acme = await seed.tenant("acme", await seed.user("owner@example.com", name="Zoe"))
        await seed.member(acme, await seed.user("ana@example.com", name="Ana"), "viewer")
        await seed.tenant("other", await seed.user("bob@example.com", name="Bob"))

        rows = await any_uow.members.of_workspace(acme.workspace.id)

        assert [(user.display_name, role.name) for _, user, role in rows] == [
            ("Ana", "viewer"),
            ("Zoe", "owner"),
        ]

    async def test_emails_match_whatever_their_case(self, any_uow: UnitOfWork, seed: Seed) -> None:
        owner = await seed.user("owner@example.com")
        acme, other = await seed.tenant("acme", owner), await seed.tenant("other", owner)
        await seed.member(acme, await seed.user("ana@example.com"))

        assert await any_uow.members.has_email(acme.workspace.id, "ANA@example.com")
        assert not await any_uow.members.has_email(other.workspace.id, "ana@example.com")

    async def test_belonging_anywhere(self, any_uow: UnitOfWork, seed: Seed) -> None:
        owner = await seed.user("owner@example.com")
        loner = await seed.user("loner@example.com")
        await seed.tenant("acme", owner)

        assert await any_uow.members.belongs_to_any(owner.id)
        assert not await any_uow.members.belongs_to_any(loner.id)

    async def test_bulk_removals_stay_in_scope(self, any_uow: UnitOfWork, seed: Seed) -> None:
        owner = await seed.user("owner@example.com")
        ana = await seed.user("ana@example.com")
        acme, other = await seed.tenant("acme", owner), await seed.tenant("other", owner)
        third = await seed.tenant("third", await seed.user("x@example.com"))
        await seed.member(other, ana)
        await seed.member(third, ana)

        await any_uow.members.remove_all(acme.workspace.id)
        await any_uow.members.remove_user(ana.id)

        assert await any_uow.members.user_ids(acme.workspace.id) == []
        assert await any_uow.members.user_ids(other.workspace.id) == [owner.id]
        assert await any_uow.members.get(third.workspace.id, ana.id) is None

    async def test_bulk_removals_reach_the_loaded_members(
        self, any_uow: UnitOfWork, seed: Seed
    ) -> None:
        owner = await seed.user("owner@example.com")
        acme = await seed.tenant("acme", owner)
        loaded = await any_uow.members.get(acme.workspace.id, owner.id)
        assert loaded is not None

        await any_uow.members.remove_all(acme.workspace.id)

        assert await any_uow.members.get(acme.workspace.id, owner.id) is None


class TestInvitations:
    async def test_found_by_token_only(self, any_uow: UnitOfWork, seed: Seed) -> None:
        owner = await seed.user("owner@example.com")
        acme = await seed.tenant("acme", owner)
        invitation, token = Invitation.issue(
            workspace_id=acme.workspace.id,
            email="a@example.com",
            role_id=acme.roles["member"].id,
            invited_by=owner.id,
        )
        any_uow.invitations.add(invitation)

        assert await any_uow.invitations.by_token(token) is invitation
        assert await any_uow.invitations.by_token(invitation.token_hash) is None

    async def test_only_a_live_pending_invitation_is_pending(
        self, any_uow: UnitOfWork, seed: Seed
    ) -> None:
        owner = await seed.user("owner@example.com")
        acme = await seed.tenant("acme", owner)
        w, r = acme.workspace.id, acme.roles["member"].id
        live = factories.invitation(workspace_id=w, role_id=r, email="live@example.com")
        old = factories.invitation(
            workspace_id=w, role_id=r, email="old@example.com", expires_in=timedelta(0)
        )
        used = factories.invitation(workspace_id=w, role_id=r, email="used@example.com")
        used.accept(owner.id, "used@example.com")
        for invitation in (live, old, used):
            any_uow.invitations.add(invitation)
        await any_uow.flush()

        assert await any_uow.invitations.has_pending(w, "LIVE@example.com")
        assert not await any_uow.invitations.has_pending(w, "old@example.com")
        assert not await any_uow.invitations.has_pending(w, "used@example.com")

    async def test_revoking_pending_ones_stays_in_the_workspace(
        self, any_uow: UnitOfWork, seed: Seed
    ) -> None:
        owner = await seed.user("owner@example.com")
        acme, other = await seed.tenant("acme", owner), await seed.tenant("other", owner)
        mine = factories.invitation(workspace_id=acme.workspace.id)
        theirs = factories.invitation(workspace_id=other.workspace.id)
        any_uow.invitations.add(mine)
        any_uow.invitations.add(theirs)
        await any_uow.flush()

        await any_uow.invitations.revoke_pending(acme.workspace.id)

        # The objects already loaded see the change, not only the rows.
        assert (mine.current_status, theirs.current_status) == ("revoked", "pending")
        assert not await any_uow.invitations.has_pending(acme.workspace.id, mine.email)
        assert await any_uow.invitations.has_pending(other.workspace.id, theirs.email)

    async def test_newest_first_with_who_accepted(self, any_uow: UnitOfWork, seed: Seed) -> None:
        owner = await seed.user("owner@example.com")
        ana = await seed.user("ana@example.com")
        acme = await seed.tenant("acme", owner)
        first = factories.invitation(workspace_id=acme.workspace.id, email=ana.email)
        first.accept(ana.id, ana.email)
        second = factories.invitation(workspace_id=acme.workspace.id, email="b@example.com")
        second.created_at = first.created_at + timedelta(seconds=1)
        any_uow.invitations.add(first)
        any_uow.invitations.add(second)
        await any_uow.flush()

        rows = await any_uow.invitations.of_workspace(acme.workspace.id)

        assert [(i.id, u.id if u else None) for i, u in rows] == [
            (second.id, None),
            (first.id, ana.id),
        ]

    async def test_deleted_by_role_or_by_sender(self, any_uow: UnitOfWork, seed: Seed) -> None:
        owner = await seed.user("owner@example.com")
        acme = await seed.tenant("acme", owner)
        by_role = factories.invitation(workspace_id=acme.workspace.id, role_id="gone-role")
        by_sender = factories.invitation(workspace_id=acme.workspace.id, invited_by="leaver")
        kept = factories.invitation(workspace_id=acme.workspace.id)
        for invitation in (by_role, by_sender, kept):
            any_uow.invitations.add(invitation)
        await any_uow.flush()

        await any_uow.invitations.delete_for_role("gone-role")
        await any_uow.invitations.delete_sent_by("leaver")

        rows = await any_uow.invitations.of_workspace(acme.workspace.id)
        assert [i.id for i, _ in rows] == [kept.id]
        assert await any_uow.invitations.get(by_role.id) is None
        assert await any_uow.invitations.get(by_sender.id) is None
