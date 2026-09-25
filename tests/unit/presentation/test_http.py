"""The HTTP edge that needs no database.

- liveness answers without the origin header; everything else needs CloudFront's secret
- a request without a bearer token is a 401 with `WWW-Authenticate`
- each kind of domain error maps to one status, subclasses included
- rate limits: over the limit is a 429 with `Retry-After`; a limiter that is down lets
  the request through
"""

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.domain import errors
from app.main import app
from app.presentation import dependencies
from app.presentation.errors import status_for
from tests.fakes import FakeRateLimiter

ORIGIN = {"x-origin-verify": "test-secret"}


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def test_liveness_needs_no_origin_header(client: TestClient) -> None:
    assert client.get("/health").json() == {"status": "ok"}


@pytest.mark.parametrize("headers", [{}, {"x-origin-verify": "wrong"}], ids=["none", "wrong"])
def test_requests_that_skip_cloudfront_are_refused(
    client: TestClient, headers: dict[str, str]
) -> None:
    assert client.get("/me", headers=headers).status_code == 403


def test_a_missing_token_is_a_401(client: TestClient) -> None:
    response = client.get("/me", headers=ORIGIN)

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (errors.UnauthenticatedError("x"), 401),
        (errors.ForbiddenError("x"), 403),
        (errors.OnboardingIncompleteError("profile_pending"), 403),
        (errors.NotFoundError("x"), 404),
        (errors.ConflictError("x"), 409),
        (errors.GoneError("x"), 410),
        (errors.PreconditionFailedError("x"), 412),
        (errors.ContentTooLargeError("x"), 413),
        (errors.UnsupportedMediaTypeError("x"), 415),
        (errors.InvalidError("x"), 422),
        (errors.DomainError("x"), 400),
    ],
    ids=lambda value: type(value).__name__ if isinstance(value, Exception) else str(value),
)
def test_each_domain_error_has_its_status(error: errors.DomainError, status: int) -> None:
    assert status_for(error) == status


class TestRateLimit:
    async def test_under_the_limit_passes_and_counts_per_caller(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        limiter = FakeRateLimiter()
        monkeypatch.setattr(dependencies, "_rate_limiter", lambda: limiter)

        await dependencies.rate_limit("auth:register", str)("1.2.3.4")

        assert limiter.hits == ["auth:register:1.2.3.4"]

    async def test_over_the_limit_is_a_429_with_retry_after(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(dependencies, "_rate_limiter", lambda: FakeRateLimiter(retry_after=7))

        with pytest.raises(HTTPException) as refused:
            await dependencies.rate_limit("auth:register", str)("1.2.3.4")

        assert (refused.value.status_code, refused.value.headers) == (429, {"Retry-After": "7"})

    async def test_a_limiter_that_is_down_lets_the_request_through(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def down() -> FakeRateLimiter:
            raise RuntimeError("DynamoDB unreachable")

        monkeypatch.setattr(dependencies, "_rate_limiter", down)

        await dependencies.rate_limit("auth:register", str)("1.2.3.4")
