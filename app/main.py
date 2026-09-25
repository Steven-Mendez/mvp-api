"""The HTTP API: composition root. Lambda Web Adapter runs it with uvicorn (see run.sh)."""

import asyncio

from fastapi import FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.routing import APIRoute
from loguru import logger
from scalar_fastapi import get_scalar_api_reference  # pyright: ignore[reportUnknownVariableType]
from sqlalchemy import text

from app.infrastructure.config import get_settings
from app.infrastructure.logging import setup_logging
from app.presentation.dependencies import SessionDep
from app.presentation.errors import ErrorDetail, install_error_handlers
from app.presentation.middleware import OriginVerificationMiddleware
from app.presentation.routers import auth, products, profile, workspaces

_READINESS_TIMEOUT_SECONDS = 5.0


def _operation_id(route: APIRoute) -> str:
    """`<tag>_<function>`: the generated clients name their methods after these."""
    return f"{route.tags[0]}_{route.name}" if route.tags else route.name


def create_app() -> FastAPI:
    settings = get_settings()
    setup_logging(settings.log_level, readable=settings.is_local)
    app = FastAPI(
        title="MVP API",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url="/openapi.json" if settings.docs_enabled else None,
        generate_unique_id_function=_operation_id,
    )
    install_error_handlers(app)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    if not settings.is_local:  # locally there is no CloudFront to add the header
        app.add_middleware(OriginVerificationMiddleware, secret=settings.origin_verify_secret)

    app.include_router(products.router)
    app.include_router(auth.router)
    app.include_router(profile.router)
    app.include_router(workspaces.router)

    if settings.docs_enabled:

        @app.get("/docs", include_in_schema=False)
        async def docs() -> HTMLResponse:  # pyright: ignore[reportUnusedFunction]
            return get_scalar_api_reference(openapi_url="/openapi.json", title=app.title)

    @app.get(
        "/health",
        tags=["meta"],
        description="Liveness: the process responds. Deliberately without dependencies.",
    )
    async def health() -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]
        return {"status": "ok"}

    @app.get(
        "/health/ready",
        tags=["meta"],
        responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ErrorDetail}},
        description="Readiness: the database answers. 503 when it does not.",
    )
    async def ready(session: SessionDep) -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]
        try:
            async with asyncio.timeout(_READINESS_TIMEOUT_SECONDS):
                await session.execute(text("SELECT 1"))
        except Exception as exc:
            logger.warning("Readiness: the database is unreachable ({})", exc)
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Not ready: database") from exc
        return {"status": "ready"}

    return app


app = create_app()
