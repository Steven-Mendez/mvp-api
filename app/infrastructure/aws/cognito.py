"""The Cognito user pool as the `IdentityProvider` port. Clients sign in against the pool
directly (USER_PASSWORD_AUTH); the API only creates, describes and deletes accounts."""

import asyncio

from botocore.exceptions import ClientError

from app.application.ports import AccountLookupError, EmailTakenError, PasswordRejectedError
from app.infrastructure.aws.clients import cognito


def _code(exc: ClientError) -> str:
    return exc.response.get("Error", {}).get("Code", "")


class CognitoIdentityProvider:
    def __init__(self, user_pool_id: str) -> None:
        self._pool = user_pool_id

    async def create_account(self, email: str, password: str) -> str:
        """A confirmed account with a permanent password: no emailed code to enter."""
        client = cognito()
        try:
            created = await asyncio.to_thread(
                client.admin_create_user,
                UserPoolId=self._pool,
                Username=email,
                TemporaryPassword=password,
                MessageAction="SUPPRESS",
                UserAttributes=[
                    {"Name": "email", "Value": email},
                    {"Name": "email_verified", "Value": "true"},
                ],
            )
        except ClientError as exc:
            if _code(exc) == "UsernameExistsException":
                raise EmailTakenError from exc
            if _code(exc) in {"InvalidPasswordException", "InvalidParameterException"}:
                raise PasswordRejectedError(
                    exc.response.get("Error", {}).get("Message", "")
                ) from exc
            raise
        attributes = {a["Name"]: a.get("Value", "") for a in created["User"].get("Attributes", [])}
        sub = attributes["sub"]
        await asyncio.to_thread(
            client.admin_set_user_password,
            UserPoolId=self._pool,
            Username=sub,
            Password=password,
            Permanent=True,
        )
        return sub

    async def email_of(self, access_token: str) -> tuple[str, str]:
        try:
            user = await asyncio.to_thread(cognito().get_user, AccessToken=access_token)
        except ClientError as exc:
            raise AccountLookupError(_code(exc)) from exc
        attributes = {a["Name"]: a.get("Value", "") for a in user["UserAttributes"]}
        if not attributes.get("sub") or not attributes.get("email"):
            raise AccountLookupError("The account has no subject or email")
        return attributes["sub"], attributes["email"].lower()

    async def delete_account(self, sub: str) -> None:
        await asyncio.to_thread(cognito().admin_delete_user, UserPoolId=self._pool, Username=sub)
