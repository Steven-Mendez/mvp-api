import os

import pytest

# The app reads its configuration at import; tests never reach AWS. Local mode's
# variables are dropped, so a shell with them exported still runs the production path.
for name in ("ENVIRONMENT", "DATABASE_URL", "AWS_ENDPOINT_URL"):
    os.environ.pop(name, None)
for name, value in {
    "AWS_REGION": "us-east-1",
    "DSQL_ENDPOINT": "cluster.dsql.us-east-1.on.aws",
    "COGNITO_USER_POOL_ID": "us-east-1_example",
    "COGNITO_CLIENT_IDS": "web,mobile",
    "ORIGIN_VERIFY_SECRET": "test-secret",
    "CORS_ORIGINS": "https://app.example.com",
    "WEB_BASE_URL": "https://app.example.com",
    "MEDIA_BUCKET": "media",
    "MEDIA_BASE_URL": "https://cdn.example.com",
    "APPSYNC_HTTP_DOMAIN": "example.appsync-api.us-east-1.amazonaws.com",
    "MAIL_FROM": "no-reply@example.com",
    "RATE_LIMIT_TABLE": "rate-limits",
}.items():
    os.environ.setdefault(name, value)

pytest_plugins = ["tests.database"]


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Tests under tests/integration and tests/e2e carry that tier's marker."""
    for item in items:
        for tier in ("integration", "e2e"):
            if f"tests/{tier}/" in item.path.as_posix():
                item.add_marker(tier)
