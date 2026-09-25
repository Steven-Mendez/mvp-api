"""Domain errors as HTTP answers, and their OpenAPI documentation."""

from typing import Any, Literal

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from loguru import logger
from pydantic import BaseModel

from app.application.ports import ConcurrencyConflictError
from app.domain import errors


class ErrorDetail(BaseModel):
    detail: str


class OnboardingIncomplete(BaseModel):
    """The structured 403 detail of an action the account's onboarding step does not
    allow yet."""

    code: Literal["ONBOARDING_INCOMPLETE"]
    onboarding_status: str


class ForbiddenDetail(BaseModel):
    detail: str | OnboardingIncomplete


def error_responses(*statuses: int) -> dict[int | str, dict[str, Any]]:
    """OpenAPI `responses=` for the errors a route can answer."""
    return {
        status: {"model": ForbiddenDetail if status == 403 else ErrorDetail} for status in statuses
    }


# The errors every route of a kind can answer (spread them first in `error_responses`).
AUTHENTICATED = (401,)
ONBOARDED = (401, 403)
WORKSPACE_SCOPED = (401, 403, 404)

_STATUS_BY_KIND: dict[type[errors.DomainError], int] = {
    errors.UnauthenticatedError: 401,
    errors.ForbiddenError: 403,
    errors.NotFoundError: 404,
    errors.ConflictError: 409,
    errors.GoneError: 410,
    errors.PreconditionFailedError: 412,
    errors.ContentTooLargeError: 413,
    errors.UnsupportedMediaTypeError: 415,
    errors.InvalidError: 422,
}


def status_for(exc: errors.DomainError) -> int:
    return next(
        (_STATUS_BY_KIND[kind] for kind in type(exc).__mro__ if kind in _STATUS_BY_KIND), 400
    )


async def _domain_error(_request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, errors.DomainError)  # noqa: S101  # registered for this type only
    status = status_for(exc)
    headers = {"WWW-Authenticate": "Bearer"} if status == 401 else None
    return JSONResponse({"detail": exc.detail}, status_code=status, headers=headers)


async def _concurrency_conflict(_request: Request, _exc: Exception) -> JSONResponse:
    return JSONResponse(
        {"detail": "Someone changed this at the same time, try again"}, status_code=409
    )


async def _unhandled(request: Request, _exc: Exception) -> JSONResponse:
    logger.opt(exception=True).error(
        "Unhandled error serving {} {}", request.method, request.url.path
    )
    return JSONResponse({"detail": "Internal server error"}, status_code=500)


def install_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(errors.DomainError, _domain_error)
    app.add_exception_handler(ConcurrencyConflictError, _concurrency_conflict)
    app.add_exception_handler(Exception, _unhandled)
