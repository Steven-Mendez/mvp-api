"""Bearer authentication with Amazon Cognito access tokens.

The API never mints tokens: clients sign in against the user pool and send the access
token. It is checked against the pool's JWKS, issuer, `token_use` and app client, since a
signature and issuer alone would also accept an ID token or another client's token.
"""

from dataclasses import dataclass
from functools import cache

import jwt
from fastapi import HTTPException, status
from fastapi.security import HTTPBearer
from loguru import logger

from app.infrastructure.config import get_settings

bearer = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class VerifiedToken:
    sub: str
    raw: str


@cache
def _jwks() -> jwt.PyJWKClient:
    # Keys are cached per execution environment; an unknown `kid` refetches them once.
    return jwt.PyJWKClient(get_settings().cognito_jwks_url, timeout=5)


def unauthorized(detail: str = "Invalid or expired token") -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


def verify_token(token: str) -> VerifiedToken:
    settings = get_settings()
    try:
        key = _jwks().get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            key.key,
            algorithms=["RS256"],
            issuer=settings.cognito_issuer,
            options={
                "verify_aud": False,  # access tokens carry `client_id`, not `aud`
                "require": ["exp", "iat", "iss", "sub", "token_use", "client_id"],
            },
        )
    except jwt.PyJWKClientConnectionError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication keys unavailable, retry shortly",
        ) from exc
    except jwt.PyJWTError as exc:
        logger.debug("Rejected token: {}", exc)
        raise unauthorized() from exc
    if claims.get("token_use") != "access":
        raise unauthorized("Not an access token")
    if claims.get("client_id") not in settings.cognito_client_ids:
        raise unauthorized("Token was issued to another application")
    subject = claims.get("sub")
    if not isinstance(subject, str) or not subject:
        raise unauthorized("Invalid subject")
    return VerifiedToken(sub=subject, raw=token)
