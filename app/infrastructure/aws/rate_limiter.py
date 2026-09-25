"""Fixed-window request counters in DynamoDB (on demand: nothing to pay without traffic).
Items carry a TTL, so old windows disappear on their own."""

import asyncio
import time

from app.infrastructure.aws.clients import dynamodb


class DynamoRateLimiter:
    def __init__(self, table: str, max_requests: int, window_seconds: int) -> None:
        self._table = table
        self._max = max_requests
        self._window = window_seconds

    async def hit(self, key: str) -> int | None:
        """Count one request; the seconds to wait when over the limit, else `None`."""
        now = int(time.time())
        window_start = now - now % self._window
        window_end = window_start + self._window
        response = await asyncio.to_thread(
            dynamodb().update_item,
            TableName=self._table,
            Key={"pk": {"S": f"{key}:{window_start}"}},
            UpdateExpression="ADD hits :one SET expires_at = if_not_exists(expires_at, :ttl)",
            ExpressionAttributeValues={":one": {"N": "1"}, ":ttl": {"N": str(window_end + 60)}},
            ReturnValues="UPDATED_NEW",
        )
        hits = int(response.get("Attributes", {}).get("hits", {}).get("N", "0"))
        return max(1, window_end - now) if hits > self._max else None
