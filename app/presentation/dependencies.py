"""Per-request wiring: the adapters behind the ports, the use cases built on them, and who
the caller is and what they may touch."""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import cache
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.access import AccessService
from app.application.accounts import AccountService
from app.application.ports import AccountLookupError
from app.application.products import ProductService
from app.application.purge import WorkspacePurge
from app.application.workspaces import WorkspaceService
from app.domain.product import ProductQuotas
from app.infrastructure.aws.cognito import CognitoIdentityProvider
from app.infrastructure.aws.mailer import SesMailer
from app.infrastructure.aws.rate_limiter import DynamoRateLimiter
from app.infrastructure.aws.realtime import AppSyncEventPublisher
from app.infrastructure.aws.storage import S3MediaStorage
from app.infrastructure.config import get_settings
from app.infrastructure.db.session import get_session
from app.infrastructure.db.unit_of_work import SqlUnitOfWork
from app.presentation.security import VerifiedToken, bearer, unauthorized, verify_token

# --- adapters (one per execution environment) --------------------------------------------


@cache
def media() -> S3MediaStorage:
    settings = get_settings()
    return S3MediaStorage(
        settings.media_bucket,
        settings.media_base_url,
        settings.media_max_upload_bytes,
        settings.media_thumbnail_size,
    )


@cache
def _identity() -> CognitoIdentityProvider:
    return CognitoIdentityProvider(get_settings().cognito_user_pool_id)


@cache
def _events() -> AppSyncEventPublisher:
    settings = get_settings()
    return AppSyncEventPublisher(settings.appsync_http_domain, settings.aws_region)


@cache
def _mailer() -> SesMailer:
    return SesMailer(get_settings().mail_from)


@cache
def _rate_limiter() -> DynamoRateLimiter:
    settings = get_settings()
    return DynamoRateLimiter(
        settings.rate_limit_table,
        settings.rate_limit_max_requests,
        settings.rate_limit_window_seconds,
    )


# --- use cases -------------------------------------------------------------------------

SessionDep = Annotated[AsyncSession, Depends(get_session)]


def _uow(session: SessionDep) -> SqlUnitOfWork:
    return SqlUnitOfWork(session)


UnitOfWorkDep = Annotated[SqlUnitOfWork, Depends(_uow)]


def _account_service(uow: UnitOfWorkDep) -> AccountService:
    return AccountService(uow, _identity(), media(), WorkspacePurge(uow, media()))


def _workspace_service(uow: UnitOfWorkDep) -> WorkspaceService:
    return WorkspaceService(uow, media(), _mailer(), get_settings().web_base_url)


def _product_service(uow: UnitOfWorkDep) -> ProductService:
    settings = get_settings()
    quotas = ProductQuotas(
        max_products=settings.max_products_per_workspace,
        max_storage_bytes=settings.max_storage_bytes_per_workspace,
    )
    return ProductService(uow, media(), _events(), quotas)


AccountServiceDep = Annotated[AccountService, Depends(_account_service)]
WorkspaceServiceDep = Annotated[WorkspaceService, Depends(_workspace_service)]
ProductServiceDep = Annotated[ProductService, Depends(_product_service)]


# --- who is calling --------------------------------------------------------------------


@dataclass(frozen=True)
class CurrentUser:
    id: str
    email: str


async def _verified_token(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> VerifiedToken:
    if credentials is None:
        raise unauthorized("Missing Authorization header: Bearer <token>")
    return await asyncio.to_thread(verify_token, credentials.credentials)


async def _current_user(
    # Declared first, so a bad token is refused before anything touches the database.
    token: Annotated[VerifiedToken, Depends(_verified_token)],
    accounts: AccountServiceDep,
) -> CurrentUser:
    try:
        email = await accounts.ensure_account(token.sub, token.raw)
    except AccountLookupError as exc:
        logger.warning("Could not resolve account {} from the user pool: {}", token.sub, exc)
        raise unauthorized("Account not found") from exc
    return CurrentUser(id=token.sub, email=email)


CurrentUserDep = Annotated[CurrentUser, Depends(_current_user)]


@dataclass(frozen=True)
class CurrentWorkspace:
    id: str
    slug: str


def workspace_access(action: str | None = None) -> Callable[..., Awaitable[CurrentWorkspace]]:
    """The workspace at `{slug}` if the caller belongs to it (404), and may perform
    `action` there when one is given (403)."""

    async def resolve(slug: str, user: CurrentUserDep, uow: UnitOfWorkDep) -> CurrentWorkspace:
        workspace = await AccessService(uow).workspace(user.id, slug, action)
        return CurrentWorkspace(id=workspace.id, slug=workspace.slug)

    return resolve


CurrentWorkspaceDep = Annotated[CurrentWorkspace, Depends(workspace_access())]
CanReadProducts = Annotated[CurrentWorkspace, Depends(workspace_access("products.read"))]
CanCreateProducts = Annotated[CurrentWorkspace, Depends(workspace_access("products.create"))]
CanUpdateProducts = Annotated[CurrentWorkspace, Depends(workspace_access("products.update"))]
CanDeleteProducts = Annotated[CurrentWorkspace, Depends(workspace_access("products.delete"))]


# --- rate limits -----------------------------------------------------------------------


def client_address(request: Request) -> str:
    """The viewer's address: a CloudFront function sets `x-viewer-ip` on every request
    (overwriting anything a client sent), see infra/cdn.tf."""
    return request.headers.get("x-viewer-ip") or (request.client.host if request.client else "-")


def current_user_id(user: CurrentUserDep) -> str:
    return user.id


def rate_limit(bucket: str, identify: Callable[..., object]) -> Callable[..., Awaitable[None]]:
    """A fixed window per `bucket` and caller; over the limit answers 429 + `Retry-After`.
    If DynamoDB cannot answer, the request goes through rather than failing."""

    async def check(identity: Annotated[str, Depends(identify)]) -> None:
        try:
            retry_after = await _rate_limiter().hit(f"{bucket}:{identity}")
        except Exception as exc:
            logger.warning("Rate limiter unavailable ({}); letting the request through", exc)
            return
        if retry_after is not None:
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                "Too many requests, try again shortly",
                headers={"Retry-After": str(retry_after)},
            )

    return check
