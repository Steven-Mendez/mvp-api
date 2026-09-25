"""The permission catalog and which onboarding step each action needs.

- actions on oneself (profile, sessions, own workspace list) name the account
- creating a workspace (and checking a slug) needs a named profile
- the profile itself (deleting the account included), sessions and accepting an
  invitation work at any step
- everything else needs finished onboarding
- system roles: admins get everything but deleting; members manage products
"""

import pytest

from app.domain.access import (
    ADMIN_PERMISSIONS,
    ANY_STATUS,
    CATALOG,
    ONBOARDED,
    READY_FOR_A_WORKSPACE,
    SYSTEM_ROLES,
    is_on_own_account,
    onboarding_needed_for,
)


@pytest.mark.parametrize(
    ("action", "own"),
    [
        ("profile.update", True),
        ("sessions.revoke", True),
        ("workspaces.list", True),
        ("workspaces.create", True),
        ("workspaces.availability", True),
        ("invitations.accept", True),
        ("workspace.read", False),
        ("invitations.revoke", False),
        ("products.create", False),
    ],
)
def test_which_actions_are_on_the_own_account(action: str, own: bool) -> None:
    assert is_on_own_account(action) is own


@pytest.mark.parametrize(
    ("action", "allowed"),
    [
        ("workspaces.create", READY_FOR_A_WORKSPACE),
        ("workspaces.availability", READY_FOR_A_WORKSPACE),
        ("profile.read", ANY_STATUS),
        ("profile.update", ANY_STATUS),
        ("sessions.list", ANY_STATUS),
        ("invitations.accept", ANY_STATUS),
        ("profile.delete", ANY_STATUS),  # leaving never waits for onboarding
        ("workspaces.list", ONBOARDED),
        ("workspace.read", ONBOARDED),
        ("products.create", ONBOARDED),
    ],
)
def test_the_onboarding_each_action_needs(action: str, allowed: frozenset[str]) -> None:
    assert onboarding_needed_for(action) == allowed


def test_admins_hold_everything_but_deleting_the_workspace() -> None:
    assert set(ADMIN_PERMISSIONS) == set(CATALOG) - {"workspace.delete"}
    assert SYSTEM_ROLES["admin"] == ADMIN_PERMISSIONS


def test_members_manage_products_and_viewers_only_read() -> None:
    assert SYSTEM_ROLES["member"] == ["products.create", "products.update", "products.delete"]
    assert SYSTEM_ROLES["viewer"] == []
