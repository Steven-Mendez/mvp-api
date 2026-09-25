# Fixed-window request counters. On demand: no cost without traffic; items expire by TTL.
resource "aws_dynamodb_table" "rate_limits" {
  name         = "${local.name}-rate-limits"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pk"

  attribute {
    name = "pk"
    type = "S"
  }

  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }
}
