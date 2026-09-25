"""Opaque list cursors: the last row's sort value and id.

- every sortable field round-trips with its own type
- anything that is not a cursor we made is a validation error, never a 500
"""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.domain.errors import InvalidError
from app.infrastructure.db.repositories import decode_cursor, encode_cursor


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("name", "Mug"),
        ("status", "active"),
        ("price", Decimal("9.99")),
        ("created_at", datetime(2026, 1, 2, 3, 4, 5, 6, tzinfo=UTC)),
    ],
)
def test_a_cursor_round_trips(field: str, value: object) -> None:
    cursor = encode_cursor(value, "0199")

    assert decode_cursor(cursor, field) == (value, "0199")


def test_a_cursor_is_url_safe_without_padding() -> None:
    cursor = encode_cursor("Mug?&/+", "0199")

    assert not set(cursor) & set("=+/?&")


@pytest.mark.parametrize(
    ("cursor", "field"),
    [
        ("%%%", "name"),
        (encode_cursor("not a date", "1"), "created_at"),
        (encode_cursor("not a price", "1"), "price"),
        ("WyJvbmx5LW9uZSJd", "name"),  # ["only-one"]
    ],
)
def test_a_forged_cursor_is_invalid(cursor: str, field: str) -> None:
    with pytest.raises(InvalidError):
        decode_cursor(cursor, field)
