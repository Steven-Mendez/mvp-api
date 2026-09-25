"""Domain errors carry the message the client shows as `detail`."""

import pytest

from app.domain.errors import InvalidError, NotFoundError, OnboardingIncompleteError, found


def test_the_message_is_the_detail() -> None:
    assert InvalidError("Enter a name").detail == "Enter a name"


def test_an_onboarding_error_details_the_step() -> None:
    error = OnboardingIncompleteError("workspace_pending")

    assert error.detail == {
        "code": "ONBOARDING_INCOMPLETE",
        "onboarding_status": "workspace_pending",
    }


def test_found_passes_a_value_through() -> None:
    assert found(0, "unused") == 0


def test_found_refuses_none_with_the_message() -> None:
    with pytest.raises(NotFoundError) as refused:
        found(None, "Product not found")

    assert refused.value.detail == "Product not found"
