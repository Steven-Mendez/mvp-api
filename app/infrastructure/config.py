"""Runtime configuration. Every value comes from the Lambda environment, which Terraform
sets (infra/lambda.tf); there are no development defaults to fall back on.

`ENVIRONMENT=local` is the local mode (local/api.env, `make run`): Postgres instead of
DSQL, moto instead of AWS, and invitations and realtime events written to the log.
"""

import os
from functools import cache
from typing import Annotated, Self

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode


class Settings(BaseSettings):
    environment: str = "prod"
    log_level: str = "INFO"
    docs_enabled: bool = False

    # Lambda sets AWS_REGION itself.
    aws_region: str

    # Aurora DSQL: `<cluster id>.dsql.<region>.on.aws`, reached with an IAM auth token.
    dsql_endpoint: str = ""
    # Local mode only: a plain Postgres URL (`postgresql+asyncpg://...`) instead of DSQL.
    database_url: str = ""
    # Local mode only: every AWS client talks to this endpoint (moto) instead of AWS.
    aws_endpoint_url: str = ""

    cognito_user_pool_id: str
    cognito_client_ids: Annotated[list[str], NoDecode]

    # CloudFront adds this header to every request it forwards; anything else is refused.
    origin_verify_secret: str
    cors_origins: Annotated[list[str], NoDecode]
    web_base_url: str

    media_bucket: str
    media_base_url: str
    media_max_upload_bytes: int = 5 * 1024 * 1024
    media_thumbnail_size: int = 512

    appsync_http_domain: str
    mail_from: str

    rate_limit_table: str
    rate_limit_max_requests: int = 60
    rate_limit_window_seconds: int = 60

    max_products_per_workspace: int = 10_000
    max_storage_bytes_per_workspace: int = 10 * 1024**3

    @property
    def is_local(self) -> bool:
        return self.environment == "local"

    @property
    def cognito_issuer(self) -> str:
        return f"https://cognito-idp.{self.aws_region}.amazonaws.com/{self.cognito_user_pool_id}"

    @property
    def cognito_jwks_url(self) -> str:
        if self.aws_endpoint_url:
            # moto signs tokens with Cognito's issuer but serves the keys itself.
            endpoint = self.aws_endpoint_url.rstrip("/")
            return f"{endpoint}/{self.cognito_user_pool_id}/.well-known/jwks.json"
        return f"{self.cognito_issuer}/.well-known/jwks.json"

    @model_validator(mode="after")
    def check_mode(self) -> Self:
        if self.is_local:
            if not (self.database_url and self.aws_endpoint_url):
                raise ValueError("Local mode needs DATABASE_URL and AWS_ENDPOINT_URL")
            if os.environ.get("AWS_LAMBDA_FUNCTION_NAME"):
                raise ValueError("Local mode refuses to run on Lambda")
        elif self.database_url or self.aws_endpoint_url:
            raise ValueError("DATABASE_URL and AWS_ENDPOINT_URL are for local mode only")
        elif not self.dsql_endpoint:
            raise ValueError("DSQL_ENDPOINT is required")
        return self

    @field_validator("cognito_client_ids", "cors_origins", mode="before")
    @classmethod
    def comma_separated(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value


@cache
def get_settings() -> Settings:
    return Settings()  # pyright: ignore[reportCallIssue]  # filled from the environment
