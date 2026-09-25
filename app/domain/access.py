"""Who may do what in a workspace, and which onboarding step each action needs."""

from typing import Literal

OnboardingStatus = Literal["profile_pending", "workspace_pending", "completed"]

# action -> (category, label, description). The owner holds every one implicitly.
CATALOG: dict[str, tuple[str, str, str]] = {
    "workspace.update": (
        "Workspace",
        "Edit workspace",
        "Change the workspace name, address and logo.",
    ),
    "workspace.delete": (
        "Workspace",
        "Delete workspace",
        "Permanently delete the workspace and everything in it.",
    ),
    "members.invite": (
        "Members",
        "Invite members",
        "Send invitations and share invite links. Inviting with a role other than member "
        "also takes Manage roles and permissions.",
    ),
    "members.remove": ("Members", "Remove members", "Remove people from the workspace."),
    "invitations.revoke": (
        "Members",
        "Revoke invitations",
        "Cancel pending invitations before they are accepted.",
    ),
    "roles.manage": (
        "Permissions",
        "Manage roles and permissions",
        "Create, rename and delete roles, change their permissions and assign them to members.",
    ),
    "products.create": ("Products", "Create products", "Add new products to the catalog."),
    "products.update": (
        "Products",
        "Edit products and images",
        "Edit product details, pricing and images.",
    ),
    "products.delete": ("Products", "Delete products", "Remove products from the catalog."),
}

OWNER_ONLY = "workspace.delete"
# Refused to everyone but the owner, whatever their role grants.
OWNER_ACTIONS = frozenset({OWNER_ONLY, "workspace.transfer"})

ADMIN_PERMISSIONS = [action for action in CATALOG if action != OWNER_ONLY]
SYSTEM_ROLES: dict[str, list[str]] = {
    "owner": [],  # implicit: all of them
    "admin": ADMIN_PERMISSIONS,
    "member": [action for action in CATALOG if action.startswith("products.")],
    "viewer": [],
}
OWNER_ROLE = "owner"
ADMIN_ROLE = "admin"
DEFAULT_MEMBER_ROLE = "member"

PERMISSION_DENIED = "You do not have permission to perform this action"

ONBOARDED: frozenset[OnboardingStatus] = frozenset({"completed"})
READY_FOR_A_WORKSPACE: frozenset[OnboardingStatus] = frozenset({"completed", "workspace_pending"})
ANY_STATUS: frozenset[OnboardingStatus] = frozenset(
    {"profile_pending", "workspace_pending", "completed"}
)


def is_on_own_account(action: str) -> bool:
    """Actions on oneself name the account as their resource, not a workspace."""
    return action.startswith(("profile.", "sessions.")) or action in {
        "workspaces.list",
        "workspaces.create",
        "workspaces.availability",
        "invitations.accept",
    }


def onboarding_needed_for(action: str) -> frozenset[OnboardingStatus]:
    """The onboarding statuses allowed to perform `action`. The account itself (the
    profile, sessions, deleting it) is reachable at every step, so someone removed from
    their only workspace can still leave."""
    if action in {"workspaces.create", "workspaces.availability"}:
        return READY_FOR_A_WORKSPACE
    if action.startswith(("profile.", "sessions.")) or action == "invitations.accept":
        return ANY_STATUS
    return ONBOARDED
