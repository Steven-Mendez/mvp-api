"""Primary keys are UUIDv7 as text: time-ordered (ids break ties in cursors) and random in
the low bits, so writes spread across Aurora DSQL's key range. `users.id` is the Cognito
`sub`, which AWS asks not to parse, so every key is plain text."""

import uuid
from datetime import UTC, datetime


def new_id() -> str:
    return str(uuid.uuid7())


def now() -> datetime:
    return datetime.now(UTC)
