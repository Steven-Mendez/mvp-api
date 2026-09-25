"""Invitation emails through Amazon SES."""

import asyncio
from html import escape

from app.infrastructure.aws.clients import ses


class SesMailer:
    def __init__(self, sender: str) -> None:
        self._sender = sender

    async def send_invitation(self, email: str, workspace_name: str, url: str) -> None:
        subject = f"You're invited to join {workspace_name}"
        text = f"You've been invited to join {workspace_name}.\n\nAccept the invitation: {url}\n"
        html = (
            f"<p>You've been invited to join <strong>{escape(workspace_name)}</strong>.</p>"
            f'<p><a href="{escape(url)}">Accept the invitation</a></p>'
        )
        await asyncio.to_thread(
            ses().send_email,
            FromEmailAddress=self._sender,
            Destination={"ToAddresses": [email]},
            Content={
                "Simple": {
                    "Subject": {"Data": subject},
                    "Body": {"Text": {"Data": text}, "Html": {"Data": html}},
                }
            },
        )
