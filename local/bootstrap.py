"""Create in moto what Terraform creates in AWS: the user pool and its two app clients
(infra/auth.tf), the media bucket (infra/storage.tf) and the rate-limit table
(infra/rate_limits.tf), plus a demo account. Safe to run again.

Run by `make up`, with local/api.env loaded.
"""

import json
import os
import sys
from contextlib import suppress
from typing import Any

import boto3

POOL_NAME = "mvp-api-local"
CLIENTS = ["web", "mobile"]
DEMO_EMAIL = "demo@example.com"
DEMO_PASSWORD = "password"  # noqa: S105  # a local account on an in-memory pool


def _clients() -> dict[str, Any]:
    options: dict[str, Any] = {
        "region_name": os.environ["AWS_REGION"],
        "endpoint_url": os.environ["AWS_ENDPOINT_URL"],
    }
    return {
        name: boto3.client(name, **options)  # pyright: ignore[reportUnknownMemberType]
        for name in ("cognito-idp", "s3", "dynamodb")
    }


def _user_pool(cognito: Any) -> tuple[str, list[str]]:
    pools = cognito.list_user_pools(MaxResults=60)["UserPools"]
    pool_id = next((p["Id"] for p in pools if p["Name"] == POOL_NAME), None)
    if pool_id is None:
        pool_id = cognito.create_user_pool(
            PoolName=POOL_NAME,
            UsernameAttributes=["email"],
            AutoVerifiedAttributes=["email"],
            UsernameConfiguration={"CaseSensitive": False},
            Policies={
                "PasswordPolicy": {
                    "MinimumLength": 8,
                    "RequireLowercase": False,
                    "RequireUppercase": False,
                    "RequireNumbers": False,
                    "RequireSymbols": False,
                }
            },
        )["UserPool"]["Id"]
    existing = {
        c["ClientName"]: c["ClientId"]
        for c in cognito.list_user_pool_clients(UserPoolId=pool_id, MaxResults=60)[
            "UserPoolClients"
        ]
    }
    client_ids: list[str] = []
    for client in CLIENTS:
        name = f"{POOL_NAME}-{client}"
        client_id = (
            existing.get(name)
            or cognito.create_user_pool_client(
                UserPoolId=pool_id,
                ClientName=name,
                GenerateSecret=False,
                ExplicitAuthFlows=["ALLOW_USER_PASSWORD_AUTH", "ALLOW_REFRESH_TOKEN_AUTH"],
            )["UserPoolClient"]["ClientId"]
        )
        client_ids.append(client_id)
    return pool_id, client_ids


def _demo_account(cognito: Any, pool_id: str) -> None:
    """Created the way the API creates accounts (infrastructure/aws/cognito.py). The
    password is set on every run, so a run that stopped halfway leaves no temporary one."""
    with suppress(cognito.exceptions.UsernameExistsException):
        cognito.admin_create_user(
            UserPoolId=pool_id,
            Username=DEMO_EMAIL,
            TemporaryPassword=DEMO_PASSWORD,
            MessageAction="SUPPRESS",
            UserAttributes=[
                {"Name": "email", "Value": DEMO_EMAIL},
                {"Name": "email_verified", "Value": "true"},
            ],
        )
    cognito.admin_set_user_password(
        UserPoolId=pool_id, Username=DEMO_EMAIL, Password=DEMO_PASSWORD, Permanent=True
    )


def _bucket(s3: Any, bucket: str) -> None:
    if bucket not in {b["Name"] for b in s3.list_buckets()["Buckets"]}:
        s3.create_bucket(Bucket=bucket)
    # CloudFront serves `media/` in AWS; here moto does, so it must be readable.
    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": "*",
                "Action": "s3:GetObject",
                "Resource": f"arn:aws:s3:::{bucket}/media/*",
            }
        ],
    }
    s3.put_bucket_policy(Bucket=bucket, Policy=json.dumps(policy))
    s3.put_bucket_cors(
        Bucket=bucket,
        CORSConfiguration={
            "CORSRules": [
                {
                    "AllowedMethods": ["POST", "GET"],
                    "AllowedOrigins": os.environ["CORS_ORIGINS"].split(","),
                    "AllowedHeaders": ["*"],
                }
            ]
        },
    )


def _rate_limit_table(dynamodb: Any, table: str) -> None:
    if table in dynamodb.list_tables()["TableNames"]:
        return
    dynamodb.create_table(
        TableName=table,
        BillingMode="PAY_PER_REQUEST",
        KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
    )


def main() -> int:
    clients = _clients()
    pool_id, client_ids = _user_pool(clients["cognito-idp"])
    expected = (os.environ["COGNITO_USER_POOL_ID"], os.environ["COGNITO_CLIENT_IDS"].split(","))
    if (pool_id, client_ids) != expected:
        sys.stderr.write(
            "moto made different Cognito ids than local/api.env names; update it with:\n"
            f"  COGNITO_USER_POOL_ID={pool_id}\n"
            f"  COGNITO_CLIENT_IDS={','.join(client_ids)}\n"
        )
        return 1
    _demo_account(clients["cognito-idp"], pool_id)
    _bucket(clients["s3"], os.environ["MEDIA_BUCKET"])
    _rate_limit_table(clients["dynamodb"], os.environ["RATE_LIMIT_TABLE"])
    sys.stdout.write(
        f"Local stack ready. Sign in as {DEMO_EMAIL} / {DEMO_PASSWORD}\n"
        f"  Cognito endpoint  {os.environ['AWS_ENDPOINT_URL']}\n"
        f"  User pool         {pool_id}\n"
        f"  Clients           web {client_ids[0]}, mobile {client_ids[1]}\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
