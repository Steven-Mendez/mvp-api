import os

# The app reads its configuration at import; tests never reach AWS.
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
