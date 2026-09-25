"""Account use cases: registration and the signed-in person's own profile."""

from loguru import logger

from app.application.access import require
from app.application.dto import ProfileView
from app.application.media import SQUARE_IMAGE_TYPES
from app.application.ports import (
    AccountLookupError,
    EmailTakenError,
    IdentityProvider,
    MediaStorage,
    PasswordRejectedError,
    UnitOfWork,
    UploadTicket,
)
from app.application.purge import WorkspacePurge
from app.application.workspaces import join_with_invitation
from app.domain.errors import ConflictError, DomainError, InvalidError
from app.domain.user import EMAIL_HELD_BY_ANOTHER_ACCOUNT, User, existing


class AccountService:
    def __init__(
        self,
        uow: UnitOfWork,
        identity: IdentityProvider,
        media: MediaStorage,
        purge: WorkspacePurge,
    ) -> None:
        self._uow = uow
        self._identity = identity
        self._media = media
        self._purge = purge

    async def register(
        self, email: str, password: str, display_name: str, invitation_token: str | None
    ) -> None:
        """Create a confirmed account in the user pool and its profile row.

        The answer is the same whether or not the email was taken, so the endpoint does not
        reveal which addresses have accounts. A valid invitation token joins its workspace
        right away; an invalid one is ignored (the app retries it after signing in).
        """
        email = email.lower()
        try:
            sub = await self._identity.create_account(email, password)
        except EmailTakenError:
            logger.info("Registration for an existing account; answering as if it were new")
            return
        except PasswordRejectedError as exc:
            raise InvalidError(str(exc)) from exc

        uow = self._uow

        async def create() -> None:
            await uow.users.add_unless_taken(User.new(sub, email, display_name))
            await uow.flush()
            if invitation_token:
                try:
                    await join_with_invitation(uow, invitation_token, sub)
                except DomainError as exc:
                    logger.info("Invitation not applied at registration: {}", exc.detail)

        await uow.transaction(create)

    async def ensure_account(self, sub: str, access_token: str) -> str:
        """The account's email, creating its row on the subject's first request.

        Access tokens carry no email, so the first request asks the user pool once. A
        subject presenting an email another row holds (the pool was recreated) gets no row:
        linking it would hand that account's workspaces to whoever holds the address now.
        """
        uow = self._uow
        user = await uow.users.get(sub)
        if user is not None:
            return user.email
        token_sub, email = await self._identity.email_of(access_token)
        if token_sub != sub:
            raise AccountLookupError("The user pool answered for a different subject")

        async def create() -> None:
            await uow.users.add_unless_taken(User.new(sub, email))

        await uow.transaction(create)
        if await uow.users.get(sub) is None:
            logger.warning("Subject {} presents an email that another account holds", sub)
            raise ConflictError(EMAIL_HELD_BY_ANOTHER_ACCOUNT)
        return email

    async def profile(self, user_id: str) -> ProfileView:
        await require(self._uow, user_id, "profile.read", user_id)
        return await self._view(existing(await self._uow.users.get(user_id)))

    async def rename(self, user_id: str, display_name: str) -> ProfileView:
        uow = self._uow

        async def rename() -> User:
            await require(uow, user_id, "profile.update", user_id)
            user = existing(await uow.users.get(user_id))
            user.rename(
                display_name, belongs_to_a_workspace=await uow.members.belongs_to_any(user_id)
            )
            return user

        return await self._view(await uow.transaction(rename))

    async def request_avatar_upload(self, user_id: str, content_type: str) -> UploadTicket:
        """A presigned POST for a new avatar; nothing changes until it is confirmed."""
        await require(self._uow, user_id, "profile.update", user_id)
        return self._media.presign(content_type, SQUARE_IMAGE_TYPES)

    async def set_avatar(self, user_id: str, upload_key: str | None) -> ProfileView:
        """Point the profile at a freshly processed avatar, or at none. The image work runs
        before the transaction and is undone if the change fails."""
        uow = self._uow
        await require(uow, user_id, "profile.update", user_id)
        new_key = await self._media.store_square(upload_key) if upload_key else None

        async def replace() -> tuple[User, str | None]:
            user = existing(await uow.users.get(user_id))
            url = self._media.url_for(new_key) if new_key else None
            return user, user.replace_avatar(new_key, url)

        try:
            user, previous = await uow.transaction(replace)
        except BaseException:
            if new_key:
                await self._media.delete(new_key)
            raise
        if previous:
            await self._media.delete(previous)
        return await self._view(user)

    async def delete(self, user_id: str) -> None:
        uow = self._uow
        await self._ensure_deletable(user_id)
        # Workspaces this account deleted still name it as owner until the scheduled purge;
        # purge them now so the account row can go.
        for workspace_id in await uow.workspaces.deleted_ids(owned_by=user_id):
            await self._purge.purge(workspace_id)

        async def delete() -> User:
            await self._ensure_deletable(user_id)
            user = existing(await uow.users.get(user_id))
            await uow.members.remove_user(user_id)
            await uow.invitations.delete_sent_by(user_id)
            await uow.users.delete(user)
            return user

        user = await uow.transaction(delete)
        if user.avatar_key:
            await self._media.delete(user.avatar_key)
        try:
            await self._identity.delete_account(user.id)
        except Exception:  # the row is gone; a stale pool account only blocks re-registration
            logger.opt(exception=True).warning("Could not delete {} from the user pool", user_id)

    async def _ensure_deletable(self, user_id: str) -> None:
        await require(self._uow, user_id, "profile.delete", user_id)
        if await self._uow.workspaces.any_owned_by(user_id):
            raise ConflictError(
                "Transfer ownership or delete your workspaces before deleting your account"
            )

    async def _view(self, user: User) -> ProfileView:
        return ProfileView(
            user=user, joined_workspace=await self._uow.members.belongs_to_any(user.id)
        )
