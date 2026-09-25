from collections.abc import Iterator

import pytest
from pydantic import ValidationError

from app.infrastructure.config import Settings, get_settings
from app.infrastructure.db.session import engine_for, postgres_ddl
from app.main import create_app
from app.presentation.middleware import OriginVerificationMiddleware


def _settings() -> Settings:
    return Settings()  # pyright: ignore[reportCallIssue]  # filled from the environment


@pytest.fixture
def local(monkeypatch: pytest.MonkeyPatch) -> Iterator[pytest.MonkeyPatch]:
    monkeypatch.setenv("ENVIRONMENT", "local")
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://postgres:postgres@localhost/mvp")
    monkeypatch.setenv("AWS_ENDPOINT_URL", "http://localhost:5055")
    get_settings.cache_clear()
    yield monkeypatch
    get_settings.cache_clear()


# --- which mode -------------------------------------------------------------------------


def test_production_needs_dsql(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DSQL_ENDPOINT")
    with pytest.raises(ValidationError, match="DSQL_ENDPOINT is required"):
        _settings()


def test_local_endpoints_are_refused_outside_local_mode(local: pytest.MonkeyPatch) -> None:
    local.setenv("ENVIRONMENT", "prod")
    with pytest.raises(ValidationError, match="local mode only"):
        _settings()


def test_local_mode_needs_its_database_and_endpoint(local: pytest.MonkeyPatch) -> None:
    local.delenv("DATABASE_URL")
    with pytest.raises(ValidationError, match="Local mode needs"):
        _settings()


def test_local_mode_refuses_to_run_on_lambda(local: pytest.MonkeyPatch) -> None:
    local.setenv("AWS_LAMBDA_FUNCTION_NAME", "mvp-api")
    with pytest.raises(ValidationError, match="refuses to run on Lambda"):
        _settings()


# --- what each mode wires ---------------------------------------------------------------


def test_production_fetches_keys_from_cognito() -> None:
    settings = _settings()
    assert settings.cognito_jwks_url == f"{settings.cognito_issuer}/.well-known/jwks.json"


@pytest.mark.usefixtures("local")
def test_local_mode_fetches_keys_from_moto() -> None:
    assert _settings().cognito_jwks_url == (
        "http://localhost:5055/us-east-1_example/.well-known/jwks.json"
    )


def test_production_connects_to_dsql() -> None:
    assert engine_for(_settings()).url.host == "cluster.dsql.us-east-1.on.aws"


@pytest.mark.usefixtures("local")
def test_local_mode_connects_to_postgres() -> None:
    url = engine_for(_settings()).url
    assert (url.host, url.database) == ("localhost", "mvp")


def _checks_origin() -> bool:
    return any(m.cls is OriginVerificationMiddleware for m in create_app().user_middleware)


def test_production_checks_the_origin_header() -> None:
    assert _checks_origin()


@pytest.mark.usefixtures("local")
def test_local_mode_skips_the_origin_header() -> None:
    assert not _checks_origin()


# --- migrations -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("dsql", "postgres"),
    [
        (
            "CREATE INDEX ASYNC members_user_idx ON members (user_id)",
            "CREATE INDEX members_user_idx ON members (user_id)",
        ),
        (
            "CREATE UNIQUE INDEX ASYNC users_email_idx ON users (email)",
            "CREATE UNIQUE INDEX users_email_idx ON users (email)",
        ),
        ("CREATE TABLE async_jobs (id text)", "CREATE TABLE async_jobs (id text)"),
    ],
)
def test_migrations_drop_dsql_async_indexes_for_postgres(dsql: str, postgres: str) -> None:
    assert postgres_ddl(dsql) == postgres
