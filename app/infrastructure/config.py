"""Runtime configuration. Every value comes from the Lambda environment, which Terraform
sets (infra/lambda.tf); there are no development defaults to fall back on."""

from functools import cache
from typing import Annotated

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode


class Settings(BaseSettings):
    environment: str = "prod"
    log_level: str = "INFO"
    docs_enabled: bool = False

    # Lambda sets AWS_REGION itself.
    aws_region: str

    # Aurora DSQL: `<cluster id>.dsql.<region>.on.aws`, reached with an IAM auth token.
    dsql_endpoint: str

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
    def cognito_issuer(self) -> str:
        return f"https://cognito-idp.{self.aws_region}.amazonaws.com/{self.cognito_user_pool_id}"

    @field_validator("cognito_client_ids", "cors_origins", mode="before")
    @classmethod
    def comma_separated(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value


@cache
def get_settings() -> Settings:
    return Settings()  # pyright: ignore[reportCallIssue]  # filled from the environment
