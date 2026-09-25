"""Only CloudFront may call the function URL: it adds a secret header to every request it
forwards, and anything without it is refused before reaching a route."""

import hmac

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

ORIGIN_HEADER = b"x-origin-verify"
# Lambda Web Adapter probes this path locally, before any request exists.
_UNGUARDED_PATHS = frozenset({"/health"})


class OriginVerificationMiddleware:
    def __init__(self, app: ASGIApp, secret: str) -> None:
        self.app = app
        self._secret = secret.encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["path"] not in _UNGUARDED_PATHS:
            sent = dict(scope["headers"]).get(ORIGIN_HEADER, b"")
            if not hmac.compare_digest(sent, self._secret):
                await JSONResponse({"detail": "Forbidden"}, status_code=403)(scope, receive, send)
                return
        await self.app(scope, receive, send)
