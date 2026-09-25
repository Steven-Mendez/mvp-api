"""boto3 clients, one per service per execution environment. The Lambda role supplies the
credentials; boto3 is synchronous, so adapters call it through `asyncio.to_thread`. In
local mode they all talk to moto (`AWS_ENDPOINT_URL`)."""

from functools import cache
from typing import TYPE_CHECKING

import boto3
from botocore.config import Config

from app.infrastructure.config import get_settings

if TYPE_CHECKING:
    from mypy_boto3_cognito_idp import CognitoIdentityProviderClient
    from mypy_boto3_dynamodb import DynamoDBClient
    from mypy_boto3_s3 import S3Client
    from mypy_boto3_sesv2 import SESV2Client

_CONFIG = Config(connect_timeout=5, read_timeout=5, retries={"max_attempts": 3, "mode": "standard"})


def _endpoint() -> str | None:
    return get_settings().aws_endpoint_url or None


@cache
def s3() -> S3Client:
    region = get_settings().aws_region
    if local := _endpoint():
        # moto answers on one host: bucket names go in the path.
        endpoint, addressing = local, "path"
    else:
        # Presigned POSTs go to the regional virtual-hosted endpoint, which works in
        # every region right after the bucket is created.
        endpoint, addressing = f"https://s3.{region}.amazonaws.com", "virtual"
    return boto3.client(
        "s3",
        region_name=region,
        endpoint_url=endpoint,
        config=_CONFIG.merge(Config(signature_version="s3v4", s3={"addressing_style": addressing})),
    )


@cache
def cognito() -> CognitoIdentityProviderClient:
    return boto3.client(
        "cognito-idp",
        region_name=get_settings().aws_region,
        endpoint_url=_endpoint(),
        config=_CONFIG,
    )


@cache
def ses() -> SESV2Client:
    return boto3.client(
        "sesv2", region_name=get_settings().aws_region, endpoint_url=_endpoint(), config=_CONFIG
    )


@cache
def dynamodb() -> DynamoDBClient:
    return boto3.client(
        "dynamodb", region_name=get_settings().aws_region, endpoint_url=_endpoint(), config=_CONFIG
    )
