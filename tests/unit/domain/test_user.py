"""Accounts and their onboarding.

- a new account has a lowercased email and a trimmed name, and names its profile next
  unless it came with a name
- naming the profile moves on to the workspace step, or finishes if already a member
- creating or joining a workspace finishes onboarding; leaving every one goes back
- an action refuses an account whose onboarding step does not allow it
- replacing the avatar hands back the storage key it replaced
"""

import pytest

from app.domain.errors import OnboardingIncompleteError, UnauthenticatedError
from app.domain.user import existing
from tests import factories


class TestNew:
    def test_email_is_lowercased_and_name_trimmed(self) -> None:
        user = factories.user(email="Ana@Example.com", display_name="  Ana ")

        assert (user.email, user.display_name) == ("ana@example.com", "Ana")

    def test_the_profile_step_comes_first(self) -> None:
        user = factories.user()

        assert user.created_at.tzinfo is not None
        assert user.onboarding_status == "profile_pending"
        assert user.required_profile_fields == ["display_name"]
        assert user.missing_profile_fields == ["display_name"]

    def test_a_blank_name_is_still_missing(self) -> None:
        user = factories.user()
        user.display_name = "   "

        assert user.missing_profile_fields == ["display_name"]

    def test_a_name_given_up_front_skips_the_profile_step(self) -> None:
        user = factories.user(display_name="Ana")

        assert user.missing_profile_fields == []
        assert user.onboarding_status == "workspace_pending"

    def test_a_blank_name_up_front_does_not(self) -> None:
        assert factories.user(display_name="   ").onboarding_status == "profile_pending"


class TestOnboarding:
    def test_naming_the_profile_leads_to_the_workspace_step(self) -> None:
        user = factories.user()

        user.rename("Ana", belongs_to_a_workspace=False)

        assert (user.display_name, user.onboarding_status) == ("Ana", "workspace_pending")

    def test_naming_it_as_a_member_finishes_onboarding(self) -> None:
        user = factories.user()

        user.rename("Ana", belongs_to_a_workspace=True)

        assert user.onboarding_status == "completed"

    def test_renaming_later_changes_no_step(self) -> None:
        user = factories.user()
        user.rename("Ana", belongs_to_a_workspace=False)

        user.rename("Ana B", belongs_to_a_workspace=True)

        assert user.onboarding_status == "workspace_pending"

    def test_creating_a_workspace_finishes_onboarding(self) -> None:
        user = factories.user()
        user.rename("Ana", belongs_to_a_workspace=False)

        user.created_a_workspace()

        assert user.onboarding_status == "completed"

    def test_joining_finishes_the_workspace_step_only(self) -> None:
        pending_profile = factories.user()
        pending_workspace = factories.user()
        pending_workspace.rename("Ana", belongs_to_a_workspace=False)

        pending_profile.joined_a_workspace()
        pending_workspace.joined_a_workspace()

        assert pending_profile.onboarding_status == "profile_pending"
        assert pending_workspace.onboarding_status == "completed"

    def test_leaving_every_workspace_goes_back_a_step(self) -> None:
        user = factories.onboarded_user()

        user.left_every_workspace()

        assert user.onboarding_status == "workspace_pending"

    def test_leaving_before_finishing_changes_nothing(self) -> None:
        user = factories.user()

        user.left_every_workspace()

        assert user.onboarding_status == "profile_pending"

    def test_an_action_needing_a_later_step_is_refused(self) -> None:
        user = factories.user()

        with pytest.raises(OnboardingIncompleteError) as refused:
            user.ensure_onboarded(frozenset({"completed"}))

        assert refused.value.detail == {
            "code": "ONBOARDING_INCOMPLETE",
            "onboarding_status": "profile_pending",
        }

    def test_an_action_allowed_at_this_step_passes(self) -> None:
        factories.user().ensure_onboarded(frozenset({"profile_pending"}))


def test_replacing_the_avatar_hands_back_the_old_key() -> None:
    user = factories.user()
    user.replace_avatar("old", "https://cdn/old")

    previous = user.replace_avatar(None, None)

    assert (previous, user.avatar_key, user.avatar_url) == ("old", None, None)


def test_a_missing_account_is_unauthenticated() -> None:
    with pytest.raises(UnauthenticatedError):
        existing(None)
