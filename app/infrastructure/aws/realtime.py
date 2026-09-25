"""Domain events to AppSync Events, the `EventPublisher` port.

Every person subscribes to their own channel `/users/<sub>` with their Cognito token (an
`onSubscribe` handler refuses any other channel, see infra/realtime.tf). The API publishes
over HTTP, signed with the Lambda role (SigV4), to each member of the workspace.
"""

import asyncio
import json

import boto3
import httpx
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from loguru import logger

from app.application.ports import ChangeEvent

_TIMEOUT_SECONDS = 5.0


def user_channel(user_id: str) -> str:
    return f"/users/{user_id}"


def event_payload(event: ChangeEvent) -> dict[str, str]:
    """What subscribers receive; tests/test_realtime_contract.py pins its shape."""
    return {
        "resource": event.resource,
        "action": event.action,
        "id": event.id,
        "owner_id": event.owner_id,
        "at": event.at.isoformat(),
    }


class AppSyncEventPublisher:
    def __init__(self, http_domain: str, region: str) -> None:
        self._url = f"https://{http_domain}/event"
        self._region = region
        self._session = boto3.Session()

    async def publish(self, event: ChangeEvent, recipients: list[str]) -> None:
        payload = json.dumps(event_payload(event))
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
            results = await asyncio.gather(
                *(self._send(client, user_channel(user_id), payload) for user_id in recipients),
                return_exceptions=True,
            )
        failures = [result for result in results if isinstance(result, BaseException)]
        if failures:
            logger.warning(
                "Realtime: {} of {} deliveries failed ({})",
                len(failures),
                len(recipients),
                failures[0],
            )

    async def _send(self, client: httpx.AsyncClient, channel: str, payload: str) -> None:
        body = json.dumps({"channel": channel, "events": [payload]})
        request = AWSRequest(
            method="POST",
            url=self._url,
            data=body,
            headers={"content-type": "application/json"},
        )
        credentials = self._session.get_credentials()
        if credentials is None:
            raise RuntimeError("No AWS credentials to sign the AppSync request")
        SigV4Auth(credentials.get_frozen_credentials(), "appsync", self._region).add_auth(request)
        response = await client.post(self._url, content=body, headers=dict(request.headers.items()))
        response.raise_for_status()
