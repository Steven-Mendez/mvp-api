"""The HTTP API end to end: the real app, routes, dependencies, use cases, repositories
and migrated Postgres. Only what lives outside the process is replaced: the AWS adapters
by the fakes, and Cognito's signature check by the fake pool's tokens (the check itself
is covered in tests/unit/presentation/test_security.py).

The client runs the app in the test's own event loop (httpx's ASGI transport), so the
app shares the test's database session and its rollback.
"""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Annotated, Any

import httpx
import pytest
from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.db.session import get_session
from app.main import app
from app.presentation import dependencies
from app.presentation.security import VerifiedToken, bearer, unauthorized
from tests.fakes import (
    FakeEventPublisher,
    FakeIdentityProvider,
    FakeMailer,
    FakeMediaStorage,
    FakeRateLimiter,
)

ORIGIN = {"x-origin-verify": "test-secret"}


@dataclass
class Outside:
    """Everything beyond the process, as the tests see it."""

    identity: FakeIdentityProvider
    media: FakeMediaStorage
    mailer: FakeMailer
    events: FakeEventPublisher
    limiter: FakeRateLimiter


@pytest.fixture
def outside(monkeypatch: pytest.MonkeyPatch) -> Outside:
    world = Outside(
        FakeIdentityProvider(),
        FakeMediaStorage(),
        FakeMailer(),
        FakeEventPublisher(),
        FakeRateLimiter(),
    )
    monkeypatch.setattr(dependencies, "_identity", lambda: world.identity)
    monkeypatch.setattr(dependencies, "media", lambda: world.media)
    monkeypatch.setattr(dependencies, "_mailer", lambda: world.mailer)
    monkeypatch.setattr(dependencies, "_events", lambda: world.events)
    monkeypatch.setattr(dependencies, "_rate_limiter", lambda: world.limiter)
    return world


class Api:
    """An HTTP client plus the people using it."""

    def __init__(self, client: httpx.AsyncClient, outside: Outside) -> None:
        self.http = client
        self.outside = outside

    async def request(self, method: str, url: str, as_: str | None = None, **kw: Any) -> Any:
        headers: dict[str, str] = kw.pop("headers", {})
        if as_:
            headers["Authorization"] = f"Bearer {as_}"
        return await self.http.request(method, url, headers=headers, **kw)

    async def sign_up(self, email: str, name: str = "Someone") -> str:
        """Register through the API and sign in against the pool: a bearer token."""
        registered = await self.request(
            "POST",
            "/auth/register",
            json={"email": email, "password": "long enough", "display_name": name},
        )
        assert registered.status_code == 201, registered.text
        return self.outside.identity.sign_in(email)

    async def onboarded_with_workspace(self, email: str, slug: str) -> str:
        token = await self.sign_up(email)
        created = await self.request(
            "POST", "/api/workspaces", token, json={"name": slug.title(), "slug": slug}
        )
        assert created.status_code == 201, created.text
        return token


@pytest.fixture
async def api(session: AsyncSession, outside: Outside) -> AsyncIterator[Api]:
    async def test_session() -> AsyncIterator[AsyncSession]:
        yield session

    async def pool_token(
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    ) -> VerifiedToken:
        if credentials is None or credentials.credentials not in outside.identity.tokens:
            raise unauthorized()
        sub, _ = outside.identity.tokens[credentials.credentials]
        return VerifiedToken(sub=sub, raw=credentials.credentials)

    app.dependency_overrides[get_session] = test_session
    app.dependency_overrides[dependencies._verified_token] = pool_token  # pyright: ignore[reportPrivateUsage]
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://api.test", headers=ORIGIN
    ) as client:
        yield Api(client, outside)
    app.dependency_overrides.clear()
