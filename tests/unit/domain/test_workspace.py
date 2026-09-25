"""Workspaces, roles and invitations.

- a workspace needs a name; names are trimmed
- slugs: 3 to 50 lowercase letters, digits and inner dashes, never a reserved path
- the owner may do anything; nobody else an owner action; every member reads;
  anything else is up to the member's role
- soft delete hides the workspace and frees its slug
- exactly one owner, and it is `owner_id`
- `owner` is a protected role name; custom roles never grant owner-only actions
- `members.invite` alone hands out the system member role only (fails closed)
- an invitation is usable while pending and unexpired, by its addressee only, once
"""

from datetime import timedelta

import pytest

from app.domain.access import CATALOG
from app.domain.errors import ConflictError, ForbiddenError, GoneError, InvalidError
from app.domain.workspace import (
    Invitation,
    Member,
    Role,
    hash_token,
    is_valid_slug,
    validate_permissions,
)
from tests import factories


class TestWorkspace:
    def test_a_new_workspace_is_live_and_has_no_logo(self) -> None:
        workspace = factories.workspace(name="  Acme ")

        assert workspace.name == "Acme"
        assert (workspace.logo_key, workspace.deleted_at) == (None, None)
        assert workspace.created_at.tzinfo is not None

    def test_a_blank_name_is_refused(self) -> None:
        with pytest.raises(InvalidError):
            factories.workspace(name="   ")

    def test_renaming_trims_the_name(self) -> None:
        workspace = factories.workspace()

        workspace.rename(" Acme Inc ", "acme-inc")

        assert (workspace.name, workspace.slug) == ("Acme Inc", "acme-inc")

    def test_replacing_the_logo_hands_back_the_old_key(self) -> None:
        workspace = factories.workspace()
        workspace.replace_logo("old", "https://cdn/old")

        previous = workspace.replace_logo("new", "https://cdn/new")

        assert (previous, workspace.logo_key, workspace.logo_url) == (
            "old",
            "new",
            "https://cdn/new",
        )

    def test_soft_delete_hides_it_and_frees_the_slug(self) -> None:
        workspace = factories.workspace()

        workspace.soft_delete()

        assert workspace.is_deleted
        assert workspace.slug.startswith("deleted-")
        assert len(workspace.slug) <= 50
        assert not factories.workspace().is_deleted


class TestSlugs:
    @pytest.mark.parametrize(
        "slug", ["abc", "acme", "acme-2", "a1b", "x" * 50, "a-b"], ids=lambda s: s[:12]
    )
    def test_valid(self, slug: str) -> None:
        assert is_valid_slug(slug)

    @pytest.mark.parametrize(
        "slug",
        ["ab", "x" * 51, "-acme", "acme-", "Acme", "ac me", "acmé", "api", "products", "invite"],
        ids=lambda s: s[:12],
    )
    def test_invalid(self, slug: str) -> None:
        assert not is_valid_slug(slug)


class TestGrants:
    @pytest.mark.parametrize("action", ["workspace.delete", "workspace.transfer", "roles.manage"])
    def test_the_owner_may_do_anything(self, action: str) -> None:
        assert factories.workspace(owner_id="owner").grants("owner", action) is True

    @pytest.mark.parametrize("action", ["workspace.delete", "workspace.transfer"])
    def test_nobody_else_takes_an_owner_action(self, action: str) -> None:
        assert factories.workspace(owner_id="owner").grants("member", action) is False

    def test_every_member_reads(self) -> None:
        assert factories.workspace().grants("member", "products.read") is True

    def test_anything_else_is_up_to_the_role(self) -> None:
        assert factories.workspace().grants("member", "products.create") is None

    def test_the_owner_holds_the_whole_catalog(self) -> None:
        workspace = factories.workspace(owner_id="owner")

        assert workspace.permissions_for("owner", []) == list(CATALOG)
        assert workspace.permissions_for("member", ["products.read"]) == ["products.read"]


class TestOwnership:
    def test_exactly_one_owner_and_it_is_owner_id(self) -> None:
        factories.workspace(owner_id="owner").ensure_single_owner(["owner"])

    @pytest.mark.parametrize("owners", [[], ["someone"], ["owner", "someone"]], ids=str)
    def test_any_other_owner_set_is_a_conflict(self, owners: list[str]) -> None:
        with pytest.raises(ConflictError):
            factories.workspace(owner_id="owner").ensure_single_owner(owners)

    def test_the_owner_is_protected_from_member_actions(self) -> None:
        workspace = factories.workspace(owner_id="owner")
        workspace.ensure_not_the_owner("member", "no")

        with pytest.raises(ForbiddenError, match="no"):
            workspace.ensure_not_the_owner("owner", "no")


class TestRoles:
    def test_only_the_owner_system_role_is_protected(self) -> None:
        assert Role.system("w1", "owner").is_protected is True
        assert Role.system("w1", "admin").is_protected is False
        assert Role.custom("w1", "editor").is_protected is False

    @pytest.mark.parametrize("name", ["owner", " owner "])
    def test_owner_is_not_a_custom_role_name(self, name: str) -> None:
        with pytest.raises(ForbiddenError):
            Role.custom("w1", name)
        with pytest.raises(ForbiddenError):
            Role.custom("w1", "editor").rename(name)

    def test_custom_names_are_trimmed(self) -> None:
        role = Role.custom("w1", " editor ")
        assert (role.name, role.is_system) == ("editor", False)

        role.rename(" writer ")

        assert role.name == "writer"

    def test_the_owner_role_is_not_configurable(self) -> None:
        Role.system("w1", "admin").ensure_configurable()

        with pytest.raises(ForbiddenError):
            Role.system("w1", "owner").ensure_configurable()

    def test_inviting_as_member_needs_no_role_management(self) -> None:
        Role.system("w1", "member").ensure_invitable(may_manage_roles=False)

    @pytest.mark.parametrize(
        "role",
        [Role.system("w1", "admin"), Role.custom("w1", "member"), Role.custom("w1", "editor")],
        ids=["admin", "renamed-member", "custom"],
    )
    def test_inviting_with_any_other_role_needs_it(self, role: Role) -> None:
        role.ensure_invitable(may_manage_roles=True)

        with pytest.raises(ForbiddenError):
            role.ensure_invitable(may_manage_roles=False)


def test_a_member_joins_now() -> None:
    member = Member.join("w1", "u1", "r1")

    assert (member.workspace_id, member.user_id, member.role_id) == ("w1", "u1", "r1")
    assert member.joined_at.tzinfo is not None


class TestPermissions:
    def test_known_permissions_pass_as_a_set(self) -> None:
        assert validate_permissions(["products.create", "products.create"]) == {"products.create"}

    def test_owner_only_actions_are_forbidden(self) -> None:
        with pytest.raises(ForbiddenError):
            validate_permissions(["products.create", "workspace.delete"])

    def test_unknown_permissions_are_invalid(self) -> None:
        with pytest.raises(InvalidError):
            validate_permissions(["products.create", "nope"])


class TestInvitation:
    def test_only_the_token_hash_is_kept(self) -> None:
        invitation, token = Invitation.issue(
            workspace_id="w1", email="a@example.com", role_id="r1", invited_by="owner"
        )

        assert invitation.token_hash == hash_token(token)
        assert token not in invitation.token_hash
        assert invitation.id
        assert invitation.accepted_by is None

    def test_the_email_is_lowercased_and_it_lasts_a_week(self) -> None:
        invitation = factories.invitation(email="Ana@Example.COM")

        assert invitation.email == "ana@example.com"
        assert invitation.expires_at - invitation.created_at == timedelta(days=7)
        assert invitation.current_status == "pending"

    def test_the_addressee_accepts_it_whatever_the_case(self) -> None:
        invitation = factories.invitation(email="ana@example.com")

        invitation.accept("u2", "ANA@example.com")

        assert (invitation.current_status, invitation.accepted_by) == ("accepted", "u2")

    def test_someone_else_cannot_accept_it(self) -> None:
        invitation = factories.invitation()

        with pytest.raises(ForbiddenError):
            invitation.accept("u2", "someone@example.com")

        assert invitation.current_status == "pending"

    def test_an_accepted_invitation_is_used_up(self) -> None:
        invitation = factories.invitation()
        invitation.accept("u2", "ana@example.com")

        with pytest.raises(GoneError):
            invitation.accept("u2", "ana@example.com")

    def test_it_expires_at_its_deadline(self) -> None:
        invitation = factories.invitation(expires_in=timedelta(0))

        assert invitation.current_status == "expired"
        with pytest.raises(GoneError):
            invitation.ensure_usable()

    def test_it_is_usable_until_then(self) -> None:
        factories.invitation(expires_in=timedelta(minutes=1)).ensure_usable()

    def test_a_pending_invitation_can_be_revoked_once(self) -> None:
        invitation = factories.invitation()

        invitation.revoke()

        assert invitation.current_status == "revoked"
        with pytest.raises(ConflictError):
            invitation.revoke()

    def test_an_expired_invitation_cannot_be_revoked(self) -> None:
        with pytest.raises(ConflictError):
            factories.invitation(expires_in=timedelta(0)).revoke()

    def test_an_accepted_invitation_never_reads_as_expired(self) -> None:
        invitation = factories.invitation()
        invitation.accept("u2", "ana@example.com")

        invitation.expires_at -= timedelta(days=30)

        assert invitation.current_status == "accepted"
