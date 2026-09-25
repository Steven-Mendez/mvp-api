variable "project" {
  description = "Prefix of every resource name."
  type        = string
  default     = "mvp"
}

variable "environment" {
  description = "Deployment environment."
  type        = string
  default     = "prod"
}

variable "region" {
  description = "AWS region of the whole stack (Aurora DSQL, AppSync Events and Cognito must exist there)."
  type        = string
  default     = "us-east-1"
}

variable "api_image_uri" {
  description = "Image of the API function (Dockerfile target `api`), by digest: <repository>@sha256:... The deploy workflow passes it."
  type        = string
}

variable "jobs_image_uri" {
  description = "Image of the jobs function (Dockerfile target `jobs`), by digest."
  type        = string
}

variable "web_origins" {
  description = "Origins of the web and mobile (Expo web) apps, for CORS on the API and on direct uploads to S3. The first one builds invitation links."
  type        = list(string)
  validation {
    condition     = length(var.web_origins) > 0 && alltrue([for o in var.web_origins : can(regex("^https?://[^/]+$", o))])
    error_message = "List at least one origin, as scheme://host[:port] with no path."
  }
}

variable "mail_from" {
  description = "Sender address of invitation emails. SES emails a verification link to it on the first apply."
  type        = string
}

variable "alert_email" {
  description = "Where budget alerts and alarms are emailed. AWS sends a confirmation link to it on the first apply."
  type        = string
}

variable "monthly_budget_usd" {
  description = "Monthly AWS spend that triggers an email (at 80% spent, and when the forecast exceeds it)."
  type        = number
  default     = 10
}

variable "docs_enabled" {
  description = "Publish /docs and /openapi.json."
  type        = bool
  default     = false
}

variable "api_memory_mb" {
  description = "Memory (and so CPU) of the API function; image processing likes 1024+."
  type        = number
  default     = 1024
}

variable "api_reserved_concurrency" {
  description = "Upper bound on concurrent API executions, as a cost ceiling. null leaves it unreserved (new accounts cannot reserve until their concurrency quota is raised)."
  type        = number
  default     = null
}


variable "log_retention_days" {
  description = "CloudWatch Logs retention."
  type        = number
  default     = 14
}

variable "deletion_protection" {
  description = "Protect the database and the user pool from `terraform destroy`."
  type        = bool
  default     = true
}
