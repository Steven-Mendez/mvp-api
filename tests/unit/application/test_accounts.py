"""AccountService.

- registering creates the pool account and its profile; a taken email answers the same
  and changes nothing; the pool's password policy is a validation error
- an invitation token at registration joins right away; a bad one is ignored
- the first request of a subject creates its row, asking the pool once; a subject
  presenting an email that another account holds is never linked to it
- naming the profile moves onboarding on
- a new avatar replaces the old file; a failed change leaves no orphan
- deleting works at every onboarding step; owners hand over first; everything the
  account holds goes, including workspaces it deleted earlier; a pool failure does not
  undo it
"""

import pytest

from app.application.ports import AccountLookupError
from app.domain.errors import ConflictError, InvalidError
from tests.unit.conftest import World


class TestRegister:
    async def test_creates_the_account_and_its_profile(self, world: World) -> None:
        await world.accounts.register("Ana@Example.com", "long enough", " Ana ", None)
        sub = world.identity.accounts["ana@example.com"]
        user = await world.uow.users.get(sub)
        assert user is not None
        assert (user.email, user.display_name) == ("ana@example.com", "Ana")
        assert user.onboarding_status == "workspace_pending"  # no profile step left

    async def test_a_taken_email_answers_the_same_and_changes_nothing(self, world: World) -> None:
        ana = await world.user("ana@example.com", name="Ana")
        await world.accounts.register("ana@example.com", "long enough", "Impostor", None)
        user = await world.uow.users.get(ana)
        assert user is not None
        assert user.display_name == "Ana"

    async def test_the_pool_password_policy_is_a_validation_error(self, world: World) -> None:
        with pytest.raises(InvalidError, match="too short"):
            await world.accounts.register("ana@example.com", "short", "Ana", None)
        assert world.uow.tables.users == {}

    async def test_an_invitation_token_joins_right_away(self, world: World) -> None:
        owner = await world.user("owner@example.com")
        acme = await world.workspace(owner)
        await world.workspaces.invite(
            acme.id, "new@example.com", await world.role_id(acme, "member"), owner
        )
        token = world.mailer.token_sent_to("new@example.com")
        await world.accounts.register("new@example.com", "long enough", "New", token)
        sub = world.identity.accounts["new@example.com"]
        user = await world.uow.users.get(sub)
        assert await world.uow.members.get(acme.id, sub) is not None
        assert user is not None
        assert user.onboarding_status == "completed"

    async def test_a_bad_invitation_token_is_ignored(self, world: World) -> None:
        await world.accounts.register("new@example.com", "long enough", "New", "stale-token")
        assert world.identity.accounts["new@example.com"] in world.uow.tables.users


class TestEnsureAccount:
    async def test_the_first_request_creates_the_row_once(self, world: World) -> None:
        token = world.identity.sign_in("Ana@Example.com")
        sub = world.identity.tokens[token][0]
        assert await world.accounts.ensure_account(sub, token) == "ana@example.com"
        world.identity.tokens.clear()  # the pool is not asked again
        assert await world.accounts.ensure_account(sub, token) == "ana@example.com"

    async def test_the_pool_must_answer_for_the_same_subject(self, world: World) -> None:
        token = world.identity.sign_in("ana@example.com")
        with pytest.raises(AccountLookupError):
            await world.accounts.ensure_account("someone-else", token)
        assert world.uow.tables.users == {}

    async def test_an_email_held_by_another_account_is_not_linked(self, world: World) -> None:
        await world.user("ana@example.com")
        world.identity.accounts.clear()  # the pool was recreated: new subjects
        token = world.identity.sign_in("ana@example.com")
        new_sub = world.identity.tokens[token][0]
        with pytest.raises(ConflictError):
            await world.accounts.ensure_account(new_sub, token)
        assert await world.uow.users.get(new_sub) is None


class TestProfile:
    async def test_naming_the_profile_moves_onboarding_on(self, world: World) -> None:
        ana = await world.user("ana@example.com", name="")
        assert (await world.accounts.profile(ana)).user.onboarding_status == "profile_pending"
        view = await world.accounts.rename(ana, "Ana")
        assert (view.user.onboarding_status, view.joined_workspace) == ("workspace_pending", False)

    async def test_an_invited_account_is_done_once_named(self, world: World) -> None:
        owner = await world.user("owner@example.com")
        acme = await world.workspace(owner)
        await world.workspaces.invite(
            acme.id, "new@example.com", await world.role_id(acme, "member"), owner
        )
        await world.accounts.register(
            "new@example.com", "long enough", "", world.mailer.token_sent_to("new@example.com")
        )
        sub = world.identity.accounts["new@example.com"]
        view = await world.accounts.rename(sub, "New")
        assert (view.user.onboarding_status, view.joined_workspace) == ("completed", True)

    async def test_an_avatar_upload_gets_a_ticket(self, world: World) -> None:
        ana = await world.user("ana@example.com")

        ticket = await world.accounts.request_avatar_upload(ana, "image/png")

        assert ticket.max_bytes == world.media.max_bytes

    async def test_a_new_avatar_replaces_the_old_file(self, world: World) -> None:
        ana = await world.user("ana@example.com")
        await world.accounts.set_avatar(ana, world.media.upload())

        view = await world.accounts.set_avatar(ana, world.media.upload())

        assert view.user.avatar_url == world.media.url_for(view.user.avatar_key or "")
        assert world.media.objects == {view.user.avatar_key}

    async def test_removing_the_avatar_deletes_its_file(self, world: World) -> None:
        ana = await world.user("ana@example.com")
        await world.accounts.set_avatar(ana, world.media.upload())

        view = await world.accounts.set_avatar(ana, None)

        assert (view.user.avatar_key, view.user.avatar_url) == (None, None)
        assert world.media.objects == set()

    async def test_a_failed_avatar_change_keeps_the_old_one(self, world: World) -> None:
        ana = await world.user("ana@example.com")
        old = (await world.accounts.set_avatar(ana, world.media.upload())).user.avatar_key
        world.uow.fail_next_commit = RuntimeError("database went away")

        with pytest.raises(RuntimeError):
            await world.accounts.set_avatar(ana, world.media.upload())

        assert (await world.accounts.profile(ana)).user.avatar_key == old
        assert world.media.objects == {old}

    async def test_a_failed_avatar_change_leaves_no_orphan(self, world: World) -> None:
        ana = await world.user("ana@example.com")
        world.uow.fail_next_commit = RuntimeError("database went away")

        with pytest.raises(RuntimeError):
            await world.accounts.set_avatar(ana, world.media.upload())

        assert world.media.objects == set()


class TestDelete:
    async def test_an_owner_must_hand_over_first(self, world: World) -> None:
        owner = await world.user("owner@example.com")
        await world.workspace(owner)
        with pytest.raises(ConflictError, match="Transfer ownership"):
            await world.accounts.delete(owner)
        assert await world.uow.users.get(owner) is not None

    async def test_an_account_that_never_finished_onboarding_can_leave(self, world: World) -> None:
        ana = await world.user("ana@example.com", name="")

        await world.accounts.delete(ana)

        assert await world.uow.users.get(ana) is None
        assert world.identity.deleted == [ana]

    async def test_someone_removed_from_their_only_workspace_can_leave(self, world: World) -> None:
        acme = await world.workspace(await world.user("owner@example.com"))
        removed = await world.join(acme, "removed@example.com")
        await world.workspaces.remove_member(acme.id, removed, acme.owner_id)

        await world.accounts.delete(removed)

        assert await world.uow.users.get(removed) is None

    async def test_removes_everything_the_account_holds(self, world: World) -> None:
        owner = await world.user("owner@example.com")
        acme = await world.workspace(owner)
        leaving = await world.join(acme, "leaving@example.com", "admin")
        await world.accounts.set_avatar(leaving, world.media.upload())
        sent = await world.workspaces.invite(
            acme.id, "friend@example.com", await world.role_id(acme, "member"), leaving
        )
        # A workspace it deleted earlier still names it as owner until the purge.
        mine = await world.workspace(leaving, "mine")
        await world.workspaces.delete(mine.id, leaving)

        await world.accounts.delete(leaving)

        assert await world.uow.users.get(leaving) is None
        assert await world.uow.members.get(acme.id, leaving) is None
        assert await world.uow.invitations.get(sent.view.invitation.id) is None
        assert await world.uow.workspaces.get_deleted(mine.id) is None
        assert world.identity.deleted == [leaving]
        assert world.media.objects == set()

    async def test_other_peoples_deleted_workspaces_wait_for_the_purge(self, world: World) -> None:
        acme = await world.workspace(await world.user("owner@example.com"))
        leaving = await world.join(acme, "leaving@example.com")
        someone = await world.user("someone@example.com")
        theirs = await world.workspace(someone, "theirs")
        await world.workspaces.delete(theirs.id, someone)

        await world.accounts.delete(leaving)

        assert await world.uow.workspaces.get_deleted(theirs.id) is not None

    async def test_a_pool_failure_does_not_undo_the_deletion(self, world: World) -> None:
        ana = await world.user("ana@example.com")
        world.identity.fail_deletes = True

        await world.accounts.delete(ana)

        assert await world.uow.users.get(ana) is None
