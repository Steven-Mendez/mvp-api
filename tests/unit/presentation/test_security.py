"""Bearer tokens, checked against keys generated here instead of Cognito's JWKS.

- a Cognito access token for one of our app clients passes and names its subject
- refused (401): an ID token, another client's token, another issuer, an expired
  token, a missing claim, an empty subject, a signature by another key, garbage
- keys that cannot be fetched are a 503, not a 401
"""

import time
from collections.abc import Iterator
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException
from jwt.algorithms import RSAAlgorithm

from app.infrastructure.config import get_settings
from app.presentation import security

_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


class _Keys:
    """`PyJWKClient`'s one method, answering with our public key."""

    def __init__(self, error: Exception | None = None) -> None:
        self._error = error

    def get_signing_key_from_jwt(self, _token: str) -> Any:
        if self._error:
            raise self._error
        return jwt.PyJWK.from_dict(
            {**RSAAlgorithm.to_jwk(_KEY.public_key(), as_dict=True), "alg": "RS256"}
        )


@pytest.fixture(autouse=True)
def keys(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(security, "_jwks", _Keys)
    yield


def _token(key: Any = _KEY, **overrides: Any) -> str:
    settings = get_settings()
    now = int(time.time())
    claims: dict[str, Any] = {
        "sub": "user-1",
        "iss": settings.cognito_issuer,
        "token_use": "access",
        "client_id": settings.cognito_client_ids[0],
        "iat": now,
        "exp": now + 300,
    }
    claims |= overrides
    return jwt.encode({k: v for k, v in claims.items() if v is not None}, key, algorithm="RS256")


def _refused(token: str) -> str:
    with pytest.raises(HTTPException) as refused:
        security.verify_token(token)
    assert refused.value.status_code == 401
    assert refused.value.headers == {"WWW-Authenticate": "Bearer"}
    return str(refused.value.detail)


def test_an_access_token_of_our_client_passes() -> None:
    token = _token(client_id=get_settings().cognito_client_ids[1])

    verified = security.verify_token(token)

    assert (verified.sub, verified.raw) == ("user-1", token)


def test_an_id_token_is_refused() -> None:
    id_token = _token(token_use="id")  # noqa: S106  # a claim, not a password

    assert _refused(id_token) == "Not an access token"


def test_a_token_of_another_client_is_refused() -> None:
    assert _refused(_token(client_id="someone-elses")) == "Token was issued to another application"


def test_a_token_of_another_pool_is_refused() -> None:
    _refused(_token(iss="https://cognito-idp.us-east-1.amazonaws.com/us-east-1_other"))


def test_an_expired_token_is_refused() -> None:
    _refused(_token(exp=int(time.time()) - 1))


@pytest.mark.parametrize("claim", ["exp", "iat", "iss", "sub", "token_use", "client_id"])
def test_a_token_missing_a_required_claim_is_refused(claim: str) -> None:
    _refused(_token(**{claim: None}))


def test_an_empty_subject_is_refused() -> None:
    assert _refused(_token(sub="")) == "Invalid subject"


def test_a_token_signed_by_another_key_is_refused() -> None:
    _refused(_token(key=_OTHER_KEY))


def test_garbage_is_refused() -> None:
    _refused("not-a-jwt")


def test_unreachable_keys_are_a_503(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        security, "_jwks", lambda: _Keys(jwt.PyJWKClientConnectionError("timed out"))
    )

    with pytest.raises(HTTPException) as refused:
        security.verify_token(_token())

    assert refused.value.status_code == 503
