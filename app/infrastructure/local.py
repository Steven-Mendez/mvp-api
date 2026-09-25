"""Local mode stand-ins for the two services moto cannot play: invitation emails and
realtime events are written to the log, so the invitation link can be opened from there."""

import json

from loguru import logger

from app.application.ports import ChangeEvent
from app.infrastructure.aws.realtime import event_payload, user_channel


class LogMailer:
    async def send_invitation(self, email: str, workspace_name: str, url: str) -> None:
        logger.info("Invitation to {} for {}: {}", email, workspace_name, url)


class LogEventPublisher:
    async def publish(self, event: ChangeEvent, recipients: list[str]) -> None:
        channels = ", ".join(user_channel(user_id) for user_id in recipients)
        logger.info("Realtime to {}: {}", channels or "nobody", json.dumps(event_payload(event)))
