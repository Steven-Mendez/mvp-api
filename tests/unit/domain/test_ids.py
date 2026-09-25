"""Keys and clocks.

- timestamps are timezone-aware UTC (the columns are timestamptz)
- ids are UUIDv7 text, time-ordered so they break ties in cursors
"""

import uuid
from datetime import UTC

from app.domain.ids import new_id, now


def test_now_is_aware_utc() -> None:
    assert now().tzinfo is UTC


def test_ids_are_uuid7_in_creation_order() -> None:
    ids = [new_id() for _ in range(50)]

    assert all(uuid.UUID(value).version == 7 for value in ids)
    assert ids == sorted(ids)
