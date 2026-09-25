# Settings that are the same for every AWS account. What belongs to one account or
# person (region, web origins, email addresses, the state bucket) comes from the GitHub
# `production` environment as TF_VAR_* variables (see docs/setup.md).
environment  = "prod"
docs_enabled = false
