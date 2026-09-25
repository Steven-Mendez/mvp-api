# Knowing when something breaks or costs more than expected: a monthly budget, a
# handful of alarms by email, and a queue that keeps every job that failed for good.
# Idle cost: about $0.80 a month (5 alarms and one custom metric).

# --- budget ---------------------------------------------------------------------------

resource "aws_budgets_budget" "monthly" {
  name         = "${local.name}-monthly"
  budget_type  = "COST"
  limit_amount = tostring(var.monthly_budget_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 80
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = [var.alert_email]
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_email_addresses = [var.alert_email]
  }
}

# --- where alarms go ------------------------------------------------------------------

# AWS emails a confirmation link to the address; alarms reach it once it is clicked.
resource "aws_sns_topic" "alerts" {
  name = "${local.name}-alerts"
}

resource "aws_sns_topic_subscription" "alerts_email" {
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

# --- jobs that failed for good ----------------------------------------------------------

# Scheduler invokes the jobs function asynchronously: Lambda retries a failed run twice,
# then sends the event here. Scheduler also sends here what it could not deliver at all.
resource "aws_sqs_queue" "failed_jobs" {
  name                      = "${local.name}-failed-jobs"
  message_retention_seconds = 14 * 24 * 60 * 60
  sqs_managed_sse_enabled   = true
}

resource "aws_lambda_function_event_invoke_config" "jobs" {
  function_name          = aws_lambda_function.function["jobs"].function_name
  maximum_retry_attempts = 2

  destination_config {
    on_failure {
      destination = aws_sqs_queue.failed_jobs.arn
    }
  }
}

# --- alarms -----------------------------------------------------------------------------

# The API answers its own errors (a 500 is a successful invocation for Lambda), so they
# are counted from the logs: every ERROR line, which includes each unhandled exception.
resource "aws_cloudwatch_log_metric_filter" "api_errors" {
  name           = "${local.name}-api-errors"
  log_group_name = aws_cloudwatch_log_group.function["api"].name
  pattern        = "{ $.record.level.name = \"ERROR\" }"

  metric_transformation {
    name      = "ApiErrors"
    namespace = local.name
    value     = "1"
  }
}

locals {
  alarms = {
    api-errors = {
      description = "The API logged errors (unhandled exceptions answer 500)."
      namespace   = local.name
      metric      = "ApiErrors"
      statistic   = "Sum"
      dimensions  = {}
    }
    api-crashes = {
      description = "API invocations failed outright (timeouts, crashes)."
      namespace   = "AWS/Lambda"
      metric      = "Errors"
      statistic   = "Sum"
      dimensions  = { FunctionName = aws_lambda_function.function["api"].function_name }
    }
    api-throttles = {
      description = "API requests were throttled: concurrency limit reached."
      namespace   = "AWS/Lambda"
      metric      = "Throttles"
      statistic   = "Sum"
      dimensions  = { FunctionName = aws_lambda_function.function["api"].function_name }
    }
    jobs-errors = {
      description = "A scheduled job run failed (Lambda retries it)."
      namespace   = "AWS/Lambda"
      metric      = "Errors"
      statistic   = "Sum"
      dimensions  = { FunctionName = aws_lambda_function.function["jobs"].function_name }
    }
    failed-jobs = {
      description = "A scheduled job failed for good: see the failed-jobs queue."
      namespace   = "AWS/SQS"
      metric      = "ApproximateNumberOfMessagesVisible"
      statistic   = "Maximum"
      dimensions  = { QueueName = aws_sqs_queue.failed_jobs.name }
    }
  }
}

resource "aws_cloudwatch_metric_alarm" "alarm" {
  for_each = local.alarms

  alarm_name          = "${local.name}-${each.key}"
  alarm_description   = each.value.description
  namespace           = each.value.namespace
  metric_name         = each.value.metric
  statistic           = each.value.statistic
  dimensions          = each.value.dimensions
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  ok_actions          = [aws_sns_topic.alerts.arn]
}
