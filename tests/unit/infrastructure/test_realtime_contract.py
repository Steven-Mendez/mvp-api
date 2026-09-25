"""The web and mobile apps validate every realtime event against this shape (their
`realtimeEventSchema`): changing it breaks live updates without any error."""

from datetime import UTC, datetime

from app.application.ports import ChangeEvent
from app.infrastructure.aws.realtime import event_payload, user_channel


def test_published_events_keep_the_shape_the_clients_expect() -> None:
    event = ChangeEvent(
        resource="product",
        action="created",
        id="0199a0d2-8d41-7000-8000-000000000001",
        owner_id="workspace-1",
        at=datetime(2024, 1, 1, tzinfo=UTC),
    )
    assert event_payload(event) == {
        "resource": "product",
        "action": "created",
        "id": "0199a0d2-8d41-7000-8000-000000000001",
        "owner_id": "workspace-1",
        "at": "2024-01-01T00:00:00+00:00",
    }


def test_each_person_has_their_own_channel() -> None:
    assert user_channel("abc") == "/users/abc"
