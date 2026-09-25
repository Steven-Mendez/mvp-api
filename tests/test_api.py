import pytest
from fastapi.testclient import TestClient

from app.domain.errors import ConflictError, OnboardingIncompleteError
from app.infrastructure.db.repositories import decode_cursor, encode_cursor
from app.main import app
from app.presentation.errors import status_for

ORIGIN = {"x-origin-verify": "test-secret"}


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def test_liveness_needs_no_origin_header(client: TestClient) -> None:
    assert client.get("/health").json() == {"status": "ok"}


def test_requests_that_skip_cloudfront_are_refused(client: TestClient) -> None:
    assert client.get("/me").status_code == 403


def test_a_missing_token_is_a_401(client: TestClient) -> None:
    response = client.get("/me", headers=ORIGIN)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_domain_errors_map_to_statuses() -> None:
    assert status_for(ConflictError("x")) == 409
    assert status_for(OnboardingIncompleteError("profile_pending")) == 403


def test_cursor_round_trip() -> None:
    cursor = encode_cursor("Mug", "0199")
    assert decode_cursor(cursor, "name") == ("Mug", "0199")
