# Two functions, one image each (the Dockerfile targets): `api` serves HTTP through its
# function URL (Lambda Web Adapter), `jobs` is a plain handler EventBridge Scheduler calls.

locals {
  common_env = {
    ENVIRONMENT          = var.environment
    LOG_LEVEL            = "INFO"
    DSQL_ENDPOINT        = local.dsql_endpoint
    COGNITO_USER_POOL_ID = aws_cognito_user_pool.main.id
    COGNITO_CLIENT_IDS   = join(",", [for c in aws_cognito_user_pool_client.app : c.id])
    ORIGIN_VERIFY_SECRET = random_password.origin_verify.result
    CORS_ORIGINS         = join(",", var.web_origins)
    WEB_BASE_URL         = var.web_origins[0]
    MEDIA_BUCKET         = aws_s3_bucket.media.bucket
    MEDIA_BASE_URL       = local.media_url
    APPSYNC_HTTP_DOMAIN  = aws_appsync_api.events.dns["HTTP"]
    MAIL_FROM            = var.mail_from
    RATE_LIMIT_TABLE     = aws_dynamodb_table.rate_limits.name
    DOCS_ENABLED         = tostring(var.docs_enabled)
  }

  functions = {
    api = {
      image_uri   = var.api_image_uri
      memory      = var.api_memory_mb
      timeout     = 30 # CloudFront waits 30 s for its origin
      concurrency = var.api_reserved_concurrency
    }
    jobs = {
      image_uri   = var.jobs_image_uri
      memory      = 512
      timeout     = 900
      concurrency = null
    }
  }
}

resource "random_password" "origin_verify" {
  length  = 48
  special = false
}

resource "aws_cloudwatch_log_group" "function" {
  for_each = local.functions

  name              = "/aws/lambda/${local.name}-${each.key}"
  retention_in_days = var.log_retention_days
}

resource "aws_lambda_function" "function" {
  for_each = local.functions

  function_name = "${local.name}-${each.key}"
  role          = aws_iam_role.lambda.arn
  package_type  = "Image"
  image_uri     = each.value.image_uri
  architectures = ["arm64"]
  memory_size   = each.value.memory
  timeout       = each.value.timeout

  reserved_concurrent_executions = each.value.concurrency == null ? -1 : each.value.concurrency

  environment {
    variables = local.common_env
  }

  logging_config {
    log_format = "Text"
    log_group  = aws_cloudwatch_log_group.function[each.key].name
  }
}

# Public at the Lambda level, but the app refuses anything without CloudFront's secret
# header, so the distribution is the only way in.
resource "aws_lambda_function_url" "api" {
  function_name      = aws_lambda_function.function["api"].function_name
  authorization_type = "NONE"
}

resource "aws_lambda_permission" "api_url_invoke" {
  statement_id             = "FunctionUrlInvokeFunction"
  action                   = "lambda:InvokeFunction"
  function_name            = aws_lambda_function.function["api"].function_name
  principal                = "*"
  invoked_via_function_url = true
}

# --- the daily purge of deleted workspaces ------------------------------------------

resource "aws_scheduler_schedule" "purge" {
  name                = "${local.name}-purge-deleted-workspaces"
  schedule_expression = "rate(1 day)"

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = aws_lambda_function.function["jobs"].arn
    role_arn = aws_iam_role.scheduler.arn
    input    = jsonencode({ job = "purge-deleted-workspaces" })

    # Scheduler invokes Lambda asynchronously: these retries and this queue cover a
    # failed delivery. A failed run is Lambda's to retry (monitoring.tf).
    retry_policy {
      maximum_retry_attempts = 2
    }

    dead_letter_config {
      arn = aws_sqs_queue.failed_jobs.arn
    }
  }
}

resource "aws_iam_role" "scheduler" {
  name = "${local.name}-scheduler"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "scheduler.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = { StringEquals = { "aws:SourceAccount" = local.account_id } }
    }]
  })
}

resource "aws_iam_role_policy" "scheduler" {
  role = aws_iam_role.scheduler.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = "lambda:InvokeFunction"
        Resource = aws_lambda_function.function["jobs"].arn
      },
      {
        Effect   = "Allow"
        Action   = "sqs:SendMessage"
        Resource = aws_sqs_queue.failed_jobs.arn
      },
    ]
  })
}

# --- what the functions may do ------------------------------------------------------

resource "aws_iam_role" "lambda" {
  name = "${local.name}-lambda"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

data "aws_iam_policy_document" "lambda" {
  statement {
    sid       = "Logs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = [for g in aws_cloudwatch_log_group.function : "${g.arn}:*"]
  }

  statement {
    sid       = "Database"
    actions   = ["dsql:DbConnectAdmin"]
    resources = [aws_dsql_cluster.main.arn]
  }

  statement {
    sid       = "Media"
    actions   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
    resources = ["${aws_s3_bucket.media.arn}/*"]
  }

  statement {
    sid       = "UserPool"
    actions   = ["cognito-idp:AdminCreateUser", "cognito-idp:AdminSetUserPassword", "cognito-idp:AdminDeleteUser"]
    resources = [aws_cognito_user_pool.main.arn]
  }

  statement {
    sid       = "Mail"
    actions   = ["ses:SendEmail"]
    resources = [aws_sesv2_email_identity.sender.arn]
  }

  statement {
    sid       = "Realtime"
    actions   = ["appsync:EventPublish"]
    resources = ["${aws_appsync_api.events.api_arn}/channelNamespace/${aws_appsync_channel_namespace.users.name}"]
  }

  # Lambda sends a job that failed for good to this queue with the function's role.
  statement {
    sid       = "FailedJobs"
    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.failed_jobs.arn]
  }

  statement {
    sid       = "RateLimits"
    actions   = ["dynamodb:UpdateItem"]
    resources = [aws_dynamodb_table.rate_limits.arn]
  }
}

resource "aws_iam_role_policy" "lambda" {
  role   = aws_iam_role.lambda.id
  policy = data.aws_iam_policy_document.lambda.json
}
