"""An account: the identity provider's subject plus the profile the product keeps."""

from dataclasses import dataclass
from datetime import datetime

from app.domain.access import OnboardingStatus
from app.domain.errors import OnboardingIncompleteError, UnauthenticatedError
from app.domain.ids import now

ACCOUNT_NOT_FOUND = "Account not found"
EMAIL_HELD_BY_ANOTHER_ACCOUNT = (
    "This email already belongs to another account. Contact support to recover it."
)


@dataclass
class User:
    id: str  # the Cognito `sub`
    email: str
    display_name: str
    avatar_url: str | None
    avatar_key: str | None
    onboarding_status: OnboardingStatus
    created_at: datetime

    @classmethod
    def new(cls, user_id: str, email: str, display_name: str = "") -> User:
        """An account the identity provider just vouched for. Its profile step comes next,
        unless it already has a name (given at registration): then joining or creating a
        workspace does."""
        name = display_name.strip()
        return cls(
            id=user_id,
            email=email.lower(),
            display_name=name,
            avatar_url=None,
            avatar_key=None,
            onboarding_status="workspace_pending" if name else "profile_pending",
            created_at=now(),
        )

    @property
    def required_profile_fields(self) -> list[str]:
        return ["display_name"]

    @property
    def missing_profile_fields(self) -> list[str]:
        return [] if self.display_name.strip() else ["display_name"]

    def ensure_onboarded(self, allowed: frozenset[OnboardingStatus]) -> None:
        if self.onboarding_status not in allowed:
            raise OnboardingIncompleteError(self.onboarding_status)

    def rename(self, display_name: str, *, belongs_to_a_workspace: bool) -> None:
        """Naming the profile finishes its step; the next is joining or creating a workspace."""
        self.display_name = display_name
        if self.onboarding_status == "profile_pending":
            self.onboarding_status = "completed" if belongs_to_a_workspace else "workspace_pending"

    def replace_avatar(self, key: str | None, url: str | None) -> str | None:
        """Point at a new avatar (or none) and return the storage key it replaces."""
        previous = self.avatar_key
        self.avatar_key, self.avatar_url = key, url
        return previous

    def created_a_workspace(self) -> None:
        self.onboarding_status = "completed"

    def joined_a_workspace(self) -> None:
        if self.onboarding_status == "workspace_pending":
            self.onboarding_status = "completed"

    def left_every_workspace(self) -> None:
        if self.onboarding_status == "completed":
            self.onboarding_status = "workspace_pending"


def existing(user: User | None) -> User:
    if user is None:
        raise UnauthenticatedError(ACCOUNT_NOT_FOUND)
    return user
